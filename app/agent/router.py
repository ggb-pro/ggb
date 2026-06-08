"""Query router: dispatch to v1.x pipeline or Agent, with graceful degradation."""

import asyncio
import json
import logging
import time

from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent.graph import get_agent_graph
from app.agent.state import AgentState
from app.config import get_settings
from app.deps import engine
from app.models.conversation import Conversation
from app.models.message import Message
from app.schemas.chat import ChatRequest, ChatMessage
from app.services.metrics import agent_execution_duration, agent_degrade_total
from app.services.search import SearchService
from app.services.llm import LLMService
from app.services.multi_turn import resolve_query_with_history
from app.services.citation import validate_citations
from app.models.user import User

logger = logging.getLogger(__name__)
settings = get_settings()

NO_DATA_RESPONSE = (
    "抱歉，经过多轮检索仍未能找到足够可靠的信息来准确回答您的问题。"
    "建议您：\n"
    "1. 尝试换一种方式描述问题\n"
    "2. 检查知识库中是否包含相关文档\n"
    "3. 如果问题涉及多个概念，可以拆分为单独的问题分别提问"
)


def _build_quality_warning(reflection: str, scores: dict, retry_count: int) -> str:
    """Generate graded quality warning based on reflection scores."""
    if reflection == "parse_failed":
        return "⚠️ 回答质量校验遇到异常，建议核实关键信息或换一种方式提问。"

    if not scores:
        return f"⚠️ 回答质量校验未通过（已重试 {retry_count} 次），请谨慎参考。"

    min_dim = min(scores, key=scores.get)
    min_val = scores[min_dim]

    if min_val <= 1:
        return (f"⚠️ 回答中可能存在事实性错误（{min_dim} 评分 {min_val}/5），"
                "建议直接查阅原始文档确认。")
    elif min_val <= 2:
        return (f"⚠️ 部分回答内容缺乏文档依据（{min_dim} 评分 {min_val}/5），"
                "建议对关键结论进行二次确认。")
    elif min_val <= 3:
        return f"💡 回答质量中等（最低维度 {min_val}/5），部分信息可能不够准确。"
    else:
        return f"回答质量尚可，但仍有提升空间（最低维度 {min_val}/5）。"


async def route_query(
    req: ChatRequest,
    user: User,
    db: AsyncSession,
) -> StreamingResponse:
    """Unified entry point: decide v1.x or Agent path."""
    from app.agent.degrade import should_degrade as _should_degrade

    if _should_degrade():
        agent_degrade_total.inc()
        logger.info("Agent degraded to v1.x (system overload)")
        return StreamingResponse(
            _degraded_v1_stream(req, user, db, reason="系统负载较高，暂时降级为标准检索"),
            media_type="text/event-stream",
        )

    try:
        return await _agent_stream(req, user, db)
    except Exception:
        agent_degrade_total.inc()
        logger.warning("Agent failed, degrading to v1.x", exc_info=True)
        return StreamingResponse(
            _degraded_v1_stream(req, user, db, reason="Agent 异常，已降级为标准检索"),
            media_type="text/event-stream",
        )


async def _ensure_conversation(
    req: ChatRequest, user: User, db: AsyncSession,
) -> tuple[Conversation, str, str]:
    """Create or load conversation, save user message. Returns (conv, conv_id, user_id)."""
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

    return conversation, str(conversation.id), str(user.id)


async def _save_assistant_msg(
    stream_db: AsyncSession, conversation: Conversation, user: User,
    content: str, citations: list, agent_trace: dict | None = None,
) -> None:
    """Save assistant message and update conversation stats."""
    from datetime import datetime, timezone

    assistant_msg = Message(
        conversation_id=conversation.id,
        user_id=user.id,
        role="assistant",
        content=content,
        citations=citations,
        model_name="glm-5.1-openai",
        agent_trace=agent_trace,
    )
    stream_db.add(assistant_msg)
    await stream_db.execute(
        Conversation.__table__.update()
        .where(Conversation.id == conversation.id)
        .values(
            message_count=Conversation.message_count + 2,
            last_message_at=datetime.now(timezone.utc),
            title=content[:50] if conversation.message_count == 0 and not conversation.title else Conversation.title,
        )
    )
    await stream_db.commit()


async def _load_history(stream_db, conversation) -> list[dict]:
    """Load recent conversation history."""
    hist_result = await stream_db.execute(
        select(Message).where(
            Message.conversation_id == conversation.id,
        ).order_by(Message.created_at.desc()).limit(6)
    )
    return [{"role": m.role, "content": m.content} for m in reversed(hist_result.scalars().all())]


# ── Degraded v1.x stream (with Agent step notification) ──────────────────────

