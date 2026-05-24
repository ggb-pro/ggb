"""Persistent vector store using numpy + pickle for single-server deployment."""

import os
import pickle
import logging
import numpy as np
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

EMBEDDING_DIM = 1024
DATA_FILE = "vector_store.pkl"

_store: "PersistentVectorStore | None" = None


class PersistentVectorStore:
    """In-memory vector store with disk persistence via pickle."""

    def __init__(self):
        self.records: list[dict] = []
        self.vectors: np.ndarray = np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        self._load()

    def _path(self) -> str:
        return os.path.join(settings.file_storage_path, "..", DATA_FILE)

    def _load(self):
        path = self._path()
        if os.path.exists(path):
            try:
                with open(path, "rb") as f:
                    data = pickle.load(f)
                self.records = data["records"]
                self.vectors = data["vectors"]
                logger.info(f"Loaded {len(self.records)} vectors from disk")
            except Exception as e:
                logger.warning(f"Failed to load vector store: {e}")

    def _save(self):
        path = self._path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump({"records": self.records, "vectors": self.vectors}, f)

    def insert(self, chunk_ids: list[str], user_id: str, document_id: str,
               vectors: list[list[float]], snippets: list[str]):
        new_vecs = np.array(vectors, dtype=np.float32)
        for cid, snip in zip(chunk_ids, snippets):
            self.records.append({
                "chunk_id": cid, "user_id": user_id,
                "document_id": document_id, "snippet": snip[:500],
            })
        if len(self.vectors) == 0:
            self.vectors = new_vecs
        else:
            self.vectors = np.vstack([self.vectors, new_vecs])
        self._save()

    def search(self, query_vector: list[float], user_id: str, top_k: int = 20) -> list[dict]:
        if len(self.records) == 0:
            return []

        user_mask = np.array([r["user_id"] == user_id for r in self.records])
        if not user_mask.any():
            return []

        user_vecs = self.vectors[user_mask]
        user_records = [r for r, m in zip(self.records, user_mask) if m]

        q = np.array(query_vector, dtype=np.float32)
        q_norm = q / (np.linalg.norm(q) + 1e-8)

        norms = np.linalg.norm(user_vecs, axis=1, keepdims=True) + 1e-8
        user_vecs_normed = user_vecs / norms
        scores = (user_vecs_normed @ q_norm).tolist()

        ranked = sorted(zip(scores, user_records), key=lambda x: x[0], reverse=True)
        return [
            {"chunk_id": r["chunk_id"], "document_id": r["document_id"],
             "score": s, "snippet": r["snippet"]}
            for s, r in ranked[:top_k]
        ]

    def delete_by_document(self, document_id: str):
        keep_mask = np.array([r["document_id"] != document_id for r in self.records])
        self.records = [r for r, m in zip(self.records, keep_mask) if m]
        if keep_mask.any():
            self.vectors = self.vectors[keep_mask]
        else:
            self.vectors = np.empty((0, EMBEDDING_DIM), dtype=np.float32)
        self._save()


def get_vector_store():
    global _store
    if _store is None:
        _store = PersistentVectorStore()
    return _store
