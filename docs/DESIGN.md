# knSpace 详细设计文档

> 基于 4C/4G 腾讯云单实例部署的私有化 RAG + Agent 知识库系统。
> 全局内容池（ContentPool）实现跨用户数据去重和引用计数管理。
> 本文档描述完整系统设计，与代码一一对应。

---

## 1. 项目概览

### 1.1 定位

knSpace 是一个私有化部署的 RAG + Agent 知识库系统。用户上传文档（PDF/Word/Markdown/图片/网页），系统自动解析、分块、向量化，然后基于文档内容进行智能问答。系统内置 LangGraph Agent 编排层，复杂查询由 Agent 驱动动态规划、调用工具、反思重试。

### 1.2 核心能力

- 多格式文档解析：PDF（PyMuPDF）、Word（python-docx）、Markdown、图片（OCR）、网页（Playwright）
- 结构化父子分块：按标题层级分组，父 chunk 提供完整上下文，子 chunk 精确检索
- 全局内容池去重：ContentPool + SHA256 引用计数，相同内容只存一份 text+vector
- 混合检索：向量检索（Milvus）+ 全文检索（Elasticsearch）+ RRF 融合
- 查询智能分析：LLM 为主 + 规则快路径分类（keyword/semantic/compare/multi_hop），动态调整检索权重
- 多轮对话指代消解：LLM 为主 + 规则快路径消解 + 幻觉校验，支持"它""这个"等代词
- 流式响应：SSE 实时推送 LLM token + 检索状态
- 跨用户文档去重：相同文件只处理一次，其他用户通过 ref_count 克隆 chunk
- Agent 编排：LangGraph 状态图驱动，意图分类→规划→工具执行→生成→反思闭环
- 混合路由：规则引擎分流，简单查询走固定管线，复杂查询走 Agent
- 降级机制：系统过载 / Agent 超时(60s) / 运行时异常均可降级到固定管线，SSE 流内降级带用户提示
- 延迟 embedding：API 不可用时文本先行入库（BM25 可用），API 恢复后自动补向量
- Agent 重试策略升级：4 级参数调整（保守→中等→激进→原始 query 全量检索），重试耗尽后诚实回答

### 1.3 技术栈

| 层级 | 技术 | 版本 | 用途 |
|------|------|------|------|
| 语言 | Python | 3.12 | 后端 |
| Web 框架 | FastAPI + Uvicorn | 0.115.6 | 异步 HTTP 服务 |
| 数据库 | PostgreSQL | 16 | 业务数据 + Agent 状态持久化 |
| ORM | SQLAlchemy | 2.0.36 | 异步 ORM（Mapped + mapped_column） |
| 向量库 | Milvus Standalone | 2.5.6 | 向量存储 + COSINE 检索（Docker） |
| 搜索引擎 | Elasticsearch | 8.17.0 | 全文检索 + jieba 分词（Docker） |
| 缓存 | Redis | 7.x | Embedding 缓存 + 限流 + Agent 热状态 |
| Agent 编排 | LangGraph | ≥0.2 | 状态图编排，Agent 循环控制 |
| 前端 | Vue 3 + Vite + Tailwind | - | SPA，构建后由 FastAPI 静态托管 |
| 反向代理 | Nginx | - | SSL + 限流 + 静态资源 |
| Embedding | SiliconFlow API | BAAI/bge-m3 | 1024 维向量（云端 API） |
| Reranker | SiliconFlow API | BAAI/bge-reranker-v2-m3 | 精排（云端 API） |
| OCR | SiliconFlow API | DeepSeek-OCR | 图片文字识别（云端 API） |
| LLM（生成） | OpenAI 兼容 API | glm-5.1-openai | 流式生成（云端 API） |
| LLM（规划/反思） | OpenAI 兼容 API | glm-4.5-air | 轻量模型，规划 + 反思 + 指代消解 + 意图分类 |
| 中文分词 | jieba | 0.42.1 | 全文检索分词 |
| 监控 | Prometheus + Grafana | - | 指标采集 + 可视化 |

### 1.4 硬件约束

4 核 AMD EPYC 7K62 / 4 GB 内存 / 40 GB SSD / 腾讯云轻量 4Mbps

全部 AI 模型走云端 API，本地 reranker 已移除（防止 4G 机器 OOM）。LangGraph Runtime 为纯编排层 Python 对象。月成本约 43 元（API）+ 100 元（服务器）。

---

## 2. 架构总览

### 2.1 系统架构

```
                        ┌──────────────────┐
                        │      Nginx       │  SSL 终结 + 限流 + 静态资源
                        └────────┬─────────┘
                                 │
                 ┌───────────────▼───────────────────┐
                 │     FastAPI (uvicorn) :8000         │
                 │                                     │
                 │  ┌──────┐ ┌──────┐ ┌─────────────┐ │
                 │  │ Auth │ │ Doc  │ │   Chat API  │ │
                 │  │ API  │ │ API  │ │  (统一入口)  │ │
                 │  └──────┘ └──────┘ └──────┬──────┘ │
                 │                            │        │
                 │         ┌─────────────────▼──────┐ │
                 │         │     Query Router       │ │ ← 意图分类，分流
                 │         │  简单→固定管线  复杂→Agent│ │
                 │         └──┬─────────────────┬───┘ │
                 │            │                 │      │
                 │  ┌─────────▼──────┐  ┌──────▼────┐│
                 │  │   固定 RAG     │  │   Agent   ││
                 │  │    管线        │  │ Controller││
                 │  │               │  │ ┌────────┐││
                 │  │ query_analyzer│  │ │LangGraph│││
                 │  │ search→rerank │  │ │Runtime  │││
                 │  │ llm generate  │  │ └────────┘││
                 │  │               │  │ ┌──────┐  ││
                 │  │               │  │ │Tools │  ││ ← 包装检索服务
                 │  │               │  │ └──────┘  ││
                 │  └───────────────┘  └──────┬────┘│
                 └──────────────────────────────┼────┘
                                                  │
              ┌───────────┬──────────────┬───────▼──────┐
              │           │              │              │
       ┌──────▼──┐  ┌────▼─────┐  ┌────▼─────┐  ┌────▼────┐
       │PostgreSQL│  │ Milvus   │  │   ES     │  │  Redis  │
       │业务+内容 │  │Standalone│  │ 8 jieba  │  │缓存+状态│
       │  池+状态 │  │ IVF_FLAT │  │ BM25全文 │  │         │
       └─────────┘  └──────────┘  └──────────┘  └─────────┘
       ┌───────────────────────────────────────────────────┐
       │                  云端 API                          │
       │  轻量 LLM：glm-4.5-air（规划 + 反思 + 指代消解）  │
       │  大模型：glm-5.1-openai（最终生成）                │
       │  原有 API：Embedding / Rerank / OCR                │
       └───────────────────────────────────────────────────┘
```

### 2.2 组件职责

| 组件 | 职责 | 端口 |
|------|------|------|
| Nginx | SSL 终结、限流 10r/s、SPA 静态资源 | 443/80 |
| FastAPI | 所有业务 API、SPA fallback | 8000 |
| PostgreSQL | 用户、文档、chunk、内容池、会话、标签、Agent 状态持久化 | 5432 |
| Milvus Standalone | 向量存储 + COSINE 检索，IVF_FLAT 索引 | 19530 |
| Elasticsearch 8.x | 全文检索，jieba 分词，content + content_jieba 双字段 | 9200 |
| Redis | Embedding 缓存（7天TTL）、API 限流（100次/时）、Agent 热状态（1h TTL） | 6379 |
| LangGraph Runtime | Agent 状态图编排（<30MB，纯内存） | - |

### 2.3 文件清单

