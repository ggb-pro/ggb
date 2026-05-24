"""Celery task: process uploaded document through parse → chunk → embed → index pipeline."""

import asyncio
import logging

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_document(self, doc_id: str, user_id: str):
    """Full document processing pipeline via Celery worker."""
    try:
        _run_async(_process(doc_id, user_id))
    except Exception as exc:
        logger.error(f"Document processing failed: {doc_id}, error: {exc}")
        _run_async(_update_status(doc_id, "failed", str(exc)))
        raise self.retry(exc=exc)


async def _process(doc_id: str, user_id: str):
    """Delegate to the existing doc_processor module."""
    from app.services.doc_processor import process_document
    await process_document(doc_id, user_id)


async def _update_status(doc_id: str, status: str, error: str | None = None):
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    from app.config import get_settings
    from app.models.document import Document

    settings = get_settings()
    engine = create_async_engine(settings.database_url, pool_size=2)
    session_factory = async_sessionmaker(engine, class_=AsyncSession)
    async with session_factory() as db:
        result = await db.execute(select(Document).where(Document.id == doc_id))
        doc = result.scalar_one_or_none()
        if doc:
            doc.processing_status = status
            doc.processing_error = error
            await db.commit()
    await engine.dispose()
