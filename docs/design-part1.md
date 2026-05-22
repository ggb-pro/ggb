# 第一部分：规模分析、系统架构与技术栈选型

---

## 1. 规模分析与核心假设

### 1.1 用户模型

| 参数 | 数值 | 依据 |
|------|------|------|
| 总注册用户 | 1,000,000 | 设计目标 |
| 日活用户（DAU） | 100,000（10%） | 知识工具类产品典型 DAU 比率 |
| 付费用户占比 | 5%（50,000） | Pro + Enterprise |
| 月活用户（MAU） | 400,000（40%） | 留存曲线稳定后 |

### 1.2 数据规模估算

**用户分层模型：**

| 用户类型 | 占比 | 人均文档 | 人均分块（chunk） | 总分块数 |
|----------|------|----------|-------------------|----------|
| 重度用户 | 5%（50K） | 200 | 10,000 | 5 亿 |
| 活跃用户 | 15%（150K） | 50 | 2,500 | 3.75 亿 |
| 普通用户 | 30%（300K） | 10 | 500 | 1.5 亿 |
| 沉默用户 | 50%（500K） | 2 | 100 | 0.5 亿 |
| **合计** | **100%** | — | — | **~10 亿** |

**存储估算：**

| 数据类型 | 单条大小 | 总条数 | 总存储 | 备注 |
|----------|----------|--------|--------|------|
| 向量（bge-m3, 1024维, float32） | 4 KB | 10 亿 | ~4 TB | int8 量化后 ~1 TB |
| 分块文本 | ~500 B（平均 200 token） | 10 亿 | ~500 GB | PostgreSQL 存储 |
| 原始文件（PDF/图片/网页归档） | ~2 MB（平均） | 1,000 万 | ~20 TB | 对象存储 |
| 图片视觉向量（CLIP, 768维） | 3 KB | 5,000 万 | ~150 GB | 单独 Collection |
| 用户/会话/元数据 | — | — | ~50 GB | PostgreSQL |

### 1.3 流量模型

| 指标 | 日均值 | 峰值（3x） | 备注 |
|------|--------|-----------|------|
| 问答请求 | 1,000,000 次/天 | ~35 QPS | 100K DAU × 10 次/天 |
| 文档上传 | 200,000 次/天 | ~7 QPS | 20% DAU 上传文档 |
| 搜索/浏览请求 | 2,000,000 次/天 | ~70 QPS | 2x 问答量 |
| **总 API QPS** | **~50 QPS** | **~150 QPS** | 含所有读写操作 |

**【设计理由】为什么要精确估算规模：**
架构设计的第一步是量化。规模决定了数据库选型（pgvector 够不够）、向量数据库选型（单机还是集群）、服务拆分粒度（要不要微服务）。不量化就做选型是拍脑袋。

---

## 2. 系统整体架构

### 2.1 架构总览

