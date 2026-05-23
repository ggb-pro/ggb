import uuid
from sqlalchemy import String, Boolean, Integer, ForeignKey

from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class Collection(Base, TimestampMixin):
    __tablename__ = "collections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000))
    icon: Mapped[str | None] = mapped_column(String(50))
    parent_id: Mapped[str | None] = mapped_column(String(36))
    type: Mapped[str] = mapped_column(String(20), default="folder")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
