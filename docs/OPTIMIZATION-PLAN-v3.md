# knSpace 深度优化方案（v3.0）

> 来源：二面深度追问 — 7 大场景 Case 全链路分析
> 日期：2026-06-08
> 基于 v2.1 已实现代码，按 P0→P3 排列
> 状态：**待审视**

---

## 优先级定义

- **P0**：数据正确性/完整性受损，可能导致跨用户数据丢失，必须立即修复
- **P1**：检索质量或 Agent 回答质量存在可复现的缺陷
- **P2**：系统韧性不足，高负载或故障场景下缺乏降级能力
- **P3**：可观测性增强和前瞻性改进

---

## P0 — 数据正确性（3 项）

### D1. ContentPool ref_count 增减不对称导致跨用户数据丢失

**场景：** 用户 A 上传含 3 个相同文本 chunk 的 PDF → ref_count=1（上传去重，同一 hash 只 +1）；用户 B 上传相同 PDF → 克隆 → ref_count=2；用户 A 删除 → `content_hashes` 包含 3 个相同 hash → 递减 3 次 → ref_count 从 2 变成 -1 → GC 删除 ContentPool 条目 + 按 content_hash 删除 Milvus/ES → **用户 B 数据被连带删除**。

**根因：** 上传时 `pool_entries` 字典按 hash 去重，每个 hash 只 +1（`doc_processor.py:79` `if h not in pool_entries`）；删除时 `content_hashes` 取全部 chunk 的 hash 不去重，逐个递减（`documents.py:207-225`）。上传 +1 次，删除 -N 次。

**修复方案：**

```python
# documents.py delete_doc — content_hashes 去重
chunk_hashes_result = await db.execute(
    select(Chunk.content_hash).where(Chunk.document_id == doc_id)
)
content_hashes = list(set(row[0] for row in chunk_hashes_result.all()))  # ← set() 去重

for h in content_hashes:
    await db.execute(text("""
        UPDATE content_pool
        SET ref_count = GREATEST(ref_count - 1, 0)
        WHERE content_hash = :hash
    """), {"hash": h})
```

**同步修复上传侧 — UPSERT 替代 check-then-insert：**

多实例部署时 SELECT + INSERT 存在竞态。改为一行 UPSERT，同时解决去重和竞态：

```python
# doc_processor.py — 替换 69-97 行
pool_entries = {}

for i, cr in enumerate(chunk_results):
    h = _content_hash(cr.content)
    if h not in pool_entries:
        vec_bytes = np.array(all_vectors[i], dtype=np.float32).tobytes()
        result = await db.execute(text("""
            INSERT INTO content_pool (content_hash, content, vector, ref_count, token_count)
            VALUES (:hash, :content, :vector, 1, :token_count)
            ON CONFLICT (content_hash) DO UPDATE
            SET ref_count = content_pool.ref_count + 1
            RETURNING content_hash, content, vector, ref_count, token_count
        """), {
            "hash": h, "content": cr.content,
            "vector": vec_bytes, "token_count": len(cr.content),
        })
        row = result.fetchone()
        pool_entries[h] = ContentPool(
            content_hash=row[0], content=row[1], vector=row[2],
            ref_count=row[3], token_count=row[4],
        )
```

**工期：** 1 天

---

### D2. GC 按 content_hash 删除 Milvus 误伤其他用户数据

**场景：** 即使 D1 修复后，如果因其他边界 case 导致 ref_count 错误归零，`content_gc.py` 按 `content_hash` 删除 Milvus 会连带删除所有引用该 hash 的记录，不区分 user_id/document_id。

**根因：** `content_gc.py:33-38` 使用 `filter=f'content_hash == "{h}"'` 全局删除。

**修复方案 — GC 增加二次校验：**

