"""Milvus vector store service."""

import uuid
from pymilvus import MilvusClient, DataType, CollectionSchema, FieldSchema
from app.config import get_settings

settings = get_settings()

COLLECTION_NAME = "knowledge_chunks"
VECTOR_DIM = 1024  # bge-m3 output dimension


def get_collection_name() -> str:
    return COLLECTION_NAME


def ensure_collection(client: MilvusClient):
    """Create collection if not exists."""
    if client.has_collection(COLLECTION_NAME):
        return

    schema = CollectionSchema(fields=[
        FieldSchema("chunk_id", DataType.VARCHAR, max_length=36, is_primary=True),
        FieldSchema("user_id", DataType.VARCHAR, max_length=36),
        FieldSchema("document_id", DataType.VARCHAR, max_length=36),
        FieldSchema("vector", DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
        FieldSchema("content_snippet", DataType.VARCHAR, max_length=500),
    ], description="Knowledge chunks")

    client.create_collection(
        collection_name=COLLECTION_NAME,
        schema=schema,
    )

    # Create index
    client.create_index(
        collection_name=COLLECTION_NAME,
        field_name="vector",
        index_params={"metric_type": "COSINE", "index_type": "FLAT"},  # FLAT for small scale
    )


def insert_vectors(
    client: MilvusClient,
    chunk_ids: list[str],
    user_id: str,
    document_id: str,
    vectors: list[list[float]],
    snippets: list[str],
):
    """Insert chunk vectors into Milvus."""
    data = [
        {
            "chunk_id": cid,
            "user_id": user_id,
            "document_id": document_id,
            "vector": vec,
            "content_snippet": s[:500],
        }
        for cid, vec, s in zip(chunk_ids, vectors, snippets)
    ]
    client.insert(collection_name=COLLECTION_NAME, data=data)


def search_vectors(
    client: MilvusClient,
    query_vector: list[float],
    user_id: str,
    top_k: int = 20,
) -> list[dict]:
    """Search similar vectors for a user."""
    results = client.search(
        collection_name=COLLECTION_NAME,
        data=[query_vector],
        limit=top_k,
        filter=f'user_id == "{user_id}"',
        output_fields=["chunk_id", "document_id", "content_snippet"],
        search_params={"metric_type": "COSINE"},
    )
    if not results or not results[0]:
        return []

    return [
        {
            "chunk_id": hit["entity"]["chunk_id"],
            "document_id": hit["entity"]["document_id"],
            "score": hit["distance"],
            "snippet": hit["entity"]["content_snippet"],
        }
        for hit in results[0]
    ]


def delete_by_document(client: MilvusClient, document_id: str):
    """Delete all vectors for a document."""
    client.delete(
        collection_name=COLLECTION_NAME,
        filter=f'document_id == "{document_id}"',
    )
