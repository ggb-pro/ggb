# knSpace 深度优化方案（v2.1）

> 来源：Agent 工程岗面试 4 大 Case 深度追问 + 工业界对标分析
> 日期：2026-06-04
> 基于现有 v2.0 代码，按 P0→P3 优先级排列，标注工期和依赖关系

---

## 优先级定义

- **P0**：数据正确性 / 数据完整性受损，必须立即修复
- **P1**：Agent 路径核心功能缺陷，影响回答质量
- **P2**：可观测性 / 评估体系缺失，无法度量就无法优化
- **P3**：体验优化和前瞻性改进，非紧急

---

## P0 — 数据完整性（2 项）

### C3-1. 跨用户 chunk 克隆缺少 Milvus/ES 写入 + 无原子性保障

**来源 Case：** Case 3 — 用户 B 上传相同文件后搜索不到任何内容

**当前代码问题（`documents.py:313-354`）：**

| 步骤 | 当前状态 | 问题 |
|------|---------|------|
| PG chunks 克隆 | ✅ 写入 | parent_chunk_id 未重映射（`func.replace` 三参数相同，是死代码） |
| Milvus 向量 | ❌ 未写入 | 克隆流程完全没有 Milvus 操作 |
| ES 索引 | ❌ 未写入 | 克隆流程完全没有 ES 操作 |
| 文档状态 | 直接标 ready | 无论克隆是否成功都标 ready |

**修复方案：**

```python
# app/api/documents.py — _clone_chunks_from_existing 重写
async def _clone_chunks_from_existing(db, source_doc_id, target_doc_id, target_user_id):
    """原子性克隆：PG → Milvus → ES，任何一步失败回滚状态。"""
    from app.services.vector_store import get_vector_store
    from app.services.es import bulk_index_chunks

    # 1. 克隆 PG chunks + 构建 old_to_new 映射
    result = await db.execute(select(Chunk).where(Chunk.document_id == source_doc_id))
    source_chunks = result.scalars().all()
    if not source_chunks:
        return

    old_to_new = {}
    chunk_data = []  # [(new_id, content), ...] 供后续 Milvus/ES 使用
    for chunk in source_chunks:
        new_id = str(uuid.uuid4())
        old_to_new[str(chunk.id)] = new_id
        db.add(Chunk(
            id=new_id, document_id=target_doc_id, user_id=target_user_id,
            content=chunk.content, chunk_index=chunk.chunk_index,
            chunk_type=chunk.chunk_type, parent_chunk_id=chunk.parent_chunk_id,
            char_start=chunk.char_start, char_end=chunk.char_end,
            page_number=chunk.page_number, token_count=chunk.token_count,
        ))
        chunk_data.append((new_id, chunk.content))
    await db.commit()

    # 2. 重映射 parent_chunk_id（第二次遍历）
    cloned = await db.execute(select(Chunk).where(Chunk.document_id == target_doc_id))
    for chunk in cloned.scalars().all():
        old_parent = str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None
        if old_parent and old_parent in old_to_new:
            chunk.parent_chunk_id = old_to_new[old_parent]
    await db.commit()

    # 3. Milvus：从源文档读取向量，用新 ID 写入
    store = get_vector_store()
    source_ids = list(old_to_new.keys())
    # vector_store 需新增 get_vectors_by_ids 方法（见 C3-2）
    source_vectors = store.get_vectors_by_ids(source_ids)
    new_ids = list(old_to_new.values())
    texts = [c[1] for c in chunk_data]
    store.insert(new_ids, target_user_id, target_doc_id, source_vectors, texts)

    # 4. ES：批量索引
    es_chunks = [
        {"chunk_id": new_id, "document_id": target_doc_id,
         "user_id": target_user_id, "content": content}
        for new_id, content in chunk_data
    ]
    bulk_index_chunks(es_chunks)
```

**upload 函数同步改造（`documents.py:79-88`）：**

