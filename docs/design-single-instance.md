# knSpace 统一设计方案：从 RAG 管线到 Agent 系统的演进

> **定位**：面向 4C/4G 单实例部署的 AI 知识库系统，以百万用户架构为最终目标，
> 记录从普通 RAG 到 Agent 系统的完整演进路径。
>
> **设计原则**：接口一致，实现可替换。每一层抽象都为横向扩展预留了替换点。
>
> **面试定位**：展示 Agent 工程师的核心能力 —— Tool 设计、自适应检索管线、
> 上下文压缩、分层记忆、处理管线 Hook 系统，以及向 LLM Tool Use Agent 的演进路径。

---

# 第一部分：项目概述与演进路线

## 1. 系统定位

knSpace 是一个私有化部署的 RAG 知识库系统，核心能力：

- 用户上传文档（PDF/Word/网页/图片）→ 自动解析、分块、向量化
- 基于文档内容的智能问答（混合检索 + 重排序 + LLM 生成）
- 多轮对话，指代消解，上下文延续
- Tool-Based 自适应检索管线（非固定管线，可迭代重试）
- 向 Agent 系统演进的设计路径（Stage 3: LLM Tool Use）

### 1.1 硬件约束

| 资源 | 规格 | 说明 |
|------|------|------|
| CPU | 4 核 AMD EPYC 7K62 | 无 AVX-512 |
| 内存 | 4 GB（可用 ~3.6 GB） | 模型全走 API，不是瓶颈 |
| 磁盘 | 40 GB SSD | 含 OS + 数据 |
| GPU | 无 | 全部 AI 走云端 API |
| 网络 | 腾讯云轻量 4Mbps | 上行受限 |

### 1.2 容量目标

| 指标 | 单实例 | 百万用户版本 |
|------|--------|-------------|
| 注册用户 | 1,000 | 1,000,000 |
| DAU | 50 | 100,000 |
| 文档总量 | 10,000 | 10,000,000 |
| 向量总量 | 500 万 | 10 亿 |
| QPS | 5 | 150 |

---

## 2. 核心架构决策：全走云端 API

**为什么这样设计**：4 GB 内存无法加载任何 AI 模型（bge-m3 单独就要 2.5 GB），
升级到 8 GB 服务器月费 ~¥100，而全走 API 月费仅 ~¥43。

| 模型 | 本地内存 | API 延迟 | API 月成本 |
|------|---------|---------|-----------|
| bge-m3（embedding） | ~2.5 GB | ~200ms/批 | ~¥0.2 |
| bge-reranker-v2-m3 | ~1.2 GB | ~300ms | ~¥7.5 |
| PaddleOCR | ~1.5 GB | ~1s/页 | ~¥5 |
| LLM（glm-5.1） | N/A | ~2s 首 token | ~¥30 |
| **合计** | **~5.2 GB（跑不了）** | | **~¥43/月** |

**面试亮点**：资源约束下的工程权衡。不是"选择最佳方案"，而是"在约束下选最优解"。
这是资深工程师和初级工程师的核心区别 —— 初级选技术，高级选约束内的最优解。

---

## 3. 演进路线：从 RAG 到 Agent

这是本文档的核心叙事。系统不是一开始就是 Agent，而是经历了 3 个阶段演进：

```
Stage 1: 固定管线 RAG      Stage 2: 自适应检索管线       Stage 3: Agent（LLM Tool Use）
─────────────────────     ───────────────────         ──────────────────────────
用户提问                    用户提问                     用户提问
  │                          │                           │
  ▼                          ▼                           ▼
查询分析（规则）             查询分析（规则）              LLM 自主规划
  │                          │                           │
  ▼                          ▼                           ▼
向量搜索 + FTS              Tool 选择 + 执行             LLM 选择 Tool + 执行
  │                          │                           │
  ▼                          ▼                           ▼
RRF 融合                    观察 → 规则决策 → 迭代        观察 → LLM 推理 → 迭代
  │                          │                           │
  ▼                          ▼                           ▼
Rerank                     Rerank（按需）               Rerank（按需）
  │                          │                           │
  ▼                          ▼                           ▼
拼接上下文                  智能上下文压缩               智能上下文压缩
  │                          │                           │
  ▼                          ▼                           ▼
LLM 生成                   LLM 生成                    LLM 生成
                            │
                            ▼
                            用户记忆学习

痛点:                      改进:                       改进:
- 搜索失败直接返回空        - 搜索失败自动重试           - LLM 自主决定策略
- 所有查询走同一流程         - 根据查询类型调整策略       - 可跳步、可迭代
- 上下文按 char 截断         - 按分数分配 token 预算      - 完整推理轨迹可观测
- 无学习，每次无状态         - 从反馈学习用户偏好         - LLM 可调用检索工具

本质:                      本质:                       本质:
固定流程的函数调用           规则驱动的状态机              LLM 驱动的 ReAct 循环
不可迭代、不可跳步           可迭代、可跳步               自主推理、自主决策
```

**面试亮点**：演进式设计。不是一步到位的 Agent，而是从最简单的 RAG 开始，
逐步解决实际痛点。每一步演进都有明确的"解决什么问题"的动机。
面试时可以说："我先做了最简单的能跑的版本，发现 X 问题，于是引入 Y。"

---

# 第二部分：架构设计

## 4. 架构总览

### 4.1 单实例架构图

```
                        ┌──────────────┐
                        │    Nginx     │ SSL 终结 + 限流 + 静态资源
                        └──────┬───────┘
                               │
                        ┌──────▼───────────────────────────┐
                        │  FastAPI (uvicorn)                 │
                        │                                    │
                        │  ┌─────────────────────────────┐  │
                        │  │     RAG Agent (核心)          │  │
                        │  │  Planner → Tool Executor →   │  │
                        │  │  Observer → Context Manager   │  │
                        │  └──────────┬──────────────────┘  │
                        │             │                      │
                        │  ┌─────┐ ┌──────┐ ┌──────┐       │
                        │  │Auth │ │Doc   │ │Chat  │       │
                        │  │     │ │API   │ │API   │       │
                        │  └─────┘ └──────┘ └──────┘       │
                        └──┬───────┬────────────────────────┘
                           │       │
              ┌────────────┘       └────────────┐
              │                                  │
       ┌──────▼──────┐                   ┌──────▼──────┐
       │ PostgreSQL  │                   │  Milvus Lite │
       │ users/docs/ │                   │  向量存储     │
       │ chunks/fts  │                   │  (嵌入式)     │
       │ convs/tags  │                   └─────────────┘
       │ memories    │
       └─────────────┘
              │
       ┌──────▼──────┐
       │   Redis     │ 缓存 + Celery broker + 记忆缓存
       └─────────────┘

       ┌──────▼──────┐
       │  本地文件系统 │ /data/knspace/files/
       └─────────────┘

      ┌──────────────────────────────────────────────────┐
      │              云端 API（零本地内存）                  │
      │  Embedding API · Rerank API · OCR API · LLM API  │
      └──────────────────────────────────────────────────┘
```

