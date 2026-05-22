# 设计方案修订稿（Rev 1）

> 基于 CTO 评审的 5 个严重问题，结合用户决策（D1 自研分片路由、D2 自建 Milvus、D3 扩充团队、D5 单供应商 DeepSeek），修订以下内容。本文档替代原文档中的对应章节。

---

## 修订 S1：关系数据库 — 自研分片路由替代 Citus

### 核心变更

移除 Citus 依赖，改为应用层分片路由中间件。

### 分片架构

```
                        ┌─────────────────┐
                        │  PgBouncer      │
                        │  (per-shard)    │
                        └────────┬────────┘
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
           ┌────────▼──┐ ┌──────▼───┐ ┌──────▼───┐
           │  Shard 0  │ │  Shard 1 │ │  Shard N │   ... 32 Shards
           │  PG 实例   │ │  PG 实例  │ │  PG 实例  │
           │ user_id   │ │ user_id  │ │ user_id  │
           │ % 32 == 0 │ │ % 32 == 1│ │ % 32 == N│
           │ 4C/32GB   │ │ 4C/32GB  │ │ 4C/32GB  │
           └───────────┘ └──────────┘ └──────────┘
                  ↑ 每个 shard 独立的 PG 实例，独立的表结构
```

### 路由中间件

```python
import hashlib

class ShardRouter:
    """应用层分片路由，按 user_id hash 路由到对应 PG 实例"""

    def __init__(self, shard_count: int = 32):
        self.shard_count = shard_count
        self.pools: dict[int, AsyncSessionPool] = {}
        # 初始化每个 shard 的连接池
        for i in range(shard_count):
            self.pools[i] = create_pool(f"postgresql://shard-{i}:5432/knspace")

    def get_shard(self, user_id: str) -> int:
        """将 user_id 映射到 shard 编号（0 ~ shard_count-1）"""
        return int(hashlib.md5(user_id.encode()).hexdigest(), 16) % self.shard_count

    def get_session(self, user_id: str) -> AsyncSession:
        """获取对应用户所在 shard 的数据库会话"""
        shard = self.get_shard(user_id)
        return self.pools[shard].get_session()

    async def get_by_document_id(self, document_id: str, user_id: str) -> AsyncSession:
        """通过 document_id 查询时，必须同时传入 user_id 来定位 shard"""
        return self.get_session(user_id)

# 全局路由实例
router = ShardRouter(shard_count=32)
```

### 每个 Shard 的表结构（与原设计相同，去掉 Citus 特有语法）

每个 shard 包含**完整的一组表**，但只存储 hash(user_id) % 32 == shard_id 的数据：

```sql
-- 每个 shard 独立创建以下表（无 Citus 语法）
-- users 表不在分片中，使用独立的全局 PG 实例

-- 以下表在每个 shard 中各有一份
CREATE TABLE collections (...);   -- 同原设计
CREATE TABLE documents (...);     -- 同原设计
CREATE TABLE chunks (...);        -- 同原设计
CREATE TABLE conversations (...); -- 同原设计
CREATE TABLE messages (...);      -- 同原设计
CREATE TABLE tags (...);          -- 同原设计
CREATE TABLE document_tags (...); -- 同原设计
CREATE TABLE processing_jobs (...); -- 同原设计
```

**users 表**单独存放在全局 PG 实例（不分片），因为需要跨用户查询（登录、管理）。

### JOIN 保证

由于同一 user_id 的所有数据（documents、chunks、messages 等）都在**同一个 shard** 上，JOIN 查询在单实例内完成，无需跨 shard：

```python
# chunks JOIN documents —— 天然在同一 shard
async def get_chunks_with_document(user_id: str, chunk_ids: list[str]):
    session = router.get_session(user_id)
    stmt = (
        select(Chunk, Document)
        .join(Document, Chunk.document_id == Document.id)
        .where(Chunk.id.in_(chunk_ids))
    )
    return await session.execute(stmt)
```

### 扩容方案

当单 shard 数据量超过阈值（~5000 万行 chunks）时，执行 **reshard**：

1. 新增 shard 实例（32 → 64）
2. 双写阶段：写入时同时写旧 shard 和新 shard
3. 迁移阶段：按新 hash 规则迁移数据到新 shard
4. 验证阶段：对比新旧 shard 数据一致性
5. 切换阶段：路由切换到新 shard
6. 清理阶段：删除旧 shard 中已迁移的数据