```python
if existing:
    doc.processing_status = "cloning"
    await db.commit()
    try:
        await _clone_chunks_from_existing(db, str(existing.id), doc_id, str(user.id))
        doc.processing_status = "ready"
    except Exception as e:
        logger.error(f"Clone failed: {e}")
        doc.processing_status = "failed"
        doc.processing_error = f"克隆失败: {str(e)[:500]}"
    doc.chunk_count = len(source_chunks) if doc.processing_status == "ready" else 0
    await db.commit()
```

**工期：** 2 天
**新增依赖：** `vector_store.py` 需新增 `get_vectors_by_ids()` 方法

---

### C3-2. 删除文档时级联安全性验证

**来源 Case：** Case 3 — 用户 A 删除原文档后用户 B 的数据是否受影响

**当前代码（`documents.py:156-205`）：** 删除按 `document_id` 隔离，PG 外键 CASCADE 只影响自己文档的 chunks，Milvus/ES 也按 `document_id` 过滤。

**结论：** 当前删除逻辑是安全的，不会级联影响其他用户。但前提是 C3-1 修复后克隆流程正确写入了 `document_id`。

**额外保障：** 删除前校验是否有其他文档依赖同一 content_hash：

```python
# 删除前检查：如果有其他用户的文档引用同一 content_hash，只删当前用户数据
siblings = await db.execute(
    select(func.count()).select_from(Document).where(
        Document.content_hash == doc.content_hash,
        Document.id != doc_id,
        Document.is_deleted == False,
    )
)
sibling_count = siblings.scalar()
# sibling_count > 0 说明还有其他用户依赖，安全删除（只删当前用户的 PG/Milvus/ES 数据）
# sibling_count == 0 说明这是最后一个引用，物理文件也可清理
```

**工期：** 0.5 天（当前已安全，加校验即可）

---

## P1 — Agent 路径核心功能（4 项）

### C1-1. 多跳查询拆分改为 LLM 生成子问题 DAG

**来源 Case：** Case 1 — "对比 glm-4.5-air 和 glm-5.1-openai 的部署方式" 被错误分类为 compare（应为 multi_hop），且按"和"暴力拆分

**当前代码问题（`query_analyzer.py:77-87`）：**

1. `_COMPARE_PATTERNS` 优先级高于 `_MULTI_ENTITY_PATTERNS`，导致含"区别"的多实体查询被误分
2. `_decompose_compare` 按连接词 `split`，不理解实体边界
3. 子查询独立执行，无依赖关系，无增量上下文

**修复方案：**

```python
# app/agent/nodes.py — generate_plan 节点改造
async def generate_plan(state: AgentState) -> dict:
    query = state["query"]

    prompt = f"""你是一个 RAG 检索规划器。分析用户查询，生成检索计划。

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
- 每个子查询必须独立可理解
- 多实体查询：分别检索每个实体
- 推理查询：前面的结果可能影响后续检索策略
- 有依赖的子查询在 args 中可引用前置结果
"""

    raw = await _call_lightweight_llm(prompt, max_tokens=500)
    parsed = _extract_json(raw)

    if parsed and isinstance(parsed, dict) and "sub_queries" in parsed:
        plan = []
        for sq in parsed["sub_queries"]:
            tool = sq.get("tool", "hybrid_search")
            if tool not in {"hybrid_search", "fulltext_search"}:
                tool = "hybrid_search"
            plan.append({
                "id": sq.get("id", f"q{len(plan)+1}"),
                "tool": tool,
                "args": {
                    "query": sq.get("query", query),
                    "top_k": min(sq.get("args", {}).get("top_k", 40), 80),  # 参数校验
                    **sq.get("args", {}),
                },
                "depends_on": sq.get("depends_on", []),
            })
        if plan:
            return {"plan": plan, "retry_count": 0}

    # Fallback：单步检索
    return {"plan": [{"tool": "hybrid_search", "args": {"query": query}}], "retry_count": 0}
```

**execute_tools 改造 — 按依赖顺序执行 + 增量上下文：**