```python
async def gc_content_pool():
    dead_hashes = await _get_dead_hashes(db)

    for h in dead_hashes:
        # 二次校验：PG 中确实没有 chunk 引用这个 hash
        remaining = await db.execute(
            select(func.count()).select_from(Chunk)
            .where(Chunk.content_hash == h)
        )
        if remaining.scalar() > 0:
            logger.error(f"GC safety: hash {h} has {remaining.scalar()} chunks but ref_count<=0, skipping")
            continue

        # 安全：确认无引用后再删除外部存储
        store.client.delete(collection_name="chunks", filter=f'content_hash == "{h}"')
        es.delete_by_query(index=settings.es_index, body={"query": {"term": {"content_hash": h}}})

    # 最后删除 PG
    await db.execute(delete(ContentPool).where(ContentPool.ref_count <= 0))
    await db.commit()
```

**工期：** 0.5 天

---

### D3. 三引擎数据一致性校验与恢复

**场景：** PG/Milvus/ES 中任何一者因网络闪断、运维误操作导致数据不一致，无自动发现和恢复机制。

**修复方案 — 每日校验 + 实时指标 + 恢复工具：**

**1) 每日全量校验（凌晨执行）：**

```python
async def daily_integrity_check():
    # 检查 A：ref_count 与实际 chunk 引用数对比
    mismatches = await db.execute(text("""
        SELECT cp.content_hash, cp.ref_count, COUNT(c.id) as actual
        FROM content_pool cp
        LEFT JOIN chunks c ON c.content_hash = cp.content_hash
        GROUP BY cp.content_hash, cp.ref_count
        HAVING cp.ref_count != COUNT(c.id)
    """))
    for row in mismatches:
        logger.error(f"ref_count mismatch: hash={row[0]}, stored={row[1]}, actual={row[2]}")

    # 检查 B：PG 有 chunk 但 Milvus 没有 → 向量缺失
    pg_ids = {str(r[0]) for r in (await db.execute(select(Chunk.id))).all()}
    milvus_ids = _get_all_milvus_ids()  # 分页扫描
    missing_in_milvus = pg_ids - milvus_ids
    orphaned_in_milvus = milvus_ids - pg_ids

    # 检查 C：自动修复 Milvus 孤立数据（PG 已删的，外部存储也应删）
    for h in orphaned_hashes:
        store.client.delete(collection_name="chunks", filter=f'content_hash == "{h}"')
```

**2) 实时指标（每次检索后检测）：**

```python
# search.py — 检索结果为空时检查是否因数据丢失
if not search_results:
    pg_count = await db.execute(
        select(func.count()).where(Chunk.user_id == user_id)
    )
    if pg_count.scalar() > 0:
        rag_data_loss.inc()  # PG 有数据但检索不到 → 数据丢失
```

**3) 恢复工具 — 从 ContentPool 重建 Milvus（零 embedding 成本）：**

```python
async def repair_milvus_from_pg():
    """ContentPool 保存了 vector 二进制数据，可直接重建 Milvus 条目。"""
    pg_chunks = await db.execute(
        select(Chunk, ContentPool.vector, ContentPool.content)
        .join(ContentPool, Chunk.content_hash == ContentPool.content_hash)
        .where(Chunk.chunk_type == "child")
    )
    pg_ids = {str(row[0].id) for row in pg_chunks.all()}
    milvus_ids = _get_all_milvus_ids()
    missing = pg_ids - milvus_ids

    repair_items = []
    for chunk, vec_bytes, content in pg_chunks.all():
        if str(chunk.id) not in missing:
            continue
        repair_items.append({
            "chunk_id": str(chunk.id), "user_id": str(chunk.user_id),
            "document_id": str(chunk.document_id),
            "content_hash": chunk.content_hash,
            "vector": np.frombuffer(vec_bytes, dtype=np.float32).tolist(),
            "snippet": content[:500],
        })
    if repair_items:
        store.insert_batch(repair_items)
```

**工期：** 2 天（校验 1d + 恢复工具 1d）

---

## P1 — 检索与回答质量（4 项）

### D4. RRF 融合前置过滤 + 加权 RRF，防止低质量 ES 结果污染排序

**场景：** 用户搜 "glm-5.1 成本"，ES 匹配到 "glm-5.1 模型"（零成本信息）但因 rank 靠前被 RRF 融合到高分位置。