```python
class ReshardManager:
    """在线 reshard，从 N shard 扩展到 2N shard"""

    async def migrate_user(self, user_id: str, old_count: int, new_count: int):
        old_shard = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % old_count
        new_shard = int(hashlib.md5(user_id.encode()).hexdigest(), 16) % new_count

        if old_shard == new_shard:
            return  # 数据不需要移动

        # 从旧 shard 读取全部数据
        old_session = self.router.pools[old_shard].get_session()
        new_session = self.router.pools[new_shard].get_session()

        # 迁移所有相关表的数据（单用户事务）
        async with new_session.begin():
            data = await self._read_all_user_data(old_session, user_id)
            await self._write_all_user_data(new_session, data)
            await self._verify_consistency(old_session, new_session, user_id)
            await self._delete_user_data(old_session, user_id)
```

### 与 Citus 的对比

| 维度 | Citus | 自研分片路由 |
|------|-------|-------------|
| 授权费 | 年费 20-50 万 | 零 |
| 跨 shard 查询 | 自动（但有性能陷阱） | 不支持（必须在同一 shard） |
| Reshard | 自动（Enterprise） | 需自研（上述方案） |
| 运维 | Citus 额外组件 | 标准 PG 实例 |
| document_id 查询 | 广播到所有 shard（性能差） | 必须带 user_id（显式约束） |
| 开发成本 | 低 | 中（路由中间件 ~500 行代码） |

**【设计理由】选择自研路由的核心原因：**
1. chunks 表 10 亿行的 `WHERE document_id = ?` 查询在 Citus 下会被广播到 32 个 shard，自研路由要求所有查询都带 user_id，天然在单 shard 内完成
2. 省掉年费 20-50 万
3. 路由中间件逻辑简单（~500 行），复杂度远低于 Citus 内核
4. 每个 shard 是标准 PG 实例，运维人员不需要学 Citus

---

## 修订 S2：Milvus 冷热分层修正

### 问题回顾

原方案声称"mmap 从 SSD 按需加载，延迟增加 ~5ms"，实际上 Milvus mmap 是 segment 级别加载（最大 512MB），冷查询延迟是百毫秒甚至秒级。

### 修正方案

#### 调整 Query Node 规格

| 组件 | 原方案 | 修正方案 | 原因 |
|------|--------|----------|------|
| Query Node | 3 × 32 GB RAM | **3 × 128 GB RAM** | 热数据 ~260 GB 需要 3 节点均分 ~87 GB/节点 |
| Query Node SSD | 未指定 | **2 TB NVMe SSD/节点** | mmap 的 page fault 从 NVMe 加载比网络 SSD 快 10x |

#### 内存分配（每个 Query Node 128 GB）

| 用途 | 大小 | 说明 |
|------|------|------|
| 热向量数据（int8） | ~60 GB | DAU 10 万用户的向量常驻 |
| HNSW 索引 | ~20 GB | 烱数据的索引结构 |
| OS + Milvus 进程 | ~10 GB | |
| mmap 缓冲 | ~30 GB | 温数据的 page cache |
| 预留 | ~8 GB | |

#### 冷热分层策略修正

```
热数据（常驻内存）：
  - 最近 30 天内活跃过的用户
  - 所有 Pro/Enterprise 用户
  - 预估 20% 数据 = ~260 GB，3 节点均分可容纳

温数据（mmap from NVMe SSD）：
  - 30-90 天未活跃的 Free 用户
  - mmap 映射到 NVMe SSD（非网络 SSD，本地盘）
  - 首次查询延迟 ~50-200ms（segment 级加载），后续查询 ~10ms（page cache 命中）

冷数据（不加载）：
  - 90 天+ 未活跃
  - 查询时触发 segment 加载，延迟 ~1-3s
  - 对冷用户返回"正在加载知识库，请稍后重试"
```

#### Phase 1 基准测试（必须在 Phase 2 前完成）

