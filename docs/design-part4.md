# 第四部分：扩展性、可靠性与成本模型

---

## 9. 扩展性设计

### 9.1 水平扩展策略

#### 各服务扩展特性

| 服务 | 扩展类型 | 扩展瓶颈 | 最小副本 | 峰值副本 | 扩展触发条件 |
|------|----------|----------|----------|----------|-------------|
| API Gateway (APISIX) | 无状态 | 网络带宽 | 2 | 10+ | 连接数 > 10K/节点 |
| Auth Service | 无状态 | CPU（密码哈希） | 2 | 5 | CPU > 70% |
| Document Service | 无状态 | CPU + IO | 3 | 10 | Kafka consumer lag > 1000 |
| Embedding Service | GPU 密集 | GPU 显存 | 1 GPU | 8 GPU | Kafka lag > 5000 |
| Search Service | 无状态（依赖 Milvus） | Milvus QPS | 3 | 10 | 请求延迟 P95 > 300ms |
| Rerank Service | GPU 密集 | GPU 显存 | 1 GPU | 4 GPU | 队列深度 > 20 |
| LLM Service | 网络（API 调用） | API 限速 | 3 | 10 | 请求排队 > 10 |
| Asset Service | 无状态 | 磁盘 IO + 带宽 | 2 | 5 | 上传/下载排队 |

#### GPU 服务扩展方案

```yaml
# K8s HPA 配置示例 — Embedding Service
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: embedding-service-hpa
spec:
  scaleTargetRef:
    apiVersion: apps/v1
    kind: Deployment
    name: embedding-service
  minReplicas: 1
  maxReplicas: 8
  metrics:
    - type: External
      external:
        metric:
          name: kafka_consumer_lag
          selector:
            matchLabels:
              topic: doc.chunked
              group: embed_worker
        target:
          type: AverageValue
          averageValue: "1000"  # 每个 pod 积压 < 1000 条
  behavior:
    scaleDown:
      stabilizationWindowSeconds: 600  # 缩容冷却 10 分钟
      policies:
        - type: Percent
          value: 50  # 每次最多缩 50%
          periodSeconds: 300
    scaleUp:
      stabilizationWindowSeconds: 60
      policies:
        - type: Percent
          value: 100  # 可以翻倍扩容
          periodSeconds: 60
```

**GPU 节点调度策略：**
- 使用 K8s GPU sharing（时间片）：单块 GPU 可调度多个小模型
- Embedding (bge-m3 ~2GB) + Reranker (~1.5GB) 可共享一块 A10（24GB）
- LLM Fallback (Qwen2-72B) 独占 4×A100，通过 K8s resource request 保证独占

**【设计理由】动态扩缩 vs 固定资源：**
- GPU 成本是 CPU 的 5-10 倍，必须按需分配
- 文档上传有明显的早晚高峰（工作时间 vs 凌晨），GPU 利用率波动大
- 凌晨低谷期可以将 GPU 缩容到 1 块，节省 70%+ GPU 成本

### 9.2 数据库扩展

#### PostgreSQL + Citus 扩展方案

```
                  ┌─────────────────┐
                  │  PgBouncer      │  连接池（transaction mode）
                  │  1000 max conn  │
                  └────────┬────────┘
                           │
              ┌────────────┼────────────┐
              │            │            │
     ┌────────▼───┐ ┌──────▼───┐ ┌──────▼───┐
     │  Coordinator│ │ Worker 1 │ │ Worker 2 │  ... 32 Workers
     │  (路由/协调) │ │ Shard    │ │ Shard    │
     │  4C/16GB   │ │ 0,32,64..│ │ 1,33,65..│
     └────────────┘ │ 4C/32GB  │ │ 4C/32GB  │
                    └──────────┘ └──────────┘
```

**Citus 配置：**
- 32 个 Worker 节点（分片）
- 每个 Worker：4 vCPU + 32 GB RAM + 500 GB SSD
- Coordinator：4 vCPU + 16 GB RAM（只做路由，不存数据）
- 每个分片约：100 万用户 / 32 = ~3.1 万用户的数据