### 4.2 单实例 vs 百万用户版本组件对照

```
百万用户版本                         单实例版本
────────────────────             ────────────────────
APISIX API Gateway           →   Nginx
Kubernetes                   →   systemd
12 个微服务（gRPC）            →   单 FastAPI（模块化）
Kafka 异步流水线              →   Celery + Redis
PostgreSQL + Citus（分片）     →   单 PostgreSQL
Milvus Cluster（HNSW）        →   Milvus Lite（IVF_FLAT）
Elasticsearch（ik 分词）       →   PostgreSQL FTS（jieba 分词）
Redis Cluster                →   单 Redis
MinIO / S3                   →   本地文件系统
GPU 自建推理服务               →   云端 API
HashiCorp Vault              →   .env 文件
Grafana + Loki + Jaeger      →   Prometheus + JSON 日志
```

---

## 5. 接口抽象层

**为什么这样设计**：业务代码只依赖抽象接口，不依赖具体实现。
迁移到百万用户版本时只需新增实现类，业务逻辑零改动。

```python
# app/services/factory.py — 所有服务的抽象与工厂

from typing import Protocol, AsyncIterator, Callable, runtime_checkable


# ── 存储抽象 ─────────────────────────────────────────────────

@runtime_checkable
class VectorStoreBase(Protocol):
    """向量存储 — 单实例: Milvus Lite / 百万版: Milvus Cluster"""
    async def upsert(self, chunk_ids: list[str], user_id: str,
                     document_id: str, vectors: list[list[float]],
                     snippets: list[str]): ...
    async def search(self, query_vector: list[float], user_id: str,
                     top_k: int) -> list[dict]: ...
    async def delete_by_document(self, document_id: str): ...


@runtime_checkable
class FullTextSearchBase(Protocol):
    """全文检索 — 单实例: PG FTS (jieba) / 百万版: Elasticsearch (ik)"""
    async def search(self, query: str, user_id: str,
                     top_k: int) -> list[dict]: ...
    async def index_chunk(self, chunk_id: str, content: str,
                          user_id: str): ...
    async def delete_chunk(self, chunk_id: str): ...


@runtime_checkable
class ObjectStorageBase(Protocol):
    """文件存储 — 单实例: 本地 FS / 百万版: MinIO/S3"""
    async def save(self, key: str, data: bytes) -> str: ...
    async def load(self, key: str) -> bytes: ...
    async def delete(self, key: str): ...
    async def get_url(self, key: str) -> str: ...


# ── AI 服务抽象 ──────────────────────────────────────────────

@runtime_checkable
class EmbeddingServiceBase(Protocol):
    """嵌入服务 — 单实例: API / 百万版: GPU 自建（同一接口签名）"""
    async def encode(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class RerankServiceBase(Protocol):
    """重排序 — 单实例: Jina API / 百万版: GPU 自建"""
    async def rerank(self, query: str, documents: list[str],
                     top_n: int) -> list[dict]: ...


@runtime_checkable
class OcrServiceBase(Protocol):
    """OCR — 单实例: 腾讯云 API / 百万版: 自建 PaddleOCR"""
    def recognize(self, image_path: str) -> str: ...


@runtime_checkable
class LlmServiceBase(Protocol):
    """LLM — 单实例: glm-5.1 API / 百万版: DeepSeek API（只改 URL）"""
    async def stream_generate(self, query: str, context: str,
                              history: list | None = None
                              ) -> AsyncIterator[str]: ...


# ── 工厂方法 ──────────────────────────────────────────────────

def get_embedding_service() -> EmbeddingServiceBase:
    """通过 EMBEDDING_BACKEND 环境变量切换实现"""
    from app.services import embedding as mod
    return mod  # 当前: 模块级函数满足 Protocol


def get_vector_store() -> VectorStoreBase:
    from app.services.vector_store import get_vector_store as _get
    return _get()


def get_llm_service() -> LlmServiceBase:
    from app.services.llm import LLMService
    return LLMService()
```

**面试亮点**：Protocol（结构化子类型）+ 工厂模式的组合。
不是继承，而是鸭子类型 —— 实现类不需要显式继承接口，只要方法签名匹配就行。
迁移时新增实现类，改工厂方法的 return 路径，业务代码零改动。

---

# 第三部分：数据模型

## 6. 数据库设计

**设计原则**：
- 表结构与百万用户版本字段一致
- UUID 主键（分布式友好，不需要中央 ID 生成器）
- JSONB 存储灵活元数据（不需要频繁加列）
- 所有查询强制带 `user_id` 过滤（为未来 RLS 做准备）

### 6.1 表结构

```sql
-- ── 用户表 ──────────────────────────────────────────────────
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           VARCHAR(255) UNIQUE NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    display_name    VARCHAR(100),
    avatar_url      VARCHAR(500),
    plan            VARCHAR(20) DEFAULT 'free',
    settings        JSONB DEFAULT '{}',
    storage_used    BIGINT DEFAULT 0,
    vector_count    INT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ── 收藏夹 ──────────────────────────────────────────────────
CREATE TABLE collections (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id),
    parent_id   UUID REFERENCES collections(id),
    name        VARCHAR(200) NOT NULL,
    icon        VARCHAR(50),
    sort_order  INT DEFAULT 0,
    is_deleted  BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- ── 文档 ────────────────────────────────────────────────────
CREATE TABLE documents (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id),
    collection_id     UUID REFERENCES collections(id),
    title             VARCHAR(500) NOT NULL,
    source_type       VARCHAR(20) DEFAULT 'upload',
    source_url        VARCHAR(2000),
    file_path         VARCHAR(500),
    file_size         BIGINT,
    mime_type         VARCHAR(100),
    content_hash      VARCHAR(64),       -- 跨用户去重（见 §8 安全说明）
    processing_status VARCHAR(20) DEFAULT 'pending',
    processing_error  TEXT,
    chunk_count       INT DEFAULT 0,
    page_count        INT,
    metadata          JSONB DEFAULT '{}',
    is_deleted        BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- ── 分块（含 FTS）──────────────────────────────────────────
-- 为什么用 jieba 而非 simple：simple 分词器只按空格切词，
-- 中文句子会变成一整个 token，FTS 完全失效。
-- jieba 在应用层分词后写入 tsvector，写入和查询使用同一套分词体系。
CREATE TABLE chunks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id),
    content         TEXT NOT NULL,
    chunk_index     INT NOT NULL,
    chunk_type      VARCHAR(20) DEFAULT 'child',
    parent_chunk_id UUID REFERENCES chunks(id),
    char_start      INT,
    char_end        INT,
    page_number     INT,
    token_count     INT,
    fts_tokens      TEXT,            -- jieba 分词结果（空格分隔）
    fts             TSVECTOR GENERATED ALWAYS AS (
                        to_tsvector('simple', COALESCE(fts_tokens, ''))
                    ) STORED,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ── 会话 ────────────────────────────────────────────────────
CREATE TABLE conversations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    title           VARCHAR(200),
    model_name      VARCHAR(50) DEFAULT 'glm-5.1-openai',
    message_count   INT DEFAULT 0,
    is_deleted      BOOLEAN DEFAULT FALSE,
    last_message_at TIMESTAMPTZ DEFAULT now(),
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- ── 消息 ────────────────────────────────────────────────────
CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id),
    role            VARCHAR(20) NOT NULL,
    content         TEXT NOT NULL,
    citations       JSONB,
    model_name      VARCHAR(50),
    feedback        VARCHAR(20),         -- 'positive' / 'negative'
    token_usage     JSONB,
    agent_trace     JSONB,               -- Agent 推理轨迹（调试 + 评估）
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- ── 标签 ────────────────────────────────────────────────────
CREATE TABLE tags (
    id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id  UUID NOT NULL REFERENCES users(id),
    name     VARCHAR(50) NOT NULL,
    color    VARCHAR(7),
    UNIQUE(user_id, name)
);

CREATE TABLE document_tags (
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    tag_id      UUID REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, tag_id)
);

-- ── 用户记忆（Agent 记忆系统）───────────────────────────────
CREATE TABLE user_memories (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id),
    memory_type VARCHAR(20) NOT NULL,    -- 'preference' / 'feedback' / 'topic'
    key         VARCHAR(100),
    value       JSONB NOT NULL,
    source      VARCHAR(50),             -- 'interaction' / 'feedback' / 'explicit'
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(user_id, memory_type, key)
);
```