```
app/
├── main.py                     # FastAPI 应用入口
├── config.py                   # Pydantic BaseSettings 配置
├── api/                        # 路由层
│   ├── auth.py                 # 认证（注册/登录/JWT）
│   ├── documents.py            # 文档上传/管理（含跨用户克隆 + ref_count GC）
│   ├── chat.py                 # 智能问答（SSE，含 Agent 路由分支）
│   ├── conversations.py        # 会话管理
│   ├── collections.py          # 收藏夹/标签
│   └── eval.py                 # RAG 评估
├── models/                     # SQLAlchemy 模型
│   ├── user.py                 # 用户
│   ├── document.py             # 文档
│   ├── chunk.py                # 分块（content_hash FK → content_pool）
│   ├── content_pool.py         # 全局内容池（SHA256 PK + text + vector + ref_count）
│   ├── conversation.py         # 会话
│   ├── message.py              # 消息（含 agent_trace JSONB）
│   ├── collection.py           # 收藏夹
│   ├── tag.py                  # 标签
│   ├── document_tag.py         # 文档标签关联
│   └── agent_checkpoint.py     # Agent 状态持久化（表结构已定义，功能待实现）
├── schemas/                    # Pydantic 请求/响应 Schema
│   └── chat.py                 # ChatRequest（含 use_agent 字段）
├── services/                   # 业务服务层
│   ├── doc_processor.py        # 文档处理管线（解析→分块→向量化→content_pool UPSERT 去重 + 延迟 embedding + 父 chunk 跳过 Milvus）
│   ├── content_gc.py           # 内容池 GC（二次校验 + 延迟删除补偿 + 每日一致性校验 + Milvus 修复 + 延迟补向量）
│   ├── parser.py               # 多格式解析
│   ├── chunking.py             # 结构化父子分块
│   ├── search.py               # 混合检索（Milvus + ES + 加权 RRF + Rerank + 余弦本地兜底）
│   ├── query_analyzer.py       # async LLM-first 查询分类 + 规则快路径
│   ├── multi_turn.py           # async LLM-first 指代消解 + 规则快路径 + 幻觉校验
│   ├── llm.py                  # LLM 流式生成
│   ├── embedding.py            # Embedding 服务（Redis→API→Local→Dummy）
│   ├── vector_store.py         # Milvus 向量存储（含 content_hash + insert_batch）
│   ├── es.py                   # Elasticsearch 全文检索（jieba 分词 + 动态 min_match + min_score 过滤）
│   ├── ocr.py                  # OCR 服务
│   ├── web_scraper.py          # 网页抓取
│   ├── tokenizer.py            # jieba 中文分词
│   ├── cache.py                # Redis 缓存 + 限流
│   ├── guard.py                # Prompt 注入检测
│   ├── citation.py             # 引用校验
│   ├── evaluator.py            # RAG 评估
│   ├── metrics.py              # Prometheus 指标
│   └── factory.py              # 7 个 Protocol 接口 + 工厂方法
├── agent/                      # Agent 编排层
│   ├── __init__.py
│   ├── state.py                # AgentState TypedDict 状态定义
│   ├── graph.py                # LangGraph 状态图构建
│   ├── nodes.py                # 7 个节点（async 意图分类/规划/执行/生成/反思/调参）
│   ├── tools.py                # 工具注册，包装检索服务
│   ├── router.py               # 查询路由（简单→固定管线，复杂→Agent，重试耗尽→诚实回答）
│   └── degrade.py              # 降级判断逻辑（CPU/内存水位检测 + LLM API 健康探针）
└── utils/
    └── security.py             # JWT + bcrypt
```

### 2.4 内存预算

| 组件 | 常驻 | 说明 |
|------|------|------|
| OS + 系统服务 | 500 MB | 含腾讯云监控 |
| PostgreSQL 16 | 310 MB | shared_buffers=128MB + checkpoint |
| Elasticsearch 8.x | 600 MB | 单节点，1 shard |
| Milvus Standalone | 500 MB | Docker，IVF_FLAT |
| FastAPI + 业务代码 | 200 MB | 含所有业务代码 + LangGraph |
| Redis | 60 MB | 缓存 + 限流 + Agent 热状态 |
| Nginx | 20 MB | 反向代理 |
| **常驻合计** | **~2.2 GB** | |
| **剩余可用** | **~1.8 GB** | 连接池 + 请求缓冲 |

---

## 3. 数据模型

### 3.1 ER 关系

```
users ──1:N──> documents ──1:N──> chunks ──N:1──> content_pool (全局唯一内容)
  │               │
  │               └── N:M ──> tags (via document_tags)
  │
  ├──1:N──> collections ──1:N──> documents
  │
  ├──1:N──> conversations ──1:N──> messages
  │
  └──1:N──> chunks

content_pool: 全局去重中心，相同内容只存一份（text + vector），通过 ref_count 管理生命周期
agent_checkpoints: Agent 状态持久化（thread_id = conversation_id，功能待实现）
```

### 3.2 表结构

```sql
-- 用户表 (app/models/user.py)
CREATE TABLE users (
    id              VARCHAR(36) PRIMARY KEY,
    email           VARCHAR(255) UNIQUE NOT NULL,
    password_hash   VARCHAR(255) NOT NULL,
    display_name    VARCHAR(100),
    avatar_url      VARCHAR(500),
    plan            VARCHAR(20) DEFAULT 'free',
    settings        JSON DEFAULT '{}',
    storage_used    BIGINT DEFAULT 0,
    vector_count    INT DEFAULT 0,
    question_count  INT DEFAULT 0,
    question_date   DATE,
    status          VARCHAR(20) DEFAULT 'active',
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- 收藏夹 (app/models/collection.py)
CREATE TABLE collections (
    id          VARCHAR(36) PRIMARY KEY,
    user_id     VARCHAR(36) NOT NULL REFERENCES users(id),
    parent_id   VARCHAR(36) REFERENCES collections(id),
    name        VARCHAR(200) NOT NULL,
    description TEXT,
    icon        VARCHAR(50),
    type        VARCHAR(20) DEFAULT 'folder',
    sort_order  INT DEFAULT 0,
    is_deleted  BOOLEAN DEFAULT FALSE,
    created_at  TIMESTAMPTZ DEFAULT now(),
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- 文档 (app/models/document.py)
CREATE TABLE documents (
    id                VARCHAR(36) PRIMARY KEY,
    user_id           VARCHAR(36) NOT NULL REFERENCES users(id),
    collection_id     VARCHAR(36) REFERENCES collections(id),
    title             VARCHAR(500) NOT NULL,
    source_type       VARCHAR(20) DEFAULT 'upload',
    source_url        VARCHAR(2000),
    file_path         VARCHAR(500),
    file_size         BIGINT,
    mime_type         VARCHAR(100),
    page_count        INT,
    word_count        INT,
    language          VARCHAR(10) DEFAULT 'zh',
    processing_status VARCHAR(20) DEFAULT 'pending',
    processing_error  TEXT,
    content_hash      VARCHAR(64),
    chunk_count       INT DEFAULT 0,
    metadata          JSON DEFAULT '{}',
    is_deleted        BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- 全局内容池 (app/models/content_pool.py)
CREATE TABLE content_pool (
    content_hash    CHAR(64) PRIMARY KEY,       -- SHA256(normalize(content)), 全局唯一去重键
    content         TEXT NOT NULL,               -- 原始文本（全局只存一份）
    vector          BYTEA,                      -- 向量二进制序列化（nullable：父 chunk/延迟 embedding 时为 NULL）
    ref_count       INT NOT NULL DEFAULT 1,      -- 引用计数：多少 Chunk 引用此内容
    token_count     INT NOT NULL,                -- 近似 token 数
    needs_embedding BOOLEAN NOT NULL DEFAULT FALSE, -- D9: API 不可用时标记，待补向量
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_content_pool_ref_count ON content_pool(ref_count);
CREATE INDEX idx_content_pool_needs_embedding ON content_pool(needs_embedding);

-- 分块 (app/models/chunk.py)
CREATE TABLE chunks (
    id              UUID PRIMARY KEY,
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id),
    content_hash    CHAR(64) NOT NULL REFERENCES content_pool(content_hash),
    chunk_index     INT NOT NULL,
    chunk_type      VARCHAR(20) DEFAULT 'child',
    parent_chunk_id UUID,
    char_start      INT,
    char_end        INT,
    page_number     INT,
    cleanup_status  VARCHAR(20) DEFAULT 'done',  -- D8: done/pending，Milvus/ES 删除失败时标记 pending
    metadata        JSON DEFAULT '{}',
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- 会话 (app/models/conversation.py)
CREATE TABLE conversations (
    id              VARCHAR(36) PRIMARY KEY,
    user_id         VARCHAR(36) NOT NULL REFERENCES users(id),
    title           VARCHAR(200),
    model_name      VARCHAR(50) DEFAULT 'glm-5.1-openai',
    message_count   INT DEFAULT 0,
    is_deleted      BOOLEAN DEFAULT FALSE,
    last_message_at TIMESTAMPTZ DEFAULT now(),
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

-- 消息 (app/models/message.py)
CREATE TABLE messages (
    id              VARCHAR(36) PRIMARY KEY,
    conversation_id VARCHAR(36) NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id         VARCHAR(36) NOT NULL REFERENCES users(id),
    role            VARCHAR(20) NOT NULL,
    content         TEXT NOT NULL,
    citations       JSON,
    model_name      VARCHAR(50),
    feedback        VARCHAR(20),
    token_usage     JSON,
    agent_trace     JSONB,          -- Agent 执行追踪数据
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- 标签 (app/models/tag.py)
CREATE TABLE tags (
    id       VARCHAR(36) PRIMARY KEY,
    user_id  VARCHAR(36) NOT NULL REFERENCES users(id),
    name     VARCHAR(50) NOT NULL,
    color    VARCHAR(7),
    UNIQUE(user_id, name)
);

-- 文档标签关联 (app/models/document_tag.py)
CREATE TABLE document_tags (
    document_id VARCHAR(36) REFERENCES documents(id) ON DELETE CASCADE,
    tag_id      VARCHAR(36) REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, tag_id)
);

-- Agent 状态持久化（功能待实现）
CREATE TABLE agent_checkpoints (
    thread_id      VARCHAR(36) PRIMARY KEY,   -- = conversation_id
    checkpoint_id  VARCHAR(36) NOT NULL,
    parent_id      VARCHAR(36),
    state          JSONB NOT NULL,
    created_at     TIMESTAMPTZ DEFAULT now()
);
```

