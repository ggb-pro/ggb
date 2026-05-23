import uuid
from pydantic import BaseModel


class ChatMessage(BaseModel):
    role: str  # user/assistant
    content: str


class ChatRequest(BaseModel):
    query: str
    conversation_id: uuid.UUID | None = None
    history: list[ChatMessage] | None = None
    collection_id: uuid.UUID | None = None