**根因：** `_rrf_fuse` 只看 rank 位置，完全忽略原始分数（`search.py:171-189`）。同时 ES 查询 `minimum_should_match: 1` 导致只要匹配一个 token 就返回。

**修复方案 — 三层过滤：**

**1) ES 查询侧收紧（源头控制）：**

```python
# es.py search() — 动态 minimum_should_match + min_score
token_count = len(tokens.split())
min_match = "100%" if token_count <= 2 else ("75%" if token_count <= 4 else "60%")

body = {
    "query": {
        "bool": {
            "must": {"term": {"user_id": user_id}},
            "should": [
                {"match": {"content_jieba": {"query": tokens, "operator": "or",
                            "minimum_should_match": min_match}}},
                {"match": {"content": {"query": query, "operator": "or",
                            "minimum_should_match": min_match}}},
            ],
            "minimum_should_match": 1,
        },
    },
    "min_score": 1.0,  # BM25 分数低于 1.0 的结果直接丢弃
}
```

**2) 加权 RRF（引入原始分数信号）：**

```python
def _rrf_fuse(self, vector_results, bm25_results, vector_weight, bm25_weight):
    scores = {}

    if vector_results:
        max_vec = max(r.get("score", 1.0) for r in vector_results) or 1.0
        for rank, r in enumerate(vector_results):
            norm = r.get("score", 1.0) / max_vec
            scores[r["chunk_id"]] = scores.get(r["chunk_id"], 0) + \
                vector_weight * norm / (RRF_K + rank + 1)

    if bm25_results:
        max_bm25 = max(r.get("score", 1.0) for r in bm25_results) or 1.0
        for rank, r in enumerate(bm25_results):
            norm = r.get("score", 1.0) / max_bm25
            scores[r["chunk_id"]] = scores.get(r["chunk_id"], 0) + \
                bm25_weight * norm / (RRF_K + rank + 1)

    return dict(sorted(scores.items(), key=lambda x: x[1], reverse=True))
```

**3) Rerank 兜底保持不变（已有）。**

**工期：** 1.5 天

---

### D5. 查询分类器重构 — LLM 为主 + 规则快路径

**场景：** "文档中 ID 为 89756 的腾讯云服务器，对比 2024 和 2023 年的 API 成本，并说明成本差异的核心原因"——同时命中 keyword + compare + multi_hop 三种特征，但单标签规则分类器只能选一个，keyword 信息丢失。

**设计原则：** 以 LLM 判断为主，规则仅处理**明确的正则命中**场景（引号包裹、UUID、纯数字 ID 等无歧义的模式），作为零延迟快路径跳过 LLM 调用。

**修复方案：**

