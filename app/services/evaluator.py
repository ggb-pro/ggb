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
