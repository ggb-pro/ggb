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

**根因分析：** `_clone_chunks_from_existing` 函数只实现了 PG chunks 的复制，完全遗漏了 Milvus 向量存储和 ES 全文索引两个关键存储引擎的写入。同时 `parent_chunk_id` 的重映射代码 `func.replace(old, old, old)` 三个参数相同，等于什么都没做。用户 B 上传同一文档后，文档状态显示 "ready"，但实际搜索时向量检索和全文检索都查不到内容——因为 Milvus 和 ES 里根本没有用户 B 的数据。

**修复思路：** 重写整个克隆函数。**执行顺序是关键**：采用"先写 Milvus/ES → 再提交 PG → 最后标 ready"的顺序，而非"先写 PG → 再写 Milvus/ES"。原因是 PG 有事务可以回滚，而 Milvus/ES 没有事务支持。如果先写 PG 再写 Milvus 失败，PG 已提交的 chunks 无法自动回滚，导致数据不一致。改为先尝试写 Milvus/ES（失败可静默忽略），确认外部存储就绪后再一次性提交 PG chunks + 标记 ready。

Milvus 的向量数据不需要重新计算 embedding（同一份文件的向量完全相同），只需从源文档读出已有向量，用新的 chunk ID 写入即可。

**修复方案：**

```python
# app/api/documents.py — _clone_chunks_from_existing 重写
async def _clone_chunks_from_existing(db, source_doc_id, target_doc_id, target_user_id):
    """原子性克隆：先写 Milvus/ES（可回退）→ 再提交 PG → 最后标 ready。"""
    from app.services.vector_store import get_vector_store
    from app.services.es import bulk_index_chunks

    # Step 0: 预计算映射关系（不写任何存储）
    # 先查询源 chunks，构建 old_to_new 映射，准备好所有数据，但不提交。
    result = await db.execute(select(Chunk).where(Chunk.document_id == source_doc_id))
    source_chunks = result.scalars().all()
    if not source_chunks:
        return

    old_to_new = {str(c.id): str(uuid.uuid4()) for c in source_chunks}
    chunk_data = [(old_to_new[str(c.id)], c.content) for c in source_chunks]

    # Step 1: 先写 Milvus（不可回滚，所以放最前面尽早发现失败）
    store = get_vector_store()
    source_ids = list(old_to_new.keys())
    source_vectors = store.get_vectors_by_ids(source_ids)
    new_ids = list(old_to_new.values())
    texts = [c[1] for c in chunk_data]
    store.insert(new_ids, target_user_id, target_doc_id, source_vectors, texts)

    # Step 2: 再写 ES（同样不可回滚，放第二步）
    es_chunks = [
        {"chunk_id": new_id, "document_id": target_doc_id,
         "user_id": target_user_id, "content": content}
        for new_id, content in chunk_data
    ]
    bulk_index_chunks(es_chunks)

    # Step 3: Milvus/ES 都成功后，一次性提交 PG chunks（可回滚）
    for chunk in source_chunks:
        db.add(Chunk(
            id=old_to_new[str(chunk.id)], document_id=target_doc_id,
            user_id=target_user_id, content=chunk.content,
            chunk_index=chunk.chunk_index, chunk_type=chunk.chunk_type,
            parent_chunk_id=chunk.parent_chunk_id,
            char_start=chunk.char_start, char_end=chunk.char_end,
            page_number=chunk.page_number, token_count=chunk.token_count,
        ))
    await db.commit()

    # Step 4: 重映射 parent_chunk_id（第二次遍历更新）
    cloned = await db.execute(select(Chunk).where(Chunk.document_id == target_doc_id))
    for chunk in cloned.scalars().all():
        old_parent = str(chunk.parent_chunk_id) if chunk.parent_chunk_id else None
        if old_parent and old_parent in old_to_new:
            chunk.parent_chunk_id = old_to_new[old_parent]
    await db.commit()
```

**upload 函数同步改造（`documents.py:79-88`）：**