```python
async def execute_tools(state: AgentState) -> dict:
    plan = state.get("plan", [])
    user_id = state["user_id"]

    # 拓扑排序：按 depends_on 确定执行顺序
    executed = {}  # id → results
    all_chunks = []
    tools_called = []

    for step in plan:
        step_id = step.get("id", "")
        # 如果有依赖未完成，跳过（理论上拓扑排序后不会出现）
        deps = step.get("depends_on", [])
        dep_context = ""
        for dep_id in deps:
            if dep_id in executed:
                dep_context += f"前置检索[{dep_id}]结果摘要: {executed[dep_id][:200]}\n"

        args = dict(step.get("args", {}))
        args["user_id"] = user_id
        # 参数校验
        args["top_k"] = min(args.get("top_k", 40), 100)
        if not args.get("query", "").strip():
            args["query"] = state["query"]

        # 如果有前置上下文，追加到 query 增强检索
        if dep_context:
            args["query"] = f"{args['query']} (参考: {dep_context[:100]})"

        func = tool_map.get(step.get("tool", ""))
        if not func:
            continue

        try:
            result = await func.ainvoke(args)
            if isinstance(result, list):
                all_chunks.extend(result)
                executed[step_id] = "; ".join(r.get("content", "")[:100] for r in result[:3])
            tools_called.append({"tool": step["tool"], "args": args,
                                 "result_count": len(result) if isinstance(result, list) else 0})
        except Exception as e:
            logger.warning(f"Tool {step['tool']} failed: {e}")
            tools_called.append({"tool": step["tool"], "args": args, "error": str(e)})

    # 去重
    seen = set()
    unique = [c for c in all_chunks if c["chunk_id"] not in seen and not seen.add(c["chunk_id"])]

    return {"chunks": unique, "tools_called": tools_called}
```

**同时修复分类优先级（`query_analyzer.py`）：**

```python
def _classify(self, query: str) -> str:
    if _EXACT_PATTERNS.search(query):
        return "keyword"
    # multi_hop 优先于 compare：含多实体+关系模式时不应只做对比
    if _MULTI_ENTITY_PATTERNS.search(query) and _COMPARE_PATTERNS.search(query):
        return "multi_hop"
    if _COMPARE_PATTERNS.search(query):
        return "compare"
    if _MULTI_ENTITY_PATTERNS.search(query):
        return "multi_hop"
    if _SEMANTIC_PATTERNS.search(query):
        return "semantic"
    return "semantic"
```

**工期：** 3 天（generate_plan 改造 1d + execute_tools 改造 1d + 分类优先级修复 + 测试 1d）

---

### C1-2. execute_tools 部分结果零感知 → 补检索 + 降级通知

**来源 Case：** Case 1 — 子查询返回 0 结果时静默跳过

**修复方案：** 在 execute_tools 中增加零结果检测和自动补检索：

```python
# execute_tools 循环内，result 为空时触发补检索
try:
    result = await func.ainvoke(args)
    if isinstance(result, list) and len(result) == 0:
        # 补检索：尝试 fulltext_search
        logger.info(f"hybrid_search returned 0 results for '{args['query']}', trying fulltext fallback")
        fallback_args = {"query": args["query"], "user_id": user_id, "top_k": 20}
        result = await fulltext_search.ainvoke(fallback_args)
        tools_called.append({
            "tool": "fulltext_search",
            "args": fallback_args,
            "result_count": len(result) if isinstance(result, list) else 0,
            "reason": "hybrid_search_zero_results_fallback",
        })
    if isinstance(result, list):
        all_chunks.extend(result)
except Exception as e:
    ...
```

**工期：** 1 天

---

### C2-1. 反思耗尽后的分级质量输出

**来源 Case：** Case 2 — 重试耗尽后只追加泛化警告，不区分失败维度

**当前代码问题（`router.py:341-344`）：**

```python
if reflection in ("skip", "parse_failed"):
    answer += "\n\n> [注：此回答的质量校验未完成，可能存在不足]"  # 泛化，无信息量
```

**修复方案：**