### 6.2 索引

```sql
-- 核心查询索引（之前缺失，CTO 评审修复）
CREATE INDEX idx_documents_user_active
    ON documents(user_id) WHERE is_deleted = FALSE;
CREATE INDEX idx_documents_collection
    ON documents(collection_id) WHERE is_deleted = FALSE;
CREATE INDEX idx_conversations_user_active
    ON conversations(user_id) WHERE is_deleted = FALSE;
CREATE INDEX idx_messages_conversation
    ON messages(conversation_id);
CREATE INDEX idx_chunks_document_user
    ON chunks(document_id, user_id);

-- FTS 索引
CREATE INDEX idx_chunks_fts ON chunks USING GIN(fts);

-- 记忆索引
CREATE INDEX idx_user_memories_user ON user_memories(user_id);
CREATE INDEX idx_user_memories_value ON user_memories USING GIN(value jsonb_path_ops);
```

---

# 第四部分：核心模块设计

## 7. 文档处理管线

### 7.1 处理流程

```
用户上传文件
     │
     ├── 1. 保存到本地文件系统
     ├── 2. 创建 Document（status=pending）
     ├── 3. content_hash 去重（用户内，不暴露跨用户信息，见 §13.3）
     ├── 4. Celery 异步任务：process_document
     │     ├── [Hook: pre_parse]  格式检测
     │     ├── 解析（PyMuPDF / python-docx / OCR API）
     │     ├── [Hook: post_parse] 去重检查、质量校验
     │     ├── 结构化分块（512 token, overlap=64）
     │     ├── [Hook: post_chunk] 分块质量检查
     │     ├── jieba 分词 → 写入 fts_tokens 列
     │     ├── Embedding API 批量向量化 → Milvus 写入
     │     ├── 写入 chunks 表
     │     ├── [Hook: post_embed] 索引一致性校验
     │     └── 更新 status=ready
     └── 5. 前端 SSE 轮询状态

     如果任何步骤失败:
     ├── [Hook: on_error] 错误处理钩子
     │     ├── partial_success: 保留已成功的部分
     │     └── retry: 换策略重试（如 OCR 失败 → 跳过该页继续）
     └── 更新 status=failed + processing_error
```

### 7.2 延迟估算（10 页 PDF，~100 chunks）

| 步骤 | 耗时 | 说明 |
|------|------|------|
| 解析 | ~3s | PyMuPDF 提取文本 |
| 分块 + jieba 分词 | ~1s | 纯 CPU 计算 |
| 向量化（100 chunks，batch=64） | ~5s | 2 次 API 调用 |
| Milvus 写入 | ~2s | 嵌入式引擎 |
| PG 写入 | ~1s | 含 FTS 索引更新 |
| **总计** | **~12s** | |

### 7.3 Hook 系统

**为什么这样设计**：借鉴 Claude Code 的 Hook 系统（事件驱动的可插拔扩展）。
文档处理管线最容易出问题（OCR 失败、解析异常、分块质量差），
Hook 让每个阶段都可以插入自定义逻辑，不需要改核心流程。

```python
# app/services/hooks.py

from dataclasses import dataclass, field
from typing import Callable, Awaitable


@dataclass
class HookContext:
    document_id: str
    user_id: str
    stage: str          # "pre_parse" | "post_parse" | "post_chunk" | "on_error"
    data: dict
    errors: list[str] = field(default_factory=list)
    skip_remaining: bool = False  # Hook 可中断后续处理


HookFunc = Callable[[HookContext], Awaitable[HookContext]]


class ProcessingHooks:
    """文档处理管线钩子注册表"""

    def __init__(self):
        self._hooks: dict[str, list[HookFunc]] = {}

    def register(self, stage: str, hook: HookFunc):
        self._hooks.setdefault(stage, []).append(hook)

    async def fire(self, ctx: HookContext) -> HookContext:
        for hook in self._hooks.get(ctx.stage, []):
            ctx = await hook(ctx)
            if ctx.skip_remaining:
                break
        return ctx


# ── 内置 Hooks ──────────────────────────────────────────────

async def partial_success_hook(ctx: HookContext) -> HookContext:
    """on_error: 保留已成功的页面，跳过失败页。

    为什么需要这个：OCR 可能在某一页失败（扫描质量差、图片格式异常），
    但其他页面已经成功解析了。直接 fail 整个文档浪费了前面的工作。
    """
    if ctx.stage == "on_error" and "ocr" in str(ctx.errors).lower():
        successful = ctx.data.get("parsed_sections", [])
        if len(successful) > 0:
            ctx.data["use_partial"] = True
            ctx.errors = []  # 清除错误，允许继续
    return ctx


async def chunk_quality_hook(ctx: HookContext) -> HookContext:
    """post_chunk: 检查分块质量。

    为什么需要这个：如果解析器输出垃圾（乱码、空白页），
    分块会生成大量极短的无效 chunk。这些 chunk 被 embedding 后
    不仅浪费存储，还会污染检索结果。
    """
    chunks = ctx.data.get("chunks", [])
    short_count = sum(1 for c in chunks if len(c.get("content", "")) < 20)
    if short_count > len(chunks) * 0.5:
        ctx.errors.append(
            f"分块质量异常: {short_count}/{len(chunks)} 个分块少于 20 字符，"
            f"可能解析失败"
        )
    return ctx
```

