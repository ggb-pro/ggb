# knSpace v2.0 Agent 层优化清单

> 来源：Agent 工程岗模拟面试，7 轮深度技术审查
> 日期：2026-06-01
> 状态：待审视

---

## P0 — 功能缺陷（影响正确性）

### 1. Agent complex 路径缺少指代消解

**问题：** Agent complex 路径从入口到出口，`state["query"]` 始终是用户原始输入。当用户问"它支持持久化吗？"，Agent 用含代词的原始 query 做意图分类、规划、检索，检索质量必然下降。v1.x 路径和 Agent simple 路径都有消解，唯独 complex 路径没有。

**涉及文件：** `router.py:220-237`，`nodes.py:77`，`state.py`

**方案：**
- 在 `graph.ainvoke` 之前调用 `resolve_query_with_history`，将消解后的 query 传入 state
- AgentState 新增 `original_query` 字段，保留原始输入用于最终回答生成
- `intent_classify` 节点传入 history 参数使 `QueryAnalyzer._resolve_references` 生效
- `_llm_resolve` 改用 `_call_lightweight_llm`（glm-4-flash），不用主模型

**验收：** 构造多轮对话 case（第一轮"Redis 如何配置"，第二轮"它的持久化策略"），验证 Agent complex 路径检索到的 chunk 包含 Redis 相关内容

---

### 2. 反思节点无法校验事实正确性

**问题：** `reflect` 节点的 Prompt 只传了 `answer[:500]` 和 `len(chunks)`，没有传 chunk 原文。轻量 LLM 拿不到 source material，"事实正确性"校验形同虚设——本质是让弱模型凭自身知识盲判。

**涉及文件：** `nodes.py:221-231`

**方案：**
- 将 top 5 chunks 的内容（各截取 300 字）拼入反思 Prompt
- 回答截断从 500 字提升到 800 字
- 输出格式改为分维度评分：`{"pass": bool, "scores": {"relevance": N, "groundedness": N, "consistency": N}, "reason": "..."}`

**验收：** 用包含事实矛盾的 case（chunks 说 A，answer 说 B）验证 reflect 能检出

---

### 3. 反思重试耗尽后返回"不合格"答案且无提示

**问题：** 当 `retry_count >= max_retries`，reflect 直接跳过 LLM 评估返回 `"skip"`，router 拿到 answer 原样输出给用户。不管 answer 质量如何，用户看到的是正常回答，没有任何降级或警告。

**涉及文件：** `nodes.py:218-219`，`router.py:286-294`

**方案：**
- router 在 graph 执行完成后检查 `reflection_result`，若为 `"skip"` 或 `"parse_failed"`，在 SSE 流中输出质量提示
- 可选：reflection 失败时降级到 v1.x 重新生成（需在 event_stream 内部做流式降级）
- 在 answer 末尾追加置信度标注：`[注：此回答经多轮优化，但仍可能存在不足]`

**验收：** 构造必定触发 2 次重试仍失败的 case，验证前端能看到质量提示

---

## P1 — 架构缺陷（影响可靠性 / 可维护性）

### 4. SSE 流开始后无法降级到 v1.x

**问题：** `route_query` 的 try/except 只能捕获 `StreamingResponse` 构建前的异常。graph 执行在 `event_stream` 生成器内部（惰性求值），此时 HTTP 200 + SSE header 已发出，无法切换到另一个 `StreamingResponse`。设计文档声称的"Agent 故障自动回退 v1.x"对运行时故障实际不可达。

**涉及文件：** `router.py:48-53`，`router.py:332-334`

**方案：**
- 在进入 `_agent_stream` 前增加预检：系统水位、Agent 并发数、LLM 可用性探测（1s timeout ping）
- `graph.ainvoke` 加全局超时 `asyncio.wait_for(..., timeout=60)`
- event_stream 内部异常改为流内降级：捕获异常后用 v1.x 逻辑重新检索生成，而非直接暴露 error

**验收：** 模拟 LLM API 超时，验证用户收到的是降级后的正常回答而非 error 事件

---

### 5. 工具层重复实现 v1.x 检索逻辑，丢失查询改写

**问题：** `hybrid_search` 工具手动重写了向量搜索 + BM25 + RRF 融合（约 45 行），没有复用 `SearchService.search()`，且跳过了 `QueryAnalyzer` 的查询改写步骤。Agent 路径的检索质量理论上比 v1.x 更差。