**【设计理由】32 分片：**
- 每个分片存储：chunks 10 亿/32 ≈ 3100 万行，messages 2 亿/32 ≈ 625 万行
- 3100 万行在单 PG 实例上完全可控，查询延迟 < 50ms
- 32 是 2 的幂，方便 hash 取模
- 未来如果需要扩展到 64 分片，Citus 支持在线分片再平衡（reshard）

**读扩展：**
- 每个分片配置 2 个读副本（streaming replication）
- 写操作走主节点，读操作（列表查询、搜索）走读副本
- 预估读写比：8:2（RAG 系统读远多于写）

#### Milvus Cluster 扩展

| 扩展维度 | 方式 | 触发条件 |
|----------|------|----------|
| 查询吞吐 | 增加 Query Node | QPS 导致 P99 > 500ms |
| 写入吞吐 | 增加 Data Node | 入库延迟 > 10s |
| 存储容量 | 增加 MinIO 节点 | 存储使用 > 80% |
| 索引构建 | 增加 Index Node | 索引构建排队 > 5min |

**关键参数：**
- `search_timeout`: 5s（超时返回部分结果）
- `topk_limit`: 16384（单次搜索最大返回数）
- `segment_max_size`: 512MB（段大小，影响索引效率）

#### Elasticsearch 扩展

- 3 节点集群（每节点 8 vCPU + 32 GB RAM + 1 TB SSD）
- 5 主分片 + 1 副本（10 亿 chunks 均分到 5 分片，每分片 ~2 亿文档）
- 按月滚动索引（chunks-2024-01, chunks-2024-02...），方便冷数据迁移

### 9.3 Kafka 扩展

| 参数 | 值 | 说明 |
|------|-----|------|
| Broker 数 | 3 | 最小 HA 配置 |
| 每个 Topic 分区数 | 32 | 与 Citus 分片数对齐 |
| 副本因子 | 3 | 每条消息 3 副本 |
| 保留时间 | 7 天 | 处理类 Topic |
| `max.message.size` | 10 MB | 单条分块内容上限 |
| `compression.type` | lz4 | 压缩率 ~60% |

**【设计理由】分区数 = Citus 分片数：**
Kafka 的分区路由和 Citus 的分片路由都基于 user_id hash。对齐后，同一 user_id 的消息始终被同一组 consumer 处理，consumer 的本地缓存命中率最大化（user 的 chunk 数据在同一 DB shard 上）。

---

## 10. 可靠性与容灾设计

### 10.1 高可用目标

**SLA：99.9% 可用性（每月最多 43.8 分钟停机）**

| 组件 | HA 方案 | 故障恢复时间 |
|------|---------|-------------|
| K8s Pod | 副本 + 反亲和 + PDB | < 30s（自动重启） |
| PostgreSQL | Patroni + 流复制 + 自动 failover | < 30s |
| Milvus | 多副本 + etcd 集群 | < 60s |
| Redis | Cluster 模式 + 哨兵 failover | < 15s |
| Kafka | 多 Broker + 副本 | Broker 宕机无感知 |
| MinIO | 纠删码 4+2 | 单盘故障无感知 |

### 10.2 灾备方案

| 指标 | 目标 | 实现方式 |
|------|------|----------|
| RPO（恢复点目标） | 1 小时 | PG WAL 归档到 S3（每 1 小时） |
| RTO（恢复时间目标） | 4 小时 | 自动化恢复 Runbook + 预置脚本 |

**备份策略：**

| 数据 | 备份方式 | 频率 | 保留 | 存储 |
|------|----------|------|------|------|
| PostgreSQL | pg_basebackup + WAL 归档 | 全量每日 + WAL 持续 | 30 天 | S3 |
| Milvus | Snapshot 备份 | 每日 | 14 天 | S3 |
| Elasticsearch | Snapshot | 每日 | 14 天 | S3 |
| MinIO | 版本控制 + 跨区域复制 | 实时 | 版本保留 90 天 | 异地 S3 |
| Redis | RDB + AOF | RDB 每小时 / AOF 实时 | 可从 PG 重建 | 本地 |