当前 upload 函数在克隆路径里没有 try/except，如果 `_clone_chunks_from_existing` 抛异常，文档状态永远卡在 "chunking"。需要改为：克隆成功标 ready，克隆失败标 failed + 记录错误信息。

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
**新增依赖：** `vector_store.py` 需新增 `get_vectors_by_ids()` 方法（通过 MilvusClient 的 query 接口按 ID 列表查询向量数据）

---

### C3-2. 删除文档时级联安全性验证

**来源 Case：** Case 3 — 用户 A 删除原文档后用户 B 的数据是否受影响

**当前代码（`documents.py:156-205`）：** 删除按 `document_id` 隔离，PG 外键 CASCADE 只影响自己文档的 chunks，Milvus/ES 也按 `document_id` 过滤。

**结论：** 当前删除逻辑是安全的，不会级联影响其他用户。原因是所有存储引擎（PG、Milvus、ES）的删除操作都以 `document_id` 为过滤条件，而用户 B 的 chunks 指向 `document_id = B 的文档 ID`，不会被 A 的删除操作命中。但这个安全性的前提是 C3-1 修复后克隆流程正确写入了独立的 `document_id`。

**额外保障：** 删除前校验是否有其他文档依赖同一 content_hash。这主要用于日志审计——当检测到被删除文件还有其他用户依赖时，记录一条 info 日志方便排查。如果没有任何其他文档引用同一 content_hash，物理文件也可以安全清理。

```python
# 删除前检查：是否有其他用户的文档引用同一 content_hash
siblings = await db.execute(
    select(func.count()).select_from(Document).where(
        Document.content_hash == doc.content_hash,
        Document.id != doc_id,
        Document.is_deleted == False,
    )
)
sibling_count = siblings.scalar()
if sibling_count > 0:
    logger.info(f"Deleting doc {doc_id}, but {sibling_count} other docs share content_hash")
# sibling_count == 0 说明这是最后一个引用，物理文件可安全清理
```

**工期：** 0.5 天（当前已安全，加校验日志即可）

---

## P1 — Agent 路径核心功能（4 项）

### C1-1. 多跳查询拆分改为 LLM 生成子问题 DAG

**来源 Case：** Case 1 — "对比 glm-4.5-air 和 glm-5.1-openai 的部署方式" 被错误分类为 compare（应为 multi_hop），且按"和"暴力拆分

**当前代码问题（`query_analyzer.py:77-87`）：**

1. `_COMPARE_PATTERNS` 优先级高于 `_MULTI_ENTITY_PATTERNS`，导致含"区别"的多实体查询被误分为 compare 而非 multi_hop
2. `_decompose_compare` 按连接词做字符串 split，不理解实体边界——"glm-4.5-air 和 glm-5.1-openai 的部署方式" 会被拆成 ["glm-4.5-air ", " glm-5.1-openai 的部署方式"]，第二个子查询包含了不属于它的内容
3. 子查询之间独立执行，没有依赖关系，也无法利用前序检索结果做增量检索

**修复思路：** 将 `generate_plan` 节点从"规则拆分 + 固定模板"升级为"轻量 LLM 生成子问题 DAG"。核心改变是让 LLM 理解查询的语义结构，输出带依赖关系的子问题列表。例如"对比 A 和 B 的部署方式，说明对 RAG 速度的影响"应被拆为三个子查询：分别查 A 和 B 的部署方式（并行），再查部署差异对 RAG 速度的影响（依赖前两个结果）。执行器按依赖顺序执行，有依赖关系的子查询可以获取前序检索结果的摘要作为上下文。

同时修复 `query_analyzer.py` 的分类优先级：当查询同时命中 compare 和 multi_hop 模式时，multi_hop 应优先（因为多实体+关系的场景比单纯的 A vs B 更复杂）。

**修复方案 — generate_plan 节点改造：**