```python
"""
Milvus 100 万 partition_key 性能基准测试
目标：验证 partition_key 在多租户场景下的实际延迟
"""
async def benchmark_partition_key():
    # 1. 准备数据
    #    - 10 万模拟用户
    #    - 每用户 1000 向量 = 1 亿总向量
    #    - HNSW M=16, efConstruction=256
    await prepare_data(users=100_000, vectors_per_user=1000)

    # 2. 测试单用户分区查询延迟
    latencies = []
    for user_id in random_users(1000):
        start = time.monotonic()
        results = await collection.search(
            data=random_vector(),
            anns_field="dense_vector",
            param={"metric_type": "COSINE", "params": {"ef": 128}},
            limit=30,
            expr=f'user_id == "{user_id}"',
        )
        latencies.append(time.monotonic() - start)

    print(f"单用户分区查询:")
    print(f"  P50: {np.percentile(latencies, 50)*1000:.1f}ms")
    print(f"  P95: {np.percentile(latencies, 95)*1000:.1f}ms")
    print(f"  P99: {np.percentile(latencies, 99)*1000:.1f}ms")

    # 3. 测试并发查询（100 并发）
    # 4. 测试 mmap 冷加载延迟
    # 5. 测试写入+查询混合负载

    # 验收标准：
    # P95 < 100ms（单分区查询）
    # 100 并发下 P95 < 200ms
    # mmap 冷加载 < 500ms
```

#### Fallback 方案

如果基准测试不达标（P95 > 200ms）：
1. 减少 partition_key 数量：改为 hash bucket（1 万个 bucket，每个 bucket 包含 ~100 用户），牺牲隔离性换性能
2. 考虑 Qdrant：其 payload filter 方案在百万级 tenant 场景有更多社区验证
3. 考虑 Zilliz Cloud（Milvus 托管版）：成本高 2-3x 但零运维

---

## 修订 S3：成本模型重算

### 修正项

| 原估算项 | 原估算 | 修正后 | 修正原因 |
|----------|--------|--------|----------|
| PG Citus 授权 | 未计入 | **¥0**（改为自研路由） | D1 决策 |
| PG 实例（32 shard + 1 全局 + 4 读副本） | ¥29,600 | **¥29,600** | 不变，去掉 Citus 但 PG 实例数不变 |
| Milvus Query Node（3×128GB） | ¥15,000 | **¥36,000** | 规格从 32GB 提升到 128GB |
| Kafka 存储 | ¥300 | **¥6,300** | 实际：7 天保留 × 500K docs × 2KB × 5 stages × 3 副本 ≈ 21TB × ¥0.3/GB ≈ ¥6,300/月 |
| Elasticsearch 存储 | ¥600（1TB SSD） | **¥4,500** | 10 亿 chunks × 500B × 2x 膨胀 × 1 副本 ≈ 2TB，3 节点各 1TB SSD ≈ ¥4,500 |
| 人力成本 | 未计入 | **¥480,000** | 8 人 × ¥6 万/月（含社保公积金办公） |

### 修订后月度成本明细

| 项目 | 月成本 |
|------|--------|
| LLM API（DeepSeek V3，含缓存优化） | ¥54,000 |
| GPU 计算（Embedding + Rerank + OCR + LLM Fallback） | ¥12,500 |
| **PG 实例（32 shard + 全局 + 读副本，无 Citus 费用）** | **¥29,600** |
| **Milvus Cluster（3×128GB Query Node + Data Node + etcd + 存储）** | **¥51,000** |
| Elasticsearch（3×1TB SSD 节点） | ¥13,500 |
| Redis Cluster | ¥12,000 |
| Kafka Cluster（含 21TB 存储） | **¥12,300** |
| MinIO/S3（50TB） | ¥6,000 |
| K8s 计算节点（含扩充到 8 人后的增量） | ¥18,000 |
| 网络（CDN + 出网 + 复制） | ¥2,750 |
| 监控/日志/Vault | ¥3,000 |
| **基础设施小计** | **~¥215,000** |
| **人力成本（8 人）** | **¥480,000** |
| **总月度成本** | **~¥695,000** |

### 修订后盈亏分析

| 指标 | 数值 |
|------|------|
| 月收入 | ¥139 万 |
| 月基础设施成本 | ¥21.5 万 |
| 月人力成本 | ¥48 万 |
| **月总成本** | **¥69.5 万** |
| **毛利率** | **~50%**（含人力），**~85%**（不含人力） |
| 盈亏平衡点 | ~14,200 Pro 用户（¥69.5 万 / ¥49） |

**【设计理由】人力成本说明：**
之前未计入人力是因为通常 ROI 分析只看基础设施毛利率。但 CTO 指出"毛利率 93% 不可信"是对的——人力是最大单项成本。修正后毛利率 ~50% 仍是健康的 SaaS 水平（对比：Notion ~60%、Slack ~65% 含人力）。

---

## 修订 S4：Kafka 消息安全

### 问题回顾

原方案中 Kafka 消息包含完整的 chunk 文本内容，多租户场景下任何 consumer 都可以读取所有用户的数据。

### 修正方案：消息只传 ID，content 从数据库读取