**备份恢复测试：**
- 每月自动执行：在 staging 环境恢复生产备份，验证数据完整性
- 季度演练：模拟全量灾难恢复，验证 RTO 指标

### 10.3 优雅降级策略

```
正常状态: 全功能可用
    │
    ▼ LLM API 异常
降级 L1: 切换到备用模型（Qwen2-72B 自部署），回答质量降低，标注"降级模式"
    │
    ▼ 向量检索超时
降级 L2: 跳过 rerank，直接用 RRF 融合结果作为上下文，速度提升但精度降低
    │
    ▼ 向量数据库不可用
降级 L3: 仅使用 BM25 关键词检索，回答质量显著降低，标注"检索受限"
    │
    ▼ 数据库不可用
降级 L4: 只读模式（Redis 缓存命中的问答仍然可用），阻止写入操作
    │
    ▼ 全部不可用
降级 L5: 静态页面，显示"系统维护中"，503 状态码
```

```python
class CircuitBreaker:
    """熔断器模式，防止级联故障"""

    def __init__(self, service_name: str, failure_threshold: int = 5, timeout: int = 30):
        self.service = service_name
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.state = "closed"  # closed | open | half_open
        self.failure_count = 0
        self.last_failure_time = 0

    async def call(self, func, *args, **kwargs):
        if self.state == "open":
            if time.time() - self.last_failure_time > self.timeout:
                self.state = "half_open"  # 尝试恢复
            else:
                raise ServiceUnavailable(self.service)

        try:
            result = await func(*args, **kwargs)
            self.failure_count = 0
            self.state = "closed"
            return result
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.failure_threshold:
                self.state = "open"
                alert(f"Circuit breaker OPEN for {self.service}")
            raise

# 使用示例
rerank_breaker = CircuitBreaker("rerank_service", failure_threshold=3, timeout=60)

async def search_with_fallback(query, chunks):
    try:
        return await rerank_breaker.call(rerank_service.rerank, query, chunks)
    except ServiceUnavailable:
        # 降级：跳过 rerank，直接用 RRF 结果
        logger.warning("Rerank unavailable, using RRF results directly")
        return chunks[:10]
```

### 10.4 错误处理矩阵

| 错误场景 | 影响范围 | 处理策略 | 用户感知 |
|----------|----------|----------|----------|
| 文档解析失败 | 单个文档 | 重试 3 次 → 标记失败 → 通知用户 | "文档处理失败，请检查文件格式" |
| 向量化失败 | 单个文档 | 死信队列 → 人工/自动重处理 | "部分内容暂不可搜索" |
| Milvus 查询超时 | 单次查询 | 重试 1 次 → 降级到 BM25 | 回答质量降低，无延迟增加 |
| LLM API 限速 | 当前请求 | 排队等待 → 超时切换备用模型 | 首次响应延迟增加 |
| LLM API 完全不可用 | 全局 | 切换自部署模型 | 回答质量降低，标注降级 |
| PG 主节点宕机 | 全局写入 | Patroni 自动 failover | 写入中断 < 30s |
| Redis 宕机 | 全局缓存 | 从 PG 恢复 + 短暂性能下降 | 响应变慢 |

---

## 11. 成本模型与优化

### 11.1 月度成本估算（云端部署）