**Step 1：reflect 节点保留最后一次评分（`nodes.py:228-229`）：**

```python
# 当前：skip 时丢弃 scores
if not answer or retry_count >= settings.agent_max_retries:
    return {"should_retry": False, "reflection_result": "skip", "reflection_scores": {}}

# 改为：保留最后一次的评分和原因
if retry_count >= settings.agent_max_retries:
    return {"should_retry": False, "reflection_result": "max_retries_exhausted",
            "reflection_scores": last_scores}  # 保留最后评分
```

**Step 2：router.py 根据评分生成差异化警告：**

```python
# 替换固定文案
reflection = result_state.get("reflection_result", "")
scores = result_state.get("reflection_scores", {})

if reflection in ("max_retries_exhausted", "skip", "parse_failed"):
    warning = _build_quality_warning(reflection, scores, result_state.get("retry_count", 0))
    answer += f"\n\n> {warning}"

def _build_quality_warning(reflection: str, scores: dict, retry_count: int) -> str:
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
```

**工期：** 1 天

---

### C2-2. adjust_params 多策略扩展

**来源 Case：** Case 2 — groundedness 低时只降 vector_weight，对"知识不存在"无效

**当前策略（`nodes.py:273-307`）：**

| 最低分维度 | 当前策略 | 问题 |
|-----------|---------|------|
| groundedness | 降 vector_weight | 无法解决知识缺失问题 |
| relevance | 扩 top_k | 仅增加数量，不解决语义偏差 |
| consistency | 两者都调 | 策略模糊 |

**修复方案 — 新增三种策略：**

```python
def adjust_params(state: AgentState) -> dict:
    plan = state.get("plan", [])
    scores = state.get("reflection_scores", {})
    retry_count = state.get("retry_count", 0)

    if not scores:
        return {"plan": plan}

    relevance = scores.get("relevance", 5)
    groundedness = scores.get("groundedness", 5)
    consistency = scores.get("consistency", 5)
    min_score = min(relevance, groundedness, consistency)

    # 新增：根据重试轮次升级策略
    strategy_level = min(retry_count, 2)  # 0→保守, 1→中等, 2→激进

    new_plan = []
    for step in plan:
        args = dict(step.get("args", {}))

        if groundedness == min_score:
            # 策略 1: 事实性问题 → 提高 BM25 精确匹配权重
            #         激进模式：切换为 fulltext_search 纯全文检索
            if strategy_level >= 2:
                step = {"tool": "fulltext_search", "args": args}
                args["top_k"] = min(args.get("top_k", 40) + 20, 80)
            else:
                args["vector_weight"] = max(args.get("vector_weight", 0.7) - 0.2, 0.3)
                args["bm25_weight"] = 1.0 - args["vector_weight"]

        elif relevance == min_score:
            # 策略 2: 相关性不足 → 扩大范围 + 重新改写查询
            args["top_k"] = min(args.get("top_k", 40) + 20 * (strategy_level + 1), 100)
            # 激进模式：在 query 后追加原始问题重新检索
            if strategy_level >= 2 and state.get("original_query"):
                args["query"] = state["original_query"]

        else:
            # 策略 3: 一致性问题 → 调整两者
            args["top_k"] = min(args.get("top_k", 40) + 10, 80)
            args["vector_weight"] = max(args.get("vector_weight", 0.7) - 0.1, 0.3)
            args["bm25_weight"] = 1.0 - args["vector_weight"]

        new_plan.append({"tool": step["tool"], "args": args})

    return {"plan": new_plan}
```

**工期：** 1 天

---

## P2 — 可观测性与评估（3 项）

### C4-1. 反思评分导出 Prometheus + agent_trace 写入

**来源 Case：** Case 2 — 反思评分随请求结束丢弃，无法量化回答质量

**当前问题：**
- `reflection_scores` 停留在 state 内存中，请求结束即丢失
- `agent_trace` JSONB 列在 SQLAlchemy 模型中尚未添加
- 无法回答"Agent 路径的回答质量到底怎么样"

**修复方案：**

