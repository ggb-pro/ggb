import json
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.deps import get_db, engine
from app.models.user import User
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.chat import ChatRequest, ChatMessage
from app.utils.security import get_current_user
from app.services.search import SearchService
from app.services.llm import LLMService
from app.services.multi_turn import resolve_query_with_history

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
        conversation = Conversation(user_id=user.id, model_name="glm-5.1-openai")
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)

    user_msg = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        role="user",
        content=req.query,
    )
    db.add(user_msg)
    await db.commit()

    conversation_id = str(conversation.id)
    user_id = str(user.id)

    async def event_stream():
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as stream_db:
            try:
                yield f"data: {json.dumps({'type': 'status', 'message': 'Searching...'})}\n\n"

                # Load conversation history for multi-turn resolution
                history_messages = []
                if req.conversation_id:
                    hist_result = await stream_db.execute(
                        select(Message).where(
                            Message.conversation_id == conversation.id,
                        ).order_by(Message.created_at.desc()).limit(6)
                    )
                    for m in reversed(hist_result.scalars().all()):
                        history_messages.append({"role": m.role, "content": m.content})

                # Resolve references in query
                resolved_query = await resolve_query_with_history(
                    req.query, history_messages
                )

                search_results = await search_svc.search(
                    resolved_query, user_id,
                    collection_id=str(req.collection_id) if req.collection_id else None,
                    history=[m["content"] for m in history_messages] if history_messages else None,
                )

                context = search_svc.build_context(search_results)
                citations = [
                    {"chunk_id": r["chunk_id"], "score": r["score"], "snippet": r["content"][:200]}
                    for r in search_results
                ]

                yield f"data: {json.dumps({'type': 'citations', 'data': citations})}\n\n"

                full_answer = ""
                # Build history for LLM from loaded messages
                chat_history = (
                    [ChatMessage(role=m["role"], content=m["content"]) for m in history_messages]
                    if history_messages else (req.history or [])
                )
                async for token in llm_svc.stream_generate(req.query, context, history=chat_history):
                    full_answer += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

                # Save assistant message
                assistant_msg = Message(
                    conversation_id=conversation.id,
                    user_id=user.id,
                    role="assistant",
                    content=full_answer,
                    citations=citations,
                    model_name="glm-5.1-openai",
                )
                stream_db.add(assistant_msg)

                from datetime import datetime, timezone
                await stream_db.execute(
                    Conversation.__table__.update()
                    .where(Conversation.id == conversation.id)
                    .values(
                        message_count=Conversation.message_count + 2,
                        last_message_at=datetime.now(timezone.utc),
                        title=req.query[:50] if conversation.message_count == 0 and not conversation.title else Conversation.title,
                    )
                )
                await stream_db.commit()

                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conversation_id})}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