| 项目 | 规格 | 单价 | 数量 | 月成本 |
|------|------|------|------|--------|
| **LLM API（最大支出）** | | | | |
| ├ DeepSeek V3 input | ¥1/M token | 2B token/天 | 30 天 | ¥60,000 |
| ├ DeepSeek V3 output | ¥2/M token | 500M token/天 | 30 天 | ¥30,000 |
| ├ Prompt cache 节省 | -40% | | | -¥36,000 |
| └ 小计 | | | | **¥54,000** |
| **GPU 计算** | | | | |
| ├ Embedding (bge-m3) | A10 × 1 | ¥3,000/月 | 1 | ¥3,000 |
| ├ Reranker | A10 × 1 | ¥3,000/月 | 1 | ¥3,000 |
| ├ OCR (PaddleOCR) | T4 × 1 | ¥1,500/月 | 1 | ¥1,500 |
| └ LLM Fallback (待机) | A100 × 4 | ¥30,000/月 | 按需 | ¥5,000 |
| **GPU 小计** | | | | **¥12,500** |
| **数据库** | | | | |
| ├ PG Citus (32 workers + coord) | 4C/32GB × 33 | ¥800/月/台 | 33 | ¥26,400 |
| ├ PG 读副本 | 4C/32GB × 4 | ¥800/月/台 | 4 | ¥3,200 |
| ├ Milvus Query Node | 32C/64GB × 3 | ¥5,000/月/台 | 3 | ¥15,000 |
| ├ Milvus Data Node | 8C/16GB × 2 | ¥1,500/月/台 | 2 | ¥3,000 |
| ├ Elasticsearch | 8C/32GB × 3 | ¥3,000/月/台 | 3 | ¥9,000 |
| └ Redis Cluster | 8C/32GB × 6 | ¥2,000/月/台 | 6 | ¥12,000 |
| **数据库 小计** | | | | **¥68,600** |
| **存储** | | | | |
| ├ Milvus 向量存储 (SSD) | 2 TB | ¥0.3/GB/月 | 2000 GB | ¥600 |
| ├ 对象存储 (S3) | 50 TB | ¥0.12/GB/月 | 50000 GB | ¥6,000 |
| ├ PG SSD | 2 TB | ¥0.3/GB/月 | 2000 GB | ¥600 |
| └ Kafka 存储 | 1 TB | ¥0.3/GB/月 | 1000 GB | ¥300 |
| **存储 小计** | | | | **¥7,500** |
| **计算节点 (K8s)** | | | | |
| ├ API/业务服务节点 | 4C/8GB × 15 | ¥600/月/台 | 15 | ¥9,000 |
| ├ Kafka Broker | 4C/32GB × 3 | ¥1,500/月/台 | 3 | ¥4,500 |
| └ 监控/日志 (Prometheus/Loki) | 8C/16GB × 2 | ¥1,500/月/台 | 2 | ¥3,000 |
| **计算 小计** | | | | **¥16,500** |
| **网络** | | | | |
| ├ CDN 流量 | ¥0.2/GB | 10 TB/月 | | ¥2,000 |
| ├ 跨区域复制 | ¥0.5/GB | 500 GB/月 | | ¥250 |
| └ 公网带宽 | ¥500/100Mbps | | | ¥500 |
| **网络 小计** | | | | **¥2,750** |
| **总月度成本** | | | | **¥161,850** |

> **年化成本：~¥194 万**

### 11.2 成本优化策略

#### LLM 成本优化（最大支出项，占 33%）

| 策略 | 预估节省 | 实现方式 |
|------|----------|----------|
| Prompt Prefix Caching | 30-40% input | DeepSeek 原生支持，System Prompt 部分自动缓存 |
| 热门问答缓存 | 5-10% total | 完全一致的 query → 直接返回缓存的回答 |
| 模型路由 | 15-20% total | 简单问题（< 100 字回答）路由到 Qwen2-7B（成本 1/10） |
| Token 预算控制 | 5-10% output | 按用户套餐限制 max_output_tokens，Free 500/Pro 2000 |
| 历史压缩 | 10-15% input | 多轮对话中，> 3 轮的历史用摘要替代原文 |

```python
class ModelRouter:
    """根据查询复杂度路由到不同模型"""
    def route(self, query: str, user_plan: str) -> ModelConfig:
        # 简单事实型问题 → 小模型
        if self.is_simple_query(query) and user_plan == 'free':
            return ModelConfig(
                model="qwen2-7b",  # 自部署，成本几乎为零
                max_tokens=500,
                temperature=0.1
            )

        # 复杂分析/对比型问题 → 大模型
        return ModelConfig(
            model="deepseek-v3",
            max_tokens=2000 if user_plan == 'pro' else 1000,
            temperature=0.3
        )

    def is_simple_query(self, query: str) -> bool:
        """简单启发式判断"""
        return (
            len(query) < 30
            and not any(w in query for w in ['对比', '分析', '为什么', '区别', '总结'])
        )
```

