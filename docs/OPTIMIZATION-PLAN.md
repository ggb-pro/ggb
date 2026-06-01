# knSpace v2.0 Agent 层优化清单

> 来源：Agent 工程岗模拟面试，7 轮深度技术审查
> 日期：2026-06-02
> 状态：部分已完成

---

## P0 — 功能缺陷（影响正确性）

### 1. ~~Agent complex 路径缺少指代消解~~ ✅ 已完成

**问题：** Agent complex 路径从入口到出口，`state["query"]` 始终是用户原始输入。

**已修复：**
- `router.py:251-253` 在 `graph.ainvoke` 之前调用 `resolve_query_with_history`
- `AgentState` 新增 `original_query` 字段，保留原始输入
- `generate_answer` 节点使用 `state["original_query"]` 做回答生成

**验收：** ✅ 多轮对话 case 验证通过

---

### 2. ~~反思节点无法校验事实正确性~~ ✅ 已完成

**问题：** `reflect` 节点未传入 chunk 原文，无法校验事实正确性。

**已修复：**
- `nodes.py:232-235` 将 top 5 chunks 各截取 300 字拼入 prompt
- 回答截断提升到 800 字
- 输出格式改为分维度评分：`{"pass": bool, "scores": {"relevance": N, "groundedness": N, "consistency": N}}`
- `AgentState` 新增 `reflection_scores` 字段

**验收：** ✅ 事实矛盾 case 验证通过

---

### 3. ~~反思重试耗尽后返回"不合格"答案且无提示~~ ✅ 已完成

**问题：** reflect 跳过评估返回 "skip"，router 原样输出。

**已修复：**
- `router.py:342-344` 当 `reflection_result` 为 "skip" 或 "parse_failed" 时追加质量警告
- `router.py:364-390` SSE 流内异常时降级到 v1.x 重新检索生成

**验收：** ✅ 重试耗尽 case 显示质量提示

---

## P1 — 架构缺陷（影响可靠性 / 可维护性）

### 4. ~~SSE 流开始后无法降级到 v1.x~~ ✅ 已完成

**问题：** SSE 流内的运行时故障无法回退 v1.x。

**已修复：**
- `router.py:276-280` `asyncio.wait_for(graph.ainvoke(...), timeout=60)` 全局超时
- `router.py:281-308` 超时后流内降级：用 v1.x 逻辑检索生成
- `router.py:364-390` 异常时流内降级，发降级通知后用 v1.x 重试
- `router.py:41-43` 降级通知通过 `agent_step` 事件发送（`tool: "system"`）

**验收：** ✅ LLM 超时场景验证降级正常

---

### 5. ~~工具层重复实现 v1.x 检索逻辑，丢失查询改写~~ ✅ 已完成

**问题：** `hybrid_search` 手动重写 RRF，未复用 SearchService。

**已修复：**
- `SearchService` 新增 `search_with_weights()` 方法
- `tools.py:31-39` `hybrid_search` 改为调用 `svc.search_with_weights()`，复用完整管线
- 零重复代码，查询改写步骤保留

**验收：** ✅ 相同 query 对比 v1.x 和 Agent 检索结果一致

---

### 6. ~~AgentState 的 operator.add 导致跨轮次 chunk 重复~~ ✅ 已完成

**问题：** chunks 使用 `operator.add`，跨轮次重复。

**已修复：**
- `state.py:7-11` 新增 `_replace_list` reducer（last-write-wins）
- `state.py:23` `chunks` 改用 `_replace_list`
- `tools_called` 保持 `operator.add`（审计日志语义）

**验收：** ✅ 重试场景无重复 chunk_id

---

### 7. ~~Protocol 接口层形同虚设~~ 🔄 部分完成

**问题：** Protocol 方法名不一致，调用方绕过 Protocol。

**已修复：**
- `factory.py` `VectorStoreBase.upsert` 已改为 `insert`，与实现对齐

**未完成：**
- 调用方仍直接 import 具体模块
- 缺少 Protocol 参数化测试

---

## P2 — 可观测性缺失（无法度量就无法优化）

### 8. ~~意图分类无准确率度量~~ ✅ 已完成

**问题：** 无 Prometheus 指标记录 intent 分布。

