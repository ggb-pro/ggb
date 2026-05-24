"""Add FTS index and trigger to chunks table."""
import asyncio
import asyncpg


async def main():
    conn = await asyncpg.connect("postgresql://knspace:knspace123@localhost/knspace")

    # Check if FTS index exists
    exists = await conn.fetchval(
        "SELECT count(*) FROM pg_indexes WHERE indexname = 'ix_chunks_fts'"
    )
    if exists:
        print("FTS index already exists, skipping")
        await conn.close()
        return

    # Add fts_vector column if not exists
    try:
        await conn.execute("ALTER TABLE chunks ADD COLUMN fts_vector tsvector")
        print("Added fts_vector column")
    except Exception as e:
        print(f"fts_vector column: {e}")

    # Update existing rows
    updated = await conn.execute(
        "UPDATE chunks SET fts_vector = to_tsvector('simple', content) WHERE fts_vector IS NULL"
    )
    print(f"Updated rows: {updated}")

    # Create GIN index
    await conn.execute("CREATE INDEX ix_chunks_fts ON chunks USING gin(fts_vector)")
    print("FTS GIN index created")

    # Create trigger function
    await conn.execute("""
        CREATE OR REPLACE FUNCTION chunks_fts_trigger() RETURNS trigger AS $$
        BEGIN
            NEW.fts_vector := to_tsvector('simple', COALESCE(NEW.content, ''));
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)

    # Create trigger
    await conn.execute("DROP TRIGGER IF EXISTS chunks_fts_update ON chunks")
    await conn.execute("""
        CREATE TRIGGER chunks_fts_update
        BEFORE INSERT OR UPDATE OF content ON chunks
        FOR EACH ROW EXECUTE FUNCTION chunks_fts_trigger()
    """)
    print("FTS trigger created")

    await conn.close()
    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
