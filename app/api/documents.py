import uuid
import os
import hashlib
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.config import get_settings
from app.models.user import User
from app.models.document import Document
from app.schemas.document import DocumentOut
from app.utils.security import get_current_user

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])
settings = get_settings()


@router.post("/upload", response_model=DocumentOut)
async def upload(
    file: UploadFile = File(...),
    collection_id: str | None = Form(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Read file content
    content = await file.read()
    file_size = len(content)
    content_hash = hashlib.sha256(content).hexdigest()

    # Check duplicate
    dup = await db.execute(
        select(Document).where(Document.content_hash == content_hash, Document.user_id == user.id, Document.is_deleted == False)
    )
    if dup.scalar_one_or_none():
        raise HTTPException(409, "File already uploaded")

    # Save to disk
    user_dir = os.path.join(settings.file_storage_path, str(user.id))
    os.makedirs(user_dir, exist_ok=True)
    doc_id = str(uuid.uuid4())
    file_path = os.path.join(user_dir, f"{doc_id}_{file.filename}")
    with open(file_path, "wb") as f:
        f.write(content)

    # Create DB record
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
    await db.commit()
    await db.refresh(doc)

    # Process document (async, same event loop)
    try:
        from app.services.doc_processor import process_document
        await process_document(str(doc.id), str(user.id))
        await db.refresh(doc)
    except Exception as e:
        doc.processing_status = "failed"
        doc.processing_error = str(e)
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
    doc.is_deleted = True
    await db.commit()
    return {"status": "deleted"}