```python
@dataclass
class AnalyzedQuery:
    original: str
    query_type: str           # 主类型：keyword / semantic / compare / multi_hop
    sub_types: list[str]      # 所有命中的类型标签（多标签）
    has_keyword: bool         # 是否包含精确匹配需求
    rewritten: str
    sub_queries: list[str]
    vector_weight: float
    bm25_weight: float


class QueryAnalyzer:
    # 规则快路径：只有这些无歧义的正则模式才跳过 LLM
    _FAST_KEYWORD = re.compile(
        r'".+?"|\'.+?\'|'            # 引号包裹：精确引用
        r'[\w\d]{8,}-[\w\d]{4,}'     # UUID 格式：550e8400-e29b
    )

    def analyze(self, query, history=None) -> AnalyzedQuery:
        q = query.strip()

        # 1. 引用消解（有历史时）
        if history:
            q = self._resolve_refs(q, history)

        # 2. 噪声词清理
        rewritten = self._rewrite(q)

        # 3. 分类：规则快路径 → LLM 主路径
        query_type, sub_types, has_keyword = self._classify(q)

        # 4. 子查询拆分（LLM 主路径已包含拆分结果）
        if query_type in ("compare", "multi_hop") and len(sub_types) > 1:
            sub_queries = self._decompose(q, query_type)
        else:
            sub_queries = [rewritten]

        # 5. 权重
        if has_keyword:
            vw, bw = 0.3, 0.7
        else:
            vw, bw = 0.7, 0.3

        return AnalyzedQuery(
            original=query, query_type=query_type, sub_types=sub_types,
            has_keyword=has_keyword, rewritten=rewritten,
            sub_queries=sub_queries, vector_weight=vw, bm25_weight=bw,
        )

    def _classify(self, query: str) -> tuple[str, list[str], bool]:
        # 规则快路径：明确的 keyword 模式直接返回，零延迟
        if self._FAST_KEYWORD.search(query):
            return "keyword", ["keyword"], True

        # LLM 主路径：让模型判断 query 的完整语义
        return self._llm_classify(query)

    def _llm_classify(self, query: str) -> tuple[str, list[str], bool]:
        """轻量 LLM 分类，返回 (主类型, 多标签, 是否含 keyword)。"""
        prompt = f"""分析以下用户查询，输出 JSON。

用户查询：{query}

判断维度：
1. query_type（主类型，必选其一）：
   - keyword: 包含精确 ID/编号/错误码等需要精确匹配的内容
   - semantic: 开放式问答（为什么/怎么/什么是）
   - compare: 对比类查询（A vs B、区别、差异）
   - multi_hop: 需要多步推理或跨实体关联
2. sub_types（所有命中的类型，可多选）：keyword/semantic/compare/multi_hop
3. has_keyword: 是否包含需要精确匹配的关键词（ID/编号/错误码）

输出：{{"query_type": "...", "sub_types": [...], "has_keyword": true/false}}"""

        raw = await _call_lightweight_llm(prompt, max_tokens=150)
        parsed = _extract_json(raw)

        if parsed and isinstance(parsed, dict):
            qt = parsed.get("query_type", "semantic")
            st = parsed.get("sub_types", [qt])
            hk = parsed.get("has_keyword", False)
            if qt not in {"keyword", "semantic", "compare", "multi_hop"}:
                qt = "semantic"
            return qt, st, hk

        # LLM 失败兜底：默认 semantic
        return "semantic", ["semantic"], False
```

**路由逻辑保持不变：** `intent_classify` 节点根据 `query_type` 判断 simple/complex。多标签和 `has_keyword` 传递给 Agent 的 `generate_plan` 使用。

**Agent 规划注入 keyword 提示（`generate_plan`）：**

```python
# 从 AnalyzedQuery 获取 has_keyword 信号
analyzed = _analyzer.analyze(state["query"])
if analyzed.has_keyword:
    keyword_hint = """重要：该查询包含精确关键词（ID/编号），必须在检索计划中优先使用
fulltext_search 定位精确匹配，再结合 hybrid_search 获取语义内容。"""
```

**工期：** 2 天

---

### D6. 多轮指代消解重构 — LLM 为主 + 规则快路径 + 校验

**场景：** 第一轮 "文档里的 AI 模型月成本是多少？" → 第二轮 "它的年维护费呢？"。规则层取最后一个实体 "月成本" 替换 "它"，得到 "月成本的年维护费呢？"（错误）。且规则层改了之后 LLM 层不执行（串行互斥）。

**设计原则：** 以 LLM 消解为主，规则仅处理**明确的代词替换模式**（如查询中只有单一实体+单一代词，无歧义），作为零延迟快路径。

**修复方案 — LLM 主路径 + 规则快路径 + 校验兜底：**