#### 修改后的 Topic 消息格式

```python
# 修改前（不安全）
@dataclass
class ChunkMessage:
    chunk_id: str
    user_id: str
    content: str          # ← 完整文本在 Kafka 中明文传输
    metadata: dict

# 修改后（安全）
@dataclass
class ChunkMessage:
    chunk_id: str
    user_id: str
    document_id: str
    # 不包含 content，consumer 从数据库读取
```

#### 各 Topic 消息定义

| Topic | 消息内容 | 不包含 |
|-------|----------|--------|
| doc.uploaded | `{doc_id, user_id, file_path, mime_type}` | 文件内容 |
| doc.parsed | `{doc_id, user_id, chunk_count, metadata}` | 解析结果文本 |
| doc.chunked | `{doc_id, user_id, chunk_ids: [id1, id2, ...]}` | chunk 文本 |
| doc.embedded | `{doc_id, user_id, chunk_ids, status}` | embedding 向量 |
| doc.completed | `{doc_id, user_id}` | — |

#### Consumer 从 DB 读取内容

```python
class EmbedWorker:
    """修改后的 Embedding Worker"""

    async def process(self, msg: ChunkedMessage):
        session = shard_router.get_session(msg.user_id)

        # 从数据库读取 chunk 文本（已通过 RLS 和分片隔离保护）
        chunks = await session.execute(
            select(Chunk).where(
                Chunk.document_id == msg.doc_id,
                Chunk.user_id == msg.user_id
            ).order_by(Chunk.chunk_index)
        )
        chunk_list = chunks.scalars().all()

        # 批量 embedding
        texts = [c.content for c in chunk_list]
        embeddings = await embedding_model.encode(texts, batch_size=64)

        # 写入 Milvus 和 Redis 缓存
        await self.write_to_milvus(chunk_list, embeddings, msg.user_id)
        await self.cache_embeddings(chunk_list, embeddings)
```

#### 额外安全措施

1. **Kafka SASL/SSL**：broker 间通信加密，consumer 认证
2. **Consumer Group ACL**：每个 consumer group 只能订阅授权的 topic
3. **审计日志**：记录所有 consumer 的消息消费行为

**【设计理由】为什么不加密 Kafka 消息而是改为只传 ID：**
1. 加密每条消息的性能开销大（每秒数千条消息 × 加密解密），且密钥管理复杂
2. 只传 ID 后消息体缩小 95%（从 ~2KB 降到 ~100 bytes），Kafka 存储成本降低 20 倍
3. DB 已经有完善的隔离机制（分片 + RLS），没必要在 Kafka 层再造一套
4. 代价：consumer 需要额外一次 DB 查询。但在批处理场景下（一批 64 个 chunk），一次查询的成本可忽略

---

## 修订 S5：Phase 1→2 架构一致性

### 问题回顾

Phase 1 使用同步处理、单节点 Milvus、无 Kafka；Phase 2 突然切换到 Kafka 异步、分片、混合检索。大量 Phase 1 代码需要重写。

### 修正方案：Phase 1 就使用与目标架构一致的基础设施

#### 修正后的 Phase 1 架构

| 组件 | 原方案（Phase 1） | 修正方案（Phase 1） | 说明 |
|------|-------------------|---------------------|------|
| 文档处理 | 同步（FastAPI BackgroundTasks） | **Kafka 异步** | 从一开始就是事件驱动 |
| 数据库 | 单节点 PG | **4 shard PG + 全局实例** | 分片路由从一开始就启用 |
| 向量库 | 单节点 Milvus | **单节点 Milvus**（不变） | 规模小不需要集群 |
| 检索 | 仅向量检索 | **仅向量检索**（不变） | Phase 2 加混合 |
| ES | 无 | **无**（不变） | Phase 2 引入 |
| Redis | 无 | **Redis 单节点** | 从一开始就有缓存 |
| K8s | 不用 | **K8s**（不变） | 基础设施一致 |

#### Phase 1 Kafka 配置（简化版）

```yaml
# Phase 1: 简化配置，但架构与大规模一致
kafka:
  brokers: 1  # 单 broker
  topics:
    doc.uploaded:  { partitions: 4 }    # 4 partition 对应 4 shard
    doc.parsed:    { partitions: 4 }
    doc.chunked:   { partitions: 4 }
    doc.embedded:  { partitions: 4 }
    doc.completed: { partitions: 4 }
  replication: 1  # 单副本
  retention: 3d
```

