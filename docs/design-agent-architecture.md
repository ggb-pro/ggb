# knSpace Agent 架构增强方案

> 基于 Claude Code 架构设计思维，将 RAG 系统从固定管线升级为 Agent 驱动的自适应检索系统。
> 解决当前方案核心痛点：管线僵化、无自纠能力、上下文浪费、无记忆学习。

---

## 0. 当前痛点分析（基于代码审计）

| 痛点 | 当前实现 | 问题 |
|------|---------|------|
| **管线僵化** | `chat.py` 硬编码：resolve → search → rerank → build_context → generate | 无论查询质量如何，都走同一套流程，无法跳步/迭代 |
| **无自纠能力** | `_do_search()` 返回空结果时直接返回 `[]` | 搜索失败不会换策略重试，直接给用户一个空回答 |
| **上下文浪费** | `build_context()` 按 char 数截断，无优先级 | 8000 token 预算可能被低相关度内容占满 |
| **无记忆学习** | 每次查询完全无状态（除对话历史） | 不知道用户偏好什么类型的回答，不会从 feedback 学习 |
| **Tool Use 缺失** | LLM 只能生成文本，不能调用检索工具 | 无法让 LLM 自主决定是否需要更多上下文 |
| **中文搜索失效** | `plainto_tsquery('simple', ...)` 不支持中文分词 | FTS 分支对中文查询几乎无效，混合检索退化成纯向量检索 |
| **处理管线脆弱** | `doc_processor.py` 任何步骤失败 = 整个文档失败 | OCR 第 5 页失败，前 4 页的结果也丢了 |

---

## 1. Claude Code 架构模式映射

Claude Code 不是一个聊天机器人，而是一个 **拥有工具、能规划、会自我纠错的 Agent**。以下是核心模式到 knSpace 的映射：

| Claude Code 模式 | knSpace 应用 | 解决的痛点 |
|------------------|-------------|-----------|
| **Tool System** (Read/Edit/Bash/Grep) | RAG Tool System (vector_search/fulltext_search/rerank/...) | 管线僵化、无 Tool Use |
| **Agent Loop** (observe → think → act → observe) | RAG Agent ReAct 循环 | 无自纠能力 |
| **Context Management** (压缩/摘要/优先级) | Smart Context Manager | 上下文浪费 |
| **Memory System** (MEMORY.md 四种类型) | User Knowledge Memory | 无记忆学习 |
| **Planning Mode** (EnterPlanMode) | Query Planner | 复杂查询无规划 |
| **Sub-Agent** (Agent tool) | Parallel Retrieval Worker | 多跳查询串行慢 |
| **Hook System** (pre/post hooks) | Processing Pipeline Hooks | 处理管线脆弱 |

---

## 2. 核心改造：Tool-Based RAG Agent

### 2.1 当前 vs 改造后

**当前（固定管线）：**
```python
# chat.py — 70 行硬编码流程，任何查询都走同一路径
resolved = await resolve_query_with_history(query, history)
results = await search_svc.search(resolved, user_id)
context = search_svc.build_context(results)
async for token in llm_svc.stream_generate(query, context, history):
    yield token
```

**改造后（Agent 驱动）：**
```python
# Agent 自主决策：用哪些工具、搜几次、是否需要换策略
agent = RAGAgent(tools=rerag_tools, memory=user_memory)
response = await agent.run(query, user_id, conversation_id)
# agent.run 内部：plan → tool_call → observe → (iterate?) → generate
```

### 2.2 Tool 定义