```python
# 明确的快路径模式：上下文中只有一个候选实体 + 查询中只有一个代词
_FAST_PRONOUN = re.compile(r'^(它|他|她|这个|那个|这|那|其|此)')

async def resolve_query_with_history(query, history_messages, use_llm=True):
    if not history_messages or not _PRONOUNS_ZH.search(query):
        return query

    # 快路径：明确的单实体替换（无歧义场景）
    fast_resolved = _fast_rule_resolve(query, history_messages)
    if fast_resolved != query:
        return fast_resolved  # 零延迟直接返回

    # 主路径：LLM 消解
    if use_llm:
        llm_resolved = await _llm_resolve(query, history_messages[-6:])
        if llm_resolved and _validate_resolution(query, llm_resolved, history_messages):
            return llm_resolved

    # 兜底：拼接最近 user 消息作为查询扩展
    last_user = [m for m in history_messages if m["role"] == "user"][-1]["content"]
    return f"{last_user}，{query}"


def _fast_rule_resolve(query, history) -> str:
    """规则快路径：仅处理无歧义的单实体+单代词场景。

    条件（全部满足才触发）：
    1. 查询以代词开头（"它的..."），而非中间出现（"这个和那个..."）
    2. 上一轮 user 消息中只有一个明确的话题实体
    """
    if not _FAST_PRONOUN.match(query):
        return query

    recent_user = [m for m in history[-4:] if m["role"] == "user"]
    if not recent_user:
        return query

    last_user = recent_user[-1]["content"]
    # 提取上一轮 user 消息中的实体
    entities = _extract_topic_entities(last_user)

    # 只有唯一候选时才用规则替换，否则交给 LLM 判断
    if len(entities) == 1:
        target = entities[0]
        for pronoun in ["它", "他", "她", "这个", "那个"]:
            if query.startswith(pronoun):
                return query.replace(pronoun, target, 1)

    return query


def _extract_topic_entities(text: str) -> list[str]:
    """从用户消息中提取话题实体（主语级名词短语）。

    提取策略：
    1. "的" 前的名词短语（"文档里的 AI 模型" → "AI 模型"）
    2. 大写开头的英文术语（"glm-5.1"）
    3. 返回去重后的列表
    """
    entities = []
    # 匹配 "的/之" 前面的名词短语
    for m in re.finditer(r'([一-鿿A-Za-z0-9\s\-\.]{2,15})(?:的|之)', text):
        candidate = m.group(1).strip()
        if len(candidate) >= 2:
            entities.append(candidate)
    return list(dict.fromkeys(entities))  # 保序去重
```

**LLM 消解结果校验（防幻觉）：**

```python
def _validate_resolution(original, resolved, history) -> bool:
    """校验 LLM 消解结果：新词必须在历史中出现过。"""
    new_terms = set(re.findall(r'[一-鿿]{2,8}|[A-Z][a-zA-Z0-9\-]+', resolved)) - \
                set(re.findall(r'[一-鿿]{2,8}|[A-Z][a-zA-Z0-9\-]+', original))
    if not new_terms:
        return False
    history_text = " ".join(m.get("content", "") for m in history)
    for term in new_terms:
        if term not in history_text:
            logger.warning(f"LLM resolve hallucination: '{term}' not in history")
            return False
    if len(resolved) > len(original) * 3:
        return False
    return True
```

**回推 D6 场景验证：**

```
第一轮 user: "文档里的 AI 模型月成本是多少？"
第二轮 user: "它的年维护费呢？"

快路径检查：
  - query 以 "它" 开头 → 命中 _FAST_PRONOUN ✓
  - 上一轮 user: "文档里的 AI 模型月成本是多少？"
  - _extract_topic_entities: "AI 模型"（"的" 前的名词短语）
  - 候选数 = 1 → 无歧义 → 规则替换
  - 结果："AI 模型的年维护费呢？" ✓

多歧义场景（不走快路径，走 LLM）：
  第二轮: "它和那个的区别是什么？"
  - query 不以代词开头（以"它和..."开头，不是单一代词）→ 快路径不触发
  - LLM 主路径消解
```

**工期：** 2 天

---

### D7. Agent 重试耗尽后三级降级：扩大检索 → 固定管线 → 诚实回答

**场景：** "对比 glm-4.5-air 和 glm-5.1-openai 的 token 成本 + 响应延迟" 走 Agent，reflect 发现 groundedness 低，adjust_params 重试 2 次仍失败，返回带幻觉的答案 + 泛化警告。