---

## 8. 中文全文检索

**为什么这样设计**：这是前一轮 CTO 评审的必修项。
PostgreSQL 的 `simple` 分词器对中文完全无效（中文没有空格），
导致混合检索中的 FTS 分支形同虚设，检索质量退化为纯向量检索。

### 8.1 方案：应用层 jieba 分词 + PG FTS

**为什么不用 zhparser 扩展**：需要编译安装 C 扩展，需要 root 权限，
在腾讯云轻量服务器上可能有兼容性问题。
jieba 是纯 Python，零依赖，写入和查询使用同一套分词器，保证一致性。

```python
# app/services/chunking.py — 分块时同步分词

import jieba


def tokenize_for_fts(text: str) -> str:
    """中文分词，返回空格分隔的 token 字符串。

    写入 chunks.fts_tokens 列，PG 的 GENERATED ALWAYS AS
    to_tsvector('simple', fts_tokens) 会自动建 tsvector。

    'simple' 配置按空格切词 → 正好和 jieba 输出格式匹配。
    """
    tokens = jieba.lcut_for_search(text)
    # 过滤停用词和单字（单字太短，噪音大）
    filtered = [t for t in tokens if len(t) >= 2]
    return " ".join(filtered)
```

```python
# app/services/search.py — 查询时分词

async def _bm25_search(self, query: str, user_id: str, top_k: int) -> list[dict]:
    """FTS 搜索：查询端也用 jieba 分词，保证和写入一致。"""
    tokens = tokenize_for_fts(query)
    if not tokens.strip():
        return []

    sql = text("""
        SELECT c.id::text AS chunk_id,
               c.document_id::text AS document_id,
               ts_rank_cd(c.fts, plainto_tsquery('simple', :tokens)) AS score
        FROM chunks c
        WHERE c.user_id::text = :user_id
          AND c.fts @@ plainto_tsquery('simple', :tokens)
        ORDER BY score DESC
        LIMIT :limit
    """)
    async with session_factory() as db:
        result = await db.execute(sql, {"tokens": tokens,
                                        "user_id": user_id, "limit": top_k})
        return [{"chunk_id": r[0], "document_id": r[1], "score": float(r[2])}
                for r in result.fetchall()]
```

**面试亮点**：
1. 发现 `simple` 分词器对中文失效是个真实 bug，不是设计选择
2. 选择应用层 jieba 而非 PG 扩展，是因为部署约束下的务实选择
3. 写入和查询必须使用同一套分词器，否则 token 不匹配

---

## 9. 自适应检索管线（核心演进）

### 9.1 Stage 1：固定管线 RAG（当前实现）

当前 `chat.py` 的处理流程是一条固定管线：

```python
# chat.py — 固定管线
resolved = await resolve_query_with_history(query, history)  # 指代消解
results = await search_svc.search(resolved, user_id)          # 混合检索
context = search_svc.build_context(results)                   # 拼接上下文
async for token in llm_svc.stream_generate(query, context):  # LLM 生成
    yield token
```

**痛点**：
1. 搜索失败（返回空结果）时直接返回空回答，不会换策略重试
2. 所有查询类型走同一套参数（vector_weight=0.7, top_k=40），无法适应
3. `build_context` 按 char 数截断，一条长结果可能占满 8000 token 预算
4. 无学习能力，每次查询完全无状态

### 9.2 Stage 2：Tool-Based 自适应检索管线

**核心思想**：把检索能力封装为 Tool，管线根据查询类型自主选择工具组合。
借鉴 Claude Code 的 Tool System（Read/Edit/Bash/Grep），
但 Tool 不是给 LLM 用的，而是给决策引擎用的。

**为什么不让 LLM 直接调工具**：
- 当前规模（50 DAU）下每次查询多调一次 LLM 做决策，月成本增加 ~¥15
- 大部分查询（keyword/semantic）用规则就够了，LLM 决策是浪费
- 只在复杂查询（compare/multi_hop）才需要 LLM 辅助决策
- 这是"规则优先 + LLM fallback"策略，和 Claude Code 的 QueryAnalyzer 思路一致

**术语澄清**：Stage 2 不是 Agent。它是一个**规则驱动的状态机**，
根据查询类型和中间结果决定下一步调用哪个 Tool。
Stage 3（LLM Tool Use）才是真正的 Agent，决策由 LLM 完成。

```python
# app/agent/tools.py — RAG 工具定义

from dataclasses import dataclass
from typing import Protocol


@dataclass
class ToolResult:
    success: bool
    data: list[dict]
    metadata: dict | None = None


class RAGTool(Protocol):
    name: str
    description: str
    async def execute(self, params: dict) -> ToolResult: ...


class HybridSearchTool:
    """混合检索（向量 + FTS + RRF 融合）— 默认推荐工具"""
    name = "hybrid_search"
    description = "语义 + 关键词混合检索，适用于大多数查询"

    async def execute(self, params: dict) -> ToolResult:
        query = params["query"]
        user_id = params["user_id"]
        top_k = params.get("top_k", 40)
        v_weight = params.get("vector_weight", 0.7)
        b_weight = params.get("bm25_weight", 0.3)

        import asyncio
        vec_task = asyncio.create_task(self._vector_search(query, user_id, top_k))
        fts_task = asyncio.create_task(self._fts_search(query, user_id, top_k))
        vec_results, fts_results = await asyncio.gather(vec_task, fts_task)

        fused = self._rrf_fuse(vec_results, fts_results, v_weight, b_weight)
        return ToolResult(success=True, data=fused,
                          metadata={"count": len(fused)})


class FullTextSearchTool:
    """纯全文检索 — 精确关键词场景"""
    name = "fulltext_search"
    description = "关键词精确匹配，适用于编号、术语、引用查找"

    async def execute(self, params: dict) -> ToolResult:
        results = await self._fts_search(params["query"],
                                          params["user_id"],
                                          params.get("top_k", 20))
        return ToolResult(success=True, data=results)


class RerankTool:
    """重排序 — 对已有候选结果精排"""
    name = "rerank"
    description = "对搜索结果重排序，提升最相关结果到顶部"

    async def execute(self, params: dict) -> ToolResult:
        reranked = await self._rerank_api(params["query"],
                                           params["documents"],
                                           params.get("top_n", 10))
        return ToolResult(success=bool(reranked), data=reranked or [])


class QueryExpandTool:
    """查询扩展 — LLM 生成同义表述以提高召回"""
    name = "query_expand"
    description = "扩展查询为多个同义表述，适用于搜索结果不足时"

    async def execute(self, params: dict) -> ToolResult:
        expansions = await self._llm_expand(params["query"])
        return ToolResult(success=bool(expansions),
                          data=[{"expanded_query": e} for e in expansions])
```