### 3.3 关键设计决策

- **UUID 主键**：分布式友好。users/collections 等表用 `VARCHAR(36)`，chunks 表用原生 `UUID` 类型
- **JSON 元数据**：documents.metadata、chunks.metadata、users.settings 使用 JSON 字段，避免频繁加列
- **软删除**：documents、collections、conversations 使用 `is_deleted` 而非物理删除
- **所有查询强制带 user_id**：为未来 PostgreSQL RLS 做准备
- **ContentPool 全局去重**：chunks 表通过 `content_hash` 外键指向 `content_pool`，不存原文。相同内容（SHA256 匹配）只存一份 text+vector，通过 `ref_count` 引用计数管理生命周期。UPSERT（INSERT ON CONFLICT）解决多实例竞态。删除文档时递减 ref_count（`GREATEST(ref_count - 1, 0)` 防负数），ref_count≤0 时 GC 清理 Milvus/ES 冗余数据
- **三引擎 content_hash 贯穿**：PG（content_pool PK）→ Milvus（content_hash 字段）→ ES（content_hash keyword），通过 hash 关联实现跨引擎数据一致性
- **延迟 embedding（D9）**：API 不可用时 content_pool.vector=NULL、needs_embedding=True，BM25 可用；API 恢复后 `backfill_embeddings` 自动补向量
- **延迟物理删除（D8）**：Milvus/ES 删除失败时 chunk 标记 `cleanup_status="pending"`，5 分钟补偿任务重试

---

## 4. API 路由表

### 4.1 认证 `/api/v1/auth`

| 方法 | 路径 | 功能 | 认证 |
|------|------|------|------|
| POST | `/register` | 注册用户 | 无 |
| POST | `/login` | 登录，返回 access + refresh token | 无 |
| POST | `/refresh` | 用 refresh token 换新 token | 无 |
| GET | `/me` | 获取当前用户信息 | JWT |
| PATCH | `/me` | 更新用户资料 | JWT |
| PUT | `/password` | 修改密码 | JWT |

### 4.2 文档 `/api/v1/documents`

| 方法 | 路径 | 功能 | 认证 |
|------|------|------|------|
| POST | `/upload` | 上传文件（multipart/form-data），支持跨用户去重 | JWT |
| GET | `/` | 列出文档（分页，可按 collection 过滤） | JWT |
| GET | `/{doc_id}` | 获取文档详情 | JWT |
| DELETE | `/{doc_id}` | 删除文档（清理 Milvus + ES + content_pool GC + 文件） | JWT |
| GET | `/{doc_id}/status` | 查询处理状态 | JWT |
| GET | `/{doc_id}/status/stream` | SSE 流式查询处理状态（5 分钟超时） | JWT |
| POST | `/import-url` | 导入网页（scrape → chunk → embed） | JWT |

### 4.3 对话 `/api/v1/chat`

| 方法 | 路径 | 功能 | 认证 |
|------|------|------|------|
| POST | `/` | 智能问答（SSE 流式返回）。`use_agent=false` 走固定管线，`true` 走 Agent | JWT |

**请求体**：`{ query, conversation_id?, collection_id?, history?, use_agent? }`

### 4.4 会话 `/api/v1/conversations`

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | 列出会话（分页） |
| GET | `/{id}` | 获取会话详情 |
| PATCH | `/{id}` | 更新会话标题 |
| DELETE | `/{id}` | 软删除会话 |
| GET | `/{id}/messages` | 列出消息（分页） |
| POST | `/{id}/messages/{mid}/feedback` | 消息反馈 |

### 4.5 收藏夹与标签 `/api/v1/collections`

| 方法 | 路径 | 功能 |
|------|------|------|
| GET | `/` | 列出收藏夹 |
| POST | `/` | 创建收藏夹 |
| GET | `/{id}` | 获取收藏夹详情 |
| PATCH | `/{id}` | 更新收藏夹 |
| DELETE | `/{id}` | 软删除收藏夹 |
| GET | `/{id}/documents` | 列出收藏夹下的文档 |
| GET | `/tags` | 列出用户标签 |
| POST | `/tags` | 创建标签 |
| DELETE | `/tags/{id}` | 删除标签 |
| POST | `/documents/{doc_id}/tags/{tag_id}` | 添加文档标签 |
| DELETE | `/documents/{doc_id}/tags/{tag_id}` | 移除文档标签 |

### 4.6 评估 `/api/v1/eval`

| 方法 | 路径 | 功能 |
|------|------|------|
| POST | `/run` | 运行 RAG 评估（Recall@5/10, MRR, NDCG@10） |
| GET | `/results` | 获取上次评估结果 |
| POST | `/samples` | 添加评估样本 |

---

## 5. 文档处理管线

**入口**: `POST /api/v1/documents/upload` 或 `POST /api/v1/documents/import-url`
**核心服务**: `app/services/doc_processor.py:process_document()`

### 5.1 处理流程

```
 upload / import-url
        │
        ▼
 ┌──────────────┐
 │ 1. 读取文件   │  支持 PDF/Word/Markdown/图片/网页
 │    SHA-256    │  计算 document 级 content_hash
 └──────┬───────┘
        │
        ▼
 ┌──────────────┐
 │ 2. 去重检查   │  先查 user 内 content_hash，再查跨用户
 │              │  跨用户命中 → 克隆 chunk（ref_count+1，向量复用）
 └──────┬───────┘
        │
        ▼
 ┌──────────────┐
 │ 3. 保存文件   │  写入 data/files/{user_id}/{doc_id}.{ext}
 │    创建记录   │  Document.status = pending
 └──────┬───────┘
        │
        ▼
 ┌──────────────┐
 │ 4. 后台处理   │  asyncio.create_task(process_document)
 │   (异步任务)  │
 │              │
 │  parsing     │  parser.py → PyMuPDF / python-docx / Playwright
 │    │         │
 │    ▼         │
 │  chunking    │  chunking.py → 结构化父子分块（按标题层级）
 │    │         │
 │    ▼         │
 │  content_pool│  对每个 chunk 计算 SHA256(content_hash)
 │  去重写入    │  hash 已存在 → ref_count+1（跳过 embedding）
 │    │         │  hash 不存在 → 插入 text+vector 到 content_pool
 │    ▼         │
 │  ES index    │  es.py → bulk_index_chunks（带 content_hash）
 │    │         │  失败 → 回退 PG FTS (to_tsvector)
 │    ▼         │
 │  Milvus      │  vector_store.py → insert_batch（带 content_hash）
 │    │         │  向量来源：content_pool 序列化 BYTEA
 │    ▼         │
 │  ready       │  Document.status = ready, chunk_count 更新
 └──────────────┘
```

### 5.2 状态机

