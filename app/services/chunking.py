"""Chunking service: split text into overlapping chunks of ~512 tokens."""

import re

# Approximate: 1 Chinese char ≈ 1 token, 1 English word ≈ 1.3 tokens
# Target: ~512 tokens ≈ ~400 Chinese chars or ~350 English words
CHUNK_SIZE = 400  # chars (approximate token count for mixed CJK)
CHUNK_OVERLAP = 50  # chars
MIN_CHUNK_SIZE = 50


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[dict]:
    """Split text into overlapping chunks. Returns list of {content, char_start, char_end}."""
    if not text.strip():
        return []

    # Split by paragraph boundaries first
    paragraphs = re.split(r'\n\s*\n', text)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    # Merge small paragraphs, split large ones
    chunks = []
    current_chunk = ""
    char_offset = 0

    for para in paragraphs:
        if len(current_chunk) + len(para) + 1 <= chunk_size:
            current_chunk = f"{current_chunk}\n{para}".strip() if current_chunk else para
        else:
            # Flush current chunk
            if len(current_chunk) >= MIN_CHUNK_SIZE:
                chunks.append({
                    "content": current_chunk,
                    "char_start": char_offset,
                    "char_end": char_offset + len(current_chunk),
                })
                char_offset += len(current_chunk) - overlap
            # Split paragraph if still too large
            if len(para) > chunk_size:
                sub_chunks = _split_long_text(para, chunk_size, overlap)
                for sc in sub_chunks:
                    sc["char_start"] += char_offset
                    sc["char_end"] += char_offset
                    chunks.append(sc)
                char_offset = sc["char_end"]
                current_chunk = para[-overlap:] if overlap > 0 else ""
            else:
                current_chunk = para

    # Flush remaining
    if len(current_chunk) >= MIN_CHUNK_SIZE:
        chunks.append({
            "content": current_chunk,
            "char_start": char_offset,
            "char_end": char_offset + len(current_chunk),
        })

    return chunks


def _split_long_text(text: str, chunk_size: int, overlap: int) -> list[dict]:
    """Split a single long text by sentence boundaries."""
    # Split by sentence-ending punctuation
    sentences = re.split(r'(?<=[。！？.!?])\s*', text)
    sentences = [s for s in sentences if s.strip()]

    chunks = []
    current = ""
    offset = 0

    for sent in sentences:
        if len(current) + len(sent) + 1 <= chunk_size:
            current = f"{current}{sent}".strip() if not current else f"{current}{sent}"
        else:
            if current:
                chunks.append({"content": current, "char_start": offset, "char_end": offset + len(current)})
                offset += len(current) - overlap
            current = sent

    if current and len(current) >= MIN_CHUNK_SIZE:
        chunks.append({"content": current, "char_start": offset, "char_end": offset + len(current)})

    return chunks