```python
# app/agent/tools.py — 每个 Tool 有清晰的输入/输出 schema

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ToolResult:
    success: bool
    data: dict | list
    metadata: dict | None = None  # token_usage, latency, etc.


class RAGTool(Protocol):
    name: str
    description: str  # 给 LLM 的自然语言描述

    async def execute(self, params: dict) -> ToolResult: ...


# ── 具体工具 ──────────────────────────────────────────────────

class VectorSearchTool:
    """在向量数据库中搜索语义相关的文档分块。"""
    name = "vector_search"
    description = "搜索与查询语义相关的文档片段。适用于概念性问题、解释类问题。"

    async def execute(self, params: dict) -> ToolResult:
        query = params["query"]
        top_k = params.get("top_k", 20)
        query_vector = await embed_query(query)
        store = get_vector_store()
        results = store.search(query_vector, params["user_id"], top_k=top_k)
        return ToolResult(success=True, data=results,
                          metadata={"tool": "vector_search", "count": len(results)})


class FullTextSearchTool:
    """全文检索工具。适用于精确关键词、编号、引用的搜索。"""
    name = "fulltext_search"
    description = "按关键词精确搜索文档。适用于编号、专有名词、引用等精确匹配场景。"

    async def execute(self, params: dict) -> ToolResult:
        # 使用 zhparser 中文分词（见 §6 修复）
        results = await self._fts_search(params["query"], params["user_id"],
                                         params.get("top_k", 20))
        return ToolResult(success=True, data=results,
                          metadata={"tool": "fulltext_search", "count": len(results)})


class HybridSearchTool:
    """混合检索 + RRF 融合。默认推荐工具。"""
    name = "hybrid_search"
    description = "同时进行语义搜索和关键词搜索并融合结果。适用于大多数查询场景。"

    async def execute(self, params: dict) -> ToolResult:
        query = params["query"]
        user_id = params["user_id"]
        top_k = params.get("top_k", 40)
        vector_weight = params.get("vector_weight", 0.7)
        bm25_weight = params.get("bm25_weight", 0.3)

        # 并行执行两种搜索
        import asyncio
        vec_task = asyncio.create_task(self._vector_search(query, user_id, top_k))
        fts_task = asyncio.create_task(self._fts_search(query, user_id, top_k))
        vec_results, fts_results = await asyncio.gather(vec_task, fts_task)

        fused = self._rrf_fuse(vec_results, fts_results, vector_weight, bm25_weight)
        return ToolResult(success=True, data=fused,
                          metadata={"tool": "hybrid_search", "count": len(fused)})


class RerankTool:
    """对已有搜索结果进行重排序。"""
    name = "rerank"
    description = "对搜索结果重新排序，提升最相关结果到顶部。适用于已有候选结果但需要精细排序的场景。"

    async def execute(self, params: dict) -> ToolResult:
        results = await self._rerank_api(params["query"], params["documents"],
                                          params.get("top_n", 10))
        return ToolResult(success=bool(results), data=results)


class QueryExpandTool:
    """用 LLM 扩展查询，生成同义表述。"""
    name = "query_expand"
    description = "将查询扩展为多个同义表述，用于提高召回率。适用于搜索结果不足时。"

    async def execute(self, params: dict) -> ToolResult:
        expansions = await self._llm_expand(params["query"])
        return ToolResult(success=True, data={"original": params["query"],
                                               "expansions": expansions})
```

### 2.3 ReAct Agent Loop