改造后的 generate_plan 不再依赖规则引擎的拆分结果，而是直接将完整查询交给轻量 LLM，让 LLM 输出结构化的子问题 DAG。prompt 中明确列出了可用工具、参数格式和拆分规则。LLM 输出经过三级容错（_extract_json）和白名单过滤后，再对 top_k 等参数做上限校验防止 LLM 生成超大值。如果 LLM 完全无法生成有效 plan，fallback 为单步 hybrid_search。

```python
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
- 子查询数量不超过 3 个（避免过多子查询导致性能问题）
- 每个子查询必须独立可理解
- 多实体查询：分别检索每个实体
- 推理查询：前面的结果可能影响后续检索策略
- 有依赖的子查询在 args 中可引用前置结果
"""

    raw = await _call_lightweight_llm(prompt, max_tokens=500)
    parsed = _extract_json(raw)

    if parsed and isinstance(parsed, dict) and "sub_queries" in parsed:
        plan = []
        for sq in parsed["sub_queries"][:3]:  # 强制上限 3 个子查询，防止 LLM 生成过多
            tool = sq.get("tool", "hybrid_search")
            if tool not in {"hybrid_search", "fulltext_search"}:
                tool = "hybrid_search"
            plan.append({
                "id": sq.get("id", f"q{len(plan)+1}"),
                "tool": tool,
                "args": {
                    "query": sq.get("query", query),
                    "top_k": min(sq.get("args", {}).get("top_k", 40), 80),  # 上限校验
                    **sq.get("args", {}),
                },
                "depends_on": sq.get("depends_on", []),
            })
        if plan:
            return {"plan": plan, "retry_count": 0}

    # Fallback：LLM 生成失败，退化为单步检索
    return {"plan": [{"tool": "hybrid_search", "args": {"query": query}}], "retry_count": 0}
```

**execute_tools 改造 — 按依赖顺序执行 + 增量上下文注入：**

改造要点有三：(1) 按 plan 中声明的 `depends_on` 字段确定执行顺序（简单实现：假设 plan 列表已按依赖排序）；(2) 每完成一个子查询，将其结果摘要存入 `executed` 字典；(3) 后续有依赖关系的子查询在检索时，将前序结果摘要拼接到 query 中，让检索引擎利用已有信息做更精准的匹配。

```python
async def execute_tools(state: AgentState) -> dict:
    plan = state.get("plan", [])
    user_id = state["user_id"]

    executed = {}  # id → 结果摘要，供后续子查询引用
    all_chunks = []
    tools_called = []

    for step in plan:
        step_id = step.get("id", "")

        # 收集前置依赖的结果摘要，拼入后续检索
        deps = step.get("depends_on", [])
        dep_context = ""
        for dep_id in deps:
            if dep_id in executed:
                dep_context += f"前置检索[{dep_id}]结果摘要: {executed[dep_id][:200]}\n"

        args = dict(step.get("args", {}))
        args["user_id"] = user_id
        args["top_k"] = min(args.get("top_k", 40), 100)  # 参数校验上限
        if not args.get("query", "").strip():
            args["query"] = state["query"]

        # 增量上下文：有前置结果时追加到 query 增强检索精度
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

    # 按 chunk_id 去重（多个子查询可能命中同一个 chunk）
    seen = set()
    unique = [c for c in all_chunks if c["chunk_id"] not in seen and not seen.add(c["chunk_id"])]

    return {"chunks": unique, "tools_called": tools_called}
```

**分类优先级修复（`query_analyzer.py`）：**

当查询同时命中 compare 和 multi_hop 模式时（例如"对比 A 和 B 的部署差异，这些差异会影响速度吗"），应优先判定为 multi_hop。因为这类查询不只是简单的 A vs B 对比，还包含多实体间的关系推理，需要更复杂的多步检索策略。