**根因：** `adjust_params` 最大 top_k=80~100，且不改变检索策略（只调参数不换 query）。重试耗尽后直接返回最后一次答案 + 警告，不尝试降级到固定管线。

**修复方案：**

**1) `adjust_params` 增加 level 3 最终策略 — 原始 query 整体检索：**

```python
def adjust_params(state: AgentState) -> dict:
    retry_count = state.get("retry_count", 0)
    strategy_level = min(retry_count, 3)  # 扩展到 3

    if strategy_level >= 3:
        return {"plan": [{
            "tool": "hybrid_search",
            "args": {
                "query": state["original_query"],  # 不拆子查询
                "top_k": 100,
                "vector_weight": 0.5,
                "bm25_weight": 0.5,
            },
        }]}
    # ... 原有 level 0-2 逻辑 ...
```

**2) `_route_after_reflect` 多给一次重试机会：**

```python
def _route_after_reflect(state: AgentState) -> str:
    max_retries = getattr(settings, 'agent_max_retries', 2)
    if state.get("should_retry") and state.get("retry_count", 0) <= max_retries + 1:
        return "retry"
    return "end"
```

**3) Agent 重试全部耗尽后降级到固定管线：**

```python
# router.py _agent_stream 中
if result_state.get("reflection_result") == "max_retries_exhausted":
    yield agent_step("降级扩大检索")
    search_svc = SearchService()
    expanded = await search_svc.search_with_weights(
        query=resolved_query, user_id=user_id,
        top_k=60, vector_weight=0.5, bm25_weight=0.5,
    )
    if len(expanded) > len(result_state.get("chunks", [])):
        # 固定管线找到更多结果 → 重新生成
        context = search_svc.build_context(expanded)
        async for token in llm_svc.stream_generate(req.query, context):
            yield token...
    else:
        # 固定管线也没更多结果 → 诚实回答
        yield NO_DATA_RESPONSE.format(...)
```

**工期：** 2 天

---

## P2 — 系统韧性与降级（3 项）

### D8. 三引擎删除一致性 — 延迟物理删除 + 定时补偿

**场景：** 用户删除文档时 PG 的 ref_count 已归零并删除 ContentPool，但 Milvus 因网络闪断删除失败，导致孤立向量数据。

**根因：** `documents.py:192-204` Milvus/ES 删除失败时 `except: pass`，PG 照常 commit。删除后无重试、无校验。

**修复方案 — 三层防御：**

**1) 延迟物理删除 — Chunk 表新增 `cleanup_status`：**

```python
# Chunk 模型新增字段
cleanup_status: Mapped[str] = mapped_column(String(20), default="done")

# 删除流程改为
async def delete_doc(doc_id, user, db):
    doc.is_deleted = True

    milvus_ok, es_ok = False, False
    try:
        store.delete_by_document(doc_id)
        milvus_ok = True
    except Exception:
        pass
    try:
        es_delete(doc_id)
        es_ok = True
    except Exception:
        pass

    if milvus_ok and es_ok:
        # 全部成功 → 减 ref_count + 物理删 chunks + 删 content_pool
        await _full_cleanup(db, doc_id)
    else:
        # 部分失败 → 标记 pending，等补偿任务
        await db.execute(
            update(Chunk).where(Chunk.document_id == doc_id)
            .values(cleanup_status="pending")
        )

    await db.commit()
```

**2) 5 分钟补偿重试：**

```python
async def reconcile_cleanup():
    failed = await db.execute(
        select(Chunk.document_id).where(Chunk.cleanup_status == "pending")
        .group_by(Chunk.document_id)
    )
    for (doc_id,) in failed.all():
        # 重试 Milvus/ES 删除
        if milvus_ok and es_ok:
            await _full_cleanup(db, doc_id)
```

**3) 每日全量校验（与 D3 合并执行）。**

**工期：** 2 天

---

### D9. 带宽打满时的分级降级 — API 探测 + 延迟 embedding + Rerank 本地兜底

**场景：** 多用户上传大文件 + Agent 调用云端 API，带宽被打满导致 Embedding/Rerank/LLM API 超时。

