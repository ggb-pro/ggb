from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    app_name: str = "knSpace"
    debug: bool = False
    secret_key: str = "change-me"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    # Database
    database_url: str = "postgresql+asyncpg://knspace:knspace123@localhost/knspace"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Vector store (Milvus Lite file URI - named differently to avoid pymilvus auto-reading)
    vector_store_uri: str = "./milvus_data.db"

    # Embedding (API fallback, primary is local bge-m3)
    embedding_api_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"

    # LLM
    llm_api_url: str = "http://1239mxgn96959.vicp.fun:4009/v1"
    llm_api_key: str = ""
    llm_model: str = "glm-5.1-openai"

    # File storage
    file_storage_path: str = "./data/files"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
