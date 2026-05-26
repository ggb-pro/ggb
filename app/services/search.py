"""Search service: hybrid search (vector + BM25) + RRF fusion + rerank + query understanding."""

import logging
import time
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.deps import engine
from app.models.chunk import Chunk
from app.services.embedding import embed_query
from app.services.vector_store import get_vector_store
from app.services.query_analyzer import QueryAnalyzer
from app.services.metrics import rag_retrieval_duration, rag_rerank_duration, rag_results_count

logger = logging.getLogger(__name__)
settings = get_settings()

RRF_K = 60
CANDIDATE_TOP_K = 40  # candidates before rerank (was 20)
RERANK_TOP_K = 10  # final results after rerank (was 5)

_analyzer = QueryAnalyzer()


class SearchService:
    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = CANDIDATE_TOP_K,
        collection_id: str | None = None,
        history: list[str] | None = None,
    ) -> list[dict]:
        t0 = time.monotonic()
        try:
            return await self._do_search(query, user_id, top_k, collection_id, history)
        finally:
            rag_retrieval_duration.observe(time.monotonic() - t0)

    async def _do_search(
        self, query: str, user_id: str, top_k: int,
        collection_id: str | None, history: list[str] | None,
    ) -> list[dict]:
        # Step 0: Query analysis
        analyzed = _analyzer.analyze(query, history=history)
        search_query = analyzed.rewritten
        vector_weight = analyzed.vector_weight
        bm25_weight = analyzed.bm25_weight

        # Step 1: Search (for compare/decompose, merge sub-query results)
        if len(analyzed.sub_queries) > 1:
            all_fused = {}
            for sq in analyzed.sub_queries:
                fused = await self._single_search(sq, user_id, top_k, vector_weight, bm25_weight)
                for cid, score in fused.items():
                    all_fused[cid] = all_fused.get(cid, 0) + score
            fused = dict(sorted(all_fused.items(), key=lambda x: x[1], reverse=True))
        else:
            fused = await self._single_search(search_query, user_id, top_k, vector_weight, bm25_weight)

        if not fused:
            return []

        # Step 2: Fetch full chunk content
        chunk_ids = list(fused.keys())
        chunks_map = await self._fetch_chunks(chunk_ids, user_id)

        results = []
        for cid, score in fused.items():
            chunk = chunks_map.get(cid)
            if chunk:
                results.append({
                    "chunk_id": cid,
                    "document_id": chunk["document_id"],
                    "score": score,
                    "content": chunk["content"],
                    "page_number": chunk.get("page_number"),
                    "parent_content": chunk.get("parent_content"),
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        candidates = results[:CANDIDATE_TOP_K]

        # Step 3: Rerank
        reranked = await self._rerank(search_query, candidates)
        result = reranked[:RERANK_TOP_K]
        rag_results_count.observe(len(result))
        return result

    async def _single_search(
        self, query: str, user_id: str, top_k: int,
        vector_weight: float, bm25_weight: float,
    ) -> dict[str, float]:
        """Run vector + BM25 search and RRF fuse with given weights."""
        query_vector = await embed_query(query)
        store = get_vector_store()
        vector_results = store.search(query_vector, user_id, top_k=top_k)
        bm25_results = await self._bm25_search(query, user_id, top_k)
        return self._rrf_fuse(vector_results, bm25_results, vector_weight, bm25_weight)

    def build_context(self, results: list[dict], max_tokens: int = 8000) -> str:
        context_parts = []
        used_tokens = 0
        for i, r in enumerate(results):
            content = r.get("parent_content") or r["content"]
            approx_tokens = len(content)
            if used_tokens + approx_tokens > max_tokens:
                break
            context_parts.append(f"[{i + 1}] {content}")
            used_tokens += approx_tokens
        return "\n\n".join(context_parts)

    def _rrf_fuse(
        self,
        vector_results: list[dict],
        bm25_results: list[dict],
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
    ) -> dict[str, float]:
        """Reciprocal Rank Fusion of vector and BM25 results."""
        scores: dict[str, float] = {}

        for rank, r in enumerate(vector_results):
            cid = r["chunk_id"]
            scores[cid] = scores.get(cid, 0) + vector_weight / (RRF_K + rank + 1)

        for rank, r in enumerate(bm25_results):
            cid = r["chunk_id"]
            scores[cid] = scores.get(cid, 0) + bm25_weight / (RRF_K + rank + 1)

        return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))

    async def _bm25_search(self, query: str, user_id: str, top_k: int) -> list[dict]:
        """Full-text search via Elasticsearch with jieba Chinese tokenization."""
        try:
            from app.services.es import search as es_search
            return es_search(query, user_id, top_k)
        except Exception as e:
            logger.warning(f"ES search failed, falling back to PG FTS: {e}")
            return await self._pg_fts_search(query, user_id, top_k)

    async def _pg_fts_search(self, query: str, user_id: str, top_k: int) -> list[dict]:
        """Fallback: PostgreSQL FTS with jieba tokenization."""
        from app.services.tokenizer import tokenize_query
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            tokens = tokenize_query(query)
            if not tokens.strip():
                return []
            async with session_factory() as db:
                sql = text("""
                    SELECT c.id::text as chunk_id,
                           c.document_id::text as document_id,
                           ts_rank_cd(c.fts_vector, to_tsquery('simple', :tokens)) as score
                    FROM chunks c
                    WHERE c.user_id::text = :user_id
                      AND c.fts_vector @@ to_tsquery('simple', :tokens)
                    ORDER BY score DESC
                    LIMIT :limit
                """)
                result = await db.execute(sql, {"tokens": tokens, "user_id": user_id, "limit": top_k})
                rows = result.fetchall()
                return [
                    {"chunk_id": row[0], "document_id": row[1], "score": float(row[2])}
                    for row in rows
                ]
        except Exception as e2:
            logger.warning(f"PG FTS search also failed: {e2}")
            return []

    async def _rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        """Rerank candidates via API or local model."""
        t0 = time.monotonic()
        try:
            if len(candidates) <= 1:
                return candidates

            if settings.rerank_backend == "api":
                result = await self._rerank_api(query, candidates)
                if result is not None:
                    return result
                logger.info("API rerank failed, falling back to local")
            else:
                result = await self._rerank_local(query, candidates)
                if result is not None:
                    return result
                logger.info("Local rerank failed, falling back to API")
                result = await self._rerank_api(query, candidates)
                if result is not None:
                    return result

            return candidates
        finally:
            rag_rerank_duration.observe(time.monotonic() - t0)

    async def _rerank_api(self, query: str, candidates: list[dict]) -> list[dict] | None:
        """Rerank via Jina/Cohere-compatible API."""
        if not settings.rerank_api_url:
            return None
        try:
            import httpx
            documents = [c["content"] for c in candidates]
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    settings.rerank_api_url,
                    headers={
                        "Authorization": f"Bearer {settings.rerank_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": settings.rerank_model,
                        "query": query,
                        "documents": documents,
                        "top_n": len(candidates),
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            for r in data.get("results", []):
                idx = r["index"]
                candidates[idx]["score"] = float(r["relevance_score"])

            candidates.sort(key=lambda x: x["score"], reverse=True)
            logger.info(f"Reranked {len(candidates)} candidates via API")
            return candidates
        except Exception as e:
            logger.warning(f"Rerank API failed: {e}")
            return None

    async def _rerank_local(self, query: str, candidates: list[dict]) -> list[dict] | None:
        """Rerank via local CrossEncoder model."""
        try:
            reranker = self._get_reranker()
            if reranker is None:
                return None

            pairs = [[query, c["content"]] for c in candidates]
            raw_scores = reranker.predict(pairs)
            try:
                scores = raw_scores.tolist()
            except AttributeError:
                scores = raw_scores if isinstance(raw_scores, list) else [raw_scores]
            scores = [float(s) for s in scores]

            for i, score in enumerate(scores):
                candidates[i]["score"] = score

            candidates.sort(key=lambda x: x["score"], reverse=True)
            logger.info(f"Reranked {len(candidates)} candidates via local model")
            return candidates
        except Exception as e:
            logger.warning(f"Local rerank failed: {e}")
            return None

    _reranker_model = None

    def _get_reranker(self):
        """Lazy-load bge-reranker model (local fallback only)."""
        if self.__class__._reranker_model is not None:
            return self.__class__._reranker_model
        try:
            import os
            if not os.environ.get("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            from sentence_transformers import CrossEncoder
            logger.info("Loading bge-reranker-v2-m3 (local fallback)...")
            self.__class__._reranker_model = CrossEncoder(
                "BAAI/bge-reranker-v2-m3",
                device="cpu",
            )
            logger.info("bge-reranker loaded")
            return self.__class__._reranker_model
        except Exception as e:
            logger.warning(f"Reranker load failed: {e}")
            return None

    async def _fetch_chunks(self, chunk_ids: list[str], user_id: str) -> dict:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        chunks_map = {}
        async with session_factory() as db:
            result = await db.execute(
                select(Chunk).where(Chunk.id.in_(chunk_ids), Chunk.user_id == user_id)
            )
            chunks = result.scalars().all()

            parent_ids = set()
            for chunk in chunks:
                if chunk.parent_chunk_id:
                    parent_ids.add(str(chunk.parent_chunk_id))

            existing_ids = {str(c.id) for c in chunks}
            missing_parent_ids = parent_ids - existing_ids
            parent_map = {}
            if missing_parent_ids:
                parent_result = await db.execute(
                    select(Chunk).where(Chunk.id.in_(list(missing_parent_ids)), Chunk.user_id == user_id)
                )
                for p in parent_result.scalars().all():
                    parent_map[str(p.id)] = {
                        "content": p.content,
                        "document_id": str(p.document_id),
                        "page_number": p.page_number,
                    }

            for chunk in chunks:
                parent_data = parent_map.get(str(chunk.parent_chunk_id)) if chunk.parent_chunk_id else None
                chunks_map[str(chunk.id)] = {
                    "content": chunk.content,
                    "document_id": str(chunk.document_id),
                    "page_number": chunk.page_number,
                    "parent_content": parent_data["content"] if parent_data else None,
                }
        return chunks_map