```
pending → parsing → chunking → content_pool UPSERT → embedding* → Milvus/ES写入 → ready
                │          │            │              │               │
                └──────────┴────────────┴──────────────┴───────────────┘→ failed

pending → cloning → ready  (跨用户去重克隆路径，失败 → failed)

* embedding 路径分支:
  API 可用 → 正常 embedding + Milvus 写入 → ready
  API 不可用 → vector=NULL + needs_embedding=True + 跳过 Milvus → pending_embedding
               → backfill_embeddings 定时补向量 → ready
```

### 5.3 关键设计

- **批量 Embedding**: 每 64 个 chunk 一批调用 API，减少请求次数
- **UPSERT 原子去重（D1）**: `INSERT INTO content_pool ... ON CONFLICT (content_hash) DO UPDATE SET ref_count = content_pool.ref_count + 1`，一行 SQL 解决多实例竞态 + 去重 + 引用计数
- **文本规范化哈希（D12）**: `_normalize_for_hash` 去除多余空白和末尾标点后再 SHA256，覆盖"成本43元" vs "成本43元。"等标点差异
- **父 chunk 跳过 Milvus（D11）**: 父 chunk 只存文本到 ContentPool（vector=NULL），不做 embedding、不写 Milvus。检索命中子 chunk 后通过 parent_chunk_id 回溯读文本
- **延迟 embedding（D9）**: `is_api_healthy()` 探测 LLM/Embedding API 可用性（30s 缓存），不可用时 `needs_embedding=True`，文档状态 `pending_embedding`。`backfill_embeddings` 用 PG advisory lock 保证单实例执行
- **Redis doc_id 锁（D10）**: `doc_processing:{doc_id}` 防止同一文档被两个 worker 并行处理
- **ES → PG FTS 回退**: Elasticsearch 不可用时自动回退到 PostgreSQL 全文检索（jieba 分词 + `to_tsvector`），同时自动降低 bm25_weight×0.5 补偿向量检索权重
- **父子分块**: 父 chunk 按标题分组提供完整段落上下文，子 chunk 精确检索；检索时自动附带 `parent_content`
- **跨用户克隆原子性**: 克隆流程通过 JOIN Chunk+ContentPool 获取源数据，增量 ref_count，插入元数据级 Chunk，从 content_pool 反序列化向量写入 Milvus/ES
- **删除 + GC**: 删除文档时：content_hashes 先 `set()` 去重再递减；`GREATEST(ref_count - 1, 0)` 防负数；Milvus/ES 删除失败时 chunk 标记 `cleanup_status="pending"`（D8），5 分钟补偿任务重试；GC 二次校验 Chunk 计数后再删 ContentPool（D2）

### 5.4 三引擎数据写入流程

```
                  chunk 文本 + 向量
                        │
          ┌─────────────┼─────────────┐
          │             │             │
          ▼             ▼             ▼
   ┌──────────┐  ┌──────────┐  ┌──────────┐
   │PostgreSQL│  │  Milvus  │  │   ES     │
   │content_  │  │ 向量检索  │  │ BM25全文  │
   │  pool    │  │  索引     │  │  索引     │
   │──────────│  │──────────│  │──────────│
   │content_  │  │chunk_id  │  │chunk_id  │
   │ hash(PK) │  │user_id   │  │user_id   │
   │content   │  │document_ │  │document_ │
   │ vector   │  │   id     │  │   id     │
   │ref_count │  │content_  │  │content_  │
   │token_    │  │  hash    │  │  hash    │
   │  count   │  │ vector   │  │ content  │
   │          │  │ snippet  │  │ content_ │
   │          │  │          │  │  jieba   │
   └──────────┘  └──────────┘  └──────────┘
        │              │              │
        └────── content_hash 贯穿 ──────┘
```

**数据一致性保证**：
- PG 是权威数据源（content_pool 存原文+向量，chunks 存元数据）
- Milvus/ES 是索引层，通过 content_hash 与 PG 关联
- 写入顺序：PG（content_pool + chunks）→ Milvus → ES
- 删除顺序：Milvus → ES → chunks → content_pool（ref_count GC）
- GC 安全：删除 ContentPool 前二次校验 PG 中 Chunk 引用计数（D2）
- 每日校验（D3）：ref_count vs 实际 Chunk 计数对比 + Milvus 孤儿检测 + 自动修复
- 实时指标（D3）：检索返回空但 PG 有数据时 `rag_data_loss` Counter 自增

---

## 6. 智能问答管线

### 6.1 固定 RAG 管线（`use_agent=false`，默认路径）

```
POST /api/v1/chat  { query, conversation_id?, collection_id?, use_agent: false }
        │
        ▼
 ┌─────────────────┐
 │ Prompt 注入检测   │  guard.py — 正则规则匹配
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 限流检查        │  cache.py — Redis 滑动窗口 100次/时
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 获取/创建会话    │  conversations 表，自动取标题
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 指代消解        │  multi_turn.py — async LLM 为主 + 规则快路径 + 幻觉校验
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 查询分析        │  query_analyzer.py — async LLM 为主 + 规则快路径 + 改写 + 子查询拆分
 │ keyword/semantic│  keyword→bm25_weight=0.7, semantic→vector_weight=0.7
 │ /compare/multi  │  compare→拆为2个子查询, multi_hop→按实体拆分
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 混合检索        │  search.py — 向量(Milvus) + 全文(ES) → RRF融合 → top40
 │                 │  _fetch_chunks() JOIN content_pool 获取文本内容
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ Rerank 精排     │  RerankAdapter — API only → top10
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 构建 Context    │  search.build_context() — 引用编号 [1]...[N], max 8000 tokens
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ LLM 流式生成    │  llm.py — OpenAI 兼容 SSE, glm-5.1-openai
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 引用校验        │  citation.py — 验证 [N] 编号合法性，去除幻觉引用
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 保存消息        │  messages 表：user_msg + assistant_msg
 └─────────────────┘
```

### 6.2 Agent 管线（`use_agent=true`）

```
POST /api/v1/chat  { query, use_agent: true }
        │
        ▼
 ┌─────────────────┐
 │ Query Router    │  router.py — 判断是否降级
 │ should_degrade? │  degrade.py: CPU>80% / MEM>85% → 降级到固定管线（带用户通知）
 └────┬────────────┘
      │ 正常
      ▼
 ┌─────────────────────┐
 │ 指代消解             │  resolve_query_with_history — async LLM 为主 + 规则快路径 + 幻觉校验
 │ resolved_query      │  消解后的 query 传入 graph，原始 query 保留在 original_query
 └────┬────────────────┘
      │
      ▼
 ┌─────────────────────┐
 │ graph.ainvoke()     │  asyncio.wait_for(timeout=60s)
 │  超时 → 流内降级     │
 │                     │
 │  ┌────────────────┐ │
 │  │intent_classify │ │  节点1: async QueryAnalyzer（LLM 为主 + 规则快路径）
 │  │                │ │  返回 intent + has_keyword 传递给 generate_plan
 │  │ simple         │ │  keyword / 单轮 semantic → simple → 走固定管线检索
 │  │ complex        │ │  compare / multi_hop → complex
 │  └───┬────────┬───┘ │
 │      │simple  │complex
 │      ▼        ▼     │
 │  ┌────────┐ ┌─────────────┐
 │  │ 固定   │ │generate_plan│  节点2: glm-4.5-air 生成子问题 DAG
 │  │ 管线   │ │             │  has_keyword=True 时注入 fulltext_search 优先提示
 │  │ 检索   │ │             │  LLM 输出带 depends_on 的子查询列表（上限3个）
 │  │+生成   │ │             │  _extract_json 三级容错 + 工具名白名单 + fallback
 │  │(END)   │ └───┬─────────┘
 │  └────────┘┌────▼──────────┐
 │            │ execute_tools │  节点3: 按 DAG 依赖顺序执行 + 增量上下文注入
 │            │               │  hybrid_search 复用 SearchService.search_with_weights()
 │            │               │  零结果时自动 fallback fulltext_search 补检索
 │            └───┬───────────┘
 │                │
 │            ┌────▼──────────┐
 │            │generate_answer│  节点4: 复用 LLMService，用 original_query 生成
 │            └───┬───────────┘
 │                │
 │            ┌────▼──────────┐
 │            │   reflect     │  节点5: glm-4.5-air 校验答案
 │            │               │  传入 top5 chunk 原文（_smart_truncate 按句子边界截断）
 │            │               │  分维度评分：relevance / groundedness / consistency
 │            │               │  上限 max_retries + 2 次（给 level 3 策略留出口）
 │            └───┬───────────┘
 │                │
 │        ┌───────┼──────────┐
 │        │ pass  │ fail &   │
 │        │       │ retry≤N+1│
 │        ▼       ▼          │
 │    ┌──────┐ ┌──────────┐  │
 │    │ END  │ │adjust_   │  │  节点6: 4 级策略（保守→中等→激进→原始 query 全量检索）
 │    │      │ │  params  │──┘  level 0-2: 调参数 / level 3: original_query + top_k=100
 │    └──────┘ └──────────┘
 └─────────────────────┘

 异常处理（三层降级）:
 1. SSE 构建前异常 → 返回降级 StreamingResponse（带通知）
 2. graph 超时 60s → 流内降级（带通知）
 3. 流内其他异常 → 流内降级（带通知）

 重试耗尽处理（D7）:
 - 4 级重试全部尝试后 → reflection_result="max_retries_exhausted"
 - router.py 输出 NO_DATA_RESPONSE（诚实回答），不再追加质量警告
 - 跳过/解析失败 → 追加分级质量警告（_build_quality_warning）
```