```
                          ┌─────────────┐
                          │   CDN/Nginx │ 静态资源 + SSL 终结
                          └──────┬──────┘
                                 │
                          ┌──────▼──────┐
                          │  API Gateway│ APISIX: 限流/鉴权/路由
                          │  (APISIX)   │
                          └──────┬──────┘
                                 │
        ┌────────────────────────┼────────────────────────┐
        │                        │                        │
  ┌─────▼─────┐          ┌──────▼──────┐         ┌───────▼───────┐
  │ Auth Svc  │          │  API Svc    │         │  WebSocket Svc│
  │ JWT/OAuth │          │ (FastAPI)   │         │ 实时通知/状态  │
  └───────────┘          └──────┬──────┘         └───────────────┘
                                │
     ┌──────────┬───────────┬───┴───┬───────────┬──────────┐
     │          │           │       │           │          │
┌────▼───┐ ┌───▼────┐ ┌────▼───┐ ┌─▼──────┐ ┌─▼──────┐ ┌▼────────┐
│ Doc Svc│ │Search  │ │ LLM Svc│ │Embed Svc│ │Rerank  │ │Asset Svc│
│ 文档处理│ │Svc     │ │问答生成│ │嵌入服务 │ │Svc     │ │文件存储  │
└────┬───┘ │检索服务 │ └────────┘ └─────────┘ │重排序   │ └─────────┘
     │     └───┬────┘                         └─────────┘
     │         │
     │    ┌────▼─────────────────────────────┐
     │    │        数据层                      │
     │    │  ┌──────────┐  ┌───────────────┐ │
     │    │  │PostgreSQL│  │ Milvus Cluster│ │
     │    │  │+ Citus   │  │ (向量数据库)    │ │
     │    │  └──────────┘  └───────────────┘ │
     │    │  ┌──────────┐  ┌───────────────┐ │
     │    │  │Elastic-  │  │ Redis Cluster │ │
     │    │  │search    │  │ (缓存/会话)    │ │
     │    │  └──────────┘  └───────────────┘ │
     │    │  ┌──────────┐  ┌───────────────┐ │
     │    │  │MinIO/S3  │  │ Kafka Cluster │ │
     │    │  │(文件存储) │  │ (消息队列)     │ │
     │    │  └──────────┘  └───────────────┘ │
     │    └──────────────────────────────────┘
     │
     ▼
┌──────────────────────────────────────────┐
│           异步处理流水线 (Kafka)           │
│  upload → parse → chunk → embed → index  │
└──────────────────────────────────────────┘
```

### 2.2 服务拆分与职责

| 服务 | 职责 | 独立部署理由 | 扩展瓶颈 |
|------|------|-------------|----------|
| **API Gateway** (APISIX) | 限流、鉴权、路由、日志、熔断 | 所有请求入口，需要独立扩展和配置 | 网络带宽 |
| **Auth Service** | 注册/登录、JWT 签发/刷新、OAuth、权限管理 | 安全敏感，独立部署降低攻击面；认证逻辑变更频繁 | CPU（密码哈希） |
| **User Service** | 用户信息、偏好设置、配额管理 | 低频变更，与认证逻辑解耦 | 数据库 IO |
| **Collection Service** | 收藏夹/文件夹/标签 CRUD、批量操作 | 与核心 RAG 流程解耦，可独立迭代 | 数据库 IO |
| **Document Service** | 文件上传、解析调度、分块、状态管理 | 文档处理是 CPU/IO 密集型，需要独立扩展 | CPU + IO |
| **Embedding Service** | 批量向量化、缓存、模型服务 | **GPU 密集型**，必须独立扩展；模型更新不影响其他服务 | GPU 显存 |
| **Search Service** | 混合检索、RRF 融合、上下文组装 | 检索是核心路径，需要独立优化延迟 | Milvus 查询延迟 |
| **Rerank Service** | bge-reranker 推理 | **GPU 密集型**，与 Embedding 资源竞争需隔离 | GPU 显存 |
| **LLM Service** | Prompt 组装、流式调用、token 计费 | LLM 调用是最高延迟环节，需要独立的超时/重试策略 | API 限速 / GPU |
| **Asset Service** | 文件上传/下载、缩略图生成、CDN 管理 | IO 密集型，需要独立管理存储配额和生命周期 | 磁盘 IO + 带宽 |
| **Notification Service** | 处理完成通知、配额告警、邮件 | 异步、低优先级，不应阻塞主路径 | 无明显瓶颈 |

**【设计理由】为什么是这些服务而不是更少或更多：**
- 太少（单体）：10 亿向量规模下，GPU 服务和 CPU 服务混部会互相争抢资源；文档处理的高 CPU 负载会影响问答延迟
- 太多（纳米服务）：每个服务都有运维成本（部署、监控、排障）。当前拆分粒度的判断标准是：(1) 资源类型不同（CPU vs GPU vs IO），(2) 扩展节奏不同（LLM 调用量增长 vs 文档上传量增长），(3) 变更频率不同。满足至少两条才独立为服务

### 2.3 服务间通信

**同步调用 — gRPC：**
- 服务间内部调用全部使用 gRPC（非 REST）
- 原因：(1) 二进制序列化比 JSON 快 3-5x；(2) 强类型接口定义（Protobuf）防止契约漂移；(3) 内置连接管理、超时、重试
- 仅 API Gateway → 客户端使用 REST/HTTP（兼容性）

