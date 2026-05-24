"""Redis cache service: embedding cache + rate limiting."""

import hashlib
import logging
import json

logger = logging.getLogger(__name__)

_redis = None


def _get_redis():
    global _redis
    if _redis is not None:
        return _redis
    try:
        import redis.asyncio as aioredis
        from app.config import get_settings
        settings = get_settings()
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        return _redis
    except Exception as e:
        logger.warning(f"Redis unavailable: {e}")
        return None


async def get_cached_embedding(text: str) -> list[float] | None:
    """Check if embedding is cached in Redis."""
    r = _get_redis()
    if r is None:
        return None
    try:
        key = f"emb:{hashlib.md5(text.encode()).hexdigest()}"
        val = await r.get(key)
        if val:
            return json.loads(val)
    except Exception:
        pass
    return None


async def cache_embedding(text: str, vector: list[float], ttl: int = 7 * 86400):
    """Cache embedding vector in Redis."""
    r = _get_redis()
    if r is None:
        return
    try:
        key = f"emb:{hashlib.md5(text.encode()).hexdigest()}"
        await r.set(key, json.dumps(vector), ex=ttl)
    except Exception:
        pass


async def check_rate_limit(user_id: str, limit: int = 100, window: int = 3600) -> bool:
    """Check if user is within rate limit. Returns True if allowed."""
    r = _get_redis()
    if r is None:
        return True
    try:
        key = f"rl:{user_id}"
        count = await r.incr(key)
        if count == 1:
            await r.expire(key, window)
        return count <= limit
    except Exception:
        return True
