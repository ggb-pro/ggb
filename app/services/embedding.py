"""Embedding service: call external bge-m3 API to vectorize text."""

import httpx
from app.config import get_settings

settings = get_settings()


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Call embedding API, return list of float vectors."""
    if not texts:
        return []

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

    # Sort by index to ensure order matches input
    embeddings = sorted(data["data"], key=lambda x: x["index"])
    return [e["embedding"] for e in embeddings]


async def embed_query(query: str) -> list[float]:
    """Embed a single query string."""
    results = await embed_texts([query])
    return results[0]