**异步通信 — Kafka：**
- 文档处理流水线使用 Kafka 事件驱动
- Topic 设计：
  ```
  doc.uploaded    → 文档上传完成，触发解析
  doc.parsed      → 解析完成，触发分块
  doc.chunked     → 分块完成，触发向量化
  doc.embedded    → 向量化完成，写入索引
  doc.completed   → 处理完成，通知用户
  doc.failed      → 处理失败，进入重试/DLQ
  ```
- 每个 Topic 32 个 partition，按 user_id hash 路由（保证同一用户的文档顺序处理）

**【设计理由】Kafka vs RabbitMQ：**
- Kafka 支持消息回溯（replay），文档处理失败时可以从任意阶段重新消费
- Kafka 的 partition 机制天然支持并行消费和有序性
- Kafka 的吞吐量（百万级 msg/s）远超 RabbitMQ（万级），满足 10 亿分块的向量化场景
- RabbitMQ 的优势（灵活路由、优先级队列）在流水线场景下用不到

### 2.4 基础设施层

| 组件 | 选型 | 用途 |
|------|------|------|
| 容器编排 | Kubernetes 1.28+ | 服务编排、自动扩缩、滚动更新 |
| 服务网格 | Istio（可选） | mTLS、流量管理、金丝雀发布 |
| CI/CD | GitHub Actions + ArgoCD | GitOps 部署，自动回归测试 |
| 密钥管理 | HashiCorp Vault | 数据库密码、API Key、JWT Secret 集中管理 |
| 服务发现 | K8s Service + DNS | 原生支持，不需要额外的 Consul/Eureka |

**【设计理由】Istio 标记为可选：**
Istio 的 sidecar 会增加 ~2ms 延迟和显著的资源开销（每个 pod 额外 ~100MB 内存）。在服务数量 < 15 的阶段，K8s 原生 Service + Ingress 足够。当需要精细化流量管理（金丝雀发布按用户比例分流、跨集群路由）时再引入。

---

## 3. 技术栈选型

### 3.1 后端技术栈

