"""Embedding service: local bge-m3 with Redis cache, API fallback."""

import hashlib
import logging
import numpy as np
from app.config import get_settings
from app.services.cache import get_cached_embedding, cache_embedding

logger = logging.getLogger(__name__)
settings = get_settings()

EMBEDDING_DIM = 1024
_model = None


def _load_model():
    """Lazy-load bge-m3 model."""
    global _model
    if _model is not None:
        return _model
    try:
        import os
        if not os.environ.get("HF_ENDPOINT"):
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        from sentence_transformers import SentenceTransformer
        logger.info("Loading bge-m3 model...")
        _model = SentenceTransformer(
            "BAAI/bge-m3",
            device="cpu",
            trust_remote_code=True,
        )
        logger.info("bge-m3 model loaded (CPU)")
        return _model
    except Exception as e:
        logger.warning(f"Failed to load bge-m3: {e}, using dummy embeddings")
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


def _is_placeholder(key: str) -> bool:
    return not key or key == "your-api-key-here"


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts: check Redis cache → local model → API → dummy."""
    if not texts:
        return []

    # Check cache for each text, collect misses
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

    # Embed misses
    miss_texts = [t for _, t in misses]
    miss_vectors = await _compute_embeddings(miss_texts)

    # Cache and fill results
    for (orig_idx, text), vec in zip(misses, miss_vectors):
        results[orig_idx] = vec
        await cache_embedding(text, vec)

    return results  # type: ignore


async def _compute_embeddings(texts: list[str]) -> list[list[float]]:
    """Actual embedding computation without cache."""
    model = _load_model()
    if model is not None:
        embeddings = model.encode(texts, batch_size=64, show_progress_bar=False, normalize_embeddings=True)
        return embeddings.tolist()

    if not _is_placeholder(settings.embedding_api_key):
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                settings.embedding_api_url,
                headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
                json={"model": settings.embedding_model, "input": texts, "encoding_format": "float"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [e["embedding"] for e in sorted(data["data"], key=lambda x: x["index"])]

    return _dummy_embed(texts)


async def embed_query(query: str) -> list[float]:
    """Embed a single query string."""
    results = await embed_texts([query])
    return results[0]
