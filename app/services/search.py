"""Search service: vector search + context assembly."""

import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.config import get_settings
from app.models.chunk import Chunk
from app.services.embedding import embed_query
from app.services.vector_store import get_vector_store

settings = get_settings()


class SearchService:
    async def search(
        self,
        query: str,
        user_id: str,
        top_k: int = 20,
        collection_id: str | None = None,
    ) -> list[dict]:
        query_vector = await embed_query(query)
        store = get_vector_store()
        vector_results = store.search(query_vector, user_id, top_k=top_k)

        if not vector_results:
            return []

        # Fetch full chunk content from DB
        chunk_ids = [r["chunk_id"] for r in vector_results]
        chunks_map = await self._fetch_chunks(chunk_ids, user_id)

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

        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:10]

    def build_context(self, results: list[dict], max_tokens: int = 6000) -> str:
        context_parts = []
        used_tokens = 0
        for i, r in enumerate(results):
            approx_tokens = len(r["content"])
            if used_tokens + approx_tokens > max_tokens:
                break
            context_parts.append(f"[{i + 1}] {r['content']}")
            used_tokens += approx_tokens
        return "\n\n".join(context_parts)

    async def _fetch_chunks(self, chunk_ids: list[str], user_id: str) -> dict:
        engine = create_async_engine(settings.database_url, pool_size=2,
                                     connect_args={"check_same_thread": False})
        session_factory = async_sessionmaker(engine, class_=AsyncSession)
        chunks_map = {}
        async with session_factory() as db:
            uuids = [str(cid) for cid in chunk_ids]
            result = await db.execute(
                select(Chunk).where(Chunk.id.in_(uuids), Chunk.user_id == str(user_id))
            )
            for chunk in result.scalars().all():
                chunks_map[str(chunk.id)] = chunk
        await engine.dispose()
        return chunks_map