async def _degraded_v1_stream(
    req: ChatRequest, user: User, db: AsyncSession, reason: str,
):
    """v1.x stream that sends a degradation notice to the frontend."""
    conversation, conversation_id, user_id = await _ensure_conversation(req, user, db)

    yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
    # #14: degradation notice
    yield f"data: {json.dumps({'type': 'agent_step', 'tool': 'system', 'thought': reason})}\n\n"

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as stream_db:
        try:
            history_messages = await _load_history(stream_db, conversation)
            resolved_query = await resolve_query_with_history(req.query, history_messages)

            search_svc = SearchService()
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
            max_cite_idx = len(search_results)

            yield f"data: {json.dumps({'type': 'citations', 'data': citations})}\n\n"

            full_answer = ""
            chat_history = (
                [ChatMessage(role=m["role"], content=m["content"]) for m in history_messages]
                if history_messages else (req.history or [])
            )
            llm_svc = LLMService()
            async for token in llm_svc.stream_generate(req.query, context, history=chat_history):
                full_answer += token
                yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

            full_answer = validate_citations(full_answer, max_cite_idx)
            await _save_assistant_msg(stream_db, conversation, user, full_answer, citations)
            yield f"data: {json.dumps({'type': 'done', 'conversation_id': conversation_id})}\n\n"

        except Exception as e:
            logger.error(f"Degraded stream error: {e}", exc_info=True)
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"


# ── v1.x stream ──────────────────────────────────────────────────────────────

async def _v1_stream(
    req: ChatRequest,
    user: User,
    db: AsyncSession,
) -> StreamingResponse:
    """v1.x fixed pipeline streaming."""
    conversation, conversation_id, user_id = await _ensure_conversation(req, user, db)

    async def event_stream():
        yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as stream_db:
            try:
                history_messages = await _load_history(stream_db, conversation)
                resolved_query = await resolve_query_with_history(req.query, history_messages)

                search_svc = SearchService()
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
                max_cite_idx = len(search_results)

                yield f"data: {json.dumps({'type': 'citations', 'data': citations})}\n\n"

                full_answer = ""
                chat_history = (
                    [ChatMessage(role=m["role"], content=m["content"]) for m in history_messages]
                    if history_messages else (req.history or [])
                )
                llm_svc = LLMService()
                async for token in llm_svc.stream_generate(req.query, context, history=chat_history):
                    full_answer += token
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

                full_answer = validate_citations(full_answer, max_cite_idx)
                await _save_assistant_msg(stream_db, conversation, user, full_answer, citations)
                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conversation_id})}\n\n"

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ── Agent stream ─────────────────────────────────────────────────────────────