### 9.3 自适应管线推理循环

**为什么用规则决策树而不是纯 LLM ReAct**：
1. **成本**：50 DAU × 10 次/天 = 500 次/天。如果每次都调 LLM 做决策，
   额外 ~500 × 200 token × ¥1/百万token = ¥3/天 = ¥90/月。翻倍了。
2. **延迟**：LLM 决策需要 ~1-2s，加上检索本身 ~0.5s，总延迟变成 3-4s。
3. **确定性**：规则决策是确定性的，同样的查询永远走同样的路径。
   LLM 决策有随机性，同样的查询可能走不同路径，影响用户体验。

**决策策略**：规则覆盖 80% 场景，LLM 只处理复杂查询（compare/multi_hop）。

```python
# app/agent/retrieval_agent.py

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 3
LATENCY_BUDGET_MS = 5000  # 5s 延迟硬上限


@dataclass
class AgentStep:
    thought: str
    tool_name: str
    tool_params: dict
    observation: dict
    latency_ms: float = 0


@dataclass
class AgentTrace:
    """完整推理轨迹 — 存入 messages.agent_trace 用于调试和评估"""
    query: str
    query_type: str
    steps: list[AgentStep] = field(default_factory=list)
    final_context: list[dict] = field(default_factory=list)
    total_tool_calls: int = 0
    total_latency_ms: float = 0


class RetrievalAgent:
    """Tool-Based 自适应检索管线。

    不是 Agent（LLM 不做决策），而是规则驱动的状态机。
    根据查询类型和中间结果决定调用哪个 Tool。
    Stage 3 演进方向：让 LLM 替代规则决策，成为真正的 ReAct Agent。
    """

    def __init__(self, tools: dict[str, RAGTool],
                 memory: "UserMemory | None" = None):
        self.tools = tools
        self.memory = memory
        self._expanded_queries: list[str] = []  # 扩展查询缓存

    async def run(self, query: str, user_id: str,
                  plan: dict) -> AgentTrace:
        trace = AgentTrace(query=query, query_type=plan["query_type"])
        t_start = time.monotonic()

        context_chunks = []

        for iteration in range(MAX_ITERATIONS):
            elapsed_ms = (time.monotonic() - t_start) * 1000
            if elapsed_ms > LATENCY_BUDGET_MS:
                logger.info(f"超出延迟预算 ({elapsed_ms:.0f}ms)，停止迭代")
                break

            # 决策：下一步做什么
            action = self._decide(query, context_chunks, plan, iteration)

            if action["type"] == "generate":
                break

            # 执行工具
            tool = self.tools.get(action["tool"])
            if not tool:
                continue

            t_tool = time.monotonic()
            result = await tool.execute({**action["params"], "user_id": user_id})
            tool_latency = (time.monotonic() - t_tool) * 1000

            step = AgentStep(
                thought=action.get("thought", ""),
                tool_name=action["tool"],
                tool_params=action["params"],
                observation={"success": result.success,
                             "count": len(result.data)},
                latency_ms=tool_latency,
            )
            trace.steps.append(step)
            trace.total_tool_calls += 1

            if result.success and result.data:
                # QueryExpandTool 返回扩展查询，不是搜索结果
                if action["tool"] == "query_expand":
                    self._expanded_queries = [
                        r["expanded_query"] for r in result.data
                    ]
                else:
                    context_chunks.extend(result.data)
                    # 去重
                    seen = set()
                    deduped = []
                    for c in context_chunks:
                        cid = c.get("chunk_id", id(c))
                        if cid not in seen:
                            seen.add(cid)
                            deduped.append(c)
                    context_chunks = deduped

        trace.final_context = context_chunks
        trace.total_latency_ms = (time.monotonic() - t_start) * 1000
        return trace

    def _decide(self, query: str, context: list,
                plan: dict, iteration: int) -> dict:
        """规则决策树。

        80% 的查询（keyword/semantic）在这里就决定了完整路径。
        只有搜索结果不足时才会进入重试/扩展逻辑。
        """
        query_type = plan["query_type"]

        # ── 第一轮：根据查询类型选择初始工具 ──
        if iteration == 0:
            if query_type == "keyword":
                return {
                    "type": "tool_call",
                    "tool": "fulltext_search",
                    "params": {"query": plan["rewritten"], "top_k": 30},
                    "thought": "精确关键词查询，使用全文检索",
                }
            else:
                return {
                    "type": "tool_call",
                    "tool": "hybrid_search",
                    "params": {"query": plan["rewritten"],
                               "vector_weight": 0.6 if query_type == "compare" else 0.7,
                               "bm25_weight": 0.4 if query_type == "compare" else 0.3},
                    "thought": f"{query_type} 类型查询，使用混合检索",
                }

        # ── 第二轮：检查结果，决定重试还是精排 ──
        if iteration == 1:
            if len(context) < 3:
                return {
                    "type": "tool_call",
                    "tool": "query_expand",
                    "params": {"query": plan["rewritten"]},
                    "thought": f"仅获得 {len(context)} 条结果，扩展查询",
                }
            else:
                return {
                    "type": "tool_call",
                    "tool": "rerank",
                    "params": {"query": plan["rewritten"],
                               "documents": context, "top_n": 10},
                    "thought": f"获得 {len(context)} 条候选，执行重排序",
                }

        # ── 第三轮：用扩展查询重新搜索 ──
        if iteration == 2 and self._expanded_queries and len(context) < 5:
            return {
                "type": "tool_call",
                "tool": "hybrid_search",
                "params": {"query": self._expanded_queries[0]},
                "thought": "使用扩展查询重新搜索",
            }

        # 默认：生成回答
        return {"type": "generate",
                "thought": f"已有 {len(context)} 条结果，开始生成"}
```

### 9.4 管线与 chat.py 的集成

