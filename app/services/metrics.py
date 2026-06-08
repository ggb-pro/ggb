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

    # Agent v2.0 metrics
    agent_execution_duration = Histogram(
        "agent_execution_duration_seconds", "Agent total execution time",
        buckets=[0.5, 1, 2, 5, 10, 30],
    )
    agent_tool_call_duration = Histogram(
        "agent_tool_call_duration_seconds", "Single tool call time",
        buckets=[0.1, 0.3, 0.5, 1, 2, 5],
    )
    agent_retry_total = Counter(
        "agent_retry_total", "Agent reflection retries",
    )
    agent_degrade_total = Counter(
        "agent_degrade_total", "Degrades to v1.x pipeline",
    )
    intent_classify_total = Counter(
        "intent_classify_total", "Intent classification results", ["intent"],
    )
    intent_classify_duration = Histogram(
        "intent_classify_duration_seconds", "Intent classification time",
        buckets=[0.01, 0.05, 0.1, 0.3, 1],
    )
    agent_api_calls_total = Counter(
        "agent_api_calls_total", "Agent API calls", ["service", "node"],
    )
    reflection_scores_hist = Histogram(
        "agent_reflection_scores",
        "Reflection quality scores by dimension",
        ["dimension"],
        buckets=[1, 2, 3, 4, 5],
    )
    rag_data_loss = Counter(
        "rag_data_loss_total",
        "Data loss: PG has data but retrieval returns nothing",
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
    agent_execution_duration = _Stub()
    agent_tool_call_duration = _Stub()
    agent_retry_total = _Stub()
    agent_degrade_total = _Stub()
    intent_classify_total = _Stub()
    intent_classify_duration = _Stub()
    agent_api_calls_total = _Stub()
    reflection_scores_hist = _Stub()
    rag_data_loss = _Stub()