| 模块 | 选型 | 选型理由 | 考虑但放弃的方案 |
|------|------|----------|-----------------|
| **开发语言** | Python 3.11+ | RAG 生态最成熟（LlamaIndex、sentence-transformers、Unstructured 均为 Python 一等公民）；数据处理库（numpy/pandas）无缝集成；3.11+ 的 asyncio 性能接近 Go | Go（RAG 生态弱，embedding/OCR 库匮乏）；Java（生态好但开发效率低，ML 库绑定弱） |
| **Web 框架** | FastAPI | 原生 async/await、自动 OpenAPI 文档、Pydantic 校验、性能接近 Go 框架 | Django（同步框架，不适合高并发流式响应）；Flask（无异步支持、需要大量手动集成） |
| **API Gateway** | APISIX | 基于 Nginx，性能极高（单核 20K+ RPS）；原生支持限流/鉴权/可观测插件；配置热更新；中文社区活跃 | Kong（Go 插件开发门槛高，内存占用更大）；Nginx 原生（缺少动态配置和插件生态） |
| **RPC 框架** | gRPC + Protobuf | 二进制协议，延迟低；强类型约束；原生流式支持（适用于 SSE 透传） | REST（JSON 序列化开销大，无类型约束）；Thrift（生态萎缩） |
| **消息队列** | Kafka 3.x | 百万级吞吐；消息回溯能力；partition 有序性；生态成熟 | RabbitMQ（吞吐量不够，不支持回溯）；RocketMQ（阿里生态绑定，社区较小）；Pulsar（运维复杂度高） |
| **关系数据库** | PostgreSQL 16 + Citus | JSONB 存储灵活元数据；Citus 水平扩展支持 10 亿行级；pgvector 可作为向量检索的兜底方案；PostgreSQL 生态成熟 | MySQL（JSON 支持弱，全文检索不如 PG）；TiDB（过于重量级，运维成本高）；MongoDB（事务支持弱，RAG 场景需要关系查询） |
| **向量数据库** | Milvus 2.4 Cluster | **10 亿级向量唯一能稳定支撑的开源方案**；支持动态字段、分区隔离、多向量类型；HNSW/IVF 索引可选；云原生架构 | Qdrant（单机性能好但集群方案不成熟，10 亿向量未经验证）；Weaviate（内存占用大，扩展性差）；pgvector（千万级以上性能急剧下降） |
| **全文检索** | Elasticsearch 8.x | BM25 + 中文分词（ik_max_word）成熟；聚合分析能力强；支持向量检索（可作 fallback） | PostgreSQL FTS（中文分词弱，性能在千万文档级不够）；Meilisearch（不支持 BM25 自定义，扩展性差） |
| **缓存** | Redis 7 Cluster | 支持 Stream（可作轻量队列）、Sorted Set（排行榜）、Hash（用户状态）；集群模式支持水平扩展；Lua 脚本实现原子操作 | Memcached（无数据结构、无持久化、无集群原生支持） |
| **对象存储** | MinIO（自建）/ S3（云端） | S3 兼容 API，可无缝切换；MinIO 支持纠删码、多租户、生命周期管理 | Ceph（运维过于复杂）；本地文件系统（无高可用、无弹性） |
| **文档解析** | Unstructured.IO + 自定义解析器 | 统一接口支持 20+ 格式；支持保留文档结构；自定义为中文 PDF 补充 PyMuPDF + PaddleOCR | Apache Tika（中文支持差，结构丢失严重）；纯 PyMuPDF（不支持 Word/PPT/Excel） |
| **OCR** | PaddleOCR Server | 中文 OCR 效果最好；支持表格识别；Server 模式支持高并发 | Tesseract（中文效果差）；Qwen-VL（成本高，适合复杂图文，不适合批量 OCR） |
| **嵌入模型** | bge-m3 (BAAI) | 多语言（中英日韩）效果最佳；输出 1024 维，信息密度高；支持 dense + sparse 双向量；开源可自部署 | OpenAI text-embedding-3（成本高，10 亿向量 API 调用费 ~$200K/年，且数据出境）；E5（中文效果不如 bge） |
| **重排序模型** | bge-reranker-v2-m3 | 与 bge-m3 配套优化；跨语言支持；长上下文（8192 token） | Cohere Rerank（API 调用，成本高）；Cross-Encoder 微调（需要标注数据，初期不现实） |
| **大语言模型** | 主：DeepSeek V3 API；备：自部署 Qwen2-72B | DeepSeek V3：中文最强，成本 ¥1/M token（GPT-4o 的 1/50），64K 上下文；Qwen2-72B 作为 fallback，自部署在 4×A100 上 | GPT-4o（成本高 30-50x，且数据出境）；Claude（同上）；Llama 3（中文效果差） |
| **任务调度** | Celery + Redis（轻量） + Kafka Consumer（重量） | Celery 处理简单异步任务（发邮件、生成缩略图）；Kafka Consumer 处理重量级流水线（文档解析、批量嵌入） | 纯 Celery（10 亿级任务场景下 Redis 队列会成为瓶颈）；纯 Kafka（轻量任务杀鸡用牛刀） |
| **监控** | Prometheus + Grafana | 事实标准，K8s 原生集成；PromQL 查询灵活；Grafana 仪表盘生态丰富 | Datadog（商业产品，成本高）；Zabbix（不支持云原生动态发现） |
| **日志** | Loki | 与 Grafana 原生集成；不对日志内容建索引（仅标签索引），存储成本低；适合结构化 JSON 日志 | ELK（Elasticsearch 建全量索引，存储成本是 Loki 的 5-10x；运维复杂，需要独立团队维护） |
| **链路追踪** | OpenTelemetry + Jaeger | OpenTelemetry 是 CNCF 标准，与 K8s/Prometheus/Grafana 生态打通；Jaeger 轻量，支持采样 | SkyWalking（Java 生态为主，Python SDK 不成熟）；Zipkin（功能较弱，社区萎缩） |

