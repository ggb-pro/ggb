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

    # Vector store (Milvus Standalone gRPC URI)
    milvus_uri: str = "http://localhost:19530"

    # Elasticsearch
    es_url: str = "http://localhost:9200"
    es_index: str = "chunks"

    # Embedding
    embedding_backend: str = "api"  # "api" | "local"
    embedding_api_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"

    # Reranker
    rerank_backend: str = "api"  # "api" | "local"
    rerank_api_url: str = ""
    rerank_api_key: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # OCR
    ocr_backend: str = "api"  # "api" | "local"
    ocr_api_url: str = "https://api.siliconflow.cn/v1"
    ocr_api_key: str = ""
    ocr_model: str = "deepseek-ai/DeepSeek-OCR"

    # LLM
    llm_api_url: str = "http://1239mxgn96959.vicp.fun:4009/v1"
    llm_api_key: str = ""
    llm_model: str = "glm-5.1-openai"

    # Agent (v2.0)
    use_agent: bool = False
    agent_lightweight_llm: str = "glm-4.5-air"
    agent_max_retries: int = 2         # max retry attempts after first try (total attempts = 3)
    agent_max_attempts: int = 3        # total attempts including first try (first + 2 retries)
    agent_degrade_cpu_threshold: float = 80.0
    agent_degrade_mem_threshold: float = 85.0

    # File storage
    file_storage_path: str = "./data/files"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache
def get_settings() -> Settings:
    return Settings()