```python
# app/agent/react.py — 核心推理循环

import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 4  # 防止无限循环


@dataclass
class AgentStep:
    thought: str          # Agent 的推理过程
    tool_name: str        # 选择的工具
    tool_params: dict     # 工具参数
    observation: dict     # 工具返回结果
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentTrace:
    """完整的 Agent 推理轨迹，用于调试和评估。"""
    query: str
    steps: list[AgentStep] = field(default_factory=list)
    final_context: list[dict] = field(default_factory=list)
    total_tool_calls: int = 0
    total_latency_ms: float = 0


class RAGAgent:
    def __init__(self, tools: dict[str, RAGTool], memory: 'UserMemory | None' = None):
        self.tools = tools
        self.memory = memory

    async def run(self, query: str, user_id: str,
                  conversation_id: str | None = None) -> AgentTrace:
        trace = AgentTrace(query=query)

        # Phase 1: Plan（参考 Claude Code 的 EnterPlanMode）
        plan = await self._plan(query, user_id)

        # Phase 2: Execute ReAct loop
        context_chunks = []
        for iteration in range(MAX_ITERATIONS):
            # Agent 决定下一步动作（通过 LLM reasoning）
            action = await self._decide_action(query, context_chunks, plan, iteration)

            if action["type"] == "generate":
                # Agent 认为上下文已足够，进入生成阶段
                break

            if action["type"] == "give_up":
                # Agent 判断无法找到相关信息
                break

            # 执行工具调用
            tool = self.tools.get(action["tool"])
            if not tool:
                continue

            tool_params = {**action["params"], "user_id": user_id}
            result = await tool.execute(tool_params)

            step = AgentStep(
                thought=action.get("thought", ""),
                tool_name=action["tool"],
                tool_params=tool_params,
                observation={"success": result.success, "count": len(result.data) if isinstance(result.data, list) else 1},
            )
            trace.steps.append(step)
            trace.total_tool_calls += 1

            if result.success:
                new_chunks = result.data if isinstance(result.data, list) else [result.data]
                context_chunks.extend(new_chunks)

                # 去重
                seen = set()
                deduped = []
                for c in context_chunks:
                    cid = c.get("chunk_id", id(c))
                    if cid not in seen:
                        seen.add(cid)
                        deduped.append(c)
                context_chunks = deduped

                # 自纠检查：结果数量不足时触发扩展
                if len(context_chunks) < 3 and iteration < MAX_ITERATIONS - 1:
                    plan["need_expand"] = True

        trace.final_context = context_chunks
        return trace

    async def _plan(self, query: str, user_id: str) -> dict:
        """查询规划（参考 Claude Code 的 Planning Mode）。

        不使用 LLM，纯规则 + 历史模式匹配。
        复杂度低于 Claude Code 的 plan mode，但解决 80% 的问题。
        """
        # 复用现有 QueryAnalyzer 的分类能力
        analyzed = _analyzer.analyze(query)

        plan = {
            "query_type": analyzed.query_type,
            "rewritten": analyzed.rewritten,
            "sub_queries": analyzed.sub_queries,
            "need_expand": False,
            "max_tools": 2,  # 简单查询最多用 2 个工具
        }

        # 复杂查询给更多工具预算
        if analyzed.query_type in ("compare", "multi_hop"):
            plan["max_tools"] = len(analyzed.sub_queries) * 2
            plan["parallel_sub_queries"] = True  # 标记可并行

        # 查询记忆：用户是否有相关偏好
        if self.memory:
            prefs = self.memory.get_preferences()
            if prefs.get("prefer_detailed"):
                plan["top_k"] = 40
            if prefs.get("prefer_precise"):
                plan["top_k"] = 10

        return plan

    async def _decide_action(self, query: str, context: list,
                             plan: dict, iteration: int) -> dict:
        """Agent 决策：下一步做什么。

        优先用规则（快速、免费），LLM 仅作为 fallback（复杂场景）。
        这是 Claude Code 模式的简化版：大部分操作用规则，
        只有规则覆盖不了的场景才调 LLM。
        """
        # Rule-based decision tree
        if iteration == 0:
            # 第一轮：根据查询类型选择工具
            if plan["query_type"] == "keyword":
                return {"type": "tool_call", "tool": "fulltext_search",
                        "params": {"query": plan["rewritten"], "top_k": 20},
                        "thought": "精确关键词查询，使用全文检索"}
            elif plan["query_type"] in ("compare", "multi_hop"):
                return {"type": "tool_call", "tool": "hybrid_search",
                        "params": {"query": plan["rewritten"],
                                   "vector_weight": 0.6, "bm25_weight": 0.4},
                        "thought": "复杂查询，使用混合检索并扩大召回"}
            else:
                return {"type": "tool_call", "tool": "hybrid_search",
                        "params": {"query": plan["rewritten"]},
                        "thought": "语义查询，使用混合检索"}

        if iteration == 1 and len(context) < 3:
            # 第二轮：结果不足，尝试扩展查询
            if plan.get("need_expand"):
                return {"type": "tool_call", "tool": "query_expand",
                        "params": {"query": plan["rewritten"]},
                        "thought": f"仅获得 {len(context)} 条结果，尝试扩展查询"}

        if iteration == 1 and len(context) >= 3:
            # 结果足够，执行 rerank
            return {"type": "tool_call", "tool": "rerank",
                    "params": {"query": plan["rewritten"],
                               "documents": context, "top_n": 10},
                    "thought": f"获得 {len(context)} 条候选，执行重排序"}

        if iteration >= 2 and len(context) >= 2:
            # 有足够上下文了，生成回答
            return {"type": "generate",
                    "thought": f"已有 {len(context)} 条相关结果，开始生成"}

        if iteration >= MAX_ITERATIONS - 1:
            return {"type": "generate",
                    "thought": "达到最大迭代次数，使用已有结果"}

        # Fallback：如果规则无法决策，使用 LLM 决策
        return {"type": "generate", "thought": "规则决策未命中，直接生成"}

    async def _llm_decide(self, query: str, context: list,
                          plan: dict, iteration: int) -> dict:
        """LLM-based decision for complex scenarios.

        仅在规则无法覆盖时调用。成本约 ¥0.001/次。
        """
        # 构造简短的决策 prompt（max 200 tokens）
        prompt = f"""基于用户查询和已获取的搜索结果，决定下一步动作。

查询: {query}
已有结果数: {len(context)}
可用工具: {', '.join(self.tools.keys())}

选项:
1. generate - 结果足够，开始回答
2. hybrid_search - 换关键词重新搜索
3. query_expand - 扩展查询提高召回
4. rerank - 对结果重排序

只输出选项编号和理由，一行。"""
        # ... 调用 LLM，解析返回
```

