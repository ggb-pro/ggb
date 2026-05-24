"""Chunking service: structure-aware chunking with parent-child relationships."""

import re
from dataclasses import dataclass

CHUNK_SIZE = 400
CHUNK_OVERLAP = 64
MIN_CHUNK_SIZE = 20
PARENT_CHUNK_SIZE = 1200


@dataclass
class ChunkResult:
    content: str
    char_start: int
    char_end: int
    chunk_type: str = "child"  # parent or child
    parent_index: int | None = None  # index of parent chunk
    heading: str | None = None


def chunk_sections(sections: list[dict]) -> list[ChunkResult]:
    """Structure-aware chunking: build parent chunks from heading sections,
    split into child chunks for retrieval."""
    if not sections:
        return []

    # Group sections by heading tree
    heading_groups = _group_by_headings(sections)
    all_chunks: list[ChunkResult] = []
    offset = 0

    for group in heading_groups:
        heading = group["heading"]
        sections_text = "\n\n".join(s["content"] for s in group["sections"])
        if not sections_text.strip():
            continue

        # Create parent chunk (full section under a heading)
        parent_idx = len(all_chunks)
        if len(sections_text) >= MIN_CHUNK_SIZE:
            all_chunks.append(ChunkResult(
                content=sections_text,
                char_start=offset,
                char_end=offset + len(sections_text),
                chunk_type="parent",
                heading=heading,
            ))

        # Create child chunks (for retrieval)
        child_chunks = _chunk_text(sections_text, CHUNK_SIZE, CHUNK_OVERLAP)
        for cc in child_chunks:
            cc.chunk_type = "child"
            cc.parent_index = parent_idx
            cc.heading = heading
            cc.char_start += offset
            cc.char_end += offset
            all_chunks.append(cc)

        offset += len(sections_text) + 2  # account for \n\n

    return all_chunks


def _group_by_headings(sections: list[dict]) -> list[dict]:
    """Group sections by their nearest heading ancestor."""
    groups: list[dict] = []
    current_heading = None
    current_sections: list[dict] = []

    for s in sections:
        is_heading = s.get("section_type") == "heading" and s.get("level", 0) > 0
        if is_heading:
            if current_sections:
                groups.append({"heading": current_heading, "sections": current_sections})
            current_heading = s["content"]
            current_sections = [s]
        else:
            current_sections.append(s)

    if current_sections:
        groups.append({"heading": current_heading, "sections": current_sections})

    return groups if groups else [{"heading": None, "sections": sections}]


def _chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[ChunkResult]:
    """Split text into overlapping child chunks."""
    if not text.strip():
        return []

    paragraphs = re.split(r'\n\s*\n', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks: list[ChunkResult] = []
    current_chunk = ""
    char_offset = 0

    for para in paragraphs:
        if len(current_chunk) + len(para) + 1 <= chunk_size:
            current_chunk = f"{current_chunk}\n{para}".strip() if current_chunk else para
        else:
            if len(current_chunk) >= MIN_CHUNK_SIZE:
                chunks.append(ChunkResult(
                    content=current_chunk,
                    char_start=char_offset,
                    char_end=char_offset + len(current_chunk),
                ))
                char_offset += len(current_chunk) - overlap

            if len(para) > chunk_size:
                sub_chunks = _split_long_text(para, chunk_size, overlap)
                for sc in sub_chunks:
                    sc.char_start += char_offset
                    sc.char_end += char_offset
                    chunks.append(sc)
                char_offset = sc.char_end
                current_chunk = para[-overlap:] if overlap > 0 else ""
            else:
                current_chunk = para

    if len(current_chunk) >= MIN_CHUNK_SIZE:
        chunks.append(ChunkResult(
            content=current_chunk,
            char_start=char_offset,
            char_end=char_offset + len(current_chunk),
        ))

    return chunks


def _split_long_text(text: str, chunk_size: int, overlap: int) -> list[ChunkResult]:
    """Split a single long text by sentence boundaries."""
    sentences = re.split(r'(?<=[。！？.!?])\s*', text)
    sentences = [s for s in sentences if s.strip()]

    chunks: list[ChunkResult] = []
    current = ""
    offset = 0

    for sent in sentences:
        if len(current) + len(sent) + 1 <= chunk_size:
            current = f"{current}{sent}".strip() if not current else f"{current}{sent}"
        else:
            if current:
                chunks.append(ChunkResult(content=current, char_start=offset, char_end=offset + len(current)))
                offset += len(current) - overlap
            current = sent

    if current and len(current) >= MIN_CHUNK_SIZE:
        chunks.append(ChunkResult(content=current, char_start=offset, char_end=offset + len(current)))

    return chunks


# Backward compatibility
def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Legacy interface: returns list of dicts."""
    return [{"content": c.content, "char_start": c.char_start, "char_end": c.char_end}
            for c in _chunk_text(text, chunk_size, overlap)]
