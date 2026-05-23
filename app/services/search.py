"""Search service: vector search + optional BM25 via PG full-text search."""

import uuid
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import get_settings
from app.models.chunk import Chunk
from app.services.embedding import embed_query
from app.services.vector_store import search_vectors, get_collection_name
from app.deps import get_milvus
from pymilvus import MilvusClient

settings = get_settings()


class SearchService:
    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 20,
        collection_id: uuid.UUID | None = None,
    ) -> list[dict]:
        """Hybrid search: vector search + optional PG text search, fused by RRF."""

        # Channel 1: Vector search
        query_vector = await embed_query(query)
        milvus_client = get_milvus()
        vector_results = search_vectors(milvus_client, query_vector, user_id, top_k=top_k)

        if not vector_results:
            return []

        # Fetch full chunk content from DB
        chunk_ids = [r["chunk_id"] for r in vector_results]
        chunks_map = await self._fetch_chunks(chunk_ids, user_id)

        # Enrich results with full content
        results = []
        for r in vector_results:
            chunk = chunks_map.get(r["chunk_id"])
            if chunk:
                results.append({
                    "chunk_id": r["chunk_id"],
                    "document_id": r["document_id"],
                    "score": r["score"],
                    "content": chunk.content,
                    "page_number": chunk.page_number,
                })

        # Rerank (simple: just use vector score, Phase 2 adds bge-reranker)
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:10]

    def build_context(self, results: list[dict], max_tokens: int = 6000) -> str:
        """Build context string from search results for LLM."""
        context_parts = []
        used_tokens = 0

        for i, r in enumerate(results):
            content = r["content"]
            approx_tokens = len(content)  # rough approximation
            if used_tokens + approx_tokens > max_tokens:
                break
            context_parts.append(f"[{i + 1}] {content}")
            used_tokens += approx_tokens

        return "\n\n".join(context_parts)

    async def _fetch_chunks(self, chunk_ids: list[str], user_id: str) -> dict:
        """Fetch chunks from DB by IDs."""
        engine = create_async_engine(settings.database_url, pool_size=2)
        session_factory = async_sessionmaker(engine, class_=AsyncSession)
        chunks_map = {}

        async with session_factory() as db:
            uuids = [uuid.UUID(cid) for cid in chunk_ids]
            result = await db.execute(
                select(Chunk).where(Chunk.id.in_(uuids), Chunk.user_id == uuid.UUID(user_id))
            )
            for chunk in result.scalars().all():
                chunks_map[str(chunk.id)] = chunk

        await engine.dispose()
        return chunks_map