### 6.3 SSE 事件格式

所有响应均为 `text/event-stream`，事件格式：

```json
{"type": "conversation", "conversation_id": "uuid"}
{"type": "status", "message": "Searching..."}
{"type": "agent_step", "tool": "agent|hybrid_search|...", "thought": "意图分析中...|检索到 N 个结果"}
{"type": "citations", "data": [{"chunk_id": "...", "score": 0.95, "snippet": "..."}]}
{"type": "token", "content": "部分回答文本"}
{"type": "done", "conversation_id": "uuid"}
{"type": "error", "message": "错误描述"}
```

前端通过 `EventSource` 或 `fetch` + `ReadableStream` 消费。

---

## 7. 混合检索引擎

### 7.1 双通道架构

```
                    query
                      │
                      ▼
              ┌───────────────┐
              │ QueryAnalyzer │  async LLM 为主 + 规则快路径 → rewritten query + weights
              └───┬───────┬───┘
                  │       │
          ┌───────▼──┐ ┌──▼────────┐
          │ Milvus   │ │   ES 8.x  │
          │ 向量检索  │ │ BM25全文   │
          │ COSINE   │ │ jieba分词  │
          │ top_k=40 │ │ 动态min_match│
          └───────┬──┘ └──┬────────┘
                  │       │
                  ▼       ▼
          ┌───────────────────┐
          │  Weighted RRF     │  加权倒数排名融合（引入原始分数惩罚低质量结果）
          │  top 40 candidates│
          └────────┬──────────┘
                   │
                   ▼
          ┌───────────────────┐
          │    Rerank 精排     │  API → 余弦相似度本地兜底（D9）
          │    top 10 results │
          └───────────────────┘
```

### 7.2 加权 RRF 融合公式（D4）

```
score(chunk) = Σ  weight × norm(score) / (RRF_K + rank + 1)

- norm(score) = raw_score / max_score  (原始分数归一化，惩罚低质量结果)
- vector_weight: keyword=0.3, semantic=0.7
- bm25_weight:  keyword=0.7, semantic=0.3
- RRF_K = 60
```

**ES 查询侧收紧（D4）**：
- 动态 `minimum_should_match`：≤2 token→100%，≤4→75%，>4→60%
- `min_score: 1.0`：BM25 分数低于 1.0 的结果直接丢弃

### 7.3 查询分析器

**设计原则**：LLM 为主 + 规则快路径。规则仅处理无歧义的正则命中场景，作为零延迟快路径跳过 LLM 调用。

| 查询类型 | 分类方式 | 检索权重 | 特殊处理 |
|----------|----------|----------|----------|
| `keyword` | 规则快路径：引号包裹 / UUID | vector=0.3, bm25=0.7 | 单查询 |
| `semantic` | LLM 主路径 | vector=0.7, bm25=0.3 | 单查询 |
| `compare` | LLM 主路径 | vector=0.7, bm25=0.3 | 拆为 2 个子查询分别检索后合并 |
| `multi_hop` | LLM 主路径 | vector=0.7, bm25=0.3 | 按实体拆分子查询 |

- 输出多标签 `sub_types` 和 `has_keyword` 标志，Agent 规划节点据此注入检索策略提示
- LLM 失败时回退到规则分类（`_fallback_classify`）
- 预处理：去除噪声词（"请问/帮我/告诉我"），去除语气词（"吗/呢/吧"）

### 7.4 中文分词一致性

写入与查询使用同一套 jieba 分词管线，确保分词结果一致：
- ES 索引时：`content`（标准分词器）+ `content_jieba`（jieba 分词）双字段
- ES 查询时：对查询文本做 jieba 分词后查 `content_jieba` 字段
- PG FTS 回退：`to_tsvector('simple', tokenize(text))` 写入 `fts_vector` 列

---

## 8. 各服务模块详解

| 模块 | 文件 | 职责 | 回退链 |
|------|------|------|--------|
| Parser | `services/parser.py` | PDF(PyMuPDF) / Word(python-docx) / Markdown / 图片 / 网页(Playwright) 解析为统一 Section 列表 | — |
| Chunking | `services/chunking.py` | 按标题层级结构化分块，生成 parent + child chunk | — |
| DocProcessor | `services/doc_processor.py` | 文档处理管线编排：解析→分块→content_pool UPSERT 去重→embedding→Milvus/ES 写入。支持延迟 embedding、父 chunk 跳过 Milvus、Redis doc_id 锁、文本规范化哈希 | — |
| Embedding | `services/embedding.py` | 文本→1024维向量 (BAAI/bge-m3) | Redis缓存 → SiliconFlow API → Local model → Dummy(零向量) |
| VectorStore | `services/vector_store.py` | Milvus 向量存储 + COSINE 检索。Schema 含 content_hash 字段，支持 `insert_batch()` 批量插入和 `get_vectors_by_hash()` 按 hash 查向量 | Milvus → 内存暴力搜索(PickleStore) |
| ES | `services/es.py` | Elasticsearch 全文检索 + jieba 分词 + bulk 索引。动态 `minimum_should_match` + `min_score: 1.0` 过滤低质量 BM25 结果 | ES → PG FTS (to_tsvector) |
| Search | `services/search.py` | 混合检索编排：async 查询分析→双通道→加权 RRF 融合→Rerank（API→余弦本地兜底）→fetch chunks。`_fetch_chunks()` JOIN content_pool 获取内容。ES 宕机时 bm25_weight×0.5 补偿。`search_with_weights()` 供 Agent 复用 | 向量/全文任一失败仍可用单通道 |
| ContentGC | `services/content_gc.py` | 内容池 GC（二次校验 Chunk 计数后再删）+ 延迟删除补偿（reconcile_cleanup）+ 每日一致性校验（ref_count vs COUNT + Milvus 孤儿检测 + 自动修复）+ 延迟补向量（backfill_embeddings，PG advisory lock） | — |
| QueryAnalyzer | `services/query_analyzer.py` | async LLM-first 查询分类 + 规则快路径（引号/UUID 直接返回）+ 多标签 sub_types + has_keyword + 改写 + 子查询拆分。LLM 失败回退规则分类 | LLM → 规则 fallback |
| MultiTurn | `services/multi_turn.py` | async LLM-first 指代消解 + 规则快路径（单实体+单代词无歧义场景）+ 幻觉校验（新词必须在历史中出现过）+ 拼接兜底 | 快路径→LLM→拼接兜底 |
| LLM | `services/llm.py` | OpenAI 兼容 SSE 流式生成 | API → 本地模型(预留) |
| Rerank | `factory.py:RerankAdapter` + `search.py:_local_rerank` | 精排重排序：API 优先 → 余弦相似度本地兜底（D9，async embed_query + Milvus 取向量） | SiliconFlow API → 余弦本地重排 |
| OCR | `services/ocr.py` | 图片文字识别 | SiliconFlow API → Tesseract(预留) |
| WebScraper | `services/web_scraper.py` | Playwright 网页抓取 | Playwright → httpx 静态抓取 |
| Cache | `services/cache.py` | Redis 缓存 + 滑动窗口限流(100次/时) | Redis 不可用→不限流 |
| Guard | `services/guard.py` | Prompt 注入检测（正则规则） | — |
| Citation | `services/citation.py` | 引用编号合法性验证，去除幻觉引用 | — |
| Evaluator | `services/evaluator.py` | RAG 评估：Recall@5/10, MRR, NDCG@10 + RRF 参数扫描 + ES/PG FTS 回退效果评估 | — |
| Factory | `services/factory.py` | 7 个 Protocol 接口定义 + Adapter 类 + 工厂方法 | — |

