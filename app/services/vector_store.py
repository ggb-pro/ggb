"""In-memory vector store for local dev. Swappable with Milvus in production."""

import numpy as np
from dataclasses import dataclass, field

_store: "VectorStore | None" = None


@dataclass
class VectorRecord:
    chunk_id: str
    user_id: str
    document_id: str
    vector: np.ndarray
    snippet: str


class VectorStore:
    """Simple in-memory vector store using numpy cosine similarity."""

    def __init__(self):
        self.records: list[VectorRecord] = []

    def insert(self, chunk_ids: list[str], user_id: str, document_id: str,
               vectors: list[list[float]], snippets: list[str]):
        for cid, vec, snip in zip(chunk_ids, vectors, snippets):
            self.records.append(VectorRecord(
                chunk_id=cid, user_id=user_id, document_id=document_id,
                vector=np.array(vec, dtype=np.float32), snippet=snip[:500],
            ))

    def search(self, query_vector: list[float], user_id: str, top_k: int = 20) -> list[dict]:
        if not self.records:
            return []

        # Filter by user
        user_records = [r for r in self.records if r.user_id == user_id]
        if not user_records:
            return []

        # Cosine similarity
        q = np.array(query_vector, dtype=np.float32)
        q_norm = q / (np.linalg.norm(q) + 1e-8)

        scores = []
        for r in user_records:
            v_norm = r.vector / (np.linalg.norm(r.vector) + 1e-8)
            score = float(np.dot(q_norm, v_norm))
            scores.append((score, r))

        scores.sort(key=lambda x: x[0], reverse=True)

        return [
            {"chunk_id": r.chunk_id, "document_id": r.document_id,
             "score": s, "snippet": r.snippet}
            for s, r in scores[:top_k]
        ]

    def delete_by_document(self, document_id: str):
        self.records = [r for r in self.records if r.document_id != document_id]


def get_vector_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store