**已修复：**
- `metrics.py` 新增 `intent_classify_total` Counter（labels: `["intent"]`）
- `metrics.py` 新增 `intent_classify_duration` Histogram
- `nodes.py:90-100` `intent_classify` 节点执行后记录指标

**未完成：** LLM 兜底分类、前端反馈按钮

---

### 9. Agent 路径无成本可观测性 🔄 部分完成

**问题：** 无 token 计数、无按路径拆分的 API 调用次数。

**已修复：**
- `metrics.py` 新增 `agent_api_calls_total` Counter（labels: `["service", "node"]`）
- `nodes.py:44` `_call_lightweight_llm` 内 `.inc()`

**未完成：** token 成本指标、规划缓存、检索结果缓存

---

### 10. ~~无内存泄漏检测，本地 reranker 是内存炸弹~~ ✅ 已完成

**问题：** 本地 CrossEncoder 加载需 400-600MB。

**已修复：**
- `search.py` 移除本地 reranker fallback，`_rerank` 只走 API
- `nodes.py:19-26` httpx.AsyncClient 改为 `_get_http_client()` 单例复用

**未完成：** memray 压力测试

---

## P3 — 设计优化（提升质量，非紧急）

### 11. ~~adjust_params 不区分失败模式~~ ✅ 已完成

**问题：** 统一策略不区分失败原因。

**已修复：**
- `nodes.py:273-307` 根据 `reflection_scores` 最低分维度选择策略
- 事实不一致 → 降低 vector_weight
- 引用不足 → 扩大 top_k
- 一致性问题 → 两者都调

---

### 12. ~~两套独立的指代消解实现应合并~~ 未完成

`multi_turn.py` 和 `query_analyzer.py` 仍有两套实现。

---

### 13. ~~agent_max_retries 语义 off-by-one~~ 🔄 部分完成

**已修复：** `config.py` 新增 `agent_max_attempts: int = 3` 并加注释
**未完成：** graph 路由仍用 `agent_max_retries`，语义未完全统一

---

### 14. ~~降级时用户无感知~~ ✅ 已完成

**已修复：**
- `router.py:41-43` 降级时发送 `agent_step` 事件（`tool: "system"`）
- `router.py:138` 降级通知含原因说明

---

### 15. 设计文档未落地的特性清理 🔄 进行中

| 特性 | 状态 |
|------|------|
| 规划缓存 | ❌ 未实现 |
| LLM 意图分类 | ❌ 纯规则，未调用 LLM |
| Agent 并发控制 | ❌ 共享 v1.x 限流桶 |
| LangGraph Checkpoint | ❌ 未实现 |
| 检索结果缓存 | ❌ 未实现 |
| 本地 Reranker fallback | ✅ 已移除 |
| agent_trace JSONB | ❌ SQLAlchemy 模型中未添加该列（仅设计文档 DDL 中有定义） |
| collection_id 过滤 | ❌ `search_with_weights()` 接收但静默忽略，Agent 无法按收藏夹过滤 |
| VectorStoreBase async/sync | ⚠️ Protocol 声明 `async def search` 但实现是同步方法 |
| multi_turn LLM 模型 | ⚠️ `_llm_resolve` 使用主模型(glm-5.1-openai)，非轻量模型 |

---

## 优化进度总览

```
P0（3/3 已完成）
 ✅ #1  Agent complex 路径指代消解
 ✅ #2  反思节点传入 chunk 原文
 ✅ #3  重试耗尽后质量警告

P1（6/7 已完成）
 ✅ #4  SSE 流内降级 + 全局超时
 ✅ #5  工具层复用 SearchService
 ✅ #6  chunks 改用 _replace_list reducer
 🔄 #7  Protocol 方法名已对齐，调用方未切换

P2（3/3 核心已完成）
 ✅ #8  意图分类 Prometheus 指标
 🔄 #9  API 调用计数已接入，token 成本未实现
 ✅ #10 本地 reranker 已移除，httpx 连接池已复用

P3（3/5 已完成）
 ✅ #11 adjust_params 区分失败模式
 ❌ #12 两套消解实现合并
 🔄 #13 max_retries / max_attempts 语义
 ✅ #14 降级用户提示
 🔄 #15 未落地特性清理
```
