"""Content pool GC + integrity check + repair + embedding backfill."""

import logging
import numpy as np
from sqlalchemy import select, delete, text, func, update
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from app.models.content_pool import ContentPool
from app.models.chunk import Chunk

logger = logging.getLogger(__name__)


async def gc_content_pool():
    """Remove content_pool entries with ref_count <= 0 with safety check (D2)."""
    from app.deps import engine

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        result = await db.execute(
            select(ContentPool.content_hash).where(ContentPool.ref_count <= 0)
        )
        dead_hashes = [row[0] for row in result.all()]

        if not dead_hashes:
            return

        for h in dead_hashes:
            # D2: Secondary check — verify no chunks still reference this hash
            remaining = await db.execute(
                select(func.count()).select_from(Chunk)
                .where(Chunk.content_hash == h)
            )
            if remaining.scalar() > 0:
                logger.error(f"GC safety: hash {h} has chunks but ref_count<=0, skipping")
                continue

            # Safe to clean external stores
            try:
                from app.services.vector_store import get_vector_store
                store = get_vector_store()
                store.client.delete(
                    collection_name="chunks",
                    filter=f'content_hash == "{h}"',
                )
            except Exception as e:
                logger.warning(f"GC: Milvus cleanup failed for {h}: {e}")

            try:
                from app.services.es import _get_es, _ensure_index
                from app.config import get_settings
                _ensure_index()
                es = _get_es()
                settings = get_settings()
                es.delete_by_query(
                    index=settings.es_index,
                    body={"query": {"term": {"content_hash": h}}},
                )
            except Exception as e:
                logger.warning(f"GC: ES cleanup failed for {h}: {e}")

        await db.execute(
            delete(ContentPool).where(ContentPool.ref_count <= 0)
        )
        await db.commit()
        logger.info(f"GC: cleaned {len(dead_hashes)} unreferenced content entries")


async def reconcile_cleanup():
    """D8: Retry pending chunk cleanups (5-minute compensation task)."""
    from app.deps import engine
    from app.services.vector_store import get_vector_store

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        result = await db.execute(
            select(Chunk.document_id).where(Chunk.cleanup_status == "pending")
            .group_by(Chunk.document_id)
        )
        pending_docs = [row[0] for row in result.all()]

        if not pending_docs:
            return

        for doc_id in pending_docs:
            milvus_ok, es_ok = False, False
            try:
                store = get_vector_store()
                store.delete_by_document(doc_id)
                milvus_ok = True
            except Exception:
                pass
            try:
                from app.services.es import delete_by_document as es_delete
                es_delete(doc_id)
                es_ok = True
            except Exception:
                pass

            if milvus_ok and es_ok:
                # Get unique hashes before deleting chunks
                hashes_result = await db.execute(
                    select(Chunk.content_hash).where(Chunk.document_id == doc_id)
                )
                content_hashes = list(set(row[0] for row in hashes_result.all()))

                await db.execute(delete(Chunk).where(Chunk.document_id == doc_id))

                for h in content_hashes:
                    await db.execute(text("""
                        UPDATE content_pool
                        SET ref_count = GREATEST(ref_count - 1, 0)
                        WHERE content_hash = :hash
                    """), {"hash": h})

                await db.execute(
                    delete(ContentPool).where(ContentPool.ref_count <= 0)
                )
                logger.info(f"Reconcile: cleaned up doc {doc_id}")

        await db.commit()


async def daily_integrity_check():
    """D3: Verify three-engine consistency."""
    from app.deps import engine
    from app.services.vector_store import get_vector_store

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        # Check A: ref_count vs actual chunk count
        mismatches = await db.execute(text("""
            SELECT cp.content_hash, cp.ref_count, COUNT(c.id) as actual
            FROM content_pool cp
            LEFT JOIN chunks c ON c.content_hash = cp.content_hash
            GROUP BY cp.content_hash, cp.ref_count
            HAVING cp.ref_count != COUNT(c.id)
        """))
        for row in mismatches:
            logger.error(f"ref_count mismatch: hash={row[0]}, stored={row[1]}, actual={row[2]}")
            # Auto-fix ref_count
            await db.execute(text("""
                UPDATE content_pool SET ref_count = :actual WHERE content_hash = :hash
            """), {"actual": row[2], "hash": row[0]})
        await db.commit()

        # Check B: Milvus orphan detection
        try:
            store = get_vector_store()
            pg_result = await db.execute(select(Chunk.id).where(Chunk.chunk_type == "child"))
            pg_ids = {str(r[0]) for r in pg_result.all()}

            milvus_ids = set()
            offset = 0
            while True:
                results = store.client.query(
                    collection_name="chunks",
                    filter="id != ''",
                    output_fields=["id"],
                    limit=1000, offset=offset,
                )
                if not results:
                    break
                for r in results:
                    milvus_ids.add(r["id"])
                offset += 1000

            missing = pg_ids - milvus_ids
            if missing:
                logger.warning(f"Integrity: {len(missing)} chunks missing from Milvus")
            orphaned = milvus_ids - pg_ids
            if orphaned:
                logger.warning(f"Integrity: {len(orphaned)} orphaned entries in Milvus")
        except Exception as e:
            logger.warning(f"Integrity check Milvus scan failed: {e}")


