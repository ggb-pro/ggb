# 第五部分：可观测性、性能SLA与分阶段交付

---

## 12. 可观测性与运维设计

### 12.1 监控指标体系

#### API 层指标

| 指标名 | 类型 | 采集方式 | 告警阈值 |
|--------|------|----------|----------|
| `http_requests_total` | Counter | APISIX → Prometheus | — |
| `http_request_duration_seconds` | Histogram | APISIX → Prometheus | P95 > 3s |
| `http_requests_errors_total` | Counter | APISIX → Prometheus | 5xx rate > 1% |
| `active_connections` | Gauge | APISIX | > 10K/节点 |
| `rate_limit_rejected_total` | Counter | APISIX | 单用户 > 10/min |

#### RAG 流水线指标（最关键）

| 指标名 | 含义 | 告警条件 |
|--------|------|----------|
| `rag_retrieval_duration_seconds` | 向量检索耗时（含 Milvus 查询） | P95 > 200ms |
| `rag_retrieval_results_count` | 每次检索返回的 chunk 数 | 中位数 < 3（检索质量下降） |
| `rag_rerank_duration_seconds` | 重排序耗时 | P95 > 300ms |
| `rag_rerank_top_score` | 重排序最高分 | P50 < 0.3（检索召回质量问题） |
| `rag_rerank_score_distribution` | 重排序分数分布（直方图） | 分布严重左偏 |
| `rag_llm_ttft_seconds` | Time To First Token | P95 > 2s |
| `rag_llm_total_duration_seconds` | LLM 总生成时间 | P95 > 5s |
| `rag_llm_tokens_input` | 输入 token 数 | 单次 > 30K（上下文过长） |
| `rag_llm_tokens_output` | 输出 token 数 | — |
| `rag_context_citations_used` | 回答中实际引用的 chunk 数 | 中位数 = 0（幻觉风险） |
| `rag_user_feedback_thumb_up` | 用户点赞数 | — |
| `rag_user_feedback_thumb_down` | 用户点踩数 | 点踩率 > 10% |

#### 文档处理流水线指标

| 指标名 | 含义 | 告警条件 |
|--------|------|----------|
| `pipeline_total_duration_seconds` | 端到端处理时间 | P95 > 5min |
| `pipeline_stage_duration_seconds` | 各阶段耗时（parse/chunk/embed/index） | 任意阶段 P95 > 2min |
| `pipeline_failure_total` | 处理失败数 | 失败率 > 5% |
| `kafka_consumer_lag` | 各 topic 消费积压 | 积压 > 10K 持续 10min |
| `embedding_batch_size` | 嵌入批处理大小 | 平均 < 10（GPU 利用率低） |
| `embedding_cache_hit_rate` | 嵌入缓存命中率 | < 5% |

#### 基础设施指标

| 指标名 | 告警阈值 |
|--------|----------|
| `node_cpu_usage` | > 80% 持续 5min |
| `node_memory_usage` | > 90% |
| `node_disk_io_utilization` | > 90% 持续 10min |
| `gpu_memory_usage` | > 95% |
| `gpu_utilization` | < 20% 持续 30min（缩容信号） |
| `pg_connection_pool_usage` | > 80% |
| `redis_memory_usage` | > 85% |
| `milvus_query_latency` | P99 > 1s |

### 12.2 Dashboard 设计

**Dashboard 1：Executive（管理驾驶舱）**
```
┌──────────────┬──────────────┬──────────────┬──────────────┐
│  DAU         │  月收入      │  系统可用性   │  月成本      │
│  100,234     │  ¥139 万     │  99.95%      │  ¥10.2 万    │
│  ↑ 5.2%     │  ↑ 12%      │              │  ↑ 3%        │
└──────────────┴──────────────┴──────────────┴──────────────┘
┌──────────────────────────────────────────────────────────┐
│  日活趋势（30天折线图）                                     │
│  ───────────────╱╲──╱╲──────────────────                │
└──────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────┐
│  收入 vs 成本趋势（双轴折线图）                              │
└──────────────────────────────────────────────────────────┘
```

