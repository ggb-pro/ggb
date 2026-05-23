from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from redis.asyncio import Redis
from pymilvus import MilvusClient

from app.config import get_settings

settings = get_settings()

# PostgreSQL
engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=20)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        yield session


# Redis
_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis


# Milvus
_milvus: MilvusClient | None = None


def get_milvus() -> MilvusClient:
    global _milvus
    if _milvus is None:
        _milvus = MilvusClient(
            uri=f"http://{settings.milvus_host}:{settings.milvus_port}"
        )
    return _milvus