```python
# app/api/chat.py — Agent 模式入口

@router.post("")
async def chat(req: ChatRequest, user: User = Depends(get_current_user),
               db: AsyncSession = Depends(get_db)):

    # Feature flag：可按用户灰度切换 Agent / 固定管线
    use_agent = settings.use_agent  # 从 config 读取，默认 True

    async def event_stream():
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as stream_db:
            try:
                # 指代消解（Agent 和固定管线共用）
                history_messages = await _load_history(stream_db, req, conversation)
                resolved_query = await resolve_query_with_history(
                    req.query, history_messages
                )

                if use_agent:
                    # ── Agent 路径 ──
                    plan = _plan_query(resolved_query, history_messages, user_memory)
                    trace = await agent.run(req.query, str(user.id), plan)

                    # 实时推送 Agent 轨迹（每次工具调用后立即 yield）
                    for step in trace.steps:
                        yield f"data: {json.dumps({'type': 'agent_step',
                            'tool': step.tool_name,
                            'thought': step.thought})}\n\n"

                    results = trace.final_context
                    context_text = SmartContextManager().format(
                        SmartContextManager().compress(results, req.query)
                    )
                else:
                    # ── 固定管线路径（回退） ──
                    results = await search_svc.search(resolved_query, str(user.id))
                    context_text = search_svc.build_context(results)

                # LLM 生成（两种路径共用）
                async for token in llm_svc.stream_generate(
                    req.query, context_text, history=chat_history
                ):
                    yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

                # 保存消息（含 agent_trace）
                await _save_message(stream_db, conversation, req, full_answer,
                                    citations, trace if use_agent else None)

            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

**面试亮点**：
1. **Feature flag**：Agent 和固定管线可以按用户灰度切换，出问题秒级回滚
2. **实时 SSE 推送**：Agent 每步执行后立即 yield，用户不用等全部完成
3. **Agent Trace 持久化**：推理轨迹存入 `messages.agent_trace`，可事后分析
4. **延迟预算**：5s 硬上限，超出停止迭代，保证用户体验

---

## 10. 智能上下文管理

**为什么这样设计**：当前 `build_context` 按 char 数截断，一条长结果可能占满预算。
改造为按分数加权分配 token 预算，高分结果完整保留，低分结果压缩。

```python
# app/agent/context.py

import jieba


class SmartContextManager:
    def compress(self, results: list[dict], query: str,
                 max_tokens: int = 8000) -> list[dict]:
        """按分数加权分配 token 预算。"""
        if not results:
            return []

        # Step 1: 分配预算
        total_score = sum(r.get("score", 0) for r in results) or 1
        for r in results:
            weight = max(r.get("score", 0) / total_score, 0.05)
            r["_budget"] = int(max_tokens * weight)

        # Step 2: 压缩超预算的内容
        compressed = []
        for r in results:
            content = r.get("parent_content") or r["content"]
            budget = r["_budget"]
            if len(content) <= budget:
                compressed.append(r)
            else:
                r = {**r, "content": self._smart_truncate(content, query, budget)}
                compressed.append(r)
        return compressed

    def _smart_truncate(self, content: str, query: str, budget: int) -> str:
        """保留与查询最相关的句子。

        为什么用 jieba 而不是 set(query)：
        set("对比Transformer") = {"对","比","T","r","a","n",...}
        每个单字都是关键词，所有句子都匹配，等于没有过滤。
        jieba 提取的是词组 ["对比","Transformer"]，才有区分度。
        """
        import re
        sentences = re.split(r'[。\n！？；]', content)
        if not sentences:
            return content[:budget]

        # 提取查询关键词
        query_keywords = set(jieba.lcut_for_search(query))
        query_keywords = {w for w in query_keywords if len(w) >= 2}

        # 为每个句子打分
        scored = []
        for i, s in enumerate(sentences):
            score = 0
            if any(kw in s for kw in query_keywords):
                score += 10
            if i == 0:
                score += 5  # 首句加权
            if i == len(sentences) - 1:
                score += 3  # 尾句加权
            scored.append((score, i, s))

        # 按分数选句子
        scored.sort(key=lambda x: x[0], reverse=True)
        selected_indices = []
        used = 0
        for score, idx, s in scored:
            if used + len(s) > budget:
                continue
            selected_indices.append((idx, s))
            used += len(s)

        # 按原文顺序排列
        selected_indices.sort(key=lambda x: x[0])
        return " ... ".join(s for _, s in selected_indices)

    def format(self, results: list[dict]) -> str:
        parts = []
        for i, r in enumerate(results):
            content = r.get("content", "")
            page = r.get("page_number")
            page_info = f"（第{page}页）" if page else ""
            parts.append(f"[{i + 1}]{page_info} {content}")
        return "\n\n".join(parts)
```

**面试亮点**：
1. 发现 `set(query)` 对中文无效是真实 bug，不是设计讨论
2. jieba 分词的一致性 —— 写入 FTS 用 jieba，截断也用 jieba，同一套分词器
3. 分数加权预算分配 —— 10 条结果不再被 1 条长结果挤掉

---

## 11. 用户记忆系统

**为什么这样设计**：借鉴 Claude Code 的分层记忆（user/feedback/project/reference），
让系统从用户交互中学习偏好，逐步优化检索策略。

```python
# app/agent/memory.py

import json
from dataclasses import dataclass, field, asdict


@dataclass
class UserPreferences:
    prefer_detailed: bool = False
    prefer_precise: bool = True
    common_topics: list[str] = field(default_factory=list)
    feedback_positive: int = 0
    feedback_negative: int = 0


class UserMemory:
    """用户记忆 — Redis 缓存 + PG 持久化"""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._prefs: UserPreferences | None = None

    async def get_preferences(self) -> UserPreferences:
        if self._prefs:
            return self._prefs
        cached = await self._redis_get(f"mem:{self.user_id}:prefs")
        if cached:
            self._prefs = UserPreferences(**json.loads(cached))
        else:
            self._prefs = await self._load_from_db() or UserPreferences()
        return self._prefs

    async def record_feedback(self, feedback: str, query: str = ""):
        """记录用户反馈，更新偏好。"""
        prefs = await self.get_preferences()
        if feedback == "positive":
            prefs.feedback_positive += 1
        elif feedback == "negative":
            prefs.feedback_negative += 1

        if query:
            topics = self._extract_topics(query)
            prefs.common_topics = (prefs.common_topics + topics)[-20:]

        await self._redis_set(f"mem:{self.user_id}:prefs",
                              json.dumps(asdict(prefs)), ttl=3600)
        await self._persist_to_db(prefs)

    def _extract_topics(self, query: str) -> list[str]:
        import jieba
        return [w for w in jieba.lcut(query) if len(w) >= 2][:3]

    # 记忆如何影响检索策略
    def apply_to_plan(self, plan: dict) -> dict:
        if not self._prefs:
            return plan
        if self._prefs.prefer_detailed:
            plan["top_k"] = 40
        if self._prefs.prefer_precise:
            plan["top_k"] = 10
        return plan
```

**面试亮点**：
1. 分层：Redis 热缓存 + PG 冷存储，TTL 1h
2. 偏好影响检索策略：详细回答偏好 → top_k=40，精确偏好 → top_k=10
3. 非阻塞持久化：反馈先写 Redis（毫秒级），异步写 PG（不阻塞响应）

---

## 12. 查询规划

复用现有 QueryAnalyzer 的分类能力，为 Agent 提供初始策略：

```python
# app/agent/planner.py

def plan_query(query: str, history: list[dict] | None,
               memory: UserMemory | None = None) -> dict:
    """查询规划 — 不调 LLM，纯规则。"""
    analyzer = QueryAnalyzer()
    analyzed = analyzer.analyze(query,
                                history=[m["content"] for m in history] if history else None)

    plan = {
        "query_type": analyzed.query_type,
        "rewritten": analyzed.rewritten,
        "sub_queries": analyzed.sub_queries,
        "top_k": 40,
        "need_expand": False,
    }

    if analyzed.query_type == "keyword":
        plan["top_k"] = 20
    elif analyzed.query_type in ("compare", "multi_hop"):
        plan["top_k"] = 40

    if memory:
        plan = memory.apply_to_plan(plan)

    return plan