**涉及文件：** `tools.py:34-79`

**方案：**
- 在 `SearchService` 新增 `search_with_weights()` 方法，支持自定义 vector_weight/bm25_weight
- `hybrid_search` 工具改为调用 `svc.search_with_weights()`，复用查询改写、RRF 融合、rerank 全链路
- 向量搜索和 BM25 改为 `asyncio.gather` 并行 + 独立容错（`return_exceptions=True`）

**验收：** 相同 query 对比 v1.x 和 Agent hybrid_search 的检索结果，验证一致性

---

### 6. AgentState 的 operator.add 导致跨轮次 chunk 重复

**问题：** `chunks` 和 `tools_called` 使用 `operator.add` reducer，重试时旧轮次和新轮次的结果直接拼接。去重只在单次 `execute_tools` 内部生效，跨轮次重复无法消除。`generate_answer` 拿到含重复 chunk 的数据。而 `plan` 字段是直接覆盖，同一重试循环内语义不一致。

**涉及文件：** `state.py:14-15`，`nodes.py:171-179`

**方案：**
- `chunks` 改用自定义 reducer `replace_list`（last write wins），每轮只保留最新检索结果
- `tools_called` 保持 `operator.add`（审计日志语义，记录完整执行历史）
- 在 `execute_tools` 返回时附加上 `_round_index` 标记，便于前端区分"当前轮"和"重试轮"

**验收：** 触发 1 次重试，验证 `chunks` 无重复 chunk_id

---

### 7. Protocol 接口层形同虚设

**问题：** `factory.py` 定义了 7 个 Protocol，但所有调用方直接 import 具体模块（4 处 `from app.services.vector_store import get_vector_store`），绕过了 Protocol 层。且 Protocol 定义方法名 `upsert`，实际实现是 `insert`，签名不一致从未被发现。

**涉及文件：** `factory.py`，`vector_store.py`，`documents.py`，`doc_processor.py`，`search.py`，`tools.py`

**方案（短期）：** 对齐方法名——Protocol 的 `upsert` 改为 `insert`，或实现层 `insert` 改为 `upsert`
**方案（中期）：** 所有调用方统一通过 `factory.get_vector_store()` 获取实例
**方案（长期）：** 编写针对 `VectorStoreBase` Protocol 的参数化测试（`@pytest.fixture(params=["milvus", "qdrant", "pickle"])`），新 backend 通过测试即可接入

**验收：** 替换为 Qdrant adapter 后，参数化测试全部通过

---

## P2 — 可观测性缺失（无法度量就无法优化）

### 8. 意图分类无准确率度量

**问题：** 70/30 的分流比例是估算，没有实际数据支撑。分类是纯规则，没有 LLM 兜底。没有 Prometheus 指标记录 intent=simple/complex 的分布和对应的用户反馈。

**涉及文件：** `query_analyzer.py`，`nodes.py:71-86`，`metrics.py`

**方案：**
- `metrics.py` 新增 `intent_classify_total` Counter（labels: `["intent"]`）和 `intent_classify_duration` Histogram
- `intent_classify` 节点执行后记录指标
- 前端在回答下方加"有帮助/无帮助"反馈按钮，反馈数据关联 intent 标签
- 当规则分类的置信度低（无正则命中的短 query）时，调用 glm-4-flash 做 LLM 兜底

**验收：** 运行 1 周后查看 intent 分布，验证是否接近 70/30

---

### 9. Agent 路径无成本可观测性

**问题：** "Agent 路径成本增量 ≤15%"是理论估算，没有线上验证。没有 token 计数、没有按路径拆分的 API 调用次数。重试场景的检索成本翻倍未计入。

**涉及文件：** `metrics.py`，`nodes.py`

**方案：**
- 新增 `agent_api_calls_total` Counter（labels: `["service", "path"]`）
- 新增 `agent_token_cost` Counter（labels: `["path"]`），在每个节点执行后估算并累加
- 新增 `agent_retry_total` 已定义但未使用，在 reflect 节点触发重试时 `.inc()`
- 实现设计文档提到但未落地的"规划缓存"（Redis TTL=1d，相似 query 复用 plan）

**验收：** Grafana dashboard 能看到 v1.x 和 Agent 的成本对比趋势

---