### 2.4 与 chat.py 的集成

```python
# app/api/chat.py — 改造后的入口

@router.post("")
async def chat(req: ChatRequest, user: User = Depends(get_current_user),
               db: AsyncSession = Depends(get_db)):
    # 初始化 Agent（工具 + 记忆）
    tools = {
        "vector_search": VectorSearchTool(),
        "fulltext_search": FullTextSearchTool(),
        "hybrid_search": HybridSearchTool(),
        "rerank": RerankTool(),
        "query_expand": QueryExpandTool(),
    }
    user_memory = UserMemory(str(user.id))
    agent = RAGAgent(tools=tools, memory=user_memory)

    async def event_stream():
        # Phase 1: Agent 推理 + 工具调用
        yield f"data: {json.dumps({'type': 'status', 'message': 'Thinking...'})}\n\n"

        trace = await agent.run(
            query=req.query,
            user_id=str(user.id),
            conversation_id=str(req.conversation_id) if req.conversation_id else None,
        )

        # 向前端推送 Agent 轨迹（可折叠显示）
        for step in trace.steps:
            yield f"data: {json.dumps({'type': 'agent_step', 'tool': step.tool_name,
                                       'thought': step.thought})}\n\n"

        # Phase 2: Context Compression（见 §3）
        compressed = SmartContextManager().compress(
            trace.final_context, query=req.query, max_tokens=8000
        )

        citations = [...]
        yield f"data: {json.dumps({'type': 'citations', 'data': citations})}\n\n"

        # Phase 3: LLM 生成（不变）
        context_text = SmartContextManager().format(compressed)
        async for token in llm_svc.stream_generate(req.query, context_text, history=chat_history):
            yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

        # Phase 4: 记忆更新（见 §4）
        await user_memory.record_interaction(
            query=req.query, results=trace.final_context,
            agent_trace=trace, feedback=None
        )

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### 2.5 前端展示 Agent 轨迹

```
┌─────────────────────────────────────────┐
│  用户: 对比 Transformer 和 RNN 的区别     │
├─────────────────────────────────────────┤
│  🤔 正在分析查询... 复杂对比查询            │
│  🔍 混合检索 "Transformer vs RNN" → 38 条  │
│  📊 重排序 → 10 条高质量结果              │
│  ─────────────────────────────────────  │
│  Transformer 和 RNN 的主要区别如下：       │
│  ...                                    │
│  [1] [2] [3] ...                        │
└─────────────────────────────────────────┘
```

---

## 3. Smart Context Manager（参考 Claude Code 上下文管理）

### 3.1 当前问题

```python
# 当前 build_context：按 char 数截断，无优先级
def build_context(self, results, max_tokens=8000):
    context_parts = []
    used_tokens = 0
    for i, r in enumerate(results):
        content = r.get("parent_content") or r["content"]
        approx_tokens = len(content)
        if used_tokens + approx_tokens > max_tokens:
            break  # ← 截断！后面的高分结果被丢弃
        context_parts.append(f"[{i + 1}] {content}")
```

问题：rerank 后的高分结果排在前面（已被排序），但如果第 1 条结果有 6000 char，后面 9 条全部被截断。

### 3.2 改造：优先级队列 + 压缩

```python
# app/agent/context.py

