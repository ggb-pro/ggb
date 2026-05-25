"""Custom RAG Prometheus metrics."""

from prometheus_client import Histogram, Counter, Gauge

# Retrieval
rag_retrieval_duration = Histogram(
    "rag_retrieval_duration_seconds", "Total retrieval time (vector + BM25 + RRF)",
    buckets=[0.5, 1, 2, 5, 10, 30],
)
rag_results_count = Histogram(
    "rag_results_count", "Number of results after rerank",
    buckets=[1, 3, 5, 10, 20],
)

# Rerank
rag_rerank_duration = Histogram(
    "rag_rerank_duration_seconds", "Rerank time",
    buckets=[0.1, 0.3, 0.5, 1, 2, 5],
)

# LLM
rag_llm_duration = Histogram(
    "rag_llm_duration_seconds", "LLM generation time (first token to last)",
    buckets=[1, 3, 5, 10, 30, 60],
)

# Embedding API
embedding_api_duration = Histogram(
    "embedding_api_duration_seconds", "Embedding API call time",
    buckets=[0.1, 0.3, 0.5, 1, 3, 5],
)

# Rerank API
rerank_api_duration = Histogram(
    "rerank_api_duration_seconds", "Rerank API call time",
    buckets=[0.1, 0.3, 0.5, 1, 2],
)

# OCR API
ocr_api_duration = Histogram(
    "ocr_api_duration_seconds", "OCR API call time",
    buckets=[0.5, 1, 2, 5, 10],
)

# Errors
api_error_total = Counter(
    "api_error_total", "API call failures", ["service"],
)

# Model memory (updated by services when models load)
model_memory_bytes = Gauge(
    "model_memory_bytes", "Local model memory usage", ["model_name"],
)
