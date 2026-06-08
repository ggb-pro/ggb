"""Document processor: parse → chunk → embed → index with content pool dedup."""

import uuid
import hashlib
import re
import asyncio
import logging
import numpy as np
from sqlalchemy import select, text, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.document import Document
from app.models.chunk import Chunk
from app.models.content_pool import ContentPool
from app.services.parser import parse_file
from app.services.chunking import chunk_sections
from app.services.embedding import embed_texts, EMBEDDING_DIM
from app.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)


def _normalize_for_hash(text: str) -> str:
    text = re.sub(r'\s+', ' ', text.strip())
    text = re.sub(r'[。，！？.!?,;；：:]+$', '', text)
    return text


def _content_hash(content: str) -> str:
    return hashlib.sha256(_normalize_for_hash(content).encode("utf-8")).hexdigest()


async def _acquire_doc_lock(doc_id: str) -> bool:
    try:
        from app.services.cache import _get_redis
        r = _get_redis()
        if r is None:
            return True
        acquired = await r.set(f"doc_processing:{doc_id}", "1", nx=True, ex=300)
        return acquired is not False
    except Exception:
        return True


async def _release_doc_lock(doc_id: str):
    try:
        from app.services.cache import _get_redis
        r = _get_redis()
        if r is not None:
            await r.delete(f"doc_processing:{doc_id}")
    except Exception:
        pass


async def process_document(doc_id: str, user_id: str):
    """Process document entirely within the current event loop."""
    from app.deps import engine

    if not await _acquire_doc_lock(doc_id):
        logger.info(f"Doc {doc_id} already being processed by another instance")
        return

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        try:
            result = await db.execute(select(Document).where(Document.id == doc_id))
            doc = result.scalar_one_or_none()
            if not doc:
                return

            # Parse
            doc.processing_status = "parsing"
            await db.commit()
            parsed = parse_file(doc.file_path, doc.mime_type)
            doc.title = doc.title or parsed.title
            doc.page_count = parsed.page_count

            # Structure-aware chunking
            doc.processing_status = "chunking"
            await db.commit()

            sections_data = [{"content": s.content, "section_type": s.section_type,
                              "level": s.level, "page_number": s.page_number}
                             for s in parsed.sections]
            chunk_results = chunk_sections(sections_data)

            page_map = {}
            for s in parsed.sections:
                for c in chunk_results:
                    if s.content in c.content:
                        page_map[c.content] = s.page_number

            # Check API health for embedding
            from app.agent.degrade import is_api_healthy
            try:
                api_ok = await is_api_healthy()
            except Exception:
                api_ok = True  # degrade check not available, assume ok

            # Embed only child chunks (D11: parent chunks skip embedding)
            child_indices = [i for i, cr in enumerate(chunk_results) if cr.chunk_type == "child"]
            child_texts = [chunk_results[i].content for i in child_indices]

            all_vectors = [[0.0] * EMBEDDING_DIM] * len(chunk_results)

            if api_ok and child_texts:
                doc.processing_status = "embedding"
                await db.commit()
                child_vectors = []
                for i in range(0, len(child_texts), 64):
                    vectors = await embed_texts(child_texts[i:i + 64])
                    child_vectors.extend(vectors)
                for idx, vec in zip(child_indices, child_vectors):
                    all_vectors[idx] = vec
                doc.processing_status = "ready"
            else:
                logger.warning(f"API unavailable, doc {doc_id} saved without embeddings")
                doc.processing_status = "pending_embedding"

            # Compute content hashes and write to content_pool via UPSERT (D1)
            chunk_ids = []
            hashes = []
            pool_entries = {}

            for i, cr in enumerate(chunk_results):
                cid = str(uuid.uuid4())
                chunk_ids.append(cid)
                h = _content_hash(cr.content)
                hashes.append(h)

                if h not in pool_entries:
                    if cr.chunk_type == "child":
                        vec_bytes = np.array(all_vectors[i], dtype=np.float32).tobytes()
                        needs_emb = not api_ok
                    else:
                        # D11: parent chunk — no vector, no embedding needed
                        vec_bytes = None
                        needs_emb = False

                    result = await db.execute(text("""
                        INSERT INTO content_pool (content_hash, content, vector, ref_count, token_count, needs_embedding)
                        VALUES (:hash, :content, :vector, 1, :token_count, :needs_embedding)
                        ON CONFLICT (content_hash) DO UPDATE
                        SET ref_count = content_pool.ref_count + 1
                        RETURNING content_hash, content, vector, ref_count, token_count, needs_embedding
                    """), {
                        "hash": h, "content": cr.content,
                        "vector": vec_bytes, "token_count": len(cr.content),
                        "needs_embedding": needs_emb,
                    })
                    row = result.fetchone()
                    pool_entries[h] = ContentPool(
                        content_hash=row[0], content=row[1], vector=row[2],
                        ref_count=row[3], token_count=row[4], needs_embedding=row[5],
                    )

            await db.commit()

            # Write PG chunks (metadata only, content in content_pool)
            for i, cr in enumerate(chunk_results):
                cid = chunk_ids[i]
                parent_id = None
                if cr.parent_index is not None and cr.parent_index != i:
                    parent_id = chunk_ids[cr.parent_index] if cr.parent_index < len(chunk_ids) else None

                db.add(Chunk(
                    id=cid, document_id=str(doc.id), user_id=str(doc.user_id),
                    content_hash=hashes[i],
                    chunk_index=i, chunk_type=cr.chunk_type,
                    parent_chunk_id=parent_id,
                    char_start=cr.char_start, char_end=cr.char_end,
                    page_number=page_map.get(cr.content),
                ))
            await db.commit()
            logger.info(f"Committed {len(chunk_results)} chunks to DB (content_pool dedup)")

            # Index child chunks into Elasticsearch
            try:
                from app.services.es import bulk_index_chunks
                child_chunks = [
                    {"chunk_id": chunk_ids[i], "document_id": str(doc.id),
                     "user_id": str(doc.user_id), "content_hash": hashes[i],
                     "content": cr.content}
                    for i, cr in enumerate(chunk_results) if cr.chunk_type == "child"
                ]
                if child_chunks:
                    bulk_index_chunks(child_chunks)
            except Exception as e:
                logger.warning(f"ES indexing failed, falling back to PG FTS: {e}")
                from app.services.tokenizer import tokenize
                for i, cr in enumerate(chunk_results):
                    if cr.chunk_type != "child":
                        continue
                    tokens = tokenize(cr.content)
                    if tokens.strip():
                        await db.execute(
                            text("UPDATE chunks SET fts_vector = to_tsvector('simple', :tokens) WHERE id::text = :cid"),
                            {"tokens": tokens, "cid": chunk_ids[i]},
                        )
                await db.commit()

            # Index child chunks into Milvus (skip if API was down — no valid vectors)
            if api_ok:
                store = get_vector_store()
                milvus_data = [
                    {"chunk_id": chunk_ids[i], "user_id": str(user_id),
                     "document_id": str(doc.id), "content_hash": hashes[i],
                     "vector": all_vectors[i], "snippet": chunk_results[i].content[:500]}
                    for i, cr in enumerate(chunk_results) if cr.chunk_type == "child"
                ]
                if milvus_data:
                    store.insert_batch(milvus_data)

            doc.processing_status = doc.processing_status  # "ready" or "pending_embedding"
            doc.chunk_count = len(chunk_results)
            await db.commit()
            logger.info(f"Document processed: {doc_id}, {len(chunk_results)} chunks, api_ok={api_ok}")

        finally:
            await _release_doc_lock(doc_id)
