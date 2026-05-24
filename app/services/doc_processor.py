"""Document processor: parse → chunk → embed → index."""

import uuid
import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.document import Document
from app.models.chunk import Chunk
from app.services.parser import parse_file
from app.services.chunking import chunk_text
from app.services.embedding import embed_texts
from app.services.vector_store import get_vector_store

logger = logging.getLogger(__name__)


async def process_document(doc_id: str, user_id: str):
    """Process document entirely within the current event loop."""
    from app.deps import engine

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        result = await db.execute(select(Document).where(Document.id == doc_id))
        doc = result.scalar_one_or_none()
        if not doc:
            return

        # Parse (CPU-bound, but fast)
        doc.processing_status = "parsing"
        await db.commit()
        parsed = parse_file(doc.file_path, doc.mime_type)
        doc.title = doc.title or parsed.title
        doc.page_count = parsed.page_count

        # Chunk
        doc.processing_status = "chunking"
        await db.commit()
        all_data = []
        for section in parsed.sections:
            for c in chunk_text(section.content):
                all_data.append({"content": c["content"], "page_number": section.page_number,
                                 "char_start": c["char_start"], "char_end": c["char_end"]})

        chunk_ids = []
        for i, cd in enumerate(all_data):
            cid = uuid.uuid4()
            chunk_ids.append(str(cid))
            db.add(Chunk(
                id=cid, document_id=doc.id, user_id=doc.user_id,
                content=cd["content"], chunk_index=i, chunk_type="text",
                char_start=cd["char_start"], char_end=cd["char_end"],
                page_number=cd["page_number"], token_count=len(cd["content"]),
            ))
        await db.commit()

        # Embed
        doc.processing_status = "embedding"
        await db.commit()
        texts = [cd["content"] for cd in all_data]
        all_vectors = []
        for i in range(0, len(texts), 64):
            vectors = await embed_texts(texts[i:i + 64])
            all_vectors.extend(vectors)

        # Index into vector store
        store = get_vector_store()
        store.insert(chunk_ids, str(user_id), str(doc.id), all_vectors, texts)

        doc.processing_status = "ready"
        await db.commit()
        logger.info(f"Document processed: {doc_id}, {len(all_data)} chunks")