**Dashboard 2：RAG Quality（质量监控）**
```
┌──────────────┬──────────────┬──────────────┬──────────────┐
│  检索 P95    │  Rerank P95  │  TTFT P95    │  满意度      │
│  180ms       │  250ms       │  1.8s        │  87% 👍      │
└──────────────┴──────────────┴──────────────┴──────────────┘
┌────────────────────────┬─────────────────────────────────┐
│  Rerank 分数分布         │  引用数分布                      │
│  ▓▓▓▓▓▓▓▓▓▓░░░         │  0: ▓▓░                        │
│  0.0 ──────── 1.0      │  1-3: ▓▓▓▓▓▓▓                  │
│                         │  4+: ▓▓▓▓                      │
└────────────────────────┴─────────────────────────────────┘
┌──────────────────────────────────────────────────────────┐
│  检索延迟趋势（P50/P95/P99 三条线）                         │
└──────────────────────────────────────────────────────────┘
```

**Dashboard 3：Infrastructure（基础设施）**
- 各服务 CPU/内存使用率热力图
- GPU 利用率趋势
- 数据库连接池使用率
- Kafka 消费延迟趋势
- 存储使用趋势 + 预测（多久后满）

### 12.3 告警规则

```yaml
# Prometheus alerting rules
groups:
  - name: rag-critical
    rules:
      - alert: ServiceDown
        expr: up{job=~"rag-.*"} == 0
        for: 2m
        labels:
          severity: P1
          channel: pagerduty
        annotations:
          summary: "Service {{ $labels.job }} is down"

      - alert: HighErrorRate
        expr: rate(http_requests_errors_total{status=~"5.."}[5m]) / rate(http_requests_total[5m]) > 0.05
        for: 3m
        labels:
          severity: P1
          channel: pagerduty

      - alert: RetrievalLatencyHigh
        expr: histogram_quantile(0.95, rag_retrieval_duration_seconds) > 0.5
        for: 5m
        labels:
          severity: P2
          channel: slack

      - alert: KafkaConsumerLag
        expr: kafka_consumer_lag > 10000
        for: 10m
        labels:
          severity: P2
          channel: slack

      - alert: CostAnomaly
        expr: daily_cost > avg_over_week(daily_cost) * 1.2
        labels:
          severity: P3
          channel: slack

      - alert: RerankQualityDegraded
        expr: avg(rag_rerank_top_score) < 0.3
        for: 30m
        labels:
          severity: P3
          channel: slack
```

### 12.4 日志设计（Loki）

**结构化日志格式：**

```json
{
  "timestamp": "2024-01-15T10:30:00.123Z",
  "level": "INFO",
  "service": "search-service",
  "trace_id": "abc123",
  "span_id": "def456",
  "user_id": "u-789",
  "request_id": "req-012",
  "message": "hybrid search completed",
  "duration_ms": 180,
  "retrieval": {
    "dense_count": 28,
    "sparse_count": 25,
    "fused_count": 40,
    "reranked_count": 10,
    "top_score": 0.87
  }
}
```

**【设计理由】Loki vs ELK：**
- ELK 的 Elasticsearch 需要对所有日志建全文索引，10 亿条日志的索引大小是原始数据的 50-100%
- Loki 只对标签（labels）建索引，日志内容压缩存储，存储成本是 ELK 的 1/5-1/10
- 在 Grafana 中统一查看 Prometheus 指标 + Loki 日志，不需要切换工具
- 本项目日志主要是结构化 JSON，通过标签过滤（service, level, trace_id）足够定位问题，不需要全文搜索

### 12.5 分布式链路追踪

**OpenTelemetry 配置：**

