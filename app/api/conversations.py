import uuid
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models.user import User
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.conversation import (
    ConversationUpdate, ConversationOut, MessageOut, FeedbackRequest,
)
from app.utils.security import get_current_user

router = APIRouter(prefix="/api/v1/conversations", tags=["conversations"])


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Conversation)
        .where(Conversation.user_id == str(user.id), Conversation.is_deleted == False)
        .order_by(Conversation.last_message_at.desc())
        .offset((page - 1) * size)
        .limit(size)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == str(conversation_id),
            Conversation.user_id == str(user.id),
            Conversation.is_deleted == False,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return conv


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_messages(
    conversation_id: uuid.UUID,
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Verify conversation ownership
    conv = await db.execute(
        select(Conversation).where(
            Conversation.id == str(conversation_id),
            Conversation.user_id == str(user.id),
            Conversation.is_deleted == False,
        )
    )
    if not conv.scalar_one_or_none():
        raise HTTPException(404, "Conversation not found")

    stmt = (
        select(Message)
        .where(Message.conversation_id == str(conversation_id))
        .order_by(Message.created_at.asc())
        .offset((page - 1) * size)
        .limit(size)
    )
    result = await db.execute(stmt)
    return result.scalars().all()


@router.patch("/{conversation_id}", response_model=ConversationOut)
async def update_conversation(
    conversation_id: uuid.UUID,
    data: ConversationUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == str(conversation_id),
            Conversation.user_id == str(user.id),
            Conversation.is_deleted == False,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if data.title is not None:
        conv.title = data.title
    await db.commit()
    await db.refresh(conv)
    return conv


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Conversation).where(
            Conversation.id == str(conversation_id),
            Conversation.user_id == str(user.id),
            Conversation.is_deleted == False,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(404, "Conversation not found")
    conv.is_deleted = True
    await db.commit()
    return {"status": "deleted"}


@router.post("/{conversation_id}/messages/{message_id}/feedback")
async def message_feedback(
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    data: FeedbackRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Message).where(
            Message.id == str(message_id),
            Message.conversation_id == str(conversation_id),
            Message.user_id == str(user.id),
        )
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(404, "Message not found")
    if data.feedback not in ("thumb_up", "thumb_down"):
        raise HTTPException(400, "feedback must be 'thumb_up' or 'thumb_down'")
    msg.feedback = data.feedback
    await db.commit()
    return {"status": "recorded"}