### 10. 无内存泄漏检测，本地 reranker 是内存炸弹

**问题：** 未使用 tracemalloc/memray 做过任何内存 profiling。bge-reranker-v2-m3 本地加载需 400-600MB，一旦 API 不可用触发本地 fallback，4GB 机器有 OOM 风险。`model_memory_bytes` Gauge 已定义但从未 `.set()`。

**涉及文件：** `search.py:257-277`，`degrade.py`，`metrics.py:38`

**方案：**
- 禁用本地模型
- httpx.AsyncClient 改为连接池复用单例，避免每次调用创建新 client

---

## P3 — 设计优化（提升质量，非紧急）

### 11. adjust_params 不区分失败模式

**问题：** `adjust_params` 的重试策略是统一的"扩大 top_k + 降低 vector_weight"，不管 reflect 反馈的是"回应不足"、"引用缺失"还是"事实错误"。不同失败模式应该对应不同的调整策略。

**涉及文件：** `nodes.py:252-268`

**方案：** reflect 节点输出分维度评分后，adjust_params 根据最低分维度选择策略：
- 事实不一致 → 降低 vector_weight（0.3），更多 BM25 精确匹配
- 引用不足 → 扩大 top_k（+20）
- 回应不足 → 改写 query 后重新搜索

---

### 12. 两套独立的指代消解实现应合并

**问题：** `multi_turn.py` 的 `_rule_based_resolve` 和 `query_analyzer.py` 的 `_resolve_references` 是两套几乎相同的实现，入参格式不同、替换策略略有差异，维护成本翻倍。

**涉及文件：** `multi_turn.py:45-65`，`query_analyzer.py:107-129`

**方案：** 统一为一套实现，放在 `multi_turn.py` 中，`query_analyzer.py` 调用它

---

### 13. agent_max_retries 语义 off-by-one

**问题：** 配置值 `agent_max_retries=2`，实际只允许 1 次重试。`retry_count` 在 reflect 里递增，route 用 `<` 比较，语义对不齐。

**涉及文件：** `nodes.py:218`，`graph.py:31`，`config.py:51`

**方案：** 统一语义——要么改为 `<=` 比较，要么重命名配置为 `agent_max_attempts`（总尝试次数含首次），值设为 3 表示首次 + 2 次重试

---

### 14. 降级时用户无感知

**问题：** 系统过载降级到 v1.x 时，前端没有 `agent_step` 事件，用户看不到"思考过程"。Agent 开关开着但走了 v1.x，用户以为 Agent 在工作实际是固定链路。

**涉及文件：** `router.py:42-45`

**方案：** 降级时在 SSE 流中发送提示事件：`{"type": "agent_step", "tool": "system", "thought": "Agent 因系统负载暂时降级为标准检索"}`，前端用不同颜色/图标展示

---

### 15. 设计文档未落地的特性清理

| 特性 | 设计文档声称 | 实际代码 |
|------|------------|---------|
| 规划缓存 | Redis TTL=1d，命中率 >50% | 未实现 |
| LLM 意图分类 | glm-4-flash 兜底 | 纯规则，未调用 LLM |
| Agent 并发控制 | 独立限流 | 共享 v1.x 的 100/h 桶 |
| LangGraph Checkpoint | Redis 热 + PG 冷双层 | 未实现 |
| 检索结果缓存 | — | 只有 embedding 缓存 |

**方案：** 实现和设计文档保持一致

---

## 优化优先级总览

```
P0（必须修复，影响正确性）
 ├── #1  Agent complex 路径指代消解缺失
 ├── #2  反思节点无法校验事实正确性
 └── #3  重试耗尽后返回无质量保障的答案

P1（应该修复，影响可靠性）
 ├── #4  SSE 流开始后无法降级
 ├── #5  工具层重复实现丢失查询改写
 ├── #6  operator.add 跨轮次 chunk 重复
 └── #7  Protocol 接口层形同虚设

P2（需要补齐，影响可度量性）
 ├── #8  意图分类无准确率度量
 ├── #9  Agent 路径无成本可观测性
 └── #10 无内存泄漏检测

P3（值得优化，提升整体质量）
 ├── #11 adjust_params 不区分失败模式
 ├── #12 两套指代消解实现合并
 ├── #13 max_retries 语义 off-by-one
 ├── #14 降级时用户无感知
 └── #15 设计文档未落地特性清理
```
