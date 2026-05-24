import uuid
from datetime import datetime
from pydantic import BaseModel


class CollectionCreate(BaseModel):
    name: str
    description: str | None = None
    icon: str | None = None
    parent_id: uuid.UUID | None = None
    type: str = "folder"


class CollectionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    icon: str | None = None
    parent_id: uuid.UUID | None = None
    sort_order: int | None = None


class CollectionOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    icon: str | None
    parent_id: uuid.UUID | None
    type: str
    sort_order: int
    created_at: datetime

    model_config = {"from_attributes": True}


class TagCreate(BaseModel):
    name: str
    color: str | None = None


class TagOut(BaseModel):
    id: uuid.UUID
    name: str
    color: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