```python
def _classify(self, query: str) -> str:
    if _EXACT_PATTERNS.search(query):
        return "keyword"
    # 关键修复：多实体+关系模式优先于纯对比
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

**来源 Case：** Case 1 — 子查询返回 0 结果时静默跳过，用户完全不知道部分信息缺失

**问题说明：** 当前的 execute_tools 循环中，如果某个子查询返回 0 个 chunk，代码只是 `all_chunks.extend([])` 然后继续执行下一步。没有任何感知机制——不触发补检索、不记录日志、不通知用户。这意味着如果"glm-4.5-air 部署方式"在知识库中不存在，这部分信息会被悄悄忽略，最终回答只包含 glm-5.1-openai 的信息，但用户不知道答案是不完整的。

**修复思路：** 在 execute_tools 循环中增加零结果检测。当 hybrid_search 返回 0 结果时，自动尝试用 fulltext_search 做一次补检索（因为全文检索和向量检索的匹配逻辑不同，可能向量检索没命中但 BM25 全文检索能命中关键词）。补检索的结果和原因都会记录到 `tools_called` 审计日志中，供后续反思节点和用户查看。

```python
# execute_tools 循环内，result 为空时触发补检索
try:
    result = await func.ainvoke(args)
    if isinstance(result, list) and len(result) == 0:
        # hybrid_search 返回 0 结果，尝试 fulltext_search 补检索
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

**来源 Case：** Case 2 — 重试耗尽后只追加泛化警告"质量校验未完成，可能存在不足"，不区分失败维度，不告知用户具体哪里有问题

**当前代码问题（`router.py:341-344`）：**

1. reflect 节点在 `retry_count >= max_retries` 时走 skip 分支，**丢弃了最后一次的评分数据**（`reflection_scores: {}`），导致 router 无法获取具体哪个维度不合格
2. router 只用固定文案追加警告，不区分是事实性错误、相关性不足还是逻辑矛盾，用户看到"可能存在不足"完全不知道该信还是不该信

**修复思路：** 分两步改造。第一步，修改 reflect 节点，在重试耗尽时保留最后一次的评分和失败原因（`reflection_result` 改为 `"max_retries_exhausted"`，`reflection_scores` 保留最后一次的值）。第二步，在 router.py 中新增 `_build_quality_warning` 函数，根据最低分维度和具体分值生成差异化的警告文案：分值越低，警告越严重、建议越具体（如"建议查阅原始文档"vs"部分信息可能不够准确"）。

**Step 1：reflect 节点保留最后一次评分（`nodes.py:228-229`）：**

```python
# 当前：skip 时丢弃 scores
if not answer or retry_count >= settings.agent_max_retries:
    return {"should_retry": False, "reflection_result": "skip", "reflection_scores": {}}

# 改为：保留最后一次的评分和原因，让 downstream 能获取具体失败维度
if retry_count >= settings.agent_max_retries:
    return {"should_retry": False, "reflection_result": "max_retries_exhausted",
            "reflection_scores": scores}  # 保留当前轮次的评分
```

**Step 2：router.py 根据评分生成差异化警告：**

