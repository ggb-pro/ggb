import uuid
from datetime import datetime
from pydantic import BaseModel


class ConversationUpdate(BaseModel):
    title: str | None = None


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str | None
    model_name: str
    message_count: int
    last_message_at: datetime
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    citations: list | None = None
    token_count: int | None = None
    model_name: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class FeedbackRequest(BaseModel):
    feedback: str  # "thumb_up" or "thumb_down"