```python
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

# 采样策略
sampler = DynamicSampler(
    error_rate=1.0,      # 100% 错误请求
    slow_rate=0.1,       # 10% 慢请求（> P95）
    normal_rate=0.01     # 1% 正常请求
)

provider = TracerProvider(sampler=sampler)
trace.set_tracer_provider(provider)
```

**RAG 请求链路示例：**

```
Trace: req-abc123
├── Span: api.chat (3500ms)
│   ├── Span: query_analyze (8ms)
│   ├── Span: hybrid_search (180ms)
│   │   ├── Span: dense_search_milvus (45ms)
│   │   ├── Span: sparse_search_es (35ms)
│   │   └── Span: rrf_fusion (2ms)
│   ├── Span: rerank (250ms)
│   ├── Span: context_assemble (5ms)
│   └── Span: llm_generate (3000ms)
│       ├── Span: llm_ttft (500ms)
│       └── Span: llm_streaming (2500ms)
```

**【设计理由】采样策略：**
- 100% 采集：成本太高（100K DAU × 10 次 = 100 万 trace/天），Jaeger 存储压力大
- 1% 采样：可能错过关键错误
- 分层采样：错误全采、慢请求多采、正常少采，在可观测性和成本间取得平衡

### 12.6 RAG 效果评估体系（核心运维系统）

#### 离线评估

```python
class RAGEvaluator:
    """定期评估 RAG 流水线效果"""

    def __init__(self):
        self.golden_dataset = self.load_golden()  # 500+ 标注数据

    async def evaluate_retrieval(self, pipeline_config: dict) -> RetrievalMetrics:
        results = []
        for sample in self.golden_dataset:
            retrieved = await self.search(pipeline_config, sample.query)
            results.append({
                "query": sample.query,
                "relevant_ids": sample.relevant_chunk_ids,  # 人工标注的相关 chunk
                "retrieved_ids": [r.chunk_id for r in retrieved],
            })

        return RetrievalMetrics(
            recall_at_5=self.calc_recall(results, k=5),
            recall_at_10=self.calc_recall(results, k=10),
            mrr=self.calc_mrr(results),
            ndcg_at_10=self.calc_ndcg(results, k=10),
        )

    async def evaluate_generation(self, pipeline_config: dict) -> GenerationMetrics:
        """使用 RAGAS 框架评估生成质量"""
        # 1. 用当前 pipeline 生成回答
        qa_pairs = []
        for sample in self.golden_dataset:
            answer = await self.generate(pipeline_config, sample.query, sample.context)
            qa_pairs.append({"q": sample.query, "a": answer, "ctx": sample.context})

        # 2. RAGAS 评估
        return GenerationMetrics(
            faithfulness=ragas_faithfulness(qa_pairs),       # 回答是否忠于上下文
            answer_relevancy=ragas_relevancy(qa_pairs),      # 回答是否切题
            context_utilization=ragas_utilization(qa_pairs),  # 上下文利用率
        )
```

**Golden Dataset 构建：**
- 初始：人工标注 500 组 (query, relevant_chunks, expected_answer)
- 持续扩充：从生产环境采样用户反馈（点踩的回答 + 对应的检索结果）→ 人工审核后加入
- 每季度扩充 200 组，保持数据集与实际用户查询分布一致

#### 在线评估

| 指标 | 采集方式 | 计算方式 |
|------|----------|----------|
| 用户满意度 | 点赞/点踩 | thumb_up / (thumb_up + thumb_down) |
| 追问率 | 会话行为 | 有第 2 轮追问的会话占比 |
| 改写率 | 会话行为 | 用户重新表述问题的比例 |
| 会话放弃率 | 会话行为 | 只问 1 轮就离开的会话占比 |
| 引用点击率 | UI 行为 | 用户点击引用查看原文的比例 |

#### 质量门禁

