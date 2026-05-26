"""Elasticsearch full-text search service: index, search, delete chunks."""

import logging
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_es = None
_index_initialized = False


def _get_es():
    global _es
    if _es is not None:
        return _es
    from elasticsearch import Elasticsearch
    _es = Elasticsearch(settings.es_url, request_timeout=30)
    return _es


def _ensure_index():
    global _index_initialized
    if _index_initialized:
        return
    es = _get_es()
    try:
        exists = es.indices.exists(index=settings.es_index)
    except Exception:
        exists = False

    if not exists:
        es.indices.create(index=settings.es_index, body={
            "settings": {
                "number_of_shards": 1,
                "number_of_replicas": 0,
            },
            "mappings": {
                "properties": {
                    "chunk_id": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "user_id": {"type": "keyword"},
                    "content": {"type": "text", "analyzer": "standard"},
                    "content_jieba": {"type": "text", "analyzer": "standard"},
                },
            },
        })
        logger.info(f"Created ES index: {settings.es_index}")

    _index_initialized = True


def index_chunk(chunk_id: str, document_id: str, user_id: str, content: str):
    """Index a single chunk into ES with jieba-tokenized content."""
    from app.services.tokenizer import tokenize
    _ensure_index()
    es = _get_es()
    tokens = tokenize(content)
    es.index(
        index=settings.es_index,
        id=chunk_id,
        body={
            "chunk_id": chunk_id,
            "document_id": document_id,
            "user_id": user_id,
            "content": content,
            "content_jieba": tokens,
        },
        refresh=False,
    )


def bulk_index_chunks(chunks: list[dict]):
    """Bulk index multiple chunks. Each dict: {chunk_id, document_id, user_id, content}."""
    from app.services.tokenizer import tokenize
    _ensure_index()
    es = _get_es()
    actions = []
    for chunk in chunks:
        tokens = tokenize(chunk["content"])
        actions.append({"index": {"_index": settings.es_index, "_id": chunk["chunk_id"]}})
        actions.append({
            "chunk_id": chunk["chunk_id"],
            "document_id": chunk["document_id"],
            "user_id": chunk["user_id"],
            "content": chunk["content"],
            "content_jieba": tokens,
        })
    if actions:
        es.bulk(body=actions, refresh=True)
        logger.info(f"Bulk indexed {len(chunks)} chunks into ES")


def search(query: str, user_id: str, top_k: int = 40) -> list[dict]:
    """Full-text search with jieba tokenization. Returns [{chunk_id, document_id, score}]."""
    from app.services.tokenizer import tokenize_query
    _ensure_index()
    es = _get_es()

    tokens = tokenize_query(query)
    if not tokens.strip():
        return []

    body = {
        "query": {
            "bool": {
                "must": {"term": {"user_id": user_id}},
                "should": [
                    {"match": {"content_jieba": {"query": tokens, "operator": "or"}}},
                    {"match": {"content": {"query": query, "operator": "or"}}},
                ],
                "minimum_should_match": 1,
            },
        },
    }

    resp = es.search(index=settings.es_index, body=body, size=top_k)
    results = []
    for hit in resp["hits"]["hits"]:
        src = hit["_source"]
        results.append({
            "chunk_id": src["chunk_id"],
            "document_id": src["document_id"],
            "score": hit["_score"],
        })
    return results


def delete_by_document(document_id: str):
    """Delete all chunks belonging to a document."""
    _ensure_index()
    es = _get_es()
    es.delete_by_query(
        index=settings.es_index,
        body={"query": {"term": {"document_id": document_id}}},
    )
    logger.info(f"Deleted ES chunks for document: {document_id}")
