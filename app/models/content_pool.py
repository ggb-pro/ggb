"""Global content pool: deduplicated text + vector storage with reference counting."""

from datetime import datetime
from sqlalchemy import String, Text, Integer, SmallInteger, DateTime, LargeBinary, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base


class ContentPool(Base):
    __tablename__ = "content_pool"

    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    vector: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    needs_embedding: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