```python
reflection = result_state.get("reflection_result", "")
scores = result_state.get("reflection_scores", {})

if reflection in ("max_retries_exhausted", "skip", "parse_failed"):
    warning = _build_quality_warning(reflection, scores, result_state.get("retry_count", 0))
    answer += f"\n\n> {warning}"

def _build_quality_warning(reflection: str, scores: dict, retry_count: int) -> str:
    """根据反思评分生成差异化的质量警告，替代固定文案。"""
    if reflection == "parse_failed":
        return "⚠️ 回答质量校验遇到异常，建议核实关键信息或换一种方式提问。"

    if not scores:
        return f"⚠️ 回答质量校验未通过（已重试 {retry_count} 次），请谨慎参考。"

    # 找出最低分维度，针对性提示
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

**来源 Case：** Case 2 — groundedness 低时只降 vector_weight，对"知识不存在"无效。连续两次重试用同样的策略调参，只是在重复无效操作

**当前策略（`nodes.py:273-307`）：**

| 最低分维度 | 当前策略 | 问题 |
|-----------|---------|------|
| groundedness | 降 vector_weight | 如果知识库里根本没有相关内容，调权重毫无意义 |
| relevance | 扩 top_k | 只增加了数量，不解决查询语义偏差 |
| consistency | 两者都调 | 策略模糊，没有针对性 |

**修复思路：** 引入"策略升级"机制——根据重试轮次（`retry_count`）自动升级调整策略的激进程度。第一轮重试用保守策略（微调权重），第二轮重试用激进策略（切换检索方式或改写查询）。具体来说：
- **groundedness 低（事实性差）**：保守→降 vector_weight 提高 BM25 权重；激进→直接切换为 fulltext_search 纯全文检索，跳过向量检索
- **relevance 低（答非所问）**：保守→扩 top_k；激进→用原始查询替换改写后的查询重新检索
- **consistency 低（逻辑矛盾）**：同时调整权重和范围

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

    # 策略升级：重试越多，策略越激进
    strategy_level = min(retry_count, 2)  # 0→保守, 1→中等, 2→激进

    new_plan = []
    for step in plan:
        args = dict(step.get("args", {}))

        if groundedness == min_score:
            # 策略 1: 事实性问题
            # 保守：提高 BM25 精确匹配权重
            # 激进：直接切换为 fulltext_search 纯全文检索
            if strategy_level >= 2:
                step = {"tool": "fulltext_search", "args": args}
                args["top_k"] = min(args.get("top_k", 40) + 20, 80)
            else:
                args["vector_weight"] = max(args.get("vector_weight", 0.7) - 0.2, 0.3)
                args["bm25_weight"] = 1.0 - args["vector_weight"]

        elif relevance == min_score:
            # 策略 2: 相关性不足
            # 保守：扩大 top_k 检索范围
            # 激进：回退到用户原始查询重新检索（绕过 LLM 的查询改写）
            args["top_k"] = min(args.get("top_k", 40) + 20 * (strategy_level + 1), 100)
            if strategy_level >= 2 and state.get("original_query"):
                args["query"] = state["original_query"]

        else:
            # 策略 3: 一致性问题 → 同时调整范围和权重
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

**来源 Case：** Case 2 — 反思评分随请求结束丢弃，无法回答"Agent 路径的回答质量到底怎么样"

**当前问题：**
- `reflection_scores` 停留在 state 内存中，请求结束即丢失，无法做历史趋势分析
- `agent_trace` JSONB 列在设计文档 DDL 中有定义，但 SQLAlchemy 模型中尚未添加
- 现有的 Prometheus 指标只有 `agent_retry_total`（重试次数）和 `agent_degrade_total`（降级次数），不包含质量维度

**修复思路：** 从两个维度建立可观测性。实时维度：在 reflect 节点中将每次的三个维度评分（relevance/groundedness/consistency）写入 Prometheus Histogram，可以按 P50/P95 查看质量趋势。离线维度：将完整的 Agent 执行过程（intent、plan、tools_called、scores、retry_count）写入 `messages.agent_trace` JSONB 列，供后续离线分析和问题排查。

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

每次 reflect 执行完成后，将三个维度的评分分别写入 Histogram。这样在 Grafana 中可以按 dimension 分别查看评分分布，快速发现某个维度是否系统性偏低。

```python
if scores:
    for dim, val in scores.items():
        reflection_scores_hist.labels(dimension=dim).observe(val)
```

**Step 3：添加 agent_trace 列（`models/message.py`）：**

在 Message 模型中新增 `agent_trace` JSONB 列，存储完整的 Agent 执行追踪数据。需要先运行 `alembic revision --autogenerate` 生成数据库迁移脚本。

```python
from sqlalchemy.dialects.postgresql import JSONB