**【设计理由】模型路由是性价比最高的优化：**
- Qwen2-7B 在简单事实型问答上效果不逊于 DeepSeek V3
- 自部署 Qwen2-7B 在 A10 上运行，边际成本为零（GPU 已为 Embedding 付费）
- 100K DAU 中约 40% 是简单问题，路由后 LLM API 成本降低 40%

#### 存储成本优化

| 策略 | 预估节省 | 实现方式 |
|------|----------|----------|
| 向量 int8 量化 | 75% 向量存储 | bge-m3 输出 float32→int8，精度损失 < 1% |
| 冷热分层 | 50% 总存储 | 30 天未活跃用户数据迁移到 HDD/Glacier |
| 文档去重 | 10-20% 文件存储 | content_hash 检测重复文件，存一份引用多次 |
| 冷数据归档 | 30% PG 存储 | messages/docs > 90 天迁移到归档表 |

```python
class DocumentDeduplicator:
    """跨用户文档去重（同一公开文件多人上传）"""
    async def deduplicate(self, content_hash: str, user_id: str) -> str | None:
        # 查找相同 hash 的已有文件
        existing = await db.execute(
            "SELECT file_path FROM documents WHERE content_hash = $1 LIMIT 1",
            content_hash
        )
        if existing:
            # 复用已有文件，增加引用计数
            await db.execute(
                "UPDATE documents SET ref_count = ref_count + 1 WHERE file_path = $1",
                existing.file_path
            )
            return existing.file_path
        return None  # 新文件，需要上传
```

#### 计算成本优化

| 策略 | 预估节省 | 实现方式 |
|------|----------|----------|
| Spot 实例（批处理） | 70% GPU 批处理成本 | 文档解析/向量化使用抢占式实例 |
| 分时段缩容 | 40% GPU 夜间成本 | 凌晨 0-6 点 GPU 缩容到 1 块 |
| GPU 分时复用 | 50% GPU 节点数 | Embedding + Reranker 非高峰共享一块 A10 |
| 连接池优化 | 20% DB 节点数 | PgBouncer 减少连接开销，同等负载需更少节点 |

### 11.3 收入模型与盈亏分析

| 套餐 | 月费 | DAU 占比 | DAU 数 | 月收入 | 单用户月成本 |
|------|------|----------|--------|--------|-------------|
| Free | ¥0 | 90% | 90,000 | ¥0 | ~¥0.5（缓存+检索） |
| Pro | ¥49 | 8% | 8,000 | ¥392,000 | ~¥5（高频问答+大文件） |
| Enterprise | ¥499+ | 2% | 2,000 | ¥998,000+ | ~¥20（专用资源） |
| **合计** | | | 100,000 | **¥1,390,000** | |

**盈亏分析：**

| 指标 | 数值 |
|------|------|
| 月收入 | ¥139 万 |
| 月成本 | ¥16.2 万（不含优化）→ ¥10 万（优化后） |
| 毛利率 | ~93% |
| 盈亏平衡点 | ~7,200 Pro 用户覆盖成本（¥10 万 / ¥49 ≈ 2040 → 含 Free 成本分摊约 7200） |
| 当前预估 Pro 用户 | 8,000 → 已过盈亏平衡 |

**【设计理由】为什么 RAG SaaS 是好生意：**
- 边际成本极低：每增加一个 Free 用户只增加少量检索和存储成本
- Pro 用户收入是成本的 10x（¥49 vs ¥5）
- 用户粘性高（知识库是持续积累的资产），流失率低
- 与纯 LLM 聊天产品相比，有数据壁垒（用户的知识库不可迁移）
