"""Search service: hybrid search (vector + BM25) + weighted RRF fusion + rerank."""

import logging
import time
import numpy as np
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.deps import engine
from app.models.chunk import Chunk
from app.services.embedding import embed_query
from app.services.vector_store import get_vector_store
from app.services.query_analyzer import QueryAnalyzer
from app.services.metrics import rag_retrieval_duration, rag_rerank_duration, rag_results_count, rag_data_loss

logger = logging.getLogger(__name__)
settings = get_settings()

RRF_K = 60
CANDIDATE_TOP_K = 40
RERANK_TOP_K = 10

_analyzer = QueryAnalyzer()


class SearchService:
    async def search_with_weights(
        self,
        query: str,
        user_id: str,
        top_k: int = CANDIDATE_TOP_K,
        collection_id: str | None = None,
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
    ) -> list[dict]:
        """Search with custom weights, reusing full pipeline."""
        t0 = time.monotonic()
        try:
            analyzed = await _analyzer.analyze(query)
            search_query = analyzed.rewritten
            fused = await self._single_search(search_query, user_id, top_k, vector_weight, bm25_weight)

            if not fused:
                self._check_data_loss(query, user_id)
                return []

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
            reranked = await self._rerank(search_query, candidates)
            return reranked[:RERANK_TOP_K]
        finally:
            rag_retrieval_duration.observe(time.monotonic() - t0)

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
        analyzed = await _analyzer.analyze(query, history=history)
        search_query = analyzed.rewritten
        vector_weight = analyzed.vector_weight
        bm25_weight = analyzed.bm25_weight

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
            self._check_data_loss(query, user_id)
            return []

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

        reranked = await self._rerank(search_query, candidates)
        result = reranked[:RERANK_TOP_K]
        rag_results_count.observe(len(result))
        return result

    async def _single_search(
        self, query: str, user_id: str, top_k: int,
        vector_weight: float, bm25_weight: float,
    ) -> dict[str, float]:
        """Run vector + BM25 search and weighted RRF fuse."""
        query_vector = await embed_query(query)
        store = get_vector_store()
        vector_results = store.search(query_vector, user_id, top_k=top_k)

        try:
            from app.services.es import search as es_search
            bm25_results = es_search(query, user_id, top_k)
            using_pg_fts = False
        except Exception as e:
            logger.warning(f"ES search failed, falling back to PG FTS: {e}")
            bm25_results = await self._pg_fts_search(query, user_id, top_k)
            using_pg_fts = True

        if using_pg_fts and bm25_results:
            bm25_weight *= 0.5
            vector_weight = 1.0 - bm25_weight

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
        """D4: Weighted RRF — incorporates raw scores to penalize low-quality results."""
        scores: dict[str, float] = {}

        if vector_results:
            max_vec = max((r.get("score", 1.0) for r in vector_results), default=1.0) or 1.0
            for rank, r in enumerate(vector_results):
                cid = r["chunk_id"]
                norm = r.get("score", 1.0) / max_vec
                scores[cid] = scores.get(cid, 0) + vector_weight * norm / (RRF_K + rank + 1)

        if bm25_results:
            max_bm25 = max((r.get("score", 1.0) for r in bm25_results), default=1.0) or 1.0
            for rank, r in enumerate(bm25_results):
                cid = r["chunk_id"]
                norm = r.get("score", 1.0) / max_bm25
                scores[cid] = scores.get(cid, 0) + bm25_weight * norm / (RRF_K + rank + 1)

        return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))

    async def _bm25_search(self, query: str, user_id: str, top_k: int) -> list[dict]:
        try:
            from app.services.es import search as es_search
            return es_search(query, user_id, top_k)
        except Exception as e:
            logger.warning(f"ES search failed, falling back to PG FTS: {e}")
            return await self._pg_fts_search(query, user_id, top_k)

    async def _pg_fts_search(self, query: str, user_id: str, top_k: int) -> list[dict]:
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
        """Rerank with API fallback to cosine similarity (D9)."""
        t0 = time.monotonic()
        try:
            if len(candidates) <= 1:
                return candidates

            result = await self._rerank_api(query, candidates)
            if result is not None:
                return result

            # D9: Local fallback — cosine similarity rerank
            return await self._local_rerank(query, candidates)
        finally:
            rag_rerank_duration.observe(time.monotonic() - t0)

    async def _rerank_api(self, query: str, candidates: list[dict]) -> list[dict] | None:
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
            return candidates
        except Exception as e:
            logger.warning(f"Rerank API failed: {e}")
            return None

    async def _local_rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        """D9: Lightweight cosine similarity rerank as API fallback."""
        try:
            query_vec = await embed_query(query)
        except Exception as e:
            logger.warning(f"Local rerank embed failed: {e}")
            return candidates

        q = np.array(query_vec, dtype=np.float32)
        q_norm = q / (np.linalg.norm(q) + 1e-8)

        store = get_vector_store()
        chunk_ids = [c["chunk_id"] for c in candidates]
        try:
            chunk_vecs = store.get_vectors_by_ids(chunk_ids)
            for c, vec in zip(candidates, chunk_vecs):
                v = np.array(vec, dtype=np.float32)
                v_norm = np.linalg.norm(v)
                if v_norm > 0:
                    c["score"] = float(q_norm @ (v / v_norm))
            candidates.sort(key=lambda x: x["score"], reverse=True)
            logger.info(f"Local rerank (cosine) for {len(candidates)} candidates")
        except Exception as e:
            logger.warning(f"Local rerank failed: {e}")
        return candidates

    def _check_data_loss(self, query: str, user_id: str):
        """D3: Detect potential data loss when retrieval returns nothing."""
        try:
            import asyncio
            from sqlalchemy.ext.asyncio import async_sessionmaker
            session_factory = async_sessionmaker(engine, expire_on_commit=False)

            async def _check():
                async with session_factory() as db:
                    pg_count = await db.execute(
                        select(func.count()).where(Chunk.user_id == user_id)
                    )
                    if pg_count.scalar() > 0:
                        rag_data_loss.inc()

            try:
                loop = asyncio.get_running_loop()
                asyncio.ensure_future(_check())
            except RuntimeError:
                pass
        except Exception:
            pass

    async def _fetch_chunks(self, chunk_ids: list[str], user_id: str) -> dict:
        from app.models.content_pool import ContentPool
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        chunks_map = {}
        async with session_factory() as db:
            result = await db.execute(
                select(Chunk, ContentPool.content)
                .join(ContentPool, Chunk.content_hash == ContentPool.content_hash)
                .where(Chunk.id.in_(chunk_ids), Chunk.user_id == user_id)
            )
            rows = result.all()

            parent_ids = set()
            for chunk, _ in rows:
                if chunk.parent_chunk_id:
                    parent_ids.add(str(chunk.parent_chunk_id))

            parent_map = {}
            if parent_ids:
                parent_result = await db.execute(
                    select(Chunk, ContentPool.content)
                    .join(ContentPool, Chunk.content_hash == ContentPool.content_hash)
                    .where(Chunk.id.in_(list(parent_ids)), Chunk.user_id == user_id)
                )
                for p_chunk, p_content in parent_result.all():
                    parent_map[str(p_chunk.id)] = {
                        "content": p_content,
                        "document_id": str(p_chunk.document_id),
                        "page_number": p_chunk.page_number,
                    }

            for chunk, content in rows:
                parent_data = parent_map.get(str(chunk.parent_chunk_id)) if chunk.parent_chunk_id else None
                chunks_map[str(chunk.id)] = {
                    "content": content,
                    "document_id": str(chunk.document_id),
                    "page_number": chunk.page_number,
                    "parent_content": parent_data["content"] if parent_data else None,
                }
        return chunks_map