#### Phase 1→2 的增量变更清单

| 变更 | 类型 | 影响 |
|------|------|------|
| Kafka 4 partition → 32 partition | 配置变更 | 无代码改动 |
| Kafka 1 broker → 3 broker | 基础设施变更 | 无代码改动 |
| PG 4 shard → 32 shard | reshard | 路由中间件自动处理 |
| Milvus 单节点 → 集群 | 基础设施变更 | 客户端配置变更 |
| 添加 ES | 新增组件 | Search Service 新增一个调用 |
| 添加 Rerank Service | 新增服务 | Search Service 插入一步 |
| Redis 单节点 → Cluster | 配置变更 | 客户端配置变更 |

**关键点：Phase 1→2 的所有变更都是"添加"和"扩展"，没有"替换"和"重写"。**

### 修正后的分阶段交付计划

#### Phase 0：基础设施（第 1-2 月，3→4 人）

**范围（不变）：**
- K8s 集群搭建
- CI/CD Pipeline
- 监控基线
- 认证服务
- **4 shard PG + 全局实例 + 分片路由中间件**（新增）
- **单节点 Kafka**（新增）
- **单节点 Redis**（新增）

**新增交付物：** 分片路由中间件通过单元测试 + 集成测试

**团队：** 2 后端 + 1 前端 + 1 DevOps（+1 人）

#### Phase 1：核心 RAG（第 2-4 月，5→6 人）

**范围（调整）：**
- 文档上传（文本：PDF、Word、Markdown）
- **Kafka 异步处理流水线**（替换原来的同步方案）
- 基础分块 + 嵌入 + Milvus 写入
- 向量检索 + DeepSeek V3 + SSE 流式
- 引用溯源
- 收藏夹管理
- **Milvus 100 万 partition_key 基准测试**（新增）

**团队：** 2 后端 + 2 前端 + 1 ML + 1 DevOps（+1 人）

#### Phase 2：质量与规模（第 4-6 月，6→8 人）

**范围（不变但减少迁移工作）：**
- ~~Kafka 异步流水线（已在 Phase 1）~~ → 直接扩展 partition 数
- ~~Citus 分片上线~~ → reshard 4→32
- Elasticsearch 全文检索
- 混合检索 + RRF + Rerank
- 查询理解
- RAG 评估 Pipeline
- 配额管理

**团队：** 3 后端 + 2 前端 + 1 ML + 1 DevOps + 1 QA（+2 人）

**【设计理由】为什么 Phase 0 就引入 Kafka 和分片：**
1. Kafka 的开发成本很低（生产者/消费者各 ~100 行代码），但 Phase 1 到 Phase 2 的重写成本很高
2. 4 shard 的路由中间件和 32 shard 的代码完全一样，只是配置不同
3. 避免数据迁移风险——Phase 1 的数据天然就在分片路由下，扩展只是加机器

---

## 修订汇总

| 严重问题 | 修订内容 | 影响文件 |
|----------|----------|----------|
| S1. Citus 方案 | 改为自研分片路由，32 shard + 应用层路由中间件 | Part 1, Part 3 |
| S2. Milvus 冷热分层 | Query Node 从 32GB→128GB，mmap 延迟修正为 50-200ms，增加基准测试 | Part 3, Part 4 |
| S3. 成本模型 | 加入人力 ¥48 万/月、修正 Kafka/ES 存储，总成本 ¥69.5 万/月，毛利率 50% | Part 4 |
| S4. Kafka 消息安全 | 消息只传 chunk_id，content 从 DB 读取，Kafka 存储成本降 20x | Part 2 |
| S5. Phase 架构跳跃 | Phase 0 引入 Kafka+分片，Phase 1→2 全是增量变更 | Part 5 |

---

## CTO 复审通过后的补充项

> CTO 复审结论：**通过**。以下为 CTO 提出的 3 条最终建议 + 3 个新发现问题，作为后续实施的补充要求。

### 补充 1：Reshard 状态机设计

双写期间需要严格的状态机管理，确保数据一致性：

```
IDLE → DUAL_WRITE → MIGRATING → VERIFYING → COMPLETED
  │        │            │           │           │
  │        │            │           │           └─ 清理旧数据
  │        │            │           └─ 比对新旧 shard 数据一致性
  │        │            └─ 按新 hash 迁移数据到目标 shard
  │        └─ 同时写入旧 shard 和新 shard（事务保证）
  └─ 等待 reshard 触发
```