---

## 9. 接口抽象层

`app/services/factory.py` 定义 7 个 `typing.Protocol` 接口 + 对应的 Adapter 类和工厂方法。

`VectorStoreBase` 的方法名已与实际实现对齐（`insert` / `search` / `delete_by_document`）。实际实现还包含 `get_vectors_by_ids` 方法（用于克隆操作），但未在 Protocol 中声明。

**当前状态：Protocol 方法名已对齐，但存在两个问题：** (1) 调用方仍直接 import 具体模块（包括 Agent 工具层直接 import `vector_store.get_vector_store()` 而非通过 `factory.get_vector_store()`）。(2) `VectorStoreBase.search` 等方法在 Protocol 中声明为 `async def`，但实际实现（如 `MilvusStandaloneStore.search`）是同步方法。由于 Python Protocol 是 `runtime_checkable` 的鸭子类型检查，签名不完全匹配不会报错，但语义上不一致。

| Protocol | 定义方法 | 实际实现方法 | 调用方是否走 Protocol |
|----------|---------|-------------|---------------------|
| `VectorStoreBase` | insert / search / delete_by_document | insert / search / delete_by_document / get_vectors_by_ids / insert_batch | 否，直接 import |
| `FullTextSearchBase` | search / index_chunk / delete_chunk | — | 否 |
| `EmbeddingServiceBase` | encode / encode_query | encode / encode_query | 否 |
| `RerankServiceBase` | rerank | rerank | 否 |
| `OcrServiceBase` | recognize | recognize | 否 |
| `LlmServiceBase` | stream_generate | stream_generate | 否 |
| `ObjectStorageBase` | save / load / delete | — | 否 |

**后续计划（详见 OPTIMIZATION-PLAN.md #7）**：调用方改走工厂方法 → 编写 Protocol 参数化测试。

---

## 10. Agent 架构详细设计

### 10.1 AgentState TypedDict

```python
# app/agent/state.py
def _replace_list(old: list, new: list) -> list:
    """Last-write-wins reducer: each node returns the complete list."""
    if new is ... or new is None:
        return old
    return new

class AgentState(TypedDict):
    query: str                              # 消解后的查询（用于检索）
    original_query: str                     # 用户原始输入（用于回答生成和展示）
    user_id: str
    conversation_id: str
    collection_id: str | None
    intent: str                             # simple / complex
    has_keyword: bool                       # D7: 传递 keyword 标志给 generate_plan
    plan: list[dict]                        # 子问题 DAG，每项含 id/tool/args/depends_on
    tools_called: Annotated[list[dict], operator.add]   # 审计日志，只追加
    chunks: Annotated[list[dict], _replace_list]        # 每轮替换，无跨轮次重复
    context: str
    answer: str
    reflection_result: str
    reflection_scores: dict                 # {"relevance": N, "groundedness": N, "consistency": N}
    retry_count: int
    should_retry: bool
    error: str | None
```

- `chunks` 使用 `_replace_list` reducer（last-write-wins），每轮只保留最新检索结果
- `original_query` 保留用户原始输入用于展示和 level 3 重试策略
- `has_keyword` 传递给 generate_plan 注入 fulltext_search 优先提示
- `reflection_scores` 记录分维度评分

### 10.2 LangGraph 状态图

```
                      ┌─────────────┐
                      │  START      │
                      └──────┬──────┘
                             │
                      ┌──────▼──────┐
                      │ intent_     │  async QueryAnalyzer（LLM 为主 + 规则快路径）
                      │ classify    │  + Prometheus 指标
                      │ → has_keyword│ → 传递给 generate_plan
                      └──────┬──────┘
                             │
                ┌────────────┼────────────┐
                │ simple     │ complex     │
                ▼            ▼             │
        ┌──────────┐  ┌──────────┐        │
        │ 固定管线 │  │generate_ │        │
        │ 检索+生成│  │  plan    │        │
        │ (END)    │  │(轻量LLM) │        │
        │          │  │+keyword  │        │ ← has_keyword 时注入 fulltext_search 提示
        └──────────┘  └────┬─────┘        │
                           │              │
                    ┌──────▼──────┐        │
                    │  execute_   │        │
                    │   tools     │ ← 按 DAG 顺序执行 + 增量上下文 + 零结果补检索
                    └──────┬──────┘        │
                           │              │
                    ┌──────▼──────┐        │
                    │  generate_  │        │
                    │   answer    │ ← 复用 LLMService（用 original_query）
                    └──────┬──────┘        │
                           │              │
                    ┌──────▼──────┐        │
                    │   reflect   │ ← 轻量LLM + top5 chunk 原文
                    │             │   分维度评分: relevance/groundedness/consistency
                    │             │   上限 max_retries + 2 次
                    └──────┬──────┘        │
                           │              │
                ┌──────────┼──────────┐
                │ pass     │ fail &   │
                │          │ retry≤   │
                │          │ N+1      │
                ▼          ▼          │
            ┌──────┐  ┌──────────┐    │
            │ END  │  │adjust_   │    │
            │      │  │  params  │────┘  4 级策略:
            └──────┘  └──────────┘  level 0-2: 调参数
                                     level 3: original_query + top_k=100
                                     耗尽 → NO_DATA_RESPONSE
```

### 10.3 工具层

| 工具名 | 实现 | 功能 | 参数 |
|--------|------|------|------|
| `hybrid_search` | 调用 `SearchService.search_with_weights()` | 向量+全文混合检索（复用完整管线：查询改写→RRF→rerank） | query, user_id, collection_id, top_k(40), vector_weight(0.7), bm25_weight(0.3) |
| `fulltext_search` | 调用 `SearchService._bm25_search()` | BM25 全文检索 | query, user_id, top_k(20) |

`hybrid_search` 复用 `SearchService.search_with_weights()`，零重复代码，查询改写步骤保留。

**generate_plan**：轻量 LLM（glm-4.5-air）生成子问题 DAG，输出带 `depends_on` 依赖关系的子查询列表，`execute_tools` 按 DAG 顺序执行并注入增量上下文。子查询上限 3 个，参数上限校验防止 LLM 生成超大值。`has_keyword=True` 时在 prompt 中注入 fulltext_search 优先提示。

**零结果补检索**：`execute_tools` 中 hybrid_search 返回 0 结果时，自动尝试 fulltext_search 补检索，并将原因记录到 `tools_called` 审计日志。

**已知限制**：`collection_id` 参数虽被 `search_with_weights()` 接收，但内部未转发到 `_single_search()` 和 `_fetch_chunks()`，当前被静默忽略。Agent 搜索路径无法按收藏夹过滤，需后续补全。

### 10.4 Checkpoint 状态持久化

**当前状态：未实现。** `app/models/agent_checkpoint.py` 表结构已定义，但 `app/agent/` 目录下没有 `checkpoint.py` 文件。LangGraph graph 使用默认的内存状态管理，进程重启后状态丢失。

设计目标（待实现）：

| 层级 | 存储 | Key | TTL | 用途 |
|------|------|-----|-----|------|
| 热层 | Redis | `agent:ckpt:{thread_id}` | 1 小时 | 正在执行的任务状态 |
| 冷层 | PostgreSQL `agent_checkpoints` | `thread_id` (PK) | 30 天 | 历史回溯、进程重启恢复 |

### 10.5 降级机制

```python
# app/agent/degrade.py — 系统水位检测 + API 健康探针
def should_degrade() -> bool:
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory().percent
    if cpu > settings.agent_degrade_cpu_threshold:   # 默认 80%
        return True
    if mem > settings.agent_degrade_mem_threshold:   # 默认 85%
        return True
    return False

async def is_api_healthy() -> bool:
    """D9: LLM/Embedding API 健康探针，30 秒缓存 TTL。"""
    # 探测 {llm_api_url}/models，缓存 30 秒
    # 用于 doc_processor 决定是否延迟 embedding
```

**降级路径（三层）：**