async def _agent_stream(
    req: ChatRequest,
    user: User,
    db: AsyncSession,
) -> StreamingResponse:
    """Agent pipeline: intent classify → plan → execute → generate → reflect."""
    conversation, conversation_id, user_id = await _ensure_conversation(req, user, db)

    async def event_stream():
        yield f"data: {json.dumps({'type': 'conversation', 'conversation_id': conversation_id})}\n\n"
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as stream_db:
            try:
                t0 = time.monotonic()
                yield f"data: {json.dumps({'type': 'agent_step', 'tool': 'agent', 'thought': '意图分析中...'})}\n\n"

                # #1: Resolve references BEFORE passing to graph
                history_messages = await _load_history(stream_db, conversation)
                resolved_query = await resolve_query_with_history(req.query, history_messages)

                graph = get_agent_graph()
                initial_state: AgentState = {
                    "query": resolved_query,
                    "original_query": req.query,
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "collection_id": str(req.collection_id) if req.collection_id else None,
                    "intent": "",
                    "has_keyword": False,
                    "plan": [],
                    "tools_called": [],
                    "chunks": [],
                    "context": "",
                    "answer": "",
                    "reflection_result": "",
                    "reflection_scores": {},
                    "retry_count": 0,
                    "should_retry": False,
                    "error": None,
                }

                # #4: Global timeout to prevent runaway graph execution
                try:
                    result_state = await asyncio.wait_for(
                        graph.ainvoke(initial_state),
                        timeout=60,
                    )
                except asyncio.TimeoutError:
                    logger.warning("Agent graph timed out (60s), degrading to v1.x")
                    yield f"data: {json.dumps({'type': 'agent_step', 'tool': 'system', 'thought': 'Agent 执行超时，降级为标准检索'})}\n\n"
                    # In-stream degradation: use v1.x to generate answer
                    result_state = None

                if result_state is None:
                    # Timed out — do v1.x within the same stream
                    search_svc = SearchService()
                    search_results = await search_svc.search(
                        resolved_query, user_id,
                        collection_id=str(req.collection_id) if req.collection_id else None,
                    )
                    context = search_svc.build_context(search_results)
                    citations = [
                        {"chunk_id": r["chunk_id"], "score": r["score"], "snippet": r["content"][:200]}
                        for r in search_results
                    ]
                    yield f"data: {json.dumps({'type': 'citations', 'data': citations})}\n\n"
                    full_answer = ""
                    llm_svc = LLMService()
                    async for token in llm_svc.stream_generate(req.query, context):
                        full_answer += token
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                    full_answer = validate_citations(full_answer, len(citations))
                    await _save_assistant_msg(stream_db, conversation, user, full_answer, citations)
                    yield f"data: {json.dumps({'type': 'done', 'conversation_id': conversation_id})}\n\n"
                    return

                # Check if intent was "simple" → fall back to v1.x search + generate
                if result_state.get("intent") == "simple":
                    yield f"data: {json.dumps({'type': 'agent_step', 'tool': 'agent', 'thought': '简单查询，使用标准检索'})}\n\n"
                    search_svc = SearchService()
                    search_results = await search_svc.search(
                        resolved_query, user_id,
                        collection_id=str(req.collection_id) if req.collection_id else None,
                    )
                    context = search_svc.build_context(search_results)
                    citations = [
                        {"chunk_id": r["chunk_id"], "score": r["score"], "snippet": r["content"][:200]}
                        for r in search_results
                    ]
                else:
                    yield f"data: {json.dumps({'type': 'agent_step', 'tool': 'agent', 'thought': '复杂查询，Agent 规划执行'})}\n\n"
                    for tc in result_state.get("tools_called", []):
                        thought = f"检索到 {tc.get('result_count', 0)} 个结果"
                        yield f"data: {json.dumps({'type': 'agent_step', 'tool': tc.get('tool', ''), 'thought': thought})}\n\n"
                    chunks = result_state.get("chunks", [])
                    citations = [
                        {"chunk_id": c["chunk_id"], "score": c.get("score", 0), "snippet": c["content"][:200]}
                        for c in chunks
                    ]
                    context = result_state.get("context", "")

                max_cite_idx = len(citations)
                yield f"data: {json.dumps({'type': 'citations', 'data': citations})}\n\n"

                # Stream answer
                if result_state.get("answer") and result_state.get("intent") != "simple":
                    reflection = result_state.get("reflection_result", "")
                    # D7: retries exhausted → honest response, no hallucination
                    if reflection == "max_retries_exhausted":
                        answer = NO_DATA_RESPONSE
                    else:
                        answer = result_state["answer"]
                        if reflection in ("skip", "parse_failed"):
                            scores = result_state.get("reflection_scores", {})
                            retry_count = result_state.get("retry_count", 0)
                            warning = _build_quality_warning(reflection, scores, retry_count)
                            answer += f"\n\n> {warning}"
                    chunk_size = max(1, len(answer) // 20)
                    for i in range(0, len(answer), chunk_size):
                        token = answer[i:i + chunk_size]
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                    full_answer = answer
                else:
                    llm_svc = LLMService()
                    full_answer = ""
                    async for token in llm_svc.stream_generate(req.query, context):
                        full_answer += token
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

                full_answer = validate_citations(full_answer, max_cite_idx)

                # Build agent trace for observability
                trace_data = None
                if result_state:
                    trace_data = {
                        "intent": result_state.get("intent"),
                        "plan_steps": len(result_state.get("plan", [])),
                        "tools_called": result_state.get("tools_called", []),
                        "reflection_result": result_state.get("reflection_result"),
                        "reflection_scores": result_state.get("reflection_scores", {}),
                        "retry_count": result_state.get("retry_count", 0),
                        "chunk_count": len(result_state.get("chunks", [])),
                    }
                await _save_assistant_msg(stream_db, conversation, user, full_answer, citations,
                                          agent_trace=trace_data)

                elapsed = time.monotonic() - t0
                agent_execution_duration.observe(elapsed)
                yield f"data: {json.dumps({'type': 'done', 'conversation_id': conversation_id})}\n\n"

            except Exception as e:
                logger.error(f"Agent stream error: {e}", exc_info=True)
                # #4: In-stream degradation instead of just error
                try:
                    yield f"data: {json.dumps({'type': 'agent_step', 'tool': 'system', 'thought': 'Agent 异常，降级为标准检索'})}\n\n"
                    search_svc = SearchService()
                    search_results = await search_svc.search(
                        req.query, user_id,
                        collection_id=str(req.collection_id) if req.collection_id else None,
                    )
                    context = search_svc.build_context(search_results)
                    citations = [
                        {"chunk_id": r["chunk_id"], "score": r["score"], "snippet": r["content"][:200]}
                        for r in search_results
                    ]
                    yield f"data: {json.dumps({'type': 'citations', 'data': citations})}\n\n"
                    full_answer = ""
                    llm_svc = LLMService()
                    async for token in llm_svc.stream_generate(req.query, context):
                        full_answer += token
                        yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"
                    full_answer = validate_citations(full_answer, len(citations))
                    await _save_assistant_msg(stream_db, conversation, user, full_answer, citations)
                    yield f"data: {json.dumps({'type': 'done', 'conversation_id': conversation_id})}\n\n"
                except Exception as e2:
                    logger.error(f"In-stream degradation also failed: {e2}", exc_info=True)
                    yield f"data: {json.dumps({'type': 'error', 'message': str(e2)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
