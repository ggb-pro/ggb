"""Vector store: Milvus Lite with pickle fallback for single-server deployment."""

import logging
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

EMBEDDING_DIM = 1024
COLLECTION_NAME = "chunks"

_store = None


class MilvusLiteStore:
    """Persistent vector store backed by Milvus Lite."""

    def __init__(self):
        from pymilvus import MilvusClient, DataType
        self.uri = settings.vector_store_uri
        self.client = MilvusClient(uri=self.uri)

        if not self.client.has_collection(COLLECTION_NAME):
            from pymilvus import CollectionSchema, FieldSchema
            schema = CollectionSchema(fields=[
                FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=36, is_primary=True),
                FieldSchema(name="user_id", dtype=DataType.VARCHAR, max_length=36, is_partition_key=True),
                FieldSchema(name="document_id", dtype=DataType.VARCHAR, max_length=36),
                FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=EMBEDDING_DIM),
                FieldSchema(name="snippet", dtype=DataType.VARCHAR, max_length=2000),
            ])
            self.client.create_collection(
                collection_name=COLLECTION_NAME,
                schema=schema,
            )
            index_params = self.client.prepare_index_params()
            index_params.add_index(field_name="vector", index_type="IVF_FLAT",
                                   metric_type="COSINE", params={"nlist": 128})
            self.client.create_index(collection_name=COLLECTION_NAME, index_params=index_params)
            self.client.load_collection(collection_name=COLLECTION_NAME)
            logger.info(f"Created Milvus collection: {COLLECTION_NAME}")

    def insert(self, chunk_ids: list[str], user_id: str, document_id: str,
               vectors: list[list[float]], snippets: list[str]):
        data = []
        for cid, vec, snip in zip(chunk_ids, vectors, snippets):
            data.append({
                "id": cid, "user_id": user_id,
                "document_id": document_id,
                "vector": vec, "snippet": snip[:2000],
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
            {"chunk_id": hit["id"], "document_id": hit["entity"]["document_id"],
             "score": hit["distance"], "snippet": hit["entity"]["snippet"]}
            for hit in results[0]
        ]

    def delete_by_document(self, document_id: str):
        self.client.delete(
            collection_name=COLLECTION_NAME,
            filter=f'document_id == "{document_id}"',
        )


class PickleStore:
    """Fallback: in-memory with pickle persistence."""

    def __init__(self):
        import os, pickle, numpy as np
        self.np = np
        self.os = os
        self.pickle = pickle
        self.records: list[dict] = []
        self.vectors = np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        path = self._path()
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    data = pickle.load(f)
                self.records = data["records"]
                self.vectors = data["vectors"]
                logger.info(f"Loaded {len(self.records)} vectors from pickle")
            except Exception:
                pass

    def _path(self):
        import os
        return os.path.join(os.path.dirname(settings.vector_store_uri), "vector_store.pkl")

    def _save(self):
        path = self._path()
        self.os.makedirs(self.os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            self.pickle.dump({"records": self.records, "vectors": self.vectors}, f)

    def insert(self, chunk_ids, user_id, document_id, vectors, snippets):
        new_vecs = self.np.array(vectors, dtype=self.np.float32)
        for cid, snip in zip(chunk_ids, snippets):
            self.records.append({"chunk_id": cid, "user_id": user_id,
                                 "document_id": document_id, "snippet": snip[:500]})
        self.vectors = self.np.vstack([self.vectors, new_vecs]) if len(self.vectors) else new_vecs
        self._save()

    def search(self, query_vector, user_id, top_k=20):
        if not self.records:
            return []
        mask = self.np.array([r["user_id"] == user_id for r in self.records])
        if not mask.any():
            return []
        user_vecs = self.vectors[mask]
        user_records = [r for r, m in zip(self.records, mask) if m]
        q = self.np.array(query_vector, dtype=self.np.float32)
        q_norm = q / (self.np.linalg.norm(q) + 1e-8)
        norms = self.np.linalg.norm(user_vecs, axis=1, keepdims=True) + 1e-8
        scores = (user_vecs / norms @ q_norm).tolist()
        ranked = sorted(zip(scores, user_records), key=lambda x: x[0], reverse=True)
        return [{"chunk_id": r["chunk_id"], "document_id": r["document_id"],
                 "score": s, "snippet": r["snippet"]} for s, r in ranked[:top_k]]

    def delete_by_document(self, document_id):
        mask = self.np.array([r["document_id"] != document_id for r in self.records])
        self.records = [r for r, m in zip(self.records, mask) if m]
        self.vectors = self.vectors[mask] if mask.any() else self.np.empty((0, EMBEDDING_DIM), dtype=self.np.float32)
        self._save()


def get_vector_store():
    global _store
    if _store is not None:
        return _store
    try:
        _store = MilvusLiteStore()
        logger.info("Using Milvus Lite vector store")
    except Exception as e:
        logger.warning(f"Milvus Lite unavailable ({e}), using pickle store")
        _store = PickleStore()
    return _store
