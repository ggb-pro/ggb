"""Vector store using Milvus Lite for persistent vector storage."""

import logging
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

EMBEDDING_DIM = 1024
COLLECTION_NAME = "chunks"

_store: "MilvusVectorStore | None" = None


class MilvusVectorStore:
    """Persistent vector store backed by Milvus Lite."""

    def __init__(self):
        from pymilvus import MilvusClient, DataType
        self.uri = settings.milvus_uri
        self.client = MilvusClient(uri=self.uri)

        if not self.client.has_collection(COLLECTION_NAME):
            self.client.create_collection(
                collection_name=COLLECTION_NAME,
                dimension=EMBEDDING_DIM,
                metric_type="COSINE",
                auto_id=False,
                fields=[
                    {"name": "id", "dtype": DataType.VARCHAR, "max_length": 36, "is_primary": True},
                    {"name": "user_id", "dtype": DataType.VARCHAR, "max_length": 36, "is_partition_key": True},
                    {"name": "document_id", "dtype": DataType.VARCHAR, "max_length": 36},
                    {"name": "vector", "dtype": DataType.FLOAT_VECTOR, "dim": EMBEDDING_DIM},
                    {"name": "snippet", "dtype": DataType.VARCHAR, "max_length": 2000},
                ],
            )
            logger.info(f"Created Milvus collection: {COLLECTION_NAME}")

    def insert(self, chunk_ids: list[str], user_id: str, document_id: str,
               vectors: list[list[float]], snippets: list[str]):
        data = []
        for cid, vec, snip in zip(chunk_ids, vectors, snippets):
            data.append({
                "id": cid,
                "user_id": user_id,
                "document_id": document_id,
                "vector": vec,
                "snippet": snip[:2000],
            })
        self.client.insert(collection_name=COLLECTION_NAME, data=data)

    def search(self, query_vector: list[float], user_id: str, top_k: int = 20) -> list[dict]:
        results = self.client.search(
            collection_name=COLLECTION_NAME,
            data=[query_vector],
            filter=f'user_id == "{user_id}"',
            limit=top_k,
            output_fields=["document_id", "snippet"],
        )
        if not results or not results[0]:
            return []

        return [
            {
                "chunk_id": hit["id"],
                "document_id": hit["entity"]["document_id"],
                "score": hit["distance"],
                "snippet": hit["entity"]["snippet"],
            }
            for hit in results[0]
        ]

    def delete_by_document(self, document_id: str):
        self.client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'document_id == "{document_id}"',
        )


class InMemoryVectorStore:
    """Fallback in-memory store when Milvus is unavailable."""

    def __init__(self):
        import numpy as np
        self.np = np
        self.records: list[dict] = []

    def insert(self, chunk_ids, user_id, document_id, vectors, snippets):
        for cid, vec, snip in zip(chunk_ids, vectors, snippets):
            self.records.append({
                "chunk_id": cid, "user_id": user_id,
                "document_id": document_id,
                "vector": self.np.array(vec, dtype=self.np.float32),
                "snippet": snip[:500],
            })

    def search(self, query_vector, user_id, top_k=20):
        user_records = [r for r in self.records if r["user_id"] == user_id]
        if not user_records:
            return []
        q = self.np.array(query_vector, dtype=self.np.float32)
        q_norm = q / (self.np.linalg.norm(q) + 1e-8)
        scores = []
        for r in user_records:
            v_norm = r["vector"] / (self.np.linalg.norm(r["vector"]) + 1e-8)
            score = float(self.np.dot(q_norm, v_norm))
            scores.append((score, r))
        scores.sort(key=lambda x: x[0], reverse=True)
        return [{"chunk_id": r["chunk_id"], "document_id": r["document_id"],
                 "score": s, "snippet": r["snippet"]} for s, r in scores[:top_k]]

    def delete_by_document(self, document_id):
        self.records = [r for r in self.records if r["document_id"] != document_id]


def get_vector_store():
    global _store
    if _store is not None:
        return _store
    try:
        _store = MilvusVectorStore()
        logger.info("Using Milvus Lite vector store")
    except Exception as e:
        logger.warning(f"Milvus unavailable ({e}), falling back to in-memory store")
        _store = InMemoryVectorStore()
    return _store