**Step 1：新增 Prometheus 指标（`metrics.py`）：**

```python
from prometheus_client import Histogram

reflection_scores_hist = Histogram(
    "agent_reflection_scores",
    "Reflection quality scores by dimension",
    ["dimension"],  # relevance / groundedness / consistency
    buckets=[1, 2, 3, 4, 5],
)
```

**Step 2：reflect 节点记录指标（`nodes.py` reflect 函数末尾）：**

```python
if scores:
    for dim, val in scores.items():
        reflection_scores_hist.labels(dimension=dim).observe(val)
```

**Step 3：添加 agent_trace 列（`models/message.py`）：**

```python
from sqlalchemy.dialects.postgresql import JSONB

agent_trace: Mapped[dict | None] = mapped_column(JSONB)
```

**Step 4：router.py 写入 trace（`result_state` 提取后）：**

```python
trace_data = {
    "intent": result_state.get("intent"),
    "plan_steps": len(result_state.get("plan", [])),
    "tools_called": result_state.get("tools_called", []),
    "reflection_result": result_state.get("reflection_result"),
    "reflection_scores": result_state.get("reflection_scores", {}),
    "retry_count": result_state.get("retry_count", 0),
    "chunk_count": len(result_state.get("chunks", [])),
}
await _save_assistant_msg(db, conversation, user, full_answer, citations, agent_trace=trace_data)
```

**工期：** 2 天（模型迁移 + 指标 + 写入逻辑）

---

### C4-2. RRF 权重 AB 测试框架

**来源 Case：** Case 4 — RRF_K=60 和权重比例未经自有数据验证

**修复方案：** 在 evaluator.py 中新增 RRF 参数扫描评估：

```python
async def evaluate_rrf_params(test_samples: list[dict]) -> dict:
    """对不同的 RRF_K 和权重组合评估检索质量。"""
    results = {}
    for k in [10, 30, 60, 100]:
        for vw, bw in [(0.3, 0.7), (0.5, 0.5), (0.7, 0.3)]:
            recalls = []
            for sample in test_samples:
                # 用指定参数执行检索
                fused = _rrf_fuse_with_k(vector_results, bm25_results, vw, bw, k=k)
                hit = sum(1 for cid in fused[:10] if cid in sample["relevant_ids"])
                recalls.append(hit / len(sample["relevant_ids"]))
            results[f"K={k}_vw={vw}"] = {
                "recall@5": sum(r[:5] for r in recalls) / len(recalls),
                "recall@10": sum(recalls) / len(recalls),
            }
    return results
```

**工期：** 2 天

---

### C4-3. ES 宕机 PG FTS 回退效果评估

**来源 Case：** Case 4 — 无量化数据，估计下降 30-50% 但未实测

**修复方案：** 在 evaluator.py 中新增回退模式评估：

```python
async def evaluate_fallback_fts(test_samples: list[dict]) -> dict:
    """对比 ES 和 PG FTS 的检索效果。"""
    es_scores = []
    pg_scores = []
    for sample in test_samples:
        es_results = es_search(sample["query"], sample["user_id"], top_k=10)
        pg_results = await pg_fts_search(sample["query"], sample["user_id"], top_k=10)
        es_scores.append(compute_recall(es_results, sample["relevant_ids"]))
        pg_scores.append(compute_recall(pg_results, sample["relevant_ids"]))
    return {
        "es_recall@10": sum(es_scores) / len(es_scores),
        "pg_recall@10": sum(pg_scores) / len(pg_scores),
        "degradation": f"{(1 - sum(pg_scores)/sum(es_scores))*100:.1f}%",
    }
```

**工期：** 1 天

---

## P3 — 体验优化与前瞻改进（3 项）

### C2-3. reflect 截断优化 — 按句子边界截断

**来源 Case：** Case 1 面试追问 — 300 字硬截断可能切断核心信息

**修复方案：**

