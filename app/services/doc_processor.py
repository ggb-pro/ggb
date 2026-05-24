"""Document processor: parse → chunk → embed → index."""

import uuid
import asyncio
import logging
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.document import Document
from app.models.chunk import Chunk
from app.services.parser import parse_file
from app.services.chunking import chunk_sections
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

        # Parse
        doc.processing_status = "parsing"
        await db.commit()
        parsed = parse_file(doc.file_path, doc.mime_type)
        doc.title = doc.title or parsed.title
        doc.page_count = parsed.page_count

        # Structure-aware chunking
        doc.processing_status = "chunking"
        await db.commit()

        sections_data = [{"content": s.content, "section_type": s.section_type,
                          "level": s.level, "page_number": s.page_number}
                         for s in parsed.sections]
        chunk_results = chunk_sections(sections_data)

        # Determine page_number for each chunk from its heading group
        chunk_ids = []
        page_map = {}
        for s in parsed.sections:
            for c in chunk_results:
                if s.content in c.content:
                    page_map[c.content] = s.page_number

        for i, cr in enumerate(chunk_results):
            cid = uuid.uuid4()
            chunk_ids.append(str(cid))
            # Find parent_chunk_id
            parent_id = None
            if cr.parent_index is not None and cr.parent_index != i:
                parent_id = chunk_ids[cr.parent_index] if cr.parent_index < len(chunk_ids) else None

            db.add(Chunk(
                id=cid, document_id=doc.id, user_id=doc.user_id,
                content=cr.content, chunk_index=i, chunk_type=cr.chunk_type,
                parent_chunk_id=parent_id,
                char_start=cr.char_start, char_end=cr.char_end,
                page_number=page_map.get(cr.content),
                token_count=len(cr.content),
            ))
        await db.commit()
        logger.info(f"Committed {len(chunk_results)} chunks to DB")

        # Embed all chunks (parents + children)
        doc.processing_status = "embedding"
        await db.commit()
        texts = [cr.content for cr in chunk_results]
        all_vectors = []
        for i in range(0, len(texts), 64):
            vectors = await embed_texts(texts[i:i + 64])
            all_vectors.extend(vectors)

        # Index into vector store
        store = get_vector_store()
        store.insert(chunk_ids, str(user_id), str(doc.id), all_vectors, texts)

        doc.processing_status = "ready"
        await db.commit()
        logger.info(f"Document processed: {doc_id}, {len(chunk_results)} chunks")