| 触发点 | 代码位置 | 行为 | 状态 |
|--------|---------|------|------|
| 系统过载 | `router.py` | SSE 构建前降级，发送 `agent_step` 降级通知 | ✅ |
| Agent 构建异常 | `router.py` | `except Exception` 兜底，发送降级通知 | ✅ |
| SSE 流内异常 | `router.py` | 流内降级：先发降级通知，再用固定管线检索生成 | ✅ |
| graph 超时(60s) | `router.py` | `asyncio.wait_for` 超时后流内降级 | ✅ |
| 反思耗尽 | `router.py` | 输出 `NO_DATA_RESPONSE` 诚实回答（D7），不再追加质量警告 | ✅ |
| API 不可用 | `degrade.py` | `is_api_healthy()` 返回 False → 延迟 embedding（D9） | ✅ |

降级通知通过 SSE `agent_step` 事件发送（`tool: "system"`），前端可区分展示。

### 10.6 成本控制策略

| 策略 | 状态 | 说明 |
|------|------|------|
| 轻量 LLM 中间步骤 | ✅ | 规划 + 反思 + 指代消解 + 意图分类用 glm-4.5-air |
| Embedding 缓存 | ✅ | Redis 缓存层（7天TTL） |
| 用户限流 | ✅ | 固定管线和 Agent 共享 100次/小时 |
| 重试上限 | ✅ | `agent_max_retries=2`（4 级策略，总尝试 4 次） |
| 余弦本地 Rerank | ✅ | Rerank API 不可用时用余弦相似度替代（D9） |
| httpx 连接池复用 | ✅ | `_get_http_client()` 单例复用 |
| API 调用计数 | ✅ | `agent_api_calls_total` 在 `_call_lightweight_llm` 中 `.inc()` |
| 规划缓存 | ❌ | 未实现 |
| 检索结果缓存 | ❌ | 未实现 |
| Agent 独立限流 | ❌ | 未实现 |

---

## 11. 可观测性

### 11.1 Prometheus 指标

| 指标 | 类型 | 含义 | 已接入 |
|------|------|------|--------|
| `rag_retrieval_duration_seconds` | Histogram | 混合检索总耗时 | ✅ |
| `rag_results_count` | Histogram | Rerank 后结果数量 | ✅ |
| `rag_rerank_duration_seconds` | Histogram | Rerank 耗时 | ✅ |
| `rag_llm_duration_seconds` | Histogram | LLM 生成耗时 | ✅ |
| `embedding_api_duration_seconds` | Histogram | Embedding API 调用耗时 | ✅ |
| `rerank_api_duration_seconds` | Histogram | Rerank API 调用耗时 | ✅ |
| `ocr_api_duration_seconds` | Histogram | OCR API 调用耗时 | ✅ |
| `api_error_total` | Counter | API 调用失败次数（按 service 标签） | ✅ |
| `model_memory_bytes` | Gauge | 本地模型内存占用 | ❌ 已定义未调用 |
| `agent_execution_duration_seconds` | Histogram | Agent 总执行时长 | ✅ |
| `agent_tool_call_duration_seconds` | Histogram | 单次工具调用时长 | ❌ 已定义未使用 |
| `agent_retry_total` | Counter | 反思重试次数 | ✅ |
| `agent_degrade_total` | Counter | 降级到固定管线次数 | ✅ |
| `intent_classify_total` | Counter | 意图分类结果分布 | ✅ |
| `intent_classify_duration_seconds` | Histogram | 意图分类耗时 | ✅ |
| `agent_api_calls_total` | Counter | Agent API 调用次数 | ✅ |
| `agent_reflection_scores` | Histogram | 反思评分分布（按 dimension 标签） | ✅ |
| `rag_data_loss` | Counter | D3: 检索返回空但 PG 有数据（数据丢失检测） | ✅ |

### 11.2 Agent Trace

`messages` 表的 `agent_trace` JSONB 字段记录 Agent 执行追踪数据，`router.py` 在 Agent 执行完成后写入。

Trace 数据结构：

```json
{
  "intent": "complex",
  "plan_steps": 2,
  "tools_called": [{"tool": "hybrid_search", "args": {...}, "result_count": 15}],
  "reflection_result": "通过",
  "reflection_scores": {"relevance": 4, "groundedness": 5, "consistency": 4},
  "retry_count": 0,
  "chunk_count": 15
}
```

### 11.3 告警规则

| 规则 | 条件 | 动作 |
|------|------|------|
| Agent 错误率过高 | 5 分钟内错误率 > 5% | 通知运维，检查 LLM API |
| 执行超时 | P95 > 2s 持续 5 分钟 | 检查 LLM API 延迟 |
| 大量降级 | 1 小时降级 > 50 次 | 检查系统负载和 LLM 可用性 |
| 重试率过高 | 1 小时重试 > 100 次 | 检查检索质量和 Plan 生成 |
| ES 不可用 | 连续 3 次健康检查失败 | 自动切换 PG FTS，通知运维 |
| Milvus 不可用 | 连续 3 次连接失败 | 降级为纯 BM25 检索 |

---

## 12. 安全设计

### 12.1 认证流程

```
注册: POST /api/v1/auth/register → bcrypt(password) → users 表
登录: POST /api/v1/auth/login → bcrypt.verify → JWT(access_token + refresh_token)
鉴权: Authorization: Bearer <access_token> → JWT decode → user_id
刷新: POST /api/v1/auth/refresh → 验证 refresh_token → 签发新 access_token
```

- access_token 有效期 15 分钟，refresh_token 有效期 7 天
- 密码存储使用 bcrypt 哈希，不存明文

### 12.2 数据隔离

- **用户级隔离**: 所有查询强制带 `user_id` 条件（SQL WHERE + Milvus filter + ES filter）
- **ContentPool 跨用户安全**: content_pool 存储全局内容但不暴露给用户直接查询，用户只能通过自己的 chunks → JOIN content_pool 获取内容
- **Agent Checkpoint 绑定**: `agent_checkpoints.thread_id` 绑定 `conversation_id`，而 `conversations.user_id` 保证了用户隔离
- **工具调用隔离**: 每个 Agent Tool 的 `_run` 方法内部校验 `user_id`，防止越权访问

### 12.3 安全防护

| 威胁 | 防护措施 | 实现位置 |
|------|----------|----------|
| SQL 注入 | SQLAlchemy ORM 参数化查询 | 所有数据库操作 |
| Prompt 注入 | `guard.py` 正则规则检测 + Agent 工具输入校验 | `services/guard.py` + `agent/tools.py` |
| 工具越权 | 每次工具调用校验 `user_id` 数据隔离 | 各 Tool 的 `_run` 方法 |
| LLM 生成恶意 Plan | JSON Schema 校验 + 工具名白名单 | `agent/nodes.py` generate_plan |
| 状态泄露 | Checkpoint 绑定 `user_id`，查询时校验归属 | `agent/checkpoint.py` |
| 输入长度限制 | FastAPI Pydantic Schema 字段约束 | `schemas/chat.py` |
| 限流 | Redis 滑动窗口 100 次/小时 | `services/cache.py` |
| 传输安全 | Nginx SSL 终结 + HSTS | Nginx 配置 |

### 12.4 已知风险

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 规则引擎绕过 | 恶意 Prompt 可能逃逸规则 | LLM 辅助分类 + 人工审核日志 |
| Checkpoint 竞态 | 并发写入同一 thread | Redis 原子操作 + PG 行锁 |
| LLM API Key 泄露 | 云端 API 被盗用 | .env 不入库 + .gitignore + 最小权限 |
| Agent 无限循环 | LLM 持续生成无效 Plan | 重试上限 2 次 + 全局超时 60s |

---

## 13. 环境变量与部署

### 13.1 `.env` 配置

```bash
# ── 基础 ──
APP_NAME=knSpace
DEBUG=false
SECRET_KEY=<your-secret-key>
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=7

# ── 数据库 ──
DATABASE_URL=postgresql+asyncpg://knspace:knspace123@localhost/knspace

# ── Redis ──
REDIS_URL=redis://localhost:6379/0

# ── Milvus ──
MILVUS_URI=http://localhost:19530

# ── Elasticsearch ──
ES_URL=http://localhost:9200
ES_INDEX=chunks

# ── Embedding ──
EMBEDDING_BACKEND=api
EMBEDDING_API_URL=https://api.siliconflow.cn/v1
EMBEDDING_API_KEY=<key>
EMBEDDING_MODEL=BAAI/bge-m3

# ── Reranker ──
RERANK_BACKEND=api
RERANK_API_URL=https://api.siliconflow.cn/v1
RERANK_API_KEY=<key>
RERANK_MODEL=BAAI/bge-reranker-v2-m3

# ── OCR ──
OCR_BACKEND=api
OCR_API_URL=https://api.siliconflow.cn/v1
OCR_API_KEY=<key>
OCR_MODEL=deepseek-ai/DeepSeek-OCR

# ── LLM ──
LLM_API_URL=<your-endpoint>/v1
LLM_API_KEY=<key>
LLM_MODEL=glm-5.1-openai

# ── Agent ──
USE_AGENT=true
AGENT_LIGHTWEIGHT_LLM=glm-4.5-air
AGENT_MAX_RETRIES=2
AGENT_DEGRADE_CPU_THRESHOLD=80.0
AGENT_DEGRADE_MEM_THRESHOLD=85.0

# ── 文件存储 ──
FILE_STORAGE_PATH=./data/files
```

