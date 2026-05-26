"""Custom RAG Prometheus metrics — fallback stubs when prometheus_client unavailable."""

try:
    from prometheus_client import Histogram, Counter, Gauge

    rag_retrieval_duration = Histogram(
        "rag_retrieval_duration_seconds", "Total retrieval time",
        buckets=[0.5, 1, 2, 5, 10, 30],
    )
    rag_results_count = Histogram(
        "rag_results_count", "Number of results after rerank",
        buckets=[1, 3, 5, 10, 20],
    )
    rag_rerank_duration = Histogram(
        "rag_rerank_duration_seconds", "Rerank time",
        buckets=[0.1, 0.3, 0.5, 1, 2, 5],
    )
    rag_llm_duration = Histogram(
        "rag_llm_duration_seconds", "LLM generation time",
        buckets=[1, 3, 5, 10, 30, 60],
    )
    embedding_api_duration = Histogram(
        "embedding_api_duration_seconds", "Embedding API call time",
        buckets=[0.1, 0.3, 0.5, 1, 3, 5],
    )
    rerank_api_duration = Histogram(
        "rerank_api_duration_seconds", "Rerank API call time",
        buckets=[0.1, 0.3, 0.5, 1, 2],
    )
    ocr_api_duration = Histogram(
        "ocr_api_duration_seconds", "OCR API call time",
        buckets=[0.5, 1, 2, 5, 10],
    )
    api_error_total = Counter(
        "api_error_total", "API call failures", ["service"],
    )
    model_memory_bytes = Gauge(
        "model_memory_bytes", "Local model memory usage", ["model_name"],
    )

except ImportError:
    class _Stub:
        def observe(self, *a, **kw): pass
        def inc(self, *a, **kw): pass
        def set(self, *a, **kw): pass
        def labels(self, *a, **kw): return self

    rag_retrieval_duration = _Stub()
    rag_results_count = _Stub()
    rag_rerank_duration = _Stub()
    rag_llm_duration = _Stub()
    embedding_api_duration = _Stub()
    rerank_api_duration = _Stub()
    ocr_api_duration = _Stub()
    api_error_total = _Stub()
    model_memory_bytes = _Stub()
