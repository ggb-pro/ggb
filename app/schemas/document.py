import uuid
from datetime import datetime
from pydantic import BaseModel


class DocumentUpload(BaseModel):
    collection_id: uuid.UUID | None = None


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    source_type: str
    mime_type: str | None
    file_size: int | None
    page_count: int | None
    processing_status: str
    created_at: datetime

    model_config = {"from_attributes": True}