**根因：** `degrade.py` 只看 CPU/内存不看网络。Embedding 失败走 `_dummy_embed` 写入垃圾向量。文档处理是同步管线，不支持延迟 embedding。Rerank 失败无本地兜底。

**修复方案：**

**1) API 健康探测（30 秒周期）：**

```python
# degrade.py
async def is_api_healthy() -> bool:
    """轻量探测 Embedding API 是否可用。"""
    if time.monotonic() - _last_probe_time < 30:
        return _last_probe_ok
    _last_probe_time = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.embedding_api_url}/health")
            _last_probe_ok = resp.status_code == 200
    except Exception:
        _last_probe_ok = False
    return _last_probe_ok
```

**2) 文档处理延迟 embedding 模式：**

```python
async def process_document(doc_id, user_id):
    # ... parsing, chunking ...
    api_ok = await is_api_healthy()

    if api_ok:
        all_vectors = await embed_texts(texts)  # 正常模式
        doc.processing_status = "ready"
    else:
        all_vectors = [[0.0] * EMBEDDING_DIM for _ in texts]  # 占位
        doc.processing_status = "pending_embedding"  # 新状态

    # ContentPool 写入（含 needs_embedding 标记）
    # ES 写入 ✓（BM25 可用）
    # Milvus：api_ok 时才写入，否则跳过

    if api_ok:
        store.insert_batch(milvus_data)
```

**3) 延迟补向量（5 分钟扫描）：**

```python
async def backfill_embeddings():
    if not await is_api_healthy():
        return
    pending = await db.execute(
        select(ContentPool).where(ContentPool.needs_embedding == True).limit(200)
    )
    texts = [p.content for p in pending.scalars().all()]
    vectors = await embed_texts(texts)  # API 已恢复
    # 更新 ContentPool.vector + 补写 Milvus + 更新文档状态为 ready
```

**4) Rerank 本地兜底（余弦相似度替代精排）：**

```python
async def _rerank(self, query, candidates):
    result = await self._rerank_api(query, candidates)
    if result is not None:
        return result

    # API 不可用 → 用 query-chunk 余弦相似度做轻量精排
    query_vec = await embed_query(query)
    for c in candidates:
        chunk_vecs = store.get_vectors_by_ids([c["chunk_id"]])
        c["score"] = cosine_similarity(query_vec, chunk_vecs[0])
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates
```

**工期：** 3 天

---

### D10. 多实例部署 — UPSERT 消除 ContentPool 写入竞态

**场景：** 3 节点集群，两个实例同时处理包含相同 chunk 的文档，`SELECT + INSERT` 竞态导致 `UniqueViolation` 事务回滚。

**修复方案：** 已在 D1 中通过 UPSERT 一并解决。无需额外分布式锁。

**需要分布式锁的场景（按 doc_id 粒度）：**

```python
# 同一文档不会被两个 worker 并行处理
async def process_document(doc_id, user_id):
    r = _get_redis()
    acquired = await r.set(f"doc_processing:{doc_id}", "1", nx=True, ex=300)
    if not acquired:
        return
    try:
        # ... 原有处理逻辑 ...
    finally:
        await r.delete(f"doc_processing:{doc_id}")
```

**定时任务用 PG advisory lock（不需要 Redis）：**

```python
async def backfill_embeddings():
    acquired = (await db.execute(text("SELECT pg_try_advisory_lock(12345)"))).scalar()
    if not acquired:
        return  # 另一个实例在跑
    try:
        # ... 补算逻辑 ...
    finally:
        await db.execute(text("SELECT pg_advisory_unlock(12345)"))
```

**工期：** 1 天（UPSERT 已在 D1 完成，仅加锁逻辑）

---

## P3 — 可观测性与前瞻改进（2 项）

### D11. 父子 chunk 存储优化 — 父 chunk 不进 ContentPool

**场景：** 父 chunk 内容是子 chunk 文本的超集，两者分别进 ContentPool 造成冗余存储。父 chunk 不参与向量检索却占用了 embedding + Milvus 资源。