```yaml
# 每个 PR 合入前自动运行
rag_quality_gate:
  retrieval:
    recall_at_10: ">= 0.80"        # 召回率不得低于 80%
    recall_regression: "<= 0.02"   # 回归不超过 2%
  generation:
    faithfulness: ">= 0.85"         # 忠实度不低于 85%
    answer_relevancy: ">= 0.80"     # 相关性不低于 80%
  latency:
    retrieval_p95: "<= 0.2s"        # 检索延迟不恶化
    total_p95: "<= 5s"              # 端到端延迟不恶化
```

**【设计理由】为什么评估体系是最重要的运维系统：**
- 没有评估就没有优化：不知道当前 recall@10 是 60% 还是 90%，就无法判断是否需要优化
- 防止回归：一个"优化"可能提升了一种查询类型的效果但损害了另一种，只有自动化评估能捕获
- 数据驱动决策：chunk_size 从 512 改为 256 是否值得？跑一次评估就知道了

---

## 13. 性能目标与 SLA

### 13.1 SLA 定义

| 指标 | P50 | P95 | P99 | 测量方式 |
|------|-----|-----|-----|----------|
| **问答端到端（首 token）** | < 1.5s | < 3s | < 5s | API Gateway → TTFT |
| **问答端到端（完整回答）** | < 3s | < 6s | < 10s | API Gateway → last token |
| **向量检索** | < 50ms | < 100ms | < 200ms | Milvus search 请求 |
| **重排序** | < 100ms | < 200ms | < 300ms | Rerank service |
| **文档处理** | < 30s | < 2min | < 5min | 上传 → 可搜索 |
| **API 可用性** | — | — | — | 99.9%（月度） |
| **数据持久性** | — | — | — | 99.9999%（年度） |

**【设计理由】各项指标的制定依据：**

- **问答 TTFT P95 < 3s**：用户心理研究表明，3 秒内看到首个 token 感知为"即时响应"，超过 5 秒开始焦虑。SSE 流式输出使得用户在 1.5 秒内就能看到回答开始生成，感知延迟远低于实际完成时间
- **检索 P95 < 100ms**：检索是同步操作，用户等待时间直接计入 TTFT。100ms 是单用户分区搜索（< 100 万向量）的合理上限
- **重排序 P95 < 200ms**：bge-reranker-v2-m3 在 GPU 上对 40 条输入的推理时间约 100-150ms，加上网络开销 200ms 足够
- **可用性 99.9%**：99.99%（4 个 9）需要多活数据中心，成本增加 3-5x，对知识库产品不必要

### 13.2 容量规划

| 阶段 | DAU | 总向量 | QPS 峰值 | GPU | 计算节点 | DB 节点 |
|------|-----|--------|----------|-----|----------|---------|
| **当前** | 10K | 5 千万 | 15 | 2 GPU | 8 | PG: 8 + Milvus: 3 + ES: 3 |
| **6 个月** | 100K | 5 亿 | 150 | 4 GPU | 20 | PG: 33 + Milvus: 6 + ES: 3 |
| **12 个月** | 500K | 20 亿 | 500 | 8 GPU | 40 | PG: 64 + Milvus: 12 + ES: 6 |
| **18 个月** | 1M | 50 亿 | 1000 | 16 GPU | 80 | PG: 128 + Milvus: 20 + ES: 9 |

### 13.3 性能测试策略

| 测试类型 | 工具 | 频率 | 场景 |
|----------|------|------|------|
| 负载测试 | Locust | 每次发版 | 模拟正常 QPS 的 3x 压力，持续 30min |
| 浸泡测试 | Locust | 每月 | 1x QPS 持续 24h，检测内存泄漏 |
| 峰值测试 | k6 | 每月 | 10x QPS 持续 5min，验证自动扩缩 |
| RAG 基准测试 | 自研 | 每次检索变更 | 在固定数据集上对比检索质量 |
| 混沌测试 | Chaos Mesh | 每季度 | 随机杀服务/网络延迟，验证降级策略 |

---

## 14. 分阶段交付计划

