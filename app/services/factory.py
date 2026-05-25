"""Service factory: abstract interfaces + backend selection via config."""

from __future__ import annotations
from typing import Protocol, runtime_checkable


# ── Vector Store ──────────────────────────────────────────────────────────

@runtime_checkable
class VectorStoreBase(Protocol):
    async def upsert(self, chunk_ids: list[str], user_id: str, document_id: str,
                     vectors: list[list[float]], snippets: list[str]): ...
    async def search(self, query_vector: list[float], user_id: str,
                     top_k: int) -> list[dict]: ...
    async def delete_by_document(self, document_id: str): ...


# ── Full-Text Search ─────────────────────────────────────────────────────

@runtime_checkable
class FullTextSearchBase(Protocol):
    async def search(self, query: str, user_id: str,
                     top_k: int) -> list[dict]: ...
    async def index_chunk(self, chunk_id: str, content: str, user_id: str): ...
    async def delete_chunk(self, chunk_id: str): ...


# ── Object Storage ───────────────────────────────────────────────────────

@runtime_checkable
class ObjectStorageBase(Protocol):
    async def save(self, key: str, data: bytes) -> str: ...
    async def load(self, key: str) -> bytes: ...
    async def delete(self, key: str): ...


# ── Embedding ─────────────────────────────────────────────────────────────

@runtime_checkable
class EmbeddingServiceBase(Protocol):
    async def encode(self, texts: list[str]) -> list[list[float]]: ...


# ── Rerank ────────────────────────────────────────────────────────────────

@runtime_checkable
class RerankServiceBase(Protocol):
    async def rerank(self, query: str, documents: list[str],
                     top_n: int) -> list[dict]: ...


# ── OCR ───────────────────────────────────────────────────────────────────

@runtime_checkable
class OcrServiceBase(Protocol):
    def recognize(self, image_path: str) -> str: ...


# ── LLM ───────────────────────────────────────────────────────────────────

@runtime_checkable
class LlmServiceBase(Protocol):
    async def stream_generate(self, query: str, context: str,
                              history: list | None = None): ...


# ── Factory helpers ───────────────────────────────────────────────────────

def get_vector_store():
    """Get vector store instance (Milvus Lite or fallback)."""
    from app.services.vector_store import get_vector_store as _get
    return _get()


def get_embedding_service() -> EmbeddingServiceBase:
    """Get embedding service. All backends expose embed_texts / embed_query."""
    from app.services import embedding as mod
    return mod  # module-level functions satisfy the protocol


def get_rerank_service() -> RerankServiceBase:
    """Get rerank service via SearchService._rerank."""
    from app.services.search import SearchService
    return SearchService()


def get_ocr_service() -> OcrServiceBase:
    """Get OCR service."""
    from app.services import ocr as mod
    return mod  # module-level ocr_image satisfies the protocol


def get_llm_service() -> LlmServiceBase:
    """Get LLM service."""
    from app.services.llm import LLMService
    return LLMService()
