import uuid
import json
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_db
from app.models.user import User
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.chat import ChatRequest, ChatMessage
from app.utils.security import get_current_user
from app.services.search import SearchService
from app.services.llm import LLMService

router = APIRouter(prefix="/api/v1/chat", tags=["chat"])


@router.post("")
async def chat(
    req: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    search_svc = SearchService()
    llm_svc = LLMService()

    # Get or create conversation
    conversation = None
    if req.conversation_id:
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == req.conversation_id,
                Conversation.user_id == user.id,
                Conversation.is_deleted == False,
            )
        )
        conversation = result.scalar_one_or_none()

    if not conversation:
        conversation = Conversation(user_id=user.id, model_name="deepseek-chat")
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)

    # Save user message
    user_msg = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=req.query,
    )
    db.add(user_msg)
    await db.commit()

    async def event_stream():
        try:
            # Step 1: Search
            yield f"data: {json.dumps({'type': 'status', 'message': 'Searching...'})}\n\n"
            search_results = await search_svc.search(req.query, str(user.id), collection_id=req.collection_id)

            # Step 2: Build context
            context = search_svc.build_context(search_results)
            citations = [
                {"chunk_id": r["chunk_id"], "score": r["score"], "snippet": r["content"][:200]}
                for r in search_results
            ]

            # Step 3: Stream citations
            yield f"data: {json.dumps({'type': 'citations', 'data': citations})}\n\n"

            # Step 4: Stream LLM response
            full_answer = ""
            async for token in llm_svc.stream_generate(req.query, context, history=req.history):
                full_answer += token
                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            # Step 5: Save assistant message
            assistant_msg = Message(
                conversation_id=conversation.id,
                user_id=user.id,
                role="assistant",
                content=full_answer,
                citations=citations,
                model_name="deepseek-chat",
            )
            db.add(assistant_msg)

            # Update conversation
            conversation.message_count += 2
            from datetime import datetime
            conversation.last_message_at = datetime.utcnow()

            # Auto-generate title from first message
            if conversation.message_count == 2 and not conversation.title:
                conversation.title = req.query[:50]

            await db.commit()

            yield f"data: {json.dumps({'type': 'done', 'conversation_id': str(conversation.id)})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