class SmartContextManager:
    def compress(self, results: list[dict], query: str,
                 max_tokens: int = 8000) -> list[dict]:
        if not results:
            return []

        # Step 1: 为每个结果分配预算（按分数加权）
        total_score = sum(r.get("score", 0) for r in results)
        for r in results:
            weight = r.get("score", 0) / total_score if total_score > 0 else 1 / len(results)
            r["_token_budget"] = int(max_tokens * weight)

        # Step 2: 对超预算的内容进行压缩
        compressed = []
        for r in results:
            content = r.get("parent_content") or r["content"]
            budget = r["_token_budget"]

            if len(content) <= budget:
                compressed.append(r)
            else:
                # 压缩策略：保留开头 + 结尾 + 包含查询关键词的句子
                shortened = self._smart_truncate(content, query, budget)
                r = {**r, "content": shortened}
                compressed.append(r)

        return compressed

    def _smart_truncate(self, content: str, query: str, budget: int) -> str:
        """智能截断：保留与查询最相关的句子。"""
        import re
        # 按句号/换行分句
        sentences = re.split(r'[。\n！？；]', content)

        # 优先级：包含查询关键词的句子 > 首句 > 尾句 > 中间
        keywords = set(query)
        scored_sentences = []
        for s in sentences:
            score = 0
            if any(k in s for k in keywords):
                score += 10
            scored_sentences.append((score, s))

        # 首句和尾句加权
        if scored_sentences:
            scored_sentences[0] = (scored_sentences[0][0] + 5, scored_sentences[0][1])
            scored_sentences[-1] = (scored_sentences[-1][0] + 3, scored_sentences[-1][1])

        # 按优先级选择句子直到预算用完
        scored_sentences.sort(key=lambda x: x[0], reverse=True)
        selected = []
        used = 0
        for score, s in scored_sentences:
            if used + len(s) > budget:
                continue
            selected.append(s)
            used += len(s)

        # 按原文顺序排列
        original_order = {s: i for i, (_, s) in enumerate(
            sorted(enumerate(sentences), key=lambda x: x[1][0], reverse=True)
        )}
        selected.sort(key=lambda s: sentences.index(s) if s in sentences else 999)

        return "...".join(selected)

    def format(self, results: list[dict]) -> str:
        """格式化为 LLM 可用的上下文文本。"""
        parts = []
        for i, r in enumerate(results):
            content = r.get("content", "")
            source = r.get("document_id", "unknown")
            page = r.get("page_number")
            page_info = f" (第{page}页)" if page else ""
            parts.append(f"[{i + 1}]{page_info} {content}")
        return "\n\n".join(parts)
```

### 3.3 效果对比

| 场景 | 当前 build_context | Smart Context Manager |
|------|-------------------|----------------------|
| 10 条结果，第 1 条 6000 char | 只有 1 条完整内容 | 10 条都有，第 1 条压缩到 2400 char |
| 5 条结果，每条 1000 char | 5 条全部展示 | 5 条全部展示（无变化） |
| 20 条结果，平均 800 char | 只展示前 10 条 | 按分数加权分配预算，高分完整展示，低分压缩 |

---

## 4. User Knowledge Memory（参考 Claude Code Memory System）

### 4.1 Claude Code 的记忆架构

```
MEMORY.md          ← 索引（每行一条，自动加载到上下文）
user.md            ← 用户角色/偏好
feedback.md        ← 行为指导（该做/不该做）
project.md         ← 项目上下文
reference.md       ← 外部系统引用
```

### 4.2 knSpace 的用户记忆

```python
# app/agent/memory.py — 用户知识记忆

import json
from datetime import datetime, timedelta
from dataclasses import dataclass, field, asdict


@dataclass
class UserPreferences:
    """用户偏好（从交互中学习）"""
    prefer_detailed: bool = False       # 偏好详细回答
    prefer_precise: bool = True         # 偏好精确引用
    preferred_collections: list[str] = field(default_factory=list)
    common_topics: list[str] = field(default_factory=list)
    avg_query_length: float = 0
    feedback_positive: int = 0
    feedback_negative: int = 0


@dataclass
class SessionMemory:
    """会话工作记忆"""
    conversation_id: str
    retrieved_doc_ids: set = field(default_factory=set)
    cited_chunks: list[str] = field(default_factory=list)
    agent_traces: list[dict] = field(default_factory=list)


