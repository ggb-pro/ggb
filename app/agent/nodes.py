"""Agent nodes: 7 LangGraph nodes — classify, plan, execute, generate, reflect, adjust."""

import json
import logging
import re
import time

import httpx

from app.agent.state import AgentState
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


_http_client: httpx.AsyncClient | None = None


async def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30)
    return _http_client


async def _call_lightweight_llm(prompt: str, max_tokens: int = 300) -> str:
    from app.services.metrics import agent_api_calls_total
    client = await _get_http_client()
    resp = await client.post(
        f"{settings.llm_api_url}/chat/completions",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": settings.agent_lightweight_llm,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.1,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    agent_api_calls_total.labels(service="llm", node="lightweight").inc()
    return data["choices"][0]["message"]["content"].strip()


def _extract_json(text: str) -> dict | list | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    for opener, closer in [("{", "}"), ("[", "]")]:
        start = text.find(opener)
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == opener:
                    depth += 1
                elif text[i] == closer:
                    depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        break
    return None


def _smart_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    for sep in ["。", "？", "！", ".", "?", "!"]:
        last = truncated.rfind(sep)
        if last > limit * 0.6:
            return truncated[:last + 1]
    return truncated


# ── Node 1: Intent Classification ────────────────────────────────────────────

async def intent_classify(state: AgentState) -> dict:
    from app.services.query_analyzer import QueryAnalyzer
    from app.services.metrics import intent_classify_total, intent_classify_duration

    t0 = time.monotonic()
    query = state["query"]
    analyzer = QueryAnalyzer()
    analyzed = await analyzer.analyze(query)

    if analyzed.query_type == "keyword":
        intent_classify_total.labels(intent="simple").inc()
        intent_classify_duration.observe(time.monotonic() - t0)
        return {"intent": "simple"}
    if analyzed.query_type == "semantic" and len(analyzed.sub_queries) == 1:
        intent_classify_total.labels(intent="simple").inc()
        intent_classify_duration.observe(time.monotonic() - t0)
        return {"intent": "simple"}

    intent_classify_total.labels(intent="complex").inc()
    intent_classify_duration.observe(time.monotonic() - t0)
    # Pass has_keyword to state for plan generation
    return {"intent": "complex", "has_keyword": analyzed.has_keyword}


# ── Node 2: Generate Plan ────────────────────────────────────────────────────

async def generate_plan(state: AgentState) -> dict:
    query = state["query"]
    has_keyword = state.get("has_keyword", False)

    keyword_hint = ""
    if has_keyword:
        keyword_hint = """重要：该查询包含精确关键词（ID/编号），必须在检索计划中优先使用
fulltext_search 定位精确匹配，再结合 hybrid_search 获取语义内容。
"""

    prompt = f"""你是一个 RAG 检索规划器。分析用户查询，生成检索计划。
{keyword_hint}
用户查询：{query}

可选工具：
- hybrid_search: 混合检索，参数: query, top_k(默认40), vector_weight(默认0.7), bm25_weight(默认0.3)
- fulltext_search: 纯全文检索，参数: query, top_k(默认20)

输出 JSON：
{{"sub_queries": [
  {{"id": "q1", "query": "子查询1", "tool": "hybrid_search", "depends_on": [], "args": {{}}}},
  {{"id": "q2", "query": "子查询2", "tool": "hybrid_search", "depends_on": ["q1"], "args": {{}}}}
]}}

规则：
- 子查询数量不超过 3 个
- 每个子查询必须独立可理解
- 多实体查询：分别检索每个实体
- 推理查询：前面的结果可能影响后续检索策略
- 有依赖的子查询在 args 中可引用前置结果
"""

    raw = await _call_lightweight_llm(prompt, max_tokens=500)
    parsed = _extract_json(raw)

    if parsed and isinstance(parsed, dict) and "sub_queries" in parsed:
        plan = []
        for sq in parsed["sub_queries"][:3]:
            tool = sq.get("tool", "hybrid_search")
            if tool not in {"hybrid_search", "fulltext_search"}:
                tool = "hybrid_search"
            plan.append({
                "id": sq.get("id", f"q{len(plan) + 1}"),
                "tool": tool,
                "args": {
                    "query": sq.get("query", query),
                    "top_k": min(sq.get("args", {}).get("top_k", 40), 80),
                    **sq.get("args", {}),
                },
                "depends_on": sq.get("depends_on", []),
            })
        if plan:
            return {"plan": plan, "retry_count": 0}

    return {"plan": [{"tool": "hybrid_search", "args": {"query": query}}], "retry_count": 0}


# ── Node 3: Execute Tools ────────────────────────────────────────────────────

async def execute_tools(state: AgentState) -> dict:
    from app.agent.tools import hybrid_search, fulltext_search

    plan = state.get("plan", [])
    user_id = state["user_id"]
    collection_id = state.get("collection_id")

    tool_map = {
        "hybrid_search": hybrid_search,
        "fulltext_search": fulltext_search,
    }

    executed = {}
    all_chunks = []
    tools_called = []

    for step in plan:
        step_id = step.get("id", "")

        deps = step.get("depends_on", [])
        dep_context = ""
        for dep_id in deps:
            if dep_id in executed:
                dep_context += f"前置检索[{dep_id}]结果摘要: {executed[dep_id][:200]}\n"

        args = dict(step.get("args", {}))
        args["user_id"] = user_id
        if collection_id and "collection_id" not in args:
            args["collection_id"] = collection_id
        args["top_k"] = min(args.get("top_k", 40), 100)
        if not args.get("query", "").strip():
            args["query"] = state["query"]

        if dep_context:
            args["query"] = f"{args['query']} (参考: {dep_context[:100]})"

        tool_name = step.get("tool", "")
        func = tool_map.get(tool_name)
        if not func:
            continue

        try:
            result = await func.ainvoke(args)
            if isinstance(result, list) and len(result) == 0:
                logger.info(f"hybrid_search returned 0 results for '{args['query']}', trying fulltext fallback")
                fallback_args = {"query": args["query"], "user_id": user_id, "top_k": 20}
                if collection_id:
                    fallback_args["collection_id"] = collection_id
                result = await fulltext_search.ainvoke(fallback_args)
                tools_called.append({
                    "tool": "fulltext_search",
                    "args": fallback_args,
                    "result_count": len(result) if isinstance(result, list) else 0,
                    "reason": "hybrid_search_zero_results_fallback",
                })
            if isinstance(result, list):
                all_chunks.extend(result)
                executed[step_id] = "; ".join(r.get("content", "")[:100] for r in result[:3])
            tools_called.append({"tool": tool_name, "args": args,
                                 "result_count": len(result) if isinstance(result, list) else 0})
        except Exception as e:
            logger.warning(f"Tool {tool_name} failed: {e}")
            tools_called.append({"tool": tool_name, "args": args, "error": str(e)})

    seen = set()
    unique_chunks = [c for c in all_chunks if c.get("chunk_id", "") not in seen and not seen.add(c["chunk_id"])]

    return {"chunks": unique_chunks, "tools_called": tools_called}


# ── Node 4: Generate Answer ──────────────────────────────────────────────────

async def generate_answer(state: AgentState) -> dict:
    from app.services.search import SearchService
    from app.services.llm import LLMService

    chunks = state.get("chunks", [])
    query = state["original_query"] or state["query"]

    if not chunks:
        return {"answer": "抱歉，未找到相关的文档内容来回答您的问题。", "context": ""}

    svc = SearchService()
    context = svc.build_context(chunks)

    llm = LLMService()
    answer = ""
    async for token in llm.stream_generate(query, context):
        answer += token

    return {"answer": answer, "context": context}


# ── Node 5: Reflect ──────────────────────────────────────────────────────────

async def reflect(state: AgentState) -> dict:
    from app.services.metrics import agent_retry_total, reflection_scores_hist

    answer = state.get("answer", "")
    query = state["query"]
    chunks = state.get("chunks", [])
    retry_count = state.get("retry_count", 0)

    if not answer:
        return {"should_retry": False, "reflection_result": "skip", "reflection_scores": {}}

    if retry_count >= settings.agent_max_retries + 2:
        return {"should_retry": False, "reflection_result": "max_retries_exhausted"}

    chunk_excerpts = "\n".join(
        f"[{i + 1}] {_smart_truncate(c.get('content', ''), 300)}"
        for i, c in enumerate(chunks[:5])
    )

    prompt = f"""评估以下回答的质量。只需输出 JSON。
用户问题：{query}
回答内容：{answer[:800]}

参考来源：
{chunk_excerpts}

从三个维度评分（1-5分）：
1. relevance（相关性）：回答是否直接回应了用户问题？
2. groundedness（事实性）：回答是否基于参考来源，有无编造？
3. consistency（一致性）：回答内部逻辑是否自洽？

输出：{{"pass": true/false, "scores": {{"relevance": N, "groundedness": N, "consistency": N}}, "reason": "原因", "suggestion": "改进建议"}}"""

    raw = await _call_lightweight_llm(prompt)
    parsed = _extract_json(raw)

    if parsed and isinstance(parsed, dict):
        scores = parsed.get("scores", {})
        for dim, val in scores.items():
            reflection_scores_hist.labels(dimension=dim).observe(val)
        if parsed.get("pass"):
            return {"should_retry": False, "reflection_result": "通过", "reflection_scores": scores}

        reason = parsed.get("reason", "质量不足")
        agent_retry_total.inc()
        return {
            "should_retry": True,
            "reflection_result": reason,
            "reflection_scores": scores,
            "retry_count": retry_count + 1,
        }

    return {"should_retry": False, "reflection_result": "parse_failed", "reflection_scores": {}}


# ── Node 6: Adjust Params (retry loop) ───────────────────────────────────────

def adjust_params(state: AgentState) -> dict:
    plan = state.get("plan", [])
    scores = state.get("reflection_scores", {})
    result = state.get("reflection_result", "")
    retry_count = state.get("retry_count", 0)

    # D7: Level 3 — final strategy: original query full search
    if retry_count >= 3:
        logger.info("D7: Level 3 strategy — original query full search")
        return {"plan": [{
            "tool": "hybrid_search",
            "args": {
                "query": state.get("original_query", state["query"]),
                "top_k": 100,
                "vector_weight": 0.5,
                "bm25_weight": 0.5,
            },
        }]}

    if not scores:
        return {"plan": plan}

    relevance = scores.get("relevance", 5)
    groundedness = scores.get("groundedness", 5)
    consistency = scores.get("consistency", 5)
    min_score = min(relevance, groundedness, consistency)

    strategy_level = min(retry_count, 2)

    new_plan = []
    for step in plan:
        args = dict(step.get("args", {}))

        if groundedness == min_score:
            if strategy_level >= 2:
                step = {"tool": "fulltext_search", "args": args}
                args["top_k"] = min(args.get("top_k", 40) + 20, 80)
            else:
                args["vector_weight"] = max(args.get("vector_weight", 0.7) - 0.2, 0.3)
                args["bm25_weight"] = 1.0 - args["vector_weight"]

        elif relevance == min_score:
            args["top_k"] = min(args.get("top_k", 40) + 20 * (strategy_level + 1), 100)
            if strategy_level >= 2 and state.get("original_query"):
                args["query"] = state["original_query"]

        else:
            args["top_k"] = min(args.get("top_k", 40) + 10, 80)
            args["vector_weight"] = max(args.get("vector_weight", 0.7) - 0.1, 0.3)
            args["bm25_weight"] = 1.0 - args["vector_weight"]

        new_plan.append({"tool": step["tool"], "args": args})

    logger.info(f"Adjusting params for retry (reason: {result}, scores: {scores}, strategy: {strategy_level})")
    return {"plan": new_plan}
