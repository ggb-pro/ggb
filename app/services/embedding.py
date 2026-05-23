"""Embedding service: call external bge-m3 API to vectorize text."""

import hashlib
import numpy as np
import httpx
from app.config import get_settings

settings = get_settings()

# bge-m3 dimension
EMBEDDING_DIM = 1024


def _dummy_embed(texts: list[str]) -> list[list[float]]:
    """Deterministic dummy embeddings for local testing without API key."""
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
    """Call embedding API, return list of float vectors."""
    if not texts:
        return []

    if _is_placeholder(settings.embedding_api_key):
        return _dummy_embed(texts)

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            settings.embedding_api_url,
            headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
            json={
                "model": settings.embedding_model,
                "input": texts,
                "encoding_format": "float",
            },
        )
        resp.raise_for_status()
        data = resp.json()

    embeddings = sorted(data["data"], key=lambda x: x["index"])
    return [e["embedding"] for e in embeddings]


async def embed_query(query: str) -> list[float]:
    """Embed a single query string."""
    results = await embed_texts([query])
    return results[0]
