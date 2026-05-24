"""Check chunk structure for a document."""
import asyncio
import asyncpg
import sys


async def main():
    doc_id = sys.argv[1] if len(sys.argv) > 1 else None
    conn = await asyncpg.connect("postgresql://knspace:knspace123@localhost/knspace")

    if doc_id:
        where = f"WHERE document_id = '{doc_id}'"
    else:
        where = ""

    rows = await conn.fetch(f"""
        SELECT id::text, chunk_index, chunk_type, parent_chunk_id::text,
               length(content) as len, left(content, 80) as preview
        FROM chunks {where}
        ORDER BY document_id, chunk_index
    """)
    print(f"Total chunks: {len(rows)}")
    for r in rows:
        pid = r["parent_chunk_id"][:8] if r["parent_chunk_id"] else "None"
        print(f'[{r["chunk_index"]:2d}] {r["chunk_type"]:6s} parent={pid:8s} len={r["len"]:4d} | {r["preview"]}')

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