**修复方案：**

```python
# doc_processor.py — 区分父子 chunk 处理
for i, cr in enumerate(chunk_results):
    h = _content_hash(cr.content)
    if cr.chunk_type == "child":
        # child 正常走 ContentPool + embedding + Milvus
        vec_bytes = np.array(all_vectors[i], dtype=np.float32).tobytes()
        await db.execute(text("""
            INSERT INTO content_pool ... ON CONFLICT ...
        """), {...})
    else:
        # parent 只存文本到 ContentPool，vector 留空
        await db.execute(text("""
            INSERT INTO content_pool (content_hash, content, vector, ref_count, token_count)
            VALUES (:hash, :content, NULL, 1, :token_count)
            ON CONFLICT (content_hash) DO UPDATE SET ref_count = content_pool.ref_count + 1
        """), {...})
```

父 chunk 不做 embedding、不写 Milvus。检索命中子 chunk 后通过 `parent_chunk_id` 回溯，直接从 ContentPool 读父 chunk 的文本（无向量需求）。

**工期：** 1 天

---

### D12. 语义哈希去重 — 规范化预处理

**场景：** "成本43元" 和 "成本43元。" 因标点差异被 SHA256 判定为不同内容，无法去重。

**修复方案 — 第一层规范化（低成本覆盖 80% 场景）：**

```python
def normalize_for_hash(text: str) -> str:
    text = re.sub(r'\s+', ' ', text.strip())
    text = re.sub(r'[。，！？.!?,;；：:]+$', '', text)  # 去末尾标点
    return text

def _content_hash(content: str) -> str:
    return hashlib.sha256(normalize_for_hash(content).encode("utf-8")).hexdigest()
```

第二层（SimHash）和第三层（embedding 相似度）留给数据量增长到百万级 chunk 时再做。

**工期：** 0.5 天

---

## 实施路线图

```
Phase 1 — 数据正确性（5 天）
  D1  ContentPool UPSERT + 删除去重                  1d
  D2  GC 二次校验                                     0.5d
  D3  每日校验 + 恢复工具                              2d
  D10 多实例 doc_id 锁 + advisory lock               1d
  ← 里程碑：三引擎数据一致性保障

Phase 2 — 检索与回答质量（5.5 天）
  D4  RRF 前置过滤 + 加权 RRF                         1.5d
  D5  多标签分类器 + keyword 注入                      2d
  D6  指代消解三级改造                                 2d
  ← 里程碑：检索准确率提升

Phase 3 — 系统韧性（5 天）
  D7  Agent 重试降级链路                               2d
  D8  删除一致性三层防御                               2d
  D9  带宽分级降级                                    3d
  ← 里程碑：故障自愈能力

Phase 4 — 优化与前瞻（1.5 天）
  D11 父子 chunk 存储优化                              1d
  D12 语义哈希规范化                                  0.5d
```

---

## 与 v2.1 的关系

v2.1 已实现的 10 项（克隆原子性、LLM 子问题 DAG、反思分级输出、adjust_params 策略升级、Prometheus 指标等）保持不变。v3.0 聚焦二面追问暴露的 **数据正确性** 和 **系统韧性** 问题，是 v2.1 的纵深补充。

---

## 风险与约束

| 风险 | 影响 | 缓解 |
|------|------|------|
| UPSERT 需 PG 9.5+ | 生产环境 PG 版本需确认 | PG 12+ 已是标配 |
| 加权 RRF 改变了排序逻辑 | 可能影响现有检索结果排序 | A/B 测试对比 Recall@10 |
| 多标签分类器增加复杂度 | 规则维护成本上升 | 后续考虑 LLM 兜底（Phase 5） |
| 延迟 embedding 导致用户困惑 | 文档状态显示"部分可用" | 前端提示"文本检索可用，语义检索补算中" |
| API 探测增加额外请求 | 每 30 秒一次轻量 GET | 可忽略 |
| 延迟补向量批量操作可能再次打满带宽 | 限速 200 条/批 + API 健康检查前置 | 可控 |
