"""Embedding service: API-first with Redis cache, local fallback."""

import hashlib
import logging
import numpy as np
from app.config import get_settings
from app.services.cache import get_cached_embedding, cache_embedding

logger = logging.getLogger(__name__)
settings = get_settings()

EMBEDDING_DIM = 1024
_model = None


def _is_placeholder(key: str) -> bool:
    return not key or key == "your-api-key-here"


def _load_model():
    """Lazy-load bge-m3 model (local fallback only)."""
    global _model
    if _model is not None:
        return _model
    try:
        import os
        if not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        from sentence_transformers import SentenceTransformer
        logger.info("Loading bge-m3 model (local fallback)...")
        _model = SentenceTransformer(
            "BAAI/bge-m3",
            device="cpu",
            trust_remote_code=True,
        )
        logger.info("bge-m3 model loaded (CPU)")
        return _model
    except Exception as e:
        logger.warning(f"Failed to load bge-m3: {e}")
        return None


def _dummy_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic dummy embeddings for fallback."""
    results = []
    for t in texts:
        h = hashlib.sha256(t.encode()).digest()
        seed = int.from_bytes(h[:4], "little")
        rng = np.random.RandomState(seed)
        vec = rng.randn(EMBEDDING_DIM).astype(np.float32)
        vec /= np.linalg.norm(vec) + 1e-8
        results.append(vec.tolist())
    return results


async def _api_embed(texts: list[str]) -> list[list[float]] | None:
    """Call OpenAI-compatible Embedding API."""
    if _is_placeholder(settings.embedding_api_url):
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                settings.embedding_api_url.rstrip("/") + "/embeddings",
                headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
                json={"model": settings.embedding_model, "input": texts, "encoding_format": "float"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [e["embedding"] for e in sorted(data["data"], key=lambda x: x["index"])]
    except Exception as e:
        logger.warning(f"Embedding API failed: {e}")
        return None


def _local_embed(texts: list[str]) -> list[list[float]] | None:
    """Local bge-m3 inference."""
    model = _load_model()
    if model is None:
        return None
    embeddings = model.encode(texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True)
    return embeddings.tolist()


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts: Redis cache → API → local → dummy."""
    if not texts:
        return []

    # Check cache
    cached = {}
    misses = []
    for i, t in enumerate(texts):
        vec = await get_cached_embedding(t)
        if vec is not None:
            cached[i] = vec
        else:
            misses.append((i, t))

    results: list[list[float] | None] = [None] * len(texts)
    for i, vec in cached.items():
        results[i] = vec

    if not misses:
        return results  # type: ignore

    # Compute misses
    miss_texts = [t for _, t in misses]

    if settings.embedding_backend == "api":
        # API first, local fallback
        miss_vectors = await _api_embed(miss_texts)
        if miss_vectors is None:
            logger.info("API embedding failed, falling back to local model")
            miss_vectors = _local_embed(miss_texts)
    else:
        # Local first, API fallback
        miss_vectors = _local_embed(miss_texts)
        if miss_vectors is None:
            logger.info("Local embedding failed, falling back to API")
            miss_vectors = await _api_embed(miss_texts)

    if miss_vectors is None:
        miss_vectors = _dummy_embed(miss_texts)

    # Cache and fill
    for (orig_idx, text), vec in zip(misses, miss_vectors):
        results[orig_idx] = vec
        await cache_embedding(text, vec)

    return results  # type: ignore


async def embed_query(query: str) -> list[float]:
    """Embed a single query string."""
    results = await embed_texts([query])
    return results[0]