class UserMemory:
    """用户知识记忆系统（参考 Claude Code 的分层记忆）"""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._prefs: UserPreferences | None = None
        self._sessions: dict[str, SessionMemory] = {}

    async def get_preferences(self) -> UserPreferences:
        """加载用户偏好（长期记忆）"""
        if self._prefs:
            return self._prefs

        # 从 Redis 加载（TTL 1 小时）
        cached = await self._load_from_redis(f"memory:prefs:{self.user_id}")
        if cached:
            self._prefs = UserPreferences(**json.loads(cached))
        else:
            self._prefs = UserPreferences()
        return self._prefs

    async def record_interaction(self, query: str, results: list[dict],
                                  agent_trace, feedback: str | None = None):
        """记录一次交互，更新偏好（反馈记忆）"""
        prefs = await self.get_preferences()

        # 更新查询长度平均值
        prefs.avg_query_length = (prefs.avg_query_length * 0.9 +
                                   len(query) * 0.1)

        # 从查询提取话题
        if len(query) > 10:
            topics = self._extract_topics(query)
            for t in topics:
                if t not in prefs.common_topics:
                    prefs.common_topics.append(t)
            prefs.common_topics = prefs.common_topics[-20:]  # 保留最近 20 个

        # 记录反馈
        if feedback == "positive":
            prefs.feedback_positive += 1
            prefs.prefer_detailed = prefs.avg_query_length > 30
        elif feedback == "negative":
            prefs.feedback_negative += 1

        # 持久化到 Redis
        await self._save_to_redis(
            f"memory:prefs:{self.user_id}",
            json.dumps(asdict(prefs)),
            ttl=3600
        )

        # 持久化到 PostgreSQL（异步，不阻塞响应）
        await self._persist_to_db(prefs)

    async def get_session(self, conversation_id: str) -> SessionMemory:
        """获取会话工作记忆"""
        if conversation_id not in self._sessions:
            self._sessions[conversation_id] = SessionMemory(
                conversation_id=conversation_id
            )
        return self._sessions[conversation_id]

    def _extract_topics(self, query: str) -> list[str]:
        """从查询中提取关键词作为话题标签。"""
        import re
        # 简单的中文关键词提取（不需要 NLP 库）
        words = re.findall(r'[一-鿿]{2,6}', query)
        return words[:3]

    async def _load_from_redis(self, key: str) -> str | None:
        from app.services.cache import redis
        if redis:
            return await redis.get(key)
        return None

    async def _save_to_redis(self, key: str, value: str, ttl: int = 3600):
        from app.services.cache import redis
        if redis:
            await redis.set(key, value, ex=ttl)

    async def _persist_to_db(self, prefs: UserPreferences):
        """异步持久化到 PostgreSQL（用户记忆表）"""
        # 非阻塞 — 通过 Celery 异步任务
        from app.tasks.celery_app import celery_app
        celery_app.send_task(
            "persist_user_memory",
            args=[self.user_id, asdict(prefs)]
        )
```

### 4.3 数据库支持

```sql
-- 用户记忆表（新增）
CREATE TABLE user_memories (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id),
    memory_type VARCHAR(20) NOT NULL,  -- 'preference' | 'feedback' | 'topic'
    key        VARCHAR(100),
    value      JSONB NOT NULL,
    source     VARCHAR(50),  -- 'interaction' | 'feedback' | 'explicit'
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, memory_type, key)
);

CREATE INDEX idx_user_memories_user ON user_memories(user_id);
```

### 4.4 记忆如何影响检索

```
用户偏好记忆                    检索策略调整
─────────────                  ────────────
prefer_detailed=True           top_k=40, 保留更多上下文
prefer_precise=True            top_k=10, rerank 更激进
common_topics=["LLM","Agent"]  查询扩展时优先使用这些领域词
feedback_positive > 10         当前策略有效，保持
feedback_negative > 3          自动降低 rerank_top_k 提升精度
preferred_collections=[id1]    优先在这些收藏夹中搜索
```

---

## 5. Processing Pipeline Hooks（参考 Claude Code Hook System）

### 5.1 Claude Code 的 Hook 模式

```json
// Claude Code 的 hooks 配置
{
  "hooks": {
    "PostToolUse": [{"command": "formatter --fix"}],
    "PreCommit": [{"command": "lint-check"}]
  }
}
```

### 5.2 knSpace 文档处理 Hook

```python
# app/agent/hooks.py — 处理管线钩子系统

from typing import Callable, Protocol
from dataclasses import dataclass


@dataclass
class HookContext:
    """Hook 上下文，携带处理状态"""
    document_id: str
    user_id: str
    stage: str          # "pre_parse" | "post_parse" | "post_chunk" | "post_embed" | "on_error"
    data: dict          # 当前阶段的数据
    errors: list[str]   # 累积的错误


HookFunction = Callable[[HookContext], HookContext]