agent_trace: Mapped[dict | None] = mapped_column(JSONB)
```

**Step 4：router.py 写入 trace（`result_state` 提取后）：**

在 Agent 执行完成后、保存消息前，将关键执行数据组装为 trace_data 写入 `agent_trace` 列。包含：意图分类结果、检索计划步数、工具调用详情、反思评分、重试次数、最终 chunk 数量等。

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

**工期：** 2 天（模型迁移 0.5d + Prometheus 指标 0.5d + 写入逻辑 1d）

---

### C4-2. RRF 权重 AB 测试框架

**来源 Case：** Case 4 — RRF_K=60 和权重比例直接从论文搬用，未经自有数据验证

**问题说明：** 当前 RRF 融合的三个关键参数（RRF_K=60, vector_weight=0.7, bm25_weight=0.3）全部是经验值，没有在实际数据集上做过参数扫描。不同的数据分布（中文/英文、技术文档/通用文档、长 chunk/短 chunk）可能需要完全不同的最优参数组合。

**修复思路：** 在 `evaluator.py` 中新增参数扫描评估框架。对 RRF_K ∈ {10, 30, 60, 100} 和权重组合 (0.3/0.7, 0.5/0.5, 0.7/0.3) 做 4×3=12 组实验，每组在标注数据集上计算 Recall@5 和 Recall@10，输出对比表格选出最优参数。

```python
async def evaluate_rrf_params(test_samples: list[dict]) -> dict:
    """对不同的 RRF_K 和权重组合评估检索质量，选出最优参数。"""
    results = {}
    for k in [10, 30, 60, 100]:
        for vw, bw in [(0.3, 0.7), (0.5, 0.5), (0.7, 0.3)]:
            recalls = []
            for sample in test_samples:
                fused = _rrf_fuse_with_k(vector_results, bm25_results, vw, bw, k=k)
                hit = sum(1 for cid in fused[:10] if cid in sample["relevant_ids"])
                recalls.append(hit / len(sample["relevant_ids"]))
            results[f"K={k}_vw={vw}"] = {
                "recall@5": sum(r[:5] for r in recalls) / len(recalls),
                "recall@10": sum(recalls) / len(recalls),
            }
    return results
```

**工期：** 2 天（框架搭建 1d + 数据集构造 1d）

---

### C4-3. ES 宕机 PG FTS 回退效果评估

**来源 Case：** Case 4 — 回退 PG FTS 后效果下降多少完全未知，只有模糊的"30-50%"估计

**问题说明：** 当 ES 不可用时，`_bm25_search` 自动回退到 PG FTS（`to_tsvector('simple', ...)`）。PG 的 `simple` 配置只做小写转换和停用词过滤，不理解中文分词语义。虽然写入时用了 jieba 分词，但查询端的 `simple` 配置丢失了词频和短语匹配能力。但目前没有任何量化数据说明这个回退的实际影响有多大。

**修复思路：** 在评估框架中新增 ES vs PG FTS 的对比评估模式。对同一组标注样本分别用 ES 和 PG FTS 检索，计算 Recall@10 的差异，输出具体的下降百分比。这个数据可以用于决策：PG FTS 回退后是否需要同步调整其他参数（如 C4-4 的权重补偿）。

```python
async def evaluate_fallback_fts(test_samples: list[dict]) -> dict:
    """对比 ES 和 PG FTS 的检索效果，量化回退的性能损失。"""
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

**来源 Case：** Case 1 面试追问 — 300 字硬截断可能在句子中间断开，丢失核心信息

**问题说明：** 当前 reflect 节点用 `content[:300]` 截取 chunk 内容，这是一个纯粹的字符串切片操作。如果 chunk 的前 300 字恰好是一些背景介绍，核心断言（如"所有查询必须带 user_id，否则会数据泄露"）出现在第 350 字，那 reflect 节点根本看不到这个关键信息，可能导致误判"回答没问题"。

**修复思路：** 新增 `_smart_truncate` 函数，在截断时优先找到最后一个句子结束符号（句号、问号、感叹号），在句子边界处截断。同时设置 60% 的保底阈值——如果最近的句子结束符在 60% 位置之前，说明前 60% 都是短句，直接在 limit 处截断即可，避免截断后内容过短。

