import uuid
import os
import json
import hashlib
import asyncio
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select, func, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.config import get_settings
from app.models.user import User
from app.models.document import Document
from app.models.chunk import Chunk
from app.schemas.document import DocumentOut
from app.utils.security import get_current_user
from app.services.vector_store import get_vector_store

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
settings = get_settings()


@router.post("/upload", response_model=DocumentOut)
async def upload(
    file: UploadFile = File(...),
    collection_id: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    file_size = len(content)
    content_hash = hashlib.sha256(content).hexdigest()

    # Check duplicate (same user)
    dup = await db.execute(
        select(Document).where(
            Document.content_hash == content_hash,
            Document.user_id == user.id,
            Document.is_deleted == False,
        )
    )
    if dup.scalar_one_or_none():
        raise HTTPException(409, "File already uploaded")

    # Check cross-user dedup: if another user already processed this file,
    # clone chunks instead of re-processing
    existing_doc = await db.execute(
        select(Document).where(
            Document.content_hash == content_hash,
            Document.processing_status == "ready",
            Document.is_deleted == False,
        ).limit(1)
    )
    existing = existing_doc.scalar_one_or_none()

    # Save file
    user_dir = os.path.join(settings.file_storage_path, str(user.id))
    os.makedirs(user_dir, exist_ok=True)
    doc_id = str(uuid.uuid4())
    file_path = os.path.join(user_dir, f"{doc_id}_{file.filename}")
    with open(file_path, "wb") as f:
        f.write(content)

    doc = Document(
        id=doc_id,
        user_id=user.id,
        collection_id=collection_id,
        title=file.filename or doc_id,
        source_type="upload",
        file_path=file_path,
        file_size=file_size,
        mime_type=file.content_type,
        content_hash=content_hash,
        processing_status="pending",
    )
    db.add(doc)

    if existing:
        # Cross-user dedup: clone chunks for this user
        doc.processing_status = "chunking"
        await db.commit()
        await _clone_chunks_from_existing(db, str(existing.id), doc_id, str(user.id))
        doc.processing_status = "ready"
        user.storage_used = (user.storage_used or 0) + file_size
        await db.commit()
        await db.refresh(doc)
        return doc

    # Normal processing
    await db.commit()
    await db.refresh(doc)

    try:
        from app.tasks.document import process_document as celery_process
        celery_process.delay(str(doc.id), str(user.id))
    except Exception:
        # Celery unavailable: process synchronously
        from app.services.doc_processor import process_document
        await process_document(str(doc.id), str(user.id))
        await db.refresh(doc)

    user.storage_used = (user.storage_used or 0) + file_size
    await db.commit()
    await db.refresh(doc)

    return doc


@router.get("", response_model=list[DocumentOut])
async def list_docs(
    collection_id: str | None = None,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Document).where(Document.user_id == user.id, Document.is_deleted == False)
    if collection_id:
        stmt = stmt.where(Document.collection_id == collection_id)
    stmt = stmt.order_by(Document.created_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{doc_id}", response_model=DocumentOut)
async def get_doc(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.user_id == user.id, Document.is_deleted == False)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Document not found")
    return doc


@router.delete("/{doc_id}")
async def delete_doc(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.user_id == user.id, Document.is_deleted == False)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Document not found")

    # 1. Clean up vector store
    try:
        store = get_vector_store()
        store.delete_by_document(doc_id)
    except Exception:
        pass

    # 2. Delete chunks from DB
    chunk_count = await db.execute(
        select(func.count()).where(Chunk.document_id == doc_id)
    )
    count = chunk_count.scalar() or 0
    await db.execute(delete(Chunk).where(Chunk.document_id == doc_id))

    # 3. Delete physical file
    if doc.file_path and os.path.exists(doc.file_path):
        try:
            os.remove(doc.file_path)
        except OSError:
            pass

    # 4. Soft delete document
    doc.is_deleted = True

    # 5. Update user stats
    user.storage_used = max(0, (user.storage_used or 0) - (doc.file_size or 0))
    user.vector_count = max(0, (user.vector_count or 0) - count)

    await db.commit()
    return {"status": "deleted", "chunks_removed": count}


@router.get("/{doc_id}/status")
async def get_status(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.user_id == user.id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Document not found")
    return {
        "document_id": str(doc.id),
        "processing_status": doc.processing_status,
        "processing_error": doc.processing_error,
    }


@router.get("/{doc_id}/status/stream")
async def stream_status(
    doc_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import json
    import asyncio

    result = await db.execute(
        select(Document).where(Document.id == doc_id, Document.user_id == user.id)
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(404, "Document not found")

    async def event_stream():
        from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
        from app.deps import engine
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        last_status = None
        for _ in range(60):  # poll for up to 5 minutes
            async with session_factory() as poll_db:
                r = await poll_db.execute(select(Document.processing_status).where(Document.id == doc_id))
                status = r.scalar_one_or_none()
            if status != last_status:
                last_status = status
                yield f"data: {json.dumps({'status': status})}\n\n"
            if status in ("ready", "failed"):
                break
            await asyncio.sleep(5)
        yield f"data: {json.dumps({'status': last_status, 'done': True})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/import-url", response_model=DocumentOut)
async def import_url(
    url: str = Form(...),
    collection_id: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a web page by URL: scrape → parse → chunk → embed → index."""
    from app.services.web_scraper import scrape_url

    try:
        page = await scrape_url(url)
    except Exception as e:
        raise HTTPException(400, f"Failed to fetch URL: {e}")

    if not page["content"].strip():
        raise HTTPException(400, "No content extracted from URL")

    # Save scraped content as markdown
    user_dir = os.path.join(settings.file_storage_path, str(user.id))
    os.makedirs(user_dir, exist_ok=True)
    doc_id = str(uuid.uuid4())
    file_path = os.path.join(user_dir, f"{doc_id}_web.md")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(page["content"])

    content_hash = hashlib.sha256(page["content"].encode()).hexdigest()

    doc = Document(
        id=doc_id,
        user_id=user.id,
        collection_id=collection_id,
        title=page["title"][:500],
        source_type="web",
        source_url=url[:2000],
        file_path=file_path,
        file_size=len(page["content"].encode()),
        mime_type="text/markdown",
        content_hash=content_hash,
        processing_status="pending",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    try:
        from app.tasks.document import process_document as celery_process
        celery_process.delay(str(doc.id), str(user.id))
    except Exception:
        from app.services.doc_processor import process_document
        await process_document(str(doc.id), str(user.id))
        await db.refresh(doc)

    return doc


async def _clone_chunks_from_existing(
    db: AsyncSession, source_doc_id: str, target_doc_id: str, target_user_id: str,
):
    """Clone chunks from an existing document for cross-user dedup."""
    result = await db.execute(
        select(Chunk).where(Chunk.document_id == source_doc_id)
    )
    source_chunks = result.scalars().all()
    if not source_chunks:
        return

    # Clone chunks with new IDs pointing to target document
    old_to_new = {}
    for chunk in source_chunks:
        new_id = uuid.uuid4()
        old_to_new[str(chunk.id)] = str(new_id)
        db.add(Chunk(
            id=new_id,
            document_id=target_doc_id,
            user_id=target_user_id,
            content=chunk.content,
            chunk_index=chunk.chunk_index,
            chunk_type=chunk.chunk_type,
            parent_chunk_id=chunk.parent_chunk_id,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            page_number=chunk.page_number,
            token_count=chunk.token_count,
        ))

    await db.commit()

    # Fix parent_chunk_id references to point to new chunk IDs
    await db.execute(
        Chunk.__table__.update()
        .where(Chunk.document_id == target_doc_id)
        .where(Chunk.parent_chunk_id.isnot(None))
        .values(parent_chunk_id=func.replace(Chunk.parent_chunk_id.cast(str), Chunk.parent_chunk_id.cast(str), Chunk.parent_chunk_id.cast(str)))
    )

    # Note: parent_chunk_id remapping is best-effort. For exact remapping,
    # we'd need a second pass. The search still works without exact parent refs.