```

---

# 第五部分：安全与可观测性

## 13. 安全设计

### 13.1 认证与授权

| 措施 | 单实例 | 百万版 |
|------|--------|--------|
| 密码 | bcrypt（cost=12） | 同 |
| Token | JWT（access 15min + refresh 7d） | 同 + Redis 黑名单 |
| 多租户隔离 | `WHERE user_id = ?`（所有查询强制） | PostgreSQL RLS |
| HTTPS | Nginx + Let's Encrypt | APISIX SSL |
| 限流 | Nginx rate_limit 10r/s | APISIX 插件 |
| 注入防护 | SQLAlchemy 参数化 + Pydantic 校验 | 同 |

### 13.2 API 调用安全

| 措施 | 说明 |
|------|------|
| HTTPS | 所有 API 端点必须使用 HTTPS（LLM/Embedding/Rerank/OCR） |
| .env 存储 | API Key 不入代码仓库 |
| 最小权限 | OCR API 只开通文字识别权限 |
| 轮换策略 | 定期更换 API Key |

**已知风险与修复计划**：

| 风险 | 严重度 | 修复方式 |
|------|--------|---------|
| Milvus filter 使用 f-string 拼接 user_id | 高 | 改为参数化过滤 `filter=QueryExpr("user_id == @uid", params={"uid": user_id})` |
| SSE error 直接返回 `str(e)` 给客户端 | 中 | 返回通用错误信息 `"处理失败，请重试"`，详细信息仅写日志 |
| LLM API 默认 HTTP 地址 | 中 | config.py 默认值改为空字符串，强制用户配置 HTTPS 地址 |
| vector_store.py pickle 反序列化 | 低 | 替换为 JSON 或 msgpack 序列化（牺牲性能换安全） |

### 13.3 content_hash 隐私说明

跨用户去重通过 content_hash 实现。

**风险**：如果用户 A 上传了敏感文档，用户 B 上传了相同文件，
通过去重查询可以判断其他用户是否上传了该文件（信息泄露）。

**修复**：
- 去重查询不返回 `user_id` 字段，只返回 `boolean` 表示是否已存在
- 百万版改为用户内去重（牺牲跨用户存储优化）

```python
# 安全的去重查询 — 不暴露其他用户信息
async def check_content_hash(hash_val: str, user_id: str) -> str | None:
    """返回已存在文档的 ID（仅限当前用户），不返回其他用户信息"""
    result = await db.execute(
        select(Document.id).where(
            Document.content_hash == hash_val,
            Document.user_id == user_id,      # 限制为当前用户
            Document.is_deleted == False
        ).limit(1)
    )
    return result.scalar_one_or_none()
```

---

## 14. 可观测性

### 14.1 监控指标

```python
# app/services/metrics.py

from prometheus_client import Histogram, Counter

# RAG 核心指标
rag_retrieval_duration = Histogram("rag_retrieval_duration_seconds", "检索耗时")
rag_rerank_duration = Histogram("rag_rerank_duration_seconds", "重排序耗时")
rag_llm_duration = Histogram("rag_llm_duration_seconds", "LLM 生成耗时")
rag_results_count = Histogram("rag_results_count", "返回结果数")

# API 调用指标
embedding_api_duration = Histogram("embedding_api_duration_seconds", "Embedding API")
rerank_api_duration = Histogram("rerank_api_duration_seconds", "Rerank API")
ocr_api_duration = Histogram("ocr_api_duration_seconds", "OCR API")
api_error_total = Counter("api_error_total", "API 失败", ["service"])

# Agent 指标（新增）
agent_iterations = Histogram("agent_iterations", "Agent 迭代次数")
agent_tool_duration = Histogram("agent_tool_duration_seconds", "Tool 执行耗时", ["tool"])
agent_trace_total = Counter("agent_trace_total", "Agent 执行总次数")
```

### 14.2 Agent Trace

每次 Agent 执行完成后，完整推理轨迹存入 `messages.agent_trace`：

```json
{
  "query": "对比 Transformer 和 RNN",
  "query_type": "compare",
  "steps": [
    {"thought": "compare 类型，混合检索", "tool": "hybrid_search",
     "latency_ms": 280, "observation": {"count": 38}},
    {"thought": "38 条候选，执行重排序", "tool": "rerank",
     "latency_ms": 310, "observation": {"count": 10}}
  ],
  "total_tool_calls": 2,
  "total_latency_ms": 590
}
```

---

## 15. 内存预算

| 组件 | 常驻内存 | 说明 |
|------|----------|------|
| OS + 系统服务 | 500 MB | 含腾讯云监控 |
| PostgreSQL 16 | 300 MB | shared_buffers=128MB |
| Redis | 50 MB | 缓存 + broker + 记忆 |
| Nginx | 20 MB | 反向代理 |
| FastAPI | 200 MB | 含 Agent + Tool 代码 |
| Milvus Lite | 200 MB | 嵌入式向量引擎 |
| Celery worker | 100 MB | 异步文档处理 |
| **常驻合计** | **~1.4 GB** | |
| **剩余** | **~2.2 GB** | 连接池 + 请求缓冲 + 数据增长 |

---

# 第六部分：部署与迁移

## 16. 部署方案

### 16.1 环境变量

```bash
# Database
DATABASE_URL=postgresql+asyncpg://knspace:knspace123@localhost/knspace

# Redis
REDIS_URL=redis://localhost:6379/0

# Embedding API
EMBEDDING_BACKEND=api
EMBEDDING_API_URL=https://your-api-host/v1
EMBEDDING_API_KEY=sk-xxx
EMBEDDING_MODEL=BAAI/bge-m3

# Rerank API
RERANK_BACKEND=api
RERANK_API_URL=https://api.jina.ai/v1/rerank
RERANK_API_KEY=jina_xxx
RERANK_MODEL=jina-reranker-v2-base-multilingual

# LLM API（必须使用 HTTPS）
LLM_API_URL=https://your-llm-host/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=glm-5.1-openai

# Agent
USE_AGENT=true
AGENT_MAX_ITERATIONS=3
AGENT_LATENCY_BUDGET_MS=5000

# File Storage
FILE_STORAGE_PATH=/data/knspace/files

# Vector Store
VECTOR_STORE_URI=./milvus_data.db
```

### 16.2 systemd

```ini
[Unit]
Description=knSpace RAG Knowledge Base
After=network.target postgresql@16-main.service redis-server.service

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/knSpacePro
Environment=PATH=/home/ubuntu/knSpacePro/venv/bin:/usr/bin
EnvironmentFile=/home/ubuntu/knSpacePro/.env
ExecStart=/home/ubuntu/knSpacePro/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 17. 迁移到百万用户版本

