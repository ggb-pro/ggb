"""Degrade check: system load monitoring + LLM API health probe."""

import logging
import time

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# D9: Cached API health probe (30s TTL)
_cached_healthy: bool | None = None
_cached_at: float = 0


def should_degrade() -> bool:
    """Check if Agent should degrade to v1.x pipeline.

    Degrade when:
    1. CPU usage exceeds threshold
    2. Memory usage exceeds threshold
    """
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent

        if cpu > settings.agent_degrade_cpu_threshold:
            logger.info(f"Degraded: CPU {cpu}% > {settings.agent_degrade_cpu_threshold}%")
            return True
        if mem > settings.agent_degrade_mem_threshold:
            logger.info(f"Degraded: Memory {mem}% > {settings.agent_degrade_mem_threshold}%")
            return True
    except ImportError:
        logger.debug("psutil not available, skip degrade check")
    except Exception as e:
        logger.warning(f"Degrade check failed: {e}")

    return False


async def is_api_healthy() -> bool:
    """D9: Check LLM API health with 30-second cache.

    Returns False if the embedding/LLM API is unreachable,
    triggering deferred embedding mode in doc_processor.
    """
    global _cached_healthy, _cached_at
    now = time.monotonic()
    if _cached_healthy is not None and now - _cached_at < 30:
        return _cached_healthy

    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.llm_api_url}/models")
            _cached_healthy = resp.status_code == 200
    except Exception as e:
        logger.debug(f"API health probe failed: {e}")
        _cached_healthy = False

    _cached_at = now
    return _cached_healthy