```python
def _smart_truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    for sep in ["。", "？", "！", ".", "?", "!"]:
        last = truncated.rfind(sep)
        if last > limit * 0.6:
            return truncated[:last + 1]
    return truncated

# reflect 节点中替换 [:300]
chunk_excerpts = "\n".join(
    f"[{i + 1}] {_smart_truncate(c.get('content', ''), 300)}"
    for i, c in enumerate(chunks[:5])
)
```

**工期：** 0.5 天

---

### C4-4. ES 宕机时动态权重补偿

**来源 Case：** Case 4 — 回退 PG FTS 后没有调整其他参数

**修复方案：** `_bm25_search` 回退时通知调用方调整权重：

```python
async def _single_search(self, query, user_id, top_k, vector_weight, bm25_weight):
    query_vector = await embed_query(query)
    store = get_vector_store()
    vector_results = store.search(query_vector, user_id, top_k=top_k)

    try:
        bm25_results = es_search(query, user_id, top_k)
        using_pg_fts = False
    except Exception:
        bm25_results = await self._pg_fts_search(query, user_id, top_k)
        using_pg_fts = True

    # ES 宕机时降低 BM25 权重，补偿向量检索
    if using_pg_fts and bm25_results:
        bm25_weight *= 0.5
        vector_weight = 1.0 - bm25_weight

    return self._rrf_fuse(vector_results, bm25_results, vector_weight, bm25_weight)
```

**工期：** 0.5 天

---

### C1-3. multi_turn 指代消解改用轻量模型

**来源 Case：** 设计文档审计发现 `_llm_resolve` 使用主模型（glm-5.1-openai）

**修复方案：**

```python
# app/services/multi_turn.py — _llm_resolve 改用轻量模型
async def _llm_resolve(query: str, history: list[dict]) -> str | None:
    from app.config import get_settings
    settings = get_settings()
    # 使用轻量模型而非主模型
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.llm_api_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.agent_lightweight_llm,  # 改用 glm-4.5-air
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 100,
                    "temperature": 0.1,
                },
            )
            ...
```

**工期：** 0.5 天

---

## 实施路线图

```
第 1 周（P0 + 关键 P1）
├── D1-2: C3-1 克隆原子性修复（含 vector_store 新方法）
├── D3:   C3-2 删除安全性校验
├── D4-5: C1-2 部分结果补检索 + 分类优先级修复
└── D5:   C2-1 分级质量输出

第 2 周（P1 完善 + P2 可观测性）
├── D1-3: C1-1 LLM 子问题 DAG + execute_tools 增量上下文
├── D4:   C2-2 adjust_params 多策略扩展
└── D5:   C4-1 反思评分导出 + agent_trace 写入

第 3 周（P2 评估 + P3 优化）
├── D1-2: C4-2 RRF 参数评估框架
├── D3:   C4-3 ES/PG FTS 回退效果评估
├── D4:   C2-3 句子边界截断 + C4-4 ES 宕机权重补偿
└── D5:   C1-3 multi_turn 轻量模型 + 全量回归测试
```

---

## 风险与约束

| 风险 | 影响 | 缓解 |
|------|------|------|
| LLM 子问题 DAG 生成不稳定 | 规划失败走 fallback 单步检索 | _extract_json 三级容错 + 白名单 + fallback |
| 反思评分导出增加 Prometheus 负载 | 低（每次请求只 3 个 histogram observe） | 可接受 |
| vector_store.get_vectors_by_ids 需新增方法 | MilvusClient 需按 ID 查询向量 | 用 query + filter 实现 |
| 克隆原子性在 Milvus/ES 失败时无法回滚 PG | 数据不一致 | 先写 Milvus/ES，最后写 PG 状态 |

---

## 与现有 OPTIMIZATION-PLAN.md 的关系

本文档是 [OPTIMIZATION-PLAN.md](./OPTIMIZATION-PLAN.md) 的延续和深化。原有 15 项优化中已完成的 10 项不再重复，未完成的 5 项（#7 Protocol、#12 消解合并、#13 语义统一、#15 特性清理）保持不变。本文档聚焦面试追问暴露的 4 大 Case 场景优化。