class ProcessingHooks:
    """文档处理管线钩子注册表"""

    def __init__(self):
        self._hooks: dict[str, list[HookFunction]] = {}

    def register(self, stage: str, hook: HookFunction):
        if stage not in self._hooks:
            self._hooks[stage] = []
        self._hooks[stage].append(hook)

    async def fire(self, ctx: HookContext) -> HookContext:
        for hook in self._hooks.get(ctx.stage, []):
            ctx = await hook(ctx)
            if ctx.errors and ctx.stage == "on_error":
                break  # 错误处理钩子执行后停止
        return ctx


# ── 内置 Hooks ──────────────────────────────────────────────

async def partial_success_hook(ctx: HookContext) -> HookContext:
    """on_error hook: OCR 失败时保留已成功的部分。"""
    if ctx.stage == "on_error" and "ocr" in str(ctx.errors):
        # 保留已成功解析的页面，跳过失败页面
        successful_pages = ctx.data.get("parsed_pages", [])
        if successful_pages:
            ctx.data["use_partial"] = True
            ctx.data["content"] = "\n".join(successful_pages)
            ctx.errors = []  # 清除错误，允许继续
    return ctx


async def dedup_hook(ctx: HookContext) -> HookContext:
    """post_parse hook: 跨用户内容去重检查。"""
    content_hash = ctx.data.get("content_hash")
    if content_hash:
        # 检查是否已有相同内容的文档
        existing = await check_content_hash(content_hash)
        if existing:
            ctx.data["dedup_of"] = existing
            # 可以复用已有的 chunks/embeddings，省去重复处理
    return ctx


async def quality_check_hook(ctx: HookContext) -> HookContext:
    """post_chunk hook: 检查分块质量。"""
    chunks = ctx.data.get("chunks", [])
    low_quality = [c for c in chunks if len(c["content"]) < 20]

    if len(low_quality) > len(chunks) * 0.5:
        # 超过一半的分块太短，可能解析有问题
        ctx.errors.append(f"分块质量过低: {len(low_quality)}/{len(chunks)} 个分块少于 20 字符")

    return ctx
```

### 5.3 改造 doc_processor.py

```python
# app/services/doc_processor.py — 集成 Hook 的文档处理

async def process_document(doc_id: str, user_id: str):
    hooks = ProcessingHooks()
    hooks.register("on_error", partial_success_hook)
    hooks.register("post_parse", dedup_hook)
    hooks.register("post_chunk", quality_check_hook)

    # ... (获取 doc 对象)

    # Parse — 支持 partial success
    ctx = HookContext(document_id=doc_id, user_id=user_id, stage="pre_parse", data={}, errors=[])
    try:
        parsed = parse_file(doc.file_path, doc.mime_type)
        ctx.data["parsed_pages"] = parsed.pages
        ctx.stage = "post_parse"
        ctx = await hooks.fire(ctx)
    except Exception as e:
        ctx.errors.append(str(e))
        ctx.stage = "on_error"
        ctx = await hooks.fire(ctx)
        if not ctx.data.get("use_partial"):
            doc.processing_status = "failed"
            doc.processing_error = str(e)
            await db.commit()
            return

    # Chunk
    ctx.stage = "post_chunk"
    ctx.data["chunks"] = chunk_results
    ctx = await hooks.fire(ctx)

    # ... (embed + index 后续步骤不变)
```

---

## 6. CTO 评审必修项修复

### 6.1 中文 FTS 修复

```python
# 方案 A：使用 zhparser 扩展（推荐，服务器有 root 权限）
# 安装: apt install postgresql-16-zhparser
# SQL:
# CREATE TEXT SEARCH CONFIGURATION zh (PARSER = zhparser);
# ALTER TEXT SEARCH CONFIGURATION zh ADD MAPPING FOR n,v,a,i,e,l WITH simple;

# chunks 表改为：
# fts TSVECTOR GENERATED ALWAYS AS (to_tsvector('zh', content)) STORED

# 方案 B：纯 Python 分词（无 root 权限时）
# 使用 jieba 分词后在应用层构建 tsvector

async def _bm25_search(self, query: str, user_id: str, top_k: int):
    # 方案 B 的搜索实现
    import jieba
    tokens = " ".join(jieba.cut_for_search(query))
    sql = text("""
        SELECT c.id::text as chunk_id,
               c.document_id::text as document_id,
               ts_rank_cd(c.fts_vector, to_tsquery('simple', :tokens)) as score
        FROM chunks c
        WHERE c.user_id::text = :user_id
          AND c.fts_vector @@ to_tsquery('simple', :tokens)
        ORDER BY score DESC
        LIMIT :limit
    """)
