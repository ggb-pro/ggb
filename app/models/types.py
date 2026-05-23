"""Cross-database compatible column types. Uses JSON for SQLite, JSONB for PostgreSQL."""

from sqlalchemy import String, Text
from sqlalchemy.types import TypeDecorator, VARCHAR
import json
import uuid

# Use String(36) for UUID (compatible with both SQLite and PG)
# Use JSON type for dict columns
from sqlalchemy import JSON


class GUID(TypeDecorator):
    """Platform-independent UUID type. Uses String(36) for storage."""
    impl = String(36)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is not None:
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            return uuid.UUID(value)
        return value
