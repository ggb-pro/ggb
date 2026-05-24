"""Search service: hybrid search (vector + BM25) + RRF fusion + rerank."""

import logging
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.deps import engine
from app.models.chunk import Chunk
from app.services.embedding import embed_query
from app.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)
settings = get_settings()

# RRF parameters
RRF_K = 60  # RRF constant
VECTOR_WEIGHT = 0.7  # vector search weight
BM25_WEIGHT = 0.3  # BM25 search weight
RERANK_TOP_K = 5  # final results after rerank


class SearchService:
    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 20,
        collection_id: str | None = None,
    ) -> list[dict]:
        # Step 1: Vector search
        query_vector = await embed_query(query)
        store = get_vector_store()
        vector_results = store.search(query_vector, user_id, top_k=top_k)

        # Step 2: BM25 full-text search via PG FTS
        bm25_results = await self._bm25_search(query, user_id, top_k)

        # Step 3: RRF fusion
        fused = self._rrf_fuse(vector_results, bm25_results)

        if not fused:
            return []

        # Step 4: Fetch full chunk content
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
                })

        results.sort(key=lambda x: x["score"], reverse=True)
        candidates = results[:20]

        # Step 5: Rerank
        reranked = await self._rerank(query, candidates)
        return reranked[:RERANK_TOP_K]

    def build_context(self, results: list[dict], max_tokens: int = 6000) -> str:
        context_parts = []
        used_tokens = 0
        for i, r in enumerate(results):
            # Use parent content for richer context when available
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
    ) -> dict[str, float]:
        """Reciprocal Rank Fusion of vector and BM25 results."""
        scores: dict[str, float] = {}

        for rank, r in enumerate(vector_results):
            cid = r["chunk_id"]
            scores[cid] = scores.get(cid, 0) + VECTOR_WEIGHT / (RRF_K + rank + 1)

        for rank, r in enumerate(bm25_results):
            cid = r["chunk_id"]
            scores[cid] = scores.get(cid, 0) + BM25_WEIGHT / (RRF_K + rank + 1)

        return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))

    async def _bm25_search(self, query: str, user_id: str, top_k: int) -> list[dict]:
        """Full-text search using PostgreSQL ts_vector."""
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with session_factory() as db:
                sql = text("""
                    SELECT c.id::text as chunk_id,
                           c.document_id::text as document_id,
                           ts_rank_cd(c.fts_vector, plainto_tsquery('simple', :query)) as score
                    FROM chunks c
                    WHERE c.user_id::text = :user_id
                      AND c.fts_vector @@ plainto_tsquery('simple', :query)
                    ORDER BY score DESC
                    LIMIT :limit
                """)
                result = await db.execute(sql, {"query": query, "user_id": user_id, "limit": top_k})
                rows = result.fetchall()
                return [
                    {"chunk_id": row[0], "document_id": row[1], "score": float(row[2])}
                    for row in rows
                ]
        except Exception as e:
            logger.warning(f"BM25 search failed: {e}")
            return []

    async def _rerank(self, query: str, candidates: list[dict]) -> list[dict]:
        """Rerank candidates using bge-reranker or fallback to score-based."""
        if len(candidates) <= 1:
            return candidates

        try:
            reranker = self._get_reranker()
            if reranker is None:
                return candidates

            pairs = [[query, c["content"]] for c in candidates]
            raw_scores = reranker.predict(pairs)
            # predict() may return numpy array or list — normalize to flat Python floats
            try:
                scores = raw_scores.tolist()
            except AttributeError:
                scores = raw_scores if isinstance(raw_scores, list) else [raw_scores]
            scores = [float(s) for s in scores]

            for i, score in enumerate(scores):
                candidates[i]["score"] = score

            candidates.sort(key=lambda x: x["score"], reverse=True)
            logger.info(f"Reranked {len(candidates)} candidates")
            return candidates
        except Exception as e:
            logger.warning(f"Rerank failed: {e}")
            return candidates

    _reranker_model = None

    def _get_reranker(self):
        """Lazy-load bge-reranker model."""
        if self.__class__._reranker_model is not None:
            return self.__class__._reranker_model
        try:
            import os
            if not os.environ.get("HF_ENDPOINT"):
                os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
            from sentence_transformers import CrossEncoder
            logger.info("Loading bge-reranker-v2-m3...")
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

            # Collect parent chunk IDs for child chunks that have parents
            parent_ids = set()
            for chunk in chunks:
                if chunk.parent_chunk_id:
                    parent_ids.add(str(chunk.parent_chunk_id))

            # Fetch parent chunks if not already in results
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
                # Use parent content for richer context if available
                parent_data = parent_map.get(str(chunk.parent_chunk_id)) if chunk.parent_chunk_id else None
                chunks_map[str(chunk.id)] = {
                    "content": chunk.content,
                    "document_id": str(chunk.document_id),
                    "page_number": chunk.page_number,
                    "parent_content": parent_data["content"] if parent_data else None,
                }
        return chunks_map