### Phase 0：基础设施（第 1-2 月，3 人）

**范围：**
- K8s 集群搭建（开发/staging/生产）
- CI/CD Pipeline（GitHub Actions → ArgoCD）
- 监控基线（Prometheus + Grafana + Loki）
- 认证服务（JWT + OAuth）
- PostgreSQL 核心表 + 基础 CRUD API

**交付物：** 用户可注册、登录、管理个人信息

**团队：** 1 后端 + 1 前端 + 1 DevOps

**验收标准：**
- CI/CD 端到端 < 15min
- 注册-登录流程可走通
- 监控 Dashboard 可用

### Phase 1：核心 RAG（第 2-4 月，5 人）

**范围：**
- 文档上传（仅文本：PDF、Word、Markdown）
- 文档处理流水线（同步，无 Kafka）
- 基础分块（RecursiveCharacterTextSplitter）
- bge-m3 嵌入服务
- Milvus 集成（单节点）
- 基础向量检索（无 BM25、无 rerank）
- DeepSeek V3 API 集成
- SSE 流式响应
- 引用溯源
- 收藏夹管理（文件夹、标签）

**交付物：** 用户可上传文档、提问、获得带引用的流式回答

**团队：** 2 后端 + 2 前端 + 1 ML

**验收标准：**
- 端到端 RAG 流程可走通
- TTFT < 3s（P95）
- 引用可点击跳转到原文

**风险：**
| 风险 | 概率 | 缓解 |
|------|------|------|
| Milvus 学习曲线 | 中 | Phase 1 用单节点，降低运维复杂度 |
| 中文 PDF 解析质量差 | 高 | 准备 PaddleOCR fallback |

### Phase 2：质量与规模（第 4-6 月，6 人）

**范围：**
- Kafka 异步流水线（替换同步处理）
- Elasticsearch 全文检索
- 混合检索（向量 + BM25）
- RRF 融合
- bge-reranker 重排序
- 查询理解（规则分类 + 查询改写）
- RAG 评估 Pipeline（golden dataset + RAGAS）
- Redis 缓存层
- Citus 分片上线
- 配额管理

**交付物：** 生产级检索质量，评估体系建立

**团队：** 2 后端 + 2 前端 + 1 ML + 1 DevOps

**验收标准：**
- Recall@10 >= 80%（golden dataset）
- 100 用户并发压力测试通过
- 文档处理延迟 P95 < 2min

**【设计理由】Phase 1 先跑通全链路，Phase 2 再优化质量：**
- 没有端到端可运行的系统就无法收集真实用户反馈
- 过早优化检索质量是浪费——不知道用户的真实查询分布
- 先用简单的向量检索建立 baseline，再通过评估数据驱动优化

### Phase 3：多模态（第 6-8 月，5 人）

**范围：**
- 图片处理流水线（OCR + CLIP）
- 网页处理流水线（Playwright + Trafilatura）
- 跨模态检索
- 多轮对话（上下文管理 + 指代消解）
- Prompt 优化（基于评估数据迭代）
- Milvus 集群化（多 Query Node）

**交付物：** 多模态支持，多轮对话

**团队：** 2 后端 + 1 前端 + 1 ML + 1 DevOps

### Phase 4：商业化（第 8-10 月，5 人）

**范围：**
- 支付系统（Pro/Enterprise 套餐）
- 模型路由（简单问题 → 小模型）
- Prompt 缓存优化（降低 LLM 成本）
- 团队知识库（共享收藏、协作）
- API 开放平台（第三方集成）
- 移动端适配

**交付物：** 可收费的完整产品

**团队：** 2 后端 + 1 前端 + 1 ML + 1 产品

### Phase 5：规模化（第 10-12 月，5 人）

**范围：**
- 水平扩展到 100K DAU
- GPU 动态调度
- 冷热数据分层
- 多区域部署准备
- 知识图谱增强 RAG（实验性）
- 高级分析 Dashboard

