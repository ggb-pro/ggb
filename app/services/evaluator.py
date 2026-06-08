"""RAG evaluation service: offline metrics (Recall, MRR, NDCG) + golden dataset."""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

GOLDEN_DATASET_PATH = Path("data/golden_dataset.json")


@dataclass
class EvalResult:
    recall_at_5: float
    recall_at_10: float
    mrr: float
    ndcg_at_10: float
    total_queries: int
    details: list[dict] = field(default_factory=list)


def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(set(retrieved[:k]) & relevant) / len(relevant)


def mrr(retrieved: list[str], relevant: set[str]) -> float:
    for i, doc_id in enumerate(retrieved):
        if doc_id in relevant:
            return 1.0 / (i + 1)
    return 0.0


def ndcg_at_k(retrieved: list[str], relevant: set[str], k: int) -> float:
    import math
    dcg = 0.0
    for i, doc_id in enumerate(retrieved[:k]):
        if doc_id in relevant:
            dcg += 1.0 / math.log2(i + 2)
    # Ideal DCG
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal > 0 else 0.0


async def evaluate_search() -> EvalResult:
    """Run evaluation using golden dataset."""
    from app.services.search import SearchService

    dataset = load_golden_dataset()
    if not dataset:
        return EvalResult(0, 0, 0, 0, 0)

    search_svc = SearchService()
    results = []

    for sample in dataset:
        query = sample["query"]
        relevant_ids = set(sample["relevant_chunk_ids"])
        user_id = sample["user_id"]

        try:
            search_results = await search_svc.search(query, user_id, top_k=20)
            retrieved_ids = [r["chunk_id"] for r in search_results]
        except Exception as e:
            logger.warning(f"Search failed for query '{query}': {e}")
            retrieved_ids = []

        results.append({
            "query": query,
            "relevant": relevant_ids,
            "retrieved": retrieved_ids,
            "recall_5": recall_at_k(retrieved_ids, relevant_ids, 5),
            "recall_10": recall_at_k(retrieved_ids, relevant_ids, 10),
            "mrr": mrr(retrieved_ids, relevant_ids),
            "ndcg_10": ndcg_at_k(retrieved_ids, relevant_ids, 10),
        })

    if not results:
        return EvalResult(0, 0, 0, 0, 0)

    n = len(results)
    return EvalResult(
        recall_at_5=sum(r["recall_5"] for r in results) / n,
        recall_at_10=sum(r["recall_10"] for r in results) / n,
        mrr=sum(r["mrr"] for r in results) / n,
        ndcg_at_10=sum(r["ndcg_10"] for r in results) / n,
        total_queries=n,
        details=results,
    )


def load_golden_dataset() -> list[dict]:
    if not GOLDEN_DATASET_PATH.exists():
        return []
    with open(GOLDEN_DATASET_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_golden_dataset(samples: list[dict]):
    GOLDEN_DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(GOLDEN_DATASET_PATH, "w", encoding="utf-8") as f:
        json.dump(samples, f, ensure_ascii=False, indent=2)


async def evaluate_rrf_params(test_samples: list[dict]) -> dict:
    """Sweep RRF_K × weight combinations and evaluate recall."""
    from app.services.search import SearchService
    from app.services.vector_store import get_vector_store
    from app.services.embedding import embed_query

    results = {}
    for k in [10, 30, 60, 100]:
        for vw, bw in [(0.3, 0.7), (0.5, 0.5), (0.7, 0.3)]:
            recalls_at_5 = []
            recalls_at_10 = []
            for sample in test_samples:
                query_vector = await embed_query(sample["query"])
                store = get_vector_store()
                vector_results = store.search(query_vector, sample["user_id"], top_k=40)
                svc = SearchService()
                bm25_results = await svc._bm25_search(sample["query"], sample["user_id"], 40)
                fused = svc._rrf_fuse(vector_results, bm25_results, vw, bw)
                retrieved = list(fused.keys())
                relevant = set(sample["relevant_ids"])
                recalls_at_5.append(recall_at_k(retrieved, relevant, 5))
                recalls_at_10.append(recall_at_k(retrieved, relevant, 10))
            key = f"K={k}_vw={vw}"
            results[key] = {
                "recall@5": sum(recalls_at_5) / len(recalls_at_5),
                "recall@10": sum(recalls_at_10) / len(recalls_at_10),
            }
    return results


async def evaluate_fallback_fts(test_samples: list[dict]) -> dict:
    """Compare ES vs PG FTS retrieval quality."""
    from app.services.search import SearchService

    es_scores = []
    pg_scores = []
    svc = SearchService()

    for sample in test_samples:
        relevant = set(sample["relevant_ids"])

        try:
            from app.services.es import search as es_search
            es_results = es_search(sample["query"], sample["user_id"], 10)
            es_ids = [r["chunk_id"] for r in es_results]
        except Exception:
            es_ids = []
        es_scores.append(recall_at_k(es_ids, relevant, 10))

        pg_results = await svc._pg_fts_search(sample["query"], sample["user_id"], 10)
        pg_ids = [r["chunk_id"] for r in pg_results]
        pg_scores.append(recall_at_k(pg_ids, relevant, 10))

    avg_es = sum(es_scores) / len(es_scores) if es_scores else 0
    avg_pg = sum(pg_scores) / len(pg_scores) if pg_scores else 0
    degradation = f"{(1 - avg_pg / avg_es) * 100:.1f}%" if avg_es > 0 else "N/A"

    return {
        "es_recall@10": avg_es,
        "pg_recall@10": avg_pg,
        "degradation": degradation,
    }