```python
def _smart_truncate(text: str, limit: int) -> str:
    """按句子边界截断，避免在句子中间断开丢失语义。"""
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    # 从后往前找最近的句子结束符
    for sep in ["。", "？", "！", ".", "?", "!"]:
        last = truncated.rfind(sep)
        if last > limit * 0.6:  # 至少保留 60% 内容，否则不如直接截断
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

**来源 Case：** Case 4 — 回退 PG FTS 后没有调整 vector_weight/bm25_weight，检索效果雪上加霜

**问题说明：** 当 ES 宕机回退到 PG FTS 时，当前代码只是静默切换了检索后端，权重参数完全不变。但 PG FTS 的 `simple` 配置远不如 ES 的 jieba 分词，用同样的 bm25_weight 意味着给了一个质量更差的数据源分配了同样高的权重。正确的做法是检测到使用了 PG FTS 后，降低 bm25_weight、提高 vector_weight，让更可靠的向量检索承担更多匹配责任。

**修复思路：** 在 `_single_search` 中检测是否使用了 PG FTS 回退。如果确认回退，将 bm25_weight 乘以 0.5 折扣系数，同时将 vector_weight 提升为 `1.0 - discounted_bm25_weight`。这样即使 ES 不可用，向量检索仍然能提供基本可靠的结果。

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

    # ES 宕机时降低 BM25 权重，让向量检索承担更多匹配责任
    if using_pg_fts and bm25_results:
        bm25_weight *= 0.5
        vector_weight = 1.0 - bm25_weight

    return self._rrf_fuse(vector_results, bm25_results, vector_weight, bm25_weight)
```

**工期：** 0.5 天

---

### C1-3. multi_turn 指代消解改用轻量模型

**来源 Case：** 设计文档审计发现 `_llm_resolve` 使用主模型 glm-5.1-openai（通过 `LLMService()`），而非配置中的轻量模型 glm-4.5-air

**问题说明：** `multi_turn.py` 的 `_llm_resolve` 函数通过 `LLMService()` 调用 LLM，而 `LLMService` 默认使用 `config.py` 中的 `llm_model = glm-5.1-openai`（主模型，成本高、延迟大）。指代消解是一个轻量任务（只需改写一句话），完全不需要用主模型。应该改用 `agent_lightweight_llm = glm-4.5-air`，通过 `httpx.AsyncClient` 直接调用 API（复用 Agent 层的轻量模型调用方式），而不是通过 `LLMService`（它绑定了主模型）。

**修复思路：** 将 `_llm_resolve` 中的 `LLMService()` 替换为直接使用 httpx 调用轻量模型 API，和 Agent 层的 `_call_lightweight_llm` 采用相同的调用方式。同时设置 max_tokens=100（指代消解只需一行输出）和 temperature=0.1（确定性输出）。

```python
async def _llm_resolve(query: str, history: list[dict]) -> str | None:
    """Use lightweight LLM to resolve references."""
    from app.config import get_settings
    settings = get_settings()
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
| LLM 子问题 DAG 生成不稳定 | 规划失败走 fallback 单步检索 | _extract_json 三级容错 + 工具名白名单 + fallback |
| 反思评分导出增加 Prometheus 负载 | 低（每次请求只 3 个 histogram observe） | 可接受 |
| vector_store.get_vectors_by_ids 需新增方法 | MilvusClient 需按 ID 查询向量 | 用 query + filter + output_fields 实现 |
| 克隆原子性在 Milvus/ES 失败时无法回滚 PG | 数据不一致 | 先写 Milvus/ES，最后写 PG 状态；失败时标记文档 failed |

---

## 与现有 OPTIMIZATION-PLAN.md 的关系

本文档是 [OPTIMIZATION-PLAN.md](./OPTIMIZATION-PLAN.md) 的延续和深化。原有 15 项优化中已完成的 10 项不再重复，未完成的 5 项（#7 Protocol、#12 消解合并、#13 语义统一、#15 特性清理）保持不变。本文档聚焦面试追问暴露的 4 大 Case 场景优化。