### 13.2 systemd 服务

```ini
# /etc/systemd/system/knspace.service
[Unit]
Description=knSpace RAG + Agent Service
After=network.target postgresql.service docker.service

[Service]
User=knspace
WorkingDirectory=/opt/knspace
ExecStart=/opt/knspace/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
EnvironmentFile=/opt/knspace/.env

[Install]
WantedBy=multi-user.target
```

### 13.3 Docker 容器

```bash
# Milvus Standalone
docker run -d --name milvus \
  -p 19530:19530 -p 9091:9091 \
  -v /opt/milvus/data:/var/lib/milvus \
  milvusdb/milvus:v2.5.6 standalone

# Elasticsearch 8.x
docker run -d --name elasticsearch \
  -p 9200:9200 \
  -e "discovery.type=single-node" \
  -e "ES_JAVA_OPTS=-Xms512m -Xmx512m" \
  -e "xpack.security.enabled=false" \
  -v /opt/es/data:/usr/share/elasticsearch/data \
  docker.elastic.co/elasticsearch/elasticsearch:8.17.0
```

### 13.4 部署步骤

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 数据库迁移
#    包含：content_pool 表 + chunks 表改造(content_hash FK)
#          + agent_checkpoints 表 + messages.agent_trace 列
alembic upgrade head

# 3. Milvus / ES Schema
#    首次启动自动创建。如需升级 Schema：
#    删除旧 collection/索引 → 应用启动时自动重建（含 content_hash 字段）

# 4. 配置 .env
cp .env.example .env
# 编辑填入实际 API Key 和数据库连接

# 5. 启动服务
sudo systemctl restart knspace

# 6. 验证
curl http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer <token>"
```

---

## 14. 延迟预算

| 路径 | 节点 | 目标延迟 |
|------|------|----------|
| 固定 RAG 链路 | 注入检测→限流→指代消解(LLM)→查询分析(LLM)→混合检索→Rerank→LLM生成→引用校验 | <500ms |
| Agent 简单路径 | 意图分类(async LLM)→simple→固定管线检索+生成 | <500ms |
| Agent 复杂路径 | 意图分类→规划→工具执行→生成→反思 | <1.5s |
| Agent 重试路径 | 上述 + 1 次反思重试（adjust_params→execute_tools→generate→reflect） | <2.5s |

---

## 15. 演进路线

| 版本 | 核心改动 | 架构影响 |
|------|----------|----------|
| 当前 | RAG + Agent + ContentPool 全局去重，单实例部署 | 4C/4G 单机 |
| 下一步 | Agent 无状态化，Redis Cluster 替换单机 Redis，水平扩展 | 可扩展至 100+ 实例 |
| 未来 | 多 Agent 协作（检索 Agent + 写作 Agent + 审核 Agent） | 复杂任务拆分为 Agent 流水线 |
| 远期 | Milvus Cluster、ES Cluster、PG 读写分离 | 支撑百万级用户 |

**平滑设计保障**：

- 接口兼容：`POST /api/v1/chat` 通过 `use_agent` 参数控制路由，默认 `false`
- 组件可替换：基于 `factory.py` 的 7 个 Protocol 抽象，替换 Milvus/ES/LLM 时 Agent 层零改动
- 灰度能力：可按用户 ID hash 控制灰度比例（10% → 50% → 100%）

---

## 16. 设计总结

### 已落地

1. **LangGraph 状态图编排**：6 节点状态图（intent_classify → generate_plan → execute_tools → generate_answer → reflect → adjust_params），条件路由 + 4 级重试循环，进程内执行零额外服务依赖。

2. **LLM 子问题 DAG 生成**：generate_plan 使用轻量 LLM（glm-4.5-air）生成带依赖关系的子查询 DAG（上限 3 个），execute_tools 按 DAG 顺序执行 + 增量上下文注入 + 零结果自动补检索。`has_keyword=True` 时注入 fulltext_search 优先提示。

3. **加权 RRF 混合检索 + LLM 查询分析**：向量检索 + BM25 全文检索 → 加权 RRF 融合（引入原始分数惩罚低质量结果）→ Rerank 精排（API → 余弦本地兜底）。ES 侧动态 `minimum_should_match` + `min_score: 1.0` 过滤。查询分类器 async LLM 为主 + 规则快路径，输出多标签 `sub_types` 和 `has_keyword`。

4. **ContentPool 全局去重（D1）**：chunks 表通过 `content_hash` FK 指向 `content_pool`，相同内容（`_normalize_for_hash` 规范化后 SHA256，D12）只存一份 text+vector。UPSERT（INSERT ON CONFLICT）解决多实例竞态。删除时 `set()` 去重 + `GREATEST(ref_count - 1, 0)` 防负数。三引擎 content_hash 贯穿实现跨引擎一致性。

5. **多层回退链**：Embedding（Redis→API→延迟模式→Dummy）、FTS（ES→PG FTS + 动态权重补偿）、Rerank（API→余弦本地兜底 D9）、VectorStore（Milvus→Pickle）。任何单点故障不影响系统可用性。

6. **结构化父子分块（D11）**：按标题层级分组，父 chunk 只存文本（vector=NULL）不做 embedding 不写 Milvus，子 chunk 精确检索。检索结果自动附带 `parent_content`。

7. **跨用户克隆原子性**：克隆流程通过 JOIN Chunk+ContentPool 获取源数据，增量 ref_count，插入元数据级 Chunk，从 content_pool 反序列化向量写入 Milvus/ES。

8. **4 级重试策略 + 诚实回答（D7）**：reflect 节点按句子边界截断，上限 `max_retries + 2` 次。adjust_params 4 级策略：level 0-2 调参数，level 3 放弃子查询用 original_query + top_k=100 全量检索。重试耗尽输出 `NO_DATA_RESPONSE` 诚实回答，不再追加质量警告。

9. **三引擎数据一致性（D2/D3/D8）**：GC 二次校验 Chunk 计数后再删 ContentPool。Milvus/ES 删除失败时 chunk 标记 `cleanup_status="pending"`，5 分钟补偿重试。每日校验 ref_count vs COUNT + Milvus 孤儿检测 + 自动修复。检索返回空但 PG 有数据时 `rag_data_loss` Counter 自增。

10. **延迟 embedding + API 健康探针（D9）**：`is_api_healthy()` 30s 缓存探针，API 不可用时 content_pool.vector=NULL + needs_embedding=True，BM25 可用。`backfill_embeddings` 用 PG advisory lock 保证单实例执行，API 恢复后自动补向量。

11. **async LLM-first 指代消解（D6）**：LLM 为主 + 规则快路径（单实体+单代词无歧义场景）+ 幻觉校验（新词必须在历史中出现过）+ 拼接兜底。

12. **轻量模型统一**：multi_turn 指代消解、Agent 规划、Agent 反思、意图分类统一使用 glm-4.5-air + httpx 直接调用。

### 待优化（详见 OPTIMIZATION-PLAN.md）

13. **Protocol 接口层**：方法名已对齐，但调用方仍直接 import 具体模块，未通过工厂方法。需编写参数化测试后才能保证替换安全。

14. **成本可观测性**：`agent_api_calls_total` 已接入，但缺少 token 成本指标和按路径拆分的成本 dashboard。

15. **规划缓存 / 检索结果缓存**：规划缓存（Redis TTL=1d）和检索结果缓存均未实现。

16. **Agent 独立限流**：固定管线和 Agent 共享同一限流桶，缺少 Agent 路径的独立并发控制。

17. **Checkpoint 状态持久化**：表结构已定义，`app/agent/checkpoint.py` 未实现。