**实施要求：**
- Phase 1 期间执行一次真实 reshard 演练（4→8 shard），验证方案可行性
- 双写期间的写入失败策略：新 shard 写失败则整体回滚（宁可少写不可不一致）
- 使用一致性哈希（jump consistent hash）替代 MD5 % N，减少任意 N→M 扩展时的迁移量

### 补充 2：Milvus 基准测试扩大到 50 万用户

原方案的 10 万用户测试点不够，partition_key 元数据管理在百万级可能非线性增长。

**扩展测试矩阵：**

| 用户数 | 向量数 | 验收标准 |
|--------|--------|----------|
| 10 万 | 1 亿 | P95 < 80ms |
| 50 万 | 5 亿 | P95 < 100ms |
| 100 万 | 10 亿 | P95 < 200ms |

如果 50 万用户测试点 P95 > 150ms，启动 Fallback 评估（hash bucket / Qdrant / Zilliz Cloud）。

冷用户加载超时上限：10 秒。超时后返回"知识库暂不可用，请稍后重试"。

### 补充 3：Phase 0 里程碑 — 分片路由压测

Phase 0 的范围已扩大，需要明确验收标准：

**Phase 0 必须通过的压测：**
- 分片路由中间件在 1000 QPS 下路由延迟 < 1ms（不含 DB 查询）
- 4 shard × 100 并发写入 + 读取混合负载下无死锁
- 如果 Phase 0 结束时分片路由未通过压测，不进入 Phase 1

优先级调整：分片路由中间件 > Kafka > Redis。如果 Phase 0 时间不够，Kafka 可延后到 Phase 1 第一个月。

### 补充 4：全局 PG 实例规格规划

users 表所在的全局 PG 实例是每个请求的必经之路（认证、token 校验）：

| 组件 | 规格 | 说明 |
|------|------|------|
| 全局 PG 主 | 4 vCPU / 16 GB RAM | 存 users + 系统配置表 |
| 全局 PG 读副本 | 2 个，2 vCPU / 8 GB RAM | 认证查询走读副本 |
| 高可用 | Patroni + 流复制 + 自动 failover | 故障恢复 < 30s |

### 补充 5：分片路由连接池管理

```python
class ShardRouter:
    def __init__(self, shard_count: int = 32):
        self.shard_count = shard_count
        self.pools: dict[int, AsyncSessionPool] = {}
        for i in range(shard_count):
            self.pools[i] = create_pool(
                f"postgresql://shard-{i}:5432/knspace",
                min_size=5,       # 每池最少 5 个连接
                max_size=20,      # 每池最多 20 个连接
                max_idle_time=300, # 空闲 5 分钟回收
                health_check=60,   # 每 60 秒健康检查
            )
        # 总连接数：32 × 20 = 640，在 PG max_connections=200 的限制下
        # 每个 shard 独立 PG 实例，实际连接数 = 本服务的 20 + 其他服务的连接
```

总连接数控制：每个服务对每个 shard 最多 20 连接，所有服务合计每 shard < 100 连接，PG `max_connections=200` 留有足够余量。

### 补充 6：Kafka Consumer 批量查 DB 优化

```python
class BatchEmbedWorker:
    """批处理优化：按 user_id 分组后批量查询"""

    async def process_batch(self, messages: list[ChunkedMessage]):
        # 按 user_id 分组（同一 user 的数据在同一 shard）
        grouped: dict[str, list[ChunkedMessage]] = defaultdict(list)
        for msg in messages:
            grouped[msg.user_id].append(msg)

        for user_id, user_msgs in grouped.items():
            session = shard_router.get_session(user_id)
            doc_ids = [msg.doc_id for msg in user_msgs]

            # 一次查询获取该用户所有待处理 chunk
            chunks = await session.execute(
                select(Chunk).where(
                    Chunk.document_id.in_(doc_ids),
                    Chunk.user_id == user_id
                ).order_by(Chunk.document_id, Chunk.chunk_index)
            )
            chunk_list = chunks.scalars().all()

            if not chunk_list:
                # 文档已被删除，直接 ack 跳过
                for msg in user_msgs:
                    await msg.ack()
                continue

            # 批量 embedding
            texts = [c.content for c in chunk_list]
            embeddings = await embedding_model.encode(texts, batch_size=64)

            await self.write_to_milvus(chunk_list, embeddings, user_id)
            for msg in user_msgs:
                await msg.ack()
```

Consumer 账号权限：只授予 SELECT 权限，不允许写入业务表（embedding 结果写入 Milvus 而非 PG）。