**交付物：** 可支撑 100K DAU 的稳定系统

### Phase 6：百万用户（第 12-18 月，持续）

**范围：**
- Citus 扩展到 64 分片
- Milvus 扩展到 20+ Query Node
- 全链路优化（延迟、成本）
- 24/7 On-call 体系
- 国际化

---

## 附录：核心设计决策汇总

| # | 决策点 | 选择 | 放弃的方案 | 核心理由 |
|---|--------|------|-----------|----------|
| 1 | 系统架构 | 微服务（12 个服务） | 单体 / 纳米服务 | GPU/CPU/IO 资源类型不同，需独立扩展 |
| 2 | 开发语言 | Python 3.11+ | Go / Java | RAG 生态最成熟，embedding/OCR 库无替代 |
| 3 | API Gateway | APISIX | Kong / Nginx | 性能高，配置热更新，中文社区 |
| 4 | 内部通信 | gRPC | REST | 二进制序列化快 3-5x，强类型约束 |
| 5 | 消息队列 | Kafka | RabbitMQ / Pulsar | 吞吐量高，支持消息回溯，文档重处理需要 |
| 6 | 关系数据库 | PostgreSQL + Citus | MySQL / TiDB | JSONB、全文检索、pgvector fallback |
| 7 | 向量数据库 | Milvus 2.4 Cluster | Qdrant / Weaviate / pgvector | 10 亿向量唯一验证过的开源方案 |
| 8 | 全文检索 | Elasticsearch | PostgreSQL FTS | 中文分词好，性能在千万级不退化 |
| 9 | 缓存 | Redis Cluster | Memcached | 数据结构丰富，支持集群 |
| 10 | 嵌入模型 | bge-m3 | OpenAI / E5 | 中英双语最佳，开源可自部署，无数据出境 |
| 11 | 重排序模型 | bge-reranker-v2-m3 | Cohere / 自训练 | 与 bge-m3 配套，开源 |
| 12 | LLM | DeepSeek V3 API + Qwen2 备用 | GPT-4o / Claude | 中文强，成本 1/50，64K 上下文 |
| 13 | 分块策略 | 结构感知递归分块 | 语义分块 | 效果差 < 5%，成本低一个数量级 |
| 14 | 分块大小 | 512 tokens | 256 / 1024 | bge-m3 最优输入，召回/精确率平衡 |
| 15 | 检索融合 | RRF (k=60) | 固定加权 (0.6/0.4) | 尺度无关，无需调参 |
| 16 | 候选数量 | Top 40 → rerank → Top 10 | Top 20 → 5 / Top 100 → 20 | 质量和成本的最佳平衡 |
| 17 | 多租户隔离 | 共享+分区+RLS | DB-per-tenant / Schema-per-tenant | 成本最低，100 万用户唯一可行方案 |
| 18 | 向量索引 | HNSW (M=16, ef=256) | IVF_FLAT / PQ | 延迟稳定，召回率高，单分区内存可控 |
| 19 | 日志系统 | Loki | ELK | 存储成本 1/5，与 Grafana 原生集成 |
| 20 | 流式响应 | SSE | WebSocket | 单向推送足够，HTTP 兼容性好 |
| 21 | 查询理解 | 规则+小模型 | LLM | 零额外成本，延迟 < 10ms |
| 22 | 配额管理 | Redis 原子计数 + PG 对账 | 纯 PG / 纯 Redis | Redis 快 + PG 准确，互补 |
| 23 | 容灾方案 | 共享集群 + 逻辑隔离 | 物理隔离 | 成本可控，RLS 保证安全 |
| 24 | GPU 调度 | 分时复用 + 动态扩缩 | 固定分配 | 成本节省 40%+ |
| 25 | LLM 成本优化 | Prompt 缓存 + 模型路由 + 热缓存 | 无优化 | LLM 占总成本 33%，优化 ROI 最高 |
