"""Service factory: Protocol interfaces + adapter classes + backend selection."""

from __future__ import annotations
from typing import Protocol, runtime_checkable, AsyncIterator


# ── Protocol Definitions ──────────────────────────────────────────────────

@runtime_checkable
class VectorStoreBase(Protocol):
    async def upsert(self, chunk_ids: list[str], user_id: str, document_id: str,
                     vectors: list[list[float]], snippets: list[str]): ...
    async def search(self, query_vector: list[float], user_id: str,
                     top_k: int) -> list[dict]: ...
    async def delete_by_document(self, document_id: str): ...


@runtime_checkable
class FullTextSearchBase(Protocol):
    async def search(self, query: str, user_id: str,
                     top_k: int) -> list[dict]: ...
    async def index_chunk(self, chunk_id: str, content: str, user_id: str): ...
    async def delete_chunk(self, chunk_id: str): ...


@runtime_checkable
class ObjectStorageBase(Protocol):
    async def save(self, key: str, data: bytes) -> str: ...
    async def load(self, key: str) -> bytes: ...
    async def delete(self, key: str): ...


@runtime_checkable
class EmbeddingServiceBase(Protocol):
    async def encode(self, texts: list[str]) -> list[list[float]]: ...
    async def encode_query(self, query: str) -> list[float]: ...


@runtime_checkable
class RerankServiceBase(Protocol):
    async def rerank(self, query: str, documents: list[str],
                     top_n: int) -> list[dict]: ...


@runtime_checkable
class OcrServiceBase(Protocol):
    def recognize(self, image_path: str) -> str: ...


@runtime_checkable
class LlmServiceBase(Protocol):
    async def stream_generate(self, query: str, context: str,
                              history: list | None = None) -> AsyncIterator[str]: ...


# ── Adapter Classes ────────────────────────────────────────────────────────

class EmbeddingAdapter:
    """Wraps embedding module to satisfy EmbeddingServiceBase protocol."""

    async def encode(self, texts: list[str]) -> list[list[float]]:
        from app.services.embedding import embed_texts
        return await embed_texts(texts)

    async def encode_query(self, query: str) -> list[float]:
        from app.services.embedding import embed_query
        return await embed_query(query)


class RerankAdapter:
    """Wraps rerank logic to satisfy RerankServiceBase protocol."""

    async def rerank(self, query: str, documents: list[str],
                     top_n: int = 10) -> list[dict]:
        from app.config import get_settings
        from app.services.metrics import rag_rerank_duration
        import time
        import httpx

        settings = get_settings()
        t0 = time.monotonic()
        try:
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
                        "top_n": top_n,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

            results = []
            for r in data.get("results", []):
                idx = r["index"]
                results.append({
                    "index": idx,
                    "score": float(r["relevance_score"]),
                    "text": documents[idx] if idx < len(documents) else "",
                })
            return results
        finally:
            rag_rerank_duration.observe(time.monotonic() - t0)


class OcrAdapter:
    """Wraps OCR module to satisfy OcrServiceBase protocol."""

    def recognize(self, image_path: str) -> str:
        from app.services.ocr import ocr_image
        return ocr_image(image_path)


class LlmAdapter:
    """Wraps LLMService to satisfy LlmServiceBase protocol."""

    async def stream_generate(self, query: str, context: str,
                              history: list | None = None) -> AsyncIterator[str]:
        from app.services.llm import LLMService
        svc = LLMService()
        async for token in svc.stream_generate(query, context, history):
            yield token


# ── Factory Functions ──────────────────────────────────────────────────────

def get_vector_store():
    from app.services.vector_store import get_vector_store as _get
    return _get()


def get_embedding_service() -> EmbeddingAdapter:
    return EmbeddingAdapter()


def get_rerank_service() -> RerankAdapter:
    return RerankAdapter()


def get_ocr_service() -> OcrAdapter:
    return OcrAdapter()


def get_llm_service() -> LlmAdapter:
    return LlmAdapter()
