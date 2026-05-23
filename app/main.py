from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.deps import engine
from app.models.base import Base
from app.api import auth, documents, chat

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Ensure Milvus collection exists
    try:
        from app.deps import get_milvus
        from app.services.vector_store import ensure_collection
        client = get_milvus()
        ensure_collection(client)
    except Exception as e:
        print(f"Warning: Milvus not available at startup: {e}")

    yield

    # Shutdown
    await engine.dispose()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(chat.router)


@app.get("/health")
async def health():
    return {"status": "ok"}
