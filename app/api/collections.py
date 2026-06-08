import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, func, String
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models.user import User
from app.models.collection import Collection
from app.models.tag import Tag
from app.models.document_tag import DocumentTag
from app.models.document import Document
from app.schemas.collection import (
    CollectionCreate, CollectionUpdate, CollectionOut,
    TagCreate, TagOut,
)
from app.utils.security import get_current_user

router = APIRouter(prefix="/api/v1/collections", tags=["collections"])


# --- Collection CRUD ---

@router.get("", response_model=list[CollectionOut])
async def list_collections(
    parent_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    uid = str(user.id)
    stmt = select(Collection).where(
        Collection.user_id == func.cast(uid, String),
        Collection.is_deleted == False,
    )
    if parent_id:
        stmt = stmt.where(Collection.parent_id == str(parent_id))
    else:
        stmt = stmt.where(Collection.parent_id == None)
    stmt = stmt.order_by(Collection.sort_order, Collection.created_at)
    result = await db.execute(stmt)
    return result.scalars().all()


@router.post("", response_model=CollectionOut)
async def create_collection(
    data: CollectionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    collection = Collection(
        user_id=str(user.id),
        name=data.name,
        description=data.description,
        icon=data.icon,
        parent_id=str(data.parent_id) if data.parent_id else None,
        type=data.type,
    )
    db.add(collection)
    await db.commit()
    await db.refresh(collection)
    return collection


# --- Tags (must be before /{collection_id} routes) ---

@router.get("/tags", response_model=list[TagOut], tags=["tags"])
async def list_tags(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Tag).where(Tag.user_id == str(user.id)).order_by(Tag.name)
    )
    return result.scalars().all()


@router.post("/tags", response_model=TagOut, tags=["tags"])
async def create_tag(
    data: TagCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    tag = Tag(user_id=str(user.id), name=data.name, color=data.color)
    db.add(tag)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(409, "Tag already exists")
    await db.refresh(tag)
    return tag


@router.delete("/tags/{tag_id}", tags=["tags"])
async def delete_tag(
    tag_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Tag).where(Tag.id == str(tag_id), Tag.user_id == str(user.id))
    )
    tag = result.scalar_one_or_none()
    if not tag:
        raise HTTPException(404, "Tag not found")
    await db.delete(tag)
    await db.commit()
    return {"status": "deleted"}


# --- Document Tags (before /{collection_id}) ---

@router.post("/documents/{doc_id}/tags/{tag_id}", tags=["document-tags"])
async def add_document_tag(
    doc_id: uuid.UUID,
    tag_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.execute(
        select(Document).where(
            Document.id == str(doc_id),
            Document.user_id == str(user.id),
            Document.is_deleted == False,
        )
    )
    if not doc.scalar_one_or_none():
        raise HTTPException(404, "Document not found")
    tag = await db.execute(
        select(Tag).where(Tag.id == str(tag_id), Tag.user_id == str(user.id))
    )
    if not tag.scalar_one_or_none():
        raise HTTPException(404, "Tag not found")

    dt = DocumentTag(document_id=str(doc_id), tag_id=str(tag_id))
    db.add(dt)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(409, "Tag already applied")
    return {"status": "added"}


@router.delete("/documents/{doc_id}/tags/{tag_id}", tags=["document-tags"])
async def remove_document_tag(
    doc_id: uuid.UUID,
    tag_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(DocumentTag).where(
            DocumentTag.document_id == str(doc_id),
            DocumentTag.tag_id == str(tag_id),
        )
    )
    dt = result.scalar_one_or_none()
    if not dt:
        raise HTTPException(404, "Tag association not found")
    await db.delete(dt)
    await db.commit()
    return {"status": "removed"}


# --- Collection detail routes (after /tags, /documents) ---

@router.get("/{collection_id}", response_model=CollectionOut)
async def get_collection(
    collection_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Collection).where(
            Collection.id == str(collection_id),
            Collection.user_id == str(user.id),
            Collection.is_deleted == False,
        )
    )
    collection = result.scalar_one_or_none()
    if not collection:
        raise HTTPException(404, "Collection not found")
    return collection


@router.patch("/{collection_id}", response_model=CollectionOut)
async def update_collection(
    collection_id: uuid.UUID,
    data: CollectionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Collection).where(
            Collection.id == str(collection_id),
            Collection.user_id == str(user.id),
            Collection.is_deleted == False,
        )
    )
    collection = result.scalar_one_or_none()
    if not collection:
        raise HTTPException(404, "Collection not found")

    update_data = data.model_dump(exclude_unset=True)
    if "parent_id" in update_data and update_data["parent_id"]:
        update_data["parent_id"] = str(update_data["parent_id"])
    for k, v in update_data.items():
        setattr(collection, k, v)
    await db.commit()
    await db.refresh(collection)
    return collection


@router.delete("/{collection_id}")
async def delete_collection(
    collection_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Collection).where(
            Collection.id == str(collection_id),
            Collection.user_id == str(user.id),
            Collection.is_deleted == False,
        )
    )
    collection = result.scalar_one_or_none()
    if not collection:
        raise HTTPException(404, "Collection not found")
    collection.is_deleted = True
    await db.commit()
    return {"status": "deleted"}


@router.get("/{collection_id}/documents")
async def list_collection_documents(
    collection_id: uuid.UUID,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from app.schemas.document import DocumentOut
    stmt = select(Document).where(
        Document.user_id == str(user.id),
        Document.collection_id == str(collection_id),
        Document.is_deleted == False,
    ).order_by(Document.created_at.desc()).offset((page - 1) * size).limit(size)
    result = await db.execute(stmt)
    return result.scalars().all()