### 17.1 迁移复杂度分级

| 组件 | 单实例 → 百万版 | 复杂度 | 工作量 | 说明 |
|------|----------------|--------|--------|------|
| AI 服务（Embed/Rerank/OCR/LLM） | 改 URL | **低** | 0.5 天 | 接口签名不变 |
| Redis 单机 → Cluster | 改连接串 | **低** | 0.5 天 | 驱动透明支持 |
| 本地 FS → MinIO/S3 | 新增实现类 | **低** | 1 天 | ObjectStorageBase 已抽象 |
| Milvus Lite → Cluster | bulk export + import | **中** | 2-3 天 | 无官方迁移工具，需自建 |
| PG FTS → Elasticsearch | 重建索引 + 调分词 | **中** | 2-3 天 | jieba → ik 分词，需验证一致性 |
| Celery → Kafka | 任务模型差异大 | **中** | 3-5 天 | Task → Event 语义转换 |
| **单 PG → Citus 分片** | **分片键 + co-location + 跨分片查询** | **高** | 5-10 天 | **非"加路由层"这么简单** |
| systemd → K8s | Dockerfile + manifests | **中** | 2-3 天 | 标准化 |

### 17.2 Citus 分片设计（为什么是"高"复杂度）

```
原方案说"加路由层"—— 这严重低估了分片迁移的复杂度。

实际情况:
1. Citus 需要 SELECT create_distributed_table('chunks', 'user_id')
   所有按 user_id 查询的 SQL 自动路由到对应 shard

2. collections.parent_id 自引用 → 跨 shard JOIN → 性能问题
   需要 co-location: create_distributed_table('collections', 'user_id',
                       colocate_with => 'chunks')

3. document_tags 多对多关联 → 跨 shard 查询
   需要改为 reference table 或在应用层做两次查询

4. ON DELETE CASCADE 行为变化 → 分布式外键 ≠ 本地外键

5. 事务边界变化 → 单机事务变成分布式 2PC，延迟增加

结论：表结构字段名可以一致，但分片键设计和 co-location 策略
是额外的高复杂度工作，不能在迁移检查清单里一笔带过。
```

### 17.3 迁移检查清单

**代码层**：
- [ ] 所有业务代码通过 `factory.py` 获取服务实例
- [ ] 所有 Protocol 接口完整覆盖
- [ ] 所有 SQL 查询包含 `user_id` 过滤
- [ ] 无硬编码路径/连接/模型名
- [ ] config.py 集中管理所有配置

**数据层**：
- [ ] 表结构字段名与百万版一致
- [ ] UUID 主键
- [ ] JSONB 灵活元数据
- [ ] jieba 分词写入 fts_tokens（迁移 ES 时作为对照）

**AI 服务层**：
- [ ] Embedding 向量维度 1024 与 bge-m3 一致
- [ ] Rerank API 返回格式与 CrossEncoder 一致
- [ ] 所有 API 调用有超时（5s）+ 重试（2 次）+ fallback

**基础设施层**：
- [ ] Dockerfile 编写
- [ ] .env 管理环境变量
- [ ] JSON 格式日志（Loki 友好）
- [ ] Prometheus 指标埋点

---

# 第七部分：面试亮点总结

## 18. 面试可讲的设计点

### 18.1 Agent 工程能力

| 亮点 | 讲什么 | 背后的问题意识 |
|------|--------|--------------|
| **Tool 抽象** | 每个检索能力封装为 Tool，管线自主组合 | Claude Code 的 Read/Edit/Bash → RAG 的 Search/Rerank/Expand |
| **规则优先 + LLM fallback** | 80% 查询用规则决策（<1ms），复杂查询才调 LLM | 成本意识：每分钱都要有 ROI |
| **Feature flag 灰度** | 自适应管线和固定管线可按用户切换，秒级回滚 | 生产就绪：新架构必须有逃生通道 |
| **Agent Trace** | 完整推理轨迹持久化，可事后分析和评估 | 可观测性：管线不能是黑盒 |
| **术语诚实** | Stage 2 不叫 Agent，叫"自适应检索管线" | 面试官追问时不会露馅 |

### 18.2 RAG 工程能力

| 亮点 | 讲什么 |
|------|--------|
| **中文 FTS 修复** | 发现 `simple` 分词器对中文失效，应用层 jieba 统一写入和查询的分词 |
| **混合检索 + RRF** | 向量检索 + BM25 双通道，RRF 无需调参自动平衡 |
| **智能上下文压缩** | 按分数加权分配 token 预算，jieba 关键词匹配选句子 |
| **Hook 系统** | 文档处理管线可插拔，partial success 不丢已解析内容 |

### 18.3 系统设计能力

| 亮点 | 讲什么 |
|------|--------|
| **接口抽象 + 工厂** | Protocol + Factory，迁移零改动 |
| **演进式设计** | 不是一步到位的 Agent，是从 RAG 痛点逐步演进 |
| **诚实的迁移评估** | Citus 分片标注为"高复杂度"，不粉饰 |
| **成本量化** | ¥43/月 API 成本 vs ¥100/月服务器升级 |

### 18.4 演进方向（展示成长空间）

面试时可以说"未来演进方向"：

1. **LLM Tool Use**：让 LLM 直接调用工具（glm-5.1 的 function calling），
   替代规则决策树，成为真正的 ReAct Agent
2. **多跳推理**：复杂问题自动分解为多步检索链
3. **RAG 评估自动化**：faithfulness / relevance / hallucination 检测
4. **对话记忆分层**：短期/长期/工作记忆，类似 Claude Code 的 memory 体系

---

## 附录 A：资源升级路线图

| 配置 | 可支撑规模 | 月成本 | 新增能力 |
|------|-----------|--------|---------|
| **4C/4G**（当前） | 1,000 用户 / 10,000 文档 | ~¥143 | 全部核心功能 |
| 4C/8G | 5,000 用户 / 50,000 文档 | ~¥300 | + ES + Grafana |
| 8C/16G | 20,000 用户 / 200,000 文档 | ~¥600 | + Milvus Standalone |
| 集群 | 100,000+ DAU | ~¥10,000+/月 | 完整百万用户架构 |

## 附录 B：API 延迟基准（待测）

| API | P50 目标 | P95 目标 | P99 目标 |
|-----|---------|---------|---------|
| Embedding（单条） | < 300ms | < 500ms | < 1s |
| Embedding（batch=64） | < 2s | < 3s | < 5s |
| Rerank（40 pairs） | < 300ms | < 500ms | < 1s |
| OCR（单页） | < 1s | < 2s | < 3s |
| LLM（首 token） | < 2s | < 3s | < 5s |
| Agent 端到端 | < 3s | < 5s | < 8s |