```

### 6.2 缺失索引

```sql
CREATE INDEX idx_documents_user_active ON documents(user_id) WHERE is_deleted = FALSE;
CREATE INDEX idx_documents_collection ON documents(collection_id) WHERE is_deleted = FALSE;
CREATE INDEX idx_conversations_user_active ON conversations(user_id) WHERE is_deleted = FALSE;
CREATE INDEX idx_messages_conversation ON messages(conversation_id);
CREATE INDEX idx_chunks_document_user ON chunks(document_id, user_id);
```

### 6.3 迁移路径修正

将原设计中"改配置"级别的迁移改为诚实的复杂度标注：

| 组件 | 迁移复杂度 | 实际工作量 |
|------|-----------|-----------|
| AI 服务（Embed/Rerank/OCR/LLM） | **低** — 改 URL | 0.5 天 |
| Redis 单机 → Cluster | **低** — 改连接串 | 0.5 天 |
| Milvus Lite → Cluster | **中** — 需要 bulk export + import | 2-3 天 |
| PG FTS → Elasticsearch | **中** — 需要重建索引 + 调分词 | 2-3 天 |
| Celery → Kafka | **中** — 任务模型差异大 | 3-5 天 |
| 单 PG → Citus 分片 | **高** — 需要分片键设计 + co-location + 跨分片查询优化 | 5-10 天 |
| 本地 FS → S3 | **低** — 已有 ObjectStorageBase 抽象 | 1 天 |

---

## 7. Agent 面试亮点总结

### 7.1 原方案 vs 增强方案

| 维度 | 原方案（后端工程师） | 增强方案（Agent 工程师） |
|------|-------------------|----------------------|
| 查询处理 | 固定管线，5 步串行 | ReAct Agent，自适应迭代 |
| LLM 角色 | 文本生成器 | 决策者 + 生成器（可选 Tool Use） |
| 上下文管理 | 按 char 数截断 | 分数加权预算分配 + 智能截断 |
| 记忆 | 仅对话历史 | 偏好记忆 + 会话记忆 + 反馈记忆 |
| 文档处理 | 线性管线，失败即弃 | Hook 系统，partial success |
| 可观测性 | Prometheus 指标 | Agent Trace 完整推理轨迹 |
| 自适应 | 无 | 根据查询类型调整策略，根据反馈学习 |

### 7.2 面试可讲的 Agent 设计点

1. **Tool 抽象设计**：每个检索能力封装为 Tool，Agent 自主选择组合。这与 Claude Code 的 Read/Edit/Bash 设计一脉相承。

2. **ReAct 循环**：不是一次性搜索，而是 observe → think → act → observe 的迭代循环。搜索结果不足时会自动扩展查询重试。

3. **规则优先 + LLM fallback**：大部分决策用规则（快、免费、确定性），规则覆盖不了才调 LLM。这是工程成熟度的体现。

4. **Hook 系统**：借鉴 Claude Code 的 hook 设计，文档处理管线可插拔扩展，支持 partial success。

5. **分层记忆**：短期（对话）、工作（会话检索）、长期（偏好）、反馈（正/负样本）。与 Claude Code 的 MEMORY.md 体系对应。

6. **Agent Trace**：完整记录 Agent 的推理过程，可用于调试、评估、展示。面试时可以现场 demo。

---

## 8. 实施优先级

| 优先级 | 模块 | 预计工作量 | 面试价值 |
|--------|------|-----------|---------|
| P0 | Tool System + ReAct Agent | 3 天 | ★★★★★ |
| P0 | 中文 FTS 修复 | 0.5 天 | ★★★ (必修) |
| P0 | 缺失索引 | 0.5 天 | ★★ (必修) |
| P1 | Smart Context Manager | 1 天 | ★★★★ |
| P1 | Agent Trace 可观测性 | 1 天 | ★★★★ |
| P2 | User Knowledge Memory | 2 天 | ★★★ |
| P2 | Processing Hooks | 1 天 | ★★★ |
| P3 | LLM Tool Use（让 LLM 调工具） | 3 天 | ★★★★★ (但有 API 成本) |

**建议实施顺序：** P0 → P1 → P2 → P3，先让 Agent 跑起来，再逐步加记忆和 Tool Use。