async def repair_milvus_from_pg():
    """D3: Rebuild missing Milvus entries from ContentPool (zero embedding cost)."""
    from app.deps import engine
    from app.services.vector_store import get_vector_store

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        pg_result = await db.execute(
            select(Chunk, ContentPool.vector, ContentPool.content)
            .join(ContentPool, Chunk.content_hash == ContentPool.content_hash)
            .where(Chunk.chunk_type == "child", ContentPool.vector != None)
        )
        rows = pg_result.all()
        pg_ids = {str(row[0].id) for row in rows}

        store = get_vector_store()
        milvus_ids = set()
        try:
            offset = 0
            while True:
                results = store.client.query(
                    collection_name="chunks",
                    filter="id != ''", output_fields=["id"],
                    limit=1000, offset=offset,
                )
                if not results:
                    break
                for r in results:
                    milvus_ids.add(r["id"])
                offset += 1000
        except Exception:
            return

        missing = pg_ids - milvus_ids
        if not missing:
            return

        repair_items = []
        for chunk, vec_bytes, content in rows:
            if str(chunk.id) not in missing or vec_bytes is None:
                continue
            repair_items.append({
                "chunk_id": str(chunk.id),
                "user_id": str(chunk.user_id),
                "document_id": str(chunk.document_id),
                "content_hash": chunk.content_hash,
                "vector": np.frombuffer(vec_bytes, dtype=np.float32).tolist(),
                "snippet": content[:500],
            })

        if repair_items:
            store.insert_batch(repair_items)
            logger.info(f"Repaired {len(repair_items)} Milvus entries from PG")


async def backfill_embeddings():
    """D9: Backfill embeddings for content_pool entries pending embedding."""
    from app.deps import engine
    from app.services.vector_store import get_vector_store
    from app.services.embedding import embed_texts, EMBEDDING_DIM
    from app.agent.degrade import is_api_healthy

    try:
        if not await is_api_healthy():
            return
    except Exception:
        return

    # PG advisory lock to prevent multi-instance duplicate backfill
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        acquired = (await db.execute(text("SELECT pg_try_advisory_lock(12345)"))).scalar()
        if not acquired:
            return

        try:
            result = await db.execute(
                select(ContentPool).where(ContentPool.needs_embedding == True).limit(200)
            )
            pending = result.scalars().all()

            if not pending:
                return

            texts = [p.content for p in pending]
            vectors = await embed_texts(texts)

            store = get_vector_store()
            for pool_row, vec in zip(pending, vectors):
                pool_row.vector = np.array(vec, dtype=np.float32).tobytes()
                pool_row.needs_embedding = False

            await db.commit()

            # Write repaired vectors to Milvus
            chunk_result = await db.execute(
                select(Chunk).where(
                    Chunk.content_hash.in_([p.content_hash for p in pending]),
                    Chunk.chunk_type == "child",
                )
            )
            milvus_items = []
            for chunk in chunk_result.scalars().all():
                pool = next((p for p in pending if p.content_hash == chunk.content_hash), None)
                if pool and pool.vector:
                    milvus_items.append({
                        "chunk_id": str(chunk.id),
                        "user_id": str(chunk.user_id),
                        "document_id": str(chunk.document_id),
                        "content_hash": chunk.content_hash,
                        "vector": np.frombuffer(pool.vector, dtype=np.float32).tolist(),
                        "snippet": pool.content[:500],
                    })
            if milvus_items:
                store.insert_batch(milvus_items)

            # Update documents from pending_embedding to ready
            await db.execute(text("""
                UPDATE documents SET processing_status = 'ready'
                WHERE processing_status = 'pending_embedding'
                AND id IN (
                    SELECT DISTINCT document_id FROM chunks c
                    JOIN content_pool cp ON c.content_hash = cp.content_hash
                    WHERE cp.needs_embedding = false
                    AND c.document_id IS NOT NULL
                )
            """))
            await db.commit()
            logger.info(f"Backfilled {len(pending)} embeddings")
        finally:
            await db.execute(text("SELECT pg_advisory_unlock(12345)"))
