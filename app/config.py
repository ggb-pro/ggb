from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "knSpace"
    debug: bool = False
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # Database (SQLite for local, PostgreSQL for production)
    database_url: str = "sqlite+aiosqlite:///./knspace.db"

    # Redis (not used in local mode)
    redis_url: str = "redis://localhost:6379/0"

    # Milvus (local file for dev, cluster for production)
    milvus_uri: str = "./milvus_data.db"

    # Embedding
    embedding_api_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"

    # LLM
    llm_api_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"

    # File storage
    file_storage_path: str = "/data/files"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
