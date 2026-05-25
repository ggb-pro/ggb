import uuid
from sqlalchemy import String, Integer, Text, ForeignKey, DDL, event
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class Chunk(Base, TimestampMixin):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    user_id: Mapped[str] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_type: Mapped[str] = mapped_column(String(20), default="child")
    parent_chunk_id: Mapped[str | None] = mapped_column(UUID(as_uuid=True))
    char_start: Mapped[int | None] = mapped_column(Integer)
    char_end: Mapped[int | None] = mapped_column(Integer)
    page_number: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    metadata_: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)


# Auto-create fts_vector column + trigger + GIN index after table creation
event.listen(
    Chunk.__table__,
    "after_create",
    DDL("""
        ALTER TABLE chunks ADD COLUMN IF NOT EXISTS fts_vector TSVECTOR;

        CREATE OR REPLACE FUNCTION chunks_fts_trigger() RETURNS trigger AS $$
        BEGIN
          NEW.fts_vector := to_tsvector('simple', COALESCE(NEW.content, ''));
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS chunks_fts_update ON chunks;
        CREATE TRIGGER chunks_fts_update
          BEFORE INSERT OR UPDATE OF content ON chunks
          FOR EACH ROW EXECUTE FUNCTION chunks_fts_trigger();

        CREATE INDEX IF NOT EXISTS idx_chunks_fts ON chunks USING GIN(fts_vector);
        CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
        CREATE INDEX IF NOT EXISTS idx_chunks_user ON chunks(user_id);
    """),
)
