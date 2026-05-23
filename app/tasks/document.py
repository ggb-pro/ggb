"""Celery task: process uploaded document through parse → chunk → embed → index pipeline."""

import uuid
import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.tasks.celery_app import celery_app
from app.config import get_settings
from app.models.document import Document
from app.models.chunk import Chunk
from app.services.parser import parse_file
from app.services.chunking import chunk_text
from app.services.embedding import embed_texts
from app.services.vector_store import ensure_collection, insert_vectors
from pymilvus import MilvusClient

logger = logging.getLogger(__name__)


def _run_async(coro):
    """Run async function from sync Celery task."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_document(self, doc_id: str, user_id: str):
    """Full document processing pipeline."""
    try:
        _run_async(_process(doc_id, user_id))
    except Exception as exc:
        logger.error(f"Document processing failed: {doc_id}, error: {exc}")
        # Update status to failed
        _run_async(_update_status(doc_id, "failed", str(exc)))
        raise self.retry(exc=exc)


async def _process(doc_id: str, user_id: str):
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_size=5)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        # Get document
        result = await db.execute(select(Document).where(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if not doc:
            logger.error(f"Document not found: {doc_id}")
            return

        # Stage 1: Parse
        doc.processing_status = "parsing"
        await db.commit()

        parsed = parse_file(doc.file_path, doc.mime_type)
        doc.title = doc.title or parsed.title
        doc.page_count = parsed.page_count
        doc.language = parsed.language

        # Stage 2: Chunk
        doc.processing_status = "chunking"
        await db.commit()

        all_chunks_data = []
        for section in parsed.sections:
            chunks = chunk_text(section.content)
            for c in chunks:
                all_chunks_data.append({
                    "content": c["content"],
                    "page_number": section.page_number,
                    "char_start": c["char_start"],
                    "char_end": c["char_end"],
                })

        # Save chunks to DB
        chunk_ids = []
        for i, cd in enumerate(all_chunks_data):
            chunk_id = uuid.uuid4()
            chunk_ids.append(str(chunk_id))
            chunk = Chunk(
                id=chunk_id,
                document_id=doc.id,
                user_id=doc.user_id,
                content=cd["content"],
                chunk_index=i,
                chunk_type="text",
                char_start=cd["char_start"],
                char_end=cd["char_end"],
                page_number=cd["page_number"],
                token_count=len(cd["content"]),  # approximate
            )
            db.add(chunk)

        await db.commit()

        # Stage 3: Embed
        doc.processing_status = "embedding"
        await db.commit()

        texts = [cd["content"] for cd in all_chunks_data]
        # Batch embed (64 at a time)
        all_vectors = []
        batch_size = 64
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            vectors = await embed_texts(batch)
            all_vectors.extend(vectors)

        # Stage 4: Index into Milvus
        doc.processing_status = "embedding"
        await db.commit()

        milvus_client = MilvusClient(uri=f"http://{settings.milvus_host}:{settings.milvus_port}")
        ensure_collection(milvus_client)

        insert_vectors(
            milvus_client,
            chunk_ids=chunk_ids,
            user_id=user_id,
            document_id=doc_id,
            vectors=all_vectors,
            snippets=texts,
        )

        # Done
        doc.processing_status = "ready"
        await db.commit()
        logger.info(f"Document processed successfully: {doc_id}, {len(all_chunks_data)} chunks")

    await engine.dispose()


async def _update_status(doc_id: str, status: str, error: str | None = None):
    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_size=2)
    session_factory = async_sessionmaker(engine, class_=AsyncSession)
    async with session_factory() as db:
        result = await db.execute(select(Document).where(Document.id == uuid.UUID(doc_id)))
        doc = result.scalar_one_or_none()
        if doc:
            doc.processing_status = status
            doc.processing_error = error
            await db.commit()
    await engine.dispose()