### 3.2 前端技术栈

| 模块 | 选型 | 选型理由 |
|------|------|----------|
| 框架 | React 18 + TypeScript | 生态最大；类型安全；团队招聘容易 |
| UI 组件 | Ant Design 5 | 企业级组件库，表格/表单/树形控件开箱即用；中文友好 |
| 状态管理 | Zustand | 比 Redux 轻量 10x；TypeScript 支持好；无 boilerplate |
| 流式渲染 | fetch + ReadableStream（SSE） | **核心 UX 需求**：LLM 生成 3-10 秒，必须逐 token 渲染；SSE 比 WebSocket 简单，单向推送足够 |
| 文档预览 | PDF.js / docx-preview / sheetjs | 按需加载，不在首屏打包 |
| 编辑器 | Tiptap | 基于 ProseMirror，可扩展性强；支持 Markdown 快捷输入 |
| 可视化 | ECharts | 检索过程可视化（相似度分布、召回率图表） |

### 3.3 GPU 资源规划

| 服务 | 模型 | GPU 需求 | 部署方式 |
|------|------|----------|----------|
| Embedding Service | bge-m3 | 1× A10 (24GB) | vLLM/Triton，批量推理 |
| Rerank Service | bge-reranker-v2-m3 | 1× A10 (24GB) | Triton，批量推理 |
| LLM Fallback | Qwen2-72B | 4× A100 (80GB) | vLLM，流式推理 |
| OCR | PaddleOCR Server | 1× T4 (16GB) | PaddlePaddle Serving |

**GPU 利用率优化：**
- Embedding 和 Rerank 可在非高峰期共享同一块 GPU（分时复用）
- 使用动态批处理（dynamic batching）：积累请求到 batch_size=64 再推理
- GPU 闲时可用于离线评估任务

**【设计理由】GPU 规划：**
GPU 是最昂贵的资源（A100 约 ¥30K/月）。必须精确规划避免浪费。Embedding 用 A10 而非 A100，因为 bge-m3 模型只有 ~2GB，A10 的 24GB 显存足够跑 batch=64。LLM Fallback 方案只在 API 不可用时启用，平时 GPU 可以缩容到 0。

---

## 3.4 技术栈全景图

```
┌──────────────────────────────────────────────────────────────────┐
│                        Frontend (React 18)                       │
│  Ant Design 5 · Zustand · Tiptap · ECharts · SSE Streaming     │
└────────────────────────────┬─────────────────────────────────────┘
                             │ HTTPS / SSE
┌────────────────────────────▼─────────────────────────────────────┐
│                    API Gateway (APISIX)                           │
│         Rate Limiting · JWT Auth · Routing · Logging             │
└────────────────────────────┬─────────────────────────────────────┘
                             │ gRPC
┌────────────────────────────▼─────────────────────────────────────┐
│                    Business Services (FastAPI)                    │
│  Auth · User · Collection · Document · Search · LLM · Asset      │
│                    Notification · Billing                         │
└──┬─────────┬──────────┬──────────┬──────────┬──────────┬────────┘
   │         │          │          │          │          │
   ▼         ▼          ▼          ▼          ▼          ▼
┌──────┐ ┌──────┐ ┌────────┐ ┌──────┐ ┌──────┐ ┌──────────┐
│ PG   │ │Milvus│ │Elastic │ │Redis │ │MinIO │ │  Kafka   │
│Citus │ │Cluster│ │search  │ │Cluster│ │/S3   │ │ Cluster  │
└──────┘ └──────┘ └────────┘ └──────┘ └──────┘ └──────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    GPU Services                                   │
│  Embedding (bge-m3, A10) · Rerank (bge-reranker, A10)           │
│  LLM Fallback (Qwen2-72B, 4×A100) · OCR (PaddleOCR, T4)        │
└──────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────┐
│                    Infrastructure (Kubernetes)                    │
│  Prometheus · Grafana · Loki · Jaeger · Vault · ArgoCD          │
└──────────────────────────────────────────────────────────────────┘
```
