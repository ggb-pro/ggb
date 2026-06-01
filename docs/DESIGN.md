# knSpace 详细设计文档

> 基于 4C/4G 腾讯云单实例部署的私有化 RAG + Agent 知识库系统。
> v1.x 固定 RAG 管线已实现并运行；v2.0 在此基础上新增 LangGraph Agent 编排层，渐进式升级。
> 本文档描述完整系统设计，与代码一一对应。

---

## 1. 项目概览

### 1.1 定位

knSpace 是一个私有化部署的 RAG + Agent 知识库系统。用户上传文档（PDF/Word/Markdown/图片/网页），系统自动解析、分块、向量化，然后基于文档内容进行智能问答。v2.0 新增 Agent 层，复杂查询由 LangGraph 驱动动态规划、调用工具、反思重试。

### 1.2 核心能力

- 多格式文档解析：PDF（PyMuPDF）、Word（python-docx）、Markdown、图片（OCR）、网页（Playwright）
- 结构化父子分块：按标题层级分组，父 chunk 提供完整上下文，子 chunk 精确检索
- 混合检索：向量检索（Milvus）+ 全文检索（Elasticsearch）+ RRF 融合
- 查询智能分析：规则引擎自动分类（keyword/semantic/compare/multi_hop），动态调整检索权重
- 多轮对话指代消解：规则 + LLM 两级消解，支持"它""这个"等代词
- 流式响应：SSE 实时推送 LLM token + 检索状态
- 跨用户文档去重：相同文件只处理一次，其他用户克隆 chunk
- **Agent 编排（v2.0）**：LangGraph 状态图驱动，意图分类→规划→工具执行→生成→反思闭环
- **混合路由（v2.0）**：规则引擎分流，keyword/单轮 semantic → simple 走 v1.x，compare/multi_hop → complex 走 Agent
- **降级机制（v2.0）**：系统过载 / Agent 超时(60s) / 运行时异常均可降级到 v1.x，SSE 流内降级带用户提示

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
| LLM（规划/反思） | OpenAI 兼容 API | glm-4.5-air | 轻量模型，规划 + 反思（意图分类当前为纯规则，未调用 LLM） |
| 中文分词 | jieba | 0.42.1 | 全文检索分词 |
| 监控 | Prometheus + Grafana | - | 指标采集 + 可视化 |

### 1.4 硬件约束

4 核 AMD EPYC 7K62 / 4 GB 内存 / 40 GB SSD / 腾讯云轻量 4Mbps

全部 AI 模型走云端 API，本地 reranker 已移除（防止 4G 机器 OOM）。LangGraph Runtime 为纯编排层 Python 对象。月成本约 43 元（API）+ 100 元（服务器），Agent 路径成本增量尚无线上数据验证。

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
                 │  │ API  │ │ API  │ │ (v1+v2 统一) │ │
                 │  └──────┘ └──────┘ └──────┬──────┘ │
                 │                            │        │
                 │         ┌─────────────────▼──────┐ │
                 │         │   Query Router (v2.0)  │ │ ← 意图分类，分流
                 │         │  简单→v1.x  复杂→Agent   │ │
                 │         └──┬─────────────────┬───┘ │
                 │            │                 │      │
                 │  ┌─────────▼──────┐  ┌──────▼────┐│
                 │  │  v1.x 固定管线  │  │   Agent   ││ ← v2.0 核心新增
                 │  │ (原封不动复用)  │  │ Controller││
                 │  │                │  │ ┌────────┐││
                 │  │ query_analyzer │  │ │LangGraph│││
                 │  │ search→rerank  │  │ │Runtime  │││
                 │  │ llm generate   │  │ └────────┘││
                 │  │                │  │ ┌──────┐  ││
                 │  │                │  │ │Tools │  ││ ← 包装 v1.x 服务
                 │  │                │  │ └──────┘  ││
                 │  └────────────────┘  └──────┬────┘│
                 └──────────────────────────────┼────┘
                                                  │
              ┌───────────┬──────────────┬───────▼──────┐
              │           │              │              │
       ┌──────▼──┐  ┌────▼─────┐  ┌────▼─────┐  ┌────▼────┐
       │PostgreSQL│  │ Milvus   │  │   ES     │  │  Redis  │
       │业务+状态 │  │Standalone│  │  8 jieba │  │缓存+状态│
       └─────────┘  └──────────┘  └──────────┘  └─────────┘
       ┌───────────────────────────────────────────────────┐
       │                  云端 API（需禁用本地 fallback）     │
       │  轻量 LLM：glm-4.5-air（规划 + 反思）              │
       │  大模型：glm-5.1-openai（最终生成）                 │
       │  原有 API：Embedding / Rerank / OCR                │
       └───────────────────────────────────────────────────┘
```

### 2.2 组件职责

| 组件 | 职责 | 端口 |
|------|------|------|
| Nginx | SSL 终结、限流 10r/s、SPA 静态资源 | 443/80 |
| FastAPI | 所有业务 API、SPA fallback | 8000 |
| PostgreSQL | 用户、文档、chunk、会话、标签、Agent 状态持久化 | 5432 |
| Milvus Standalone | 向量存储 + COSINE 检索，IVF_FLAT 索引 | 19530 |
| Elasticsearch 8.x | 全文检索，jieba 分词，content + content_jieba 双字段 | 9200 |
| Redis | Embedding 缓存（7天TTL）、API 限流（100次/时）、Agent 热状态（1h TTL） | 6379 |
| LangGraph Runtime | Agent 状态图编排（<30MB，纯内存） | - |

### 2.3 文件清单

#### v1.x 已实现

```
app/
├── main.py                     # FastAPI 应用入口
├── config.py                   # Pydantic BaseSettings 配置
├── api/                        # 路由层
│   ├── auth.py                 # 认证（注册/登录/JWT）
│   ├── documents.py            # 文档上传/管理
│   ├── chat.py                 # 智能问答（SSE）
│   ├── conversations.py        # 会话管理
│   ├── collections.py          # 收藏夹/标签
│   └── eval.py                 # RAG 评估
├── models/                     # SQLAlchemy 模型
│   ├── user.py, document.py, chunk.py, conversation.py
│   ├── message.py, collection.py, tag.py, document_tag.py
├── schemas/                    # Pydantic 请求/响应 Schema
├── services/                   # 业务服务层
│   ├── doc_processor.py        # 文档处理管线（解析→分块→向量化）
│   ├── parser.py               # 多格式解析
│   ├── chunking.py             # 结构化父子分块
│   ├── search.py               # 混合检索（Milvus + ES + RRF + Rerank）
│   ├── query_analyzer.py       # 规则引擎查询分类
│   ├── multi_turn.py           # 多轮对话指代消解
│   ├── llm.py                  # LLM 流式生成
│   ├── embedding.py            # Embedding 服务（Redis→API→Local→Dummy）
│   ├── vector_store.py         # Milvus 向量存储
│   ├── es.py                   # Elasticsearch 全文检索
│   ├── rerank.py               # Rerank 服务
│   ├── ocr.py                  # OCR 服务
│   ├── web_scraper.py          # 网页抓取
│   ├── cache.py                # Redis 缓存 + 限流
│   ├── guard.py                # Prompt 注入检测
│   ├── citation.py             # 引用校验
│   ├── evaluator.py            # RAG 评估
│   ├── metrics.py              # Prometheus 指标
│   └── factory.py              # 7 个 Protocol 接口 + 工厂方法
└── utils/
    └── security.py             # JWT + bcrypt
```

#### v2.0 新增（`app/agent/`）

```
app/agent/                      ← 整个目录为 v2.0 新增
├── __init__.py
├── state.py                    # AgentState TypedDict 状态定义
├── graph.py                    # LangGraph 状态图构建
├── nodes.py                    # 6 个节点（分类/规划/执行/生成/反思/调参）
├── tools.py                    # 工具注册，包装 v1.x 服务
├── router.py                   # 查询路由（简单→v1.x，复杂→Agent）
└── degrade.py                  # 降级判断逻辑（CPU/内存水位检测）

app/models/agent_checkpoint.py  # 表结构已定义，Checkpoint 功能待实现
app/schemas/chat.py             # ChatRequest 新增 use_agent 字段
```

#### v2.0 最小改动（仅 3 个文件约 20 行）

| 文件 | 改动 |
|------|------|
| `app/api/chat.py` | 新增 `use_agent` 分支，调用 `agent.router.route_query()` |
| `app/schemas/chat.py` | `ChatRequest` 新增 `use_agent: bool = False` |
| `app/config.py` | 新增 Agent 相关配置项 |

### 2.4 内存预算

| 组件 | 常驻 | 说明 |
|------|------|------|
| OS + 系统服务 | 500 MB | 含腾讯云监控 |
| PostgreSQL 16 | 310 MB | shared_buffers=128MB + checkpoint |
| Elasticsearch 8.x | 600 MB | 单节点，1 shard |
| Milvus Standalone | 500 MB | Docker，IVF_FLAT |
| FastAPI + 业务代码 | 200 MB | 含所有业务代码 |
| Redis | 60 MB | 缓存 + 限流 + Agent 热状态 |
| Nginx | 20 MB | 反向代理 |
| **LangGraph Runtime** | **<30 MB** | 纯编排层 |
| **常驻合计** | **~2.2 GB** | |
| **剩余可用** | **~1.8 GB** | 连接池 + 请求缓冲 |

---

## 3. 数据模型

### 3.1 ER 关系

```
users ──1:N──> documents ──1:N──> chunks
  │               │
  │               └── N:M ──> tags (via document_tags)
  │
  ├──1:N──> collections ──1:N──> documents
  │
  ├──1:N──> conversations ──1:N──> messages
  │
  └──1:N──> chunks

agent_checkpoints (v2.0 新增，thread_id = conversation_id)
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

-- 分块 (app/models/chunk.py)
CREATE TABLE chunks (
    id              UUID PRIMARY KEY,
    document_id     UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id),
    content         TEXT NOT NULL,
    chunk_index     INT NOT NULL,
    chunk_type      VARCHAR(20) DEFAULT 'child',
    parent_chunk_id UUID,
    char_start      INT,
    char_end        INT,
    page_number     INT,
    token_count     INT NOT NULL,
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
    agent_trace     JSONB,          -- v2.0 新增：Agent 执行追踪
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

-- Agent 状态持久化 (v2.0 新增)
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
- **v2.0 零数据迁移**：只新增 `agent_checkpoints` 表和 `messages.agent_trace` 列，不修改任何 v1.x 表结构

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
| POST | `/upload` | 上传文件（multipart/form-data） | JWT |
| GET | `/` | 列出文档（分页，可按 collection 过滤） | JWT |
| GET | `/{doc_id}` | 获取文档详情 | JWT |
| DELETE | `/{doc_id}` | 删除文档（清理 Milvus + ES + 文件） | JWT |
| GET | `/{doc_id}/status` | 查询处理状态 | JWT |
| GET | `/{doc_id}/status/stream` | SSE 流式查询处理状态（5 分钟超时） | JWT |
| POST | `/import-url` | 导入网页（scrape → chunk → embed） | JWT |

### 4.3 对话 `/api/v1/chat`

| 方法 | 路径 | 功能 | 认证 |
|------|------|------|------|
| POST | `/` | 智能问答（SSE 流式返回）。`use_agent=false` 走 v1.x 管线，`true` 走 Agent | JWT |

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
 │    SHA-256    │
 └──────┬───────┘
        │
        ▼
 ┌──────────────┐
 │ 2. 去重检查   │  先查 user 内 content_hash，再查跨用户
 │              │  跨用户命中 → 克隆 chunk（共享向量）
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
 │  ES index    │  es.py → bulk_index_chunks（jieba 分词）
 │    │         │  失败 → 回退 PG FTS (to_tsvector)
 │    ▼         │
 │  embedding   │  embedding.py → batch 64 → SiliconFlow API
 │    │         │  回退链: Redis缓存 → API → Local → Dummy
 │    ▼         │
 │  Milvus      │  vector_store.py → insert (IVF_FLAT, COSINE)
 │    │         │
 │    ▼         │
 │  ready       │  Document.status = ready, chunk_count 更新
 └──────────────┘
```

### 5.2 状态机

```
pending → parsing → chunking → embedding → ready
                │          │          │
                └──────────┴──────────┘→ failed (processing_error 记录原因)
```

### 5.3 关键设计

- **批量 Embedding**: 每 64 个 chunk 一批调用 API，减少请求次数
- **ES → PG FTS 回退**: Elasticsearch 不可用时自动回退到 PostgreSQL 全文检索（jieba 分词 + `to_tsvector`）
- **父子分块**: 父 chunk 按标题分组提供完整段落上下文，子 chunk 精确检索；检索时自动附带 `parent_content`

---

## 6. 智能问答管线

### 6.1 v1.x 固定管线（默认路径）

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
 │ 指代消解        │  multi_turn.py — 规则替换 + 历史上下文拼接
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 查询分析        │  query_analyzer.py — 纯规则分类 + 改写 + 子查询拆分
 │ keyword/semantic│  keyword→bm25_weight=0.7, semantic→vector_weight=0.7
 │ /compare/multi  │  compare→拆为2个子查询, multi_hop→按实体拆分
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ 混合检索        │  search.py — 向量(Milvus) + 全文(ES) → RRF融合 → top40
 └────┬────────────┘
      │
      ▼
 ┌─────────────────┐
 │ Rerank 精排     │  RerankAdapter — API / Local CrossEncoder → top10
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

### 6.2 v2.0 Agent 管线（`use_agent=true`）

```
POST /api/v1/chat  { query, use_agent: true }
        │
        ▼
 ┌─────────────────┐
 │ Query Router    │  router.py — 判断是否降级
 │ should_degrade? │  degrade.py: CPU>80% / MEM>85% → 降级到 v1.x（带用户通知）
 └────┬────────────┘
      │ 正常
      ▼
 ┌─────────────────────┐
 │ 指代消解             │  #1: resolve_query_with_history — 规则+LLM 两级消解
 │ resolved_query      │  消解后的 query 传入 graph，原始 query 保留在 original_query
 └────┬────────────────┘
      │
      ▼
 ┌─────────────────────┐
 │ graph.ainvoke()     │  asyncio.wait_for(timeout=60s)
 │  超时 → 流内降级v1.x │
 │                     │
 │  ┌────────────────┐ │
 │  │intent_classify │ │  节点1: 纯规则(query_analyzer) + Prometheus 指标
 │  │                │ │
 │  │ simple         │ │  keyword / 单轮 semantic → simple → 走 v1.x 检索
 │  │ complex        │ │  compare / multi_hop → complex
 │  └───┬────────┬───┘ │
 │      │simple  │complex
 │      ▼        ▼     │
 │  ┌────────┐ ┌─────────────┐
 │  │ v1.x   │ │generate_plan│  节点2: glm-4.5-air 生成工具调用计划
 │  │ 检索   │ │             │  JSON 解析 + 工具名白名单校验
 │  │+生成   │ │             │  fallback: 单步 hybrid_search
 │  │(END)   │ └───┬─────────┘
 │  └────────┘     │
 │            ┌────▼──────────┐
 │            │ execute_tools │  节点3: 执行计划中的工具
 │            │               │  hybrid_search 复用 SearchService.search_with_weights()
 │            └───┬───────────┘
 │                │
 │            ┌────▼──────────┐
 │            │generate_answer│  节点4: 复用 LLMService，用 original_query 生成
 │            └───┬───────────┘
 │                │
 │            ┌────▼──────────┐
 │            │   reflect     │  节点5: glm-4.5-air 校验答案
 │            │               │  传入 top5 chunk 原文（各300字）做事实校验
 │            │               │  分维度评分：relevance / groundedness / consistency
 │            └───┬───────────┘
 │                │
 │        ┌───────┼──────────┐
 │        │ pass  │ fail &   │
 │        │       │ retry<N  │
 │        ▼       ▼          │
 │    ┌──────┐ ┌──────────┐  │
 │    │ END  │ │adjust_   │  │  节点6: 根据最低分维度选择调整策略
 │    │      │ │  params  │──┘  事实问题→降 vector_weight / 引用不足→扩 top_k
 │    └──────┘ └──────────┘
 └─────────────────────┘

 异常处理（三层降级）:
 1. SSE 构建前异常 → 返回降级 v1.x StreamingResponse（带通知）
 2. graph 超时 60s → 流内降级 v1.x（带通知）
 3. 流内其他异常 → 流内降级 v1.x（带通知）
 反思耗尽 → answer 末尾追加质量警告
```

### 6.3 SSE 事件格式

所有响应均为 `text/event-stream`，事件格式：

```json
{"type": "conversation", "conversation_id": "uuid"}
{"type": "status", "message": "Searching..."}
{"type": "agent_step", "tool": "agent|hybrid_search|...", "thought": "意图分析中...|检索到 N 个结果"}  // v2.0 Agent 专用
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
              │ QueryAnalyzer │  规则分类 → rewritten query + weights
              └───┬───────┬───┘
                  │       │
          ┌───────▼──┐ ┌──▼────────┐
          │ Milvus   │ │   ES 8.x  │
          │ 向量检索  │ │ BM25全文   │
          │ COSINE   │ │ jieba分词  │
          │ top_k=40 │ │ top_k=40  │
          └───────┬──┘ └──┬────────┘
                  │       │
                  ▼       ▼
          ┌───────────────────┐
          │    RRF Fusion     │  加权倒数排名融合
          │  top 40 candidates│
          └────────┬──────────┘
                   │
                   ▼
          ┌───────────────────┐
          │    Rerank 精排     │  RerankAdapter: API → Local → 不排序
          │    top 10 results │
          └───────────────────┘
```

### 7.2 RRF 融合公式

```
score(chunk) = Σ  weight / (RRF_K + rank + 1)

- vector_weight: keyword=0.3, semantic=0.7
- bm25_weight:  keyword=0.7, semantic=0.3
- RRF_K = 60 (标准常数，抑制头部结果的支配效应)
```

### 7.3 查询分析器

| 查询类型 | 匹配规则 | 检索权重 | 特殊处理 |
|----------|----------|----------|----------|
| `keyword` | 引号包裹 / UUID / 8+位连续字符 | vector=0.3, bm25=0.7 | 单查询 |
| `semantic` | "为什么/怎么/如何" 等疑问词 | vector=0.7, bm25=0.3 | 单查询 |
| `compare` | "对比/区别/vs" 等比较词 | vector=0.7, bm25=0.3 | 拆为 2 个子查询分别检索后合并 |
| `multi_hop` | "和/与...的关系" 多实体模式 | vector=0.7, bm25=0.3 | 按实体拆分子查询 |

预处理：去除噪声词（"请问/帮我/告诉我"），去除语气词（"吗/呢/吧"）。

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
| Embedding | `services/embedding.py` | 文本→1024维向量 (BAAI/bge-m3) | Redis缓存 → SiliconFlow API → Local model → Dummy(零向量) |
| VectorStore | `services/vector_store.py` | Milvus 向量存储 + COSINE 检索 | Milvus → 内存暴力搜索 |
| ES | `services/es.py` | Elasticsearch 全文检索 + jieba 分词 + bulk 索引 | ES → PG FTS (to_tsvector) |
| Search | `services/search.py` | 混合检索编排：查询分析→双通道→RRF融合→Rerank→fetch chunks。新增 `search_with_weights()` 供 Agent 工具层复用 | 向量/全文任一失败仍可用单通道 |
| QueryAnalyzer | `services/query_analyzer.py` | 纯规则查询分类(keyword/semantic/compare/multi_hop) + 改写 + 子查询拆分 | — |
| MultiTurn | `services/multi_turn.py` | 规则指代消解（"它/这个"→历史实体）+ LLM 上下文拼接 | 规则失败→原文透传 |
| LLM | `services/llm.py` | OpenAI 兼容 SSE 流式生成 | API → 本地模型(预留) |
| Rerank | `factory.py:RerankAdapter` | 精排重排序（本地 fallback 已移除，仅 API） | SiliconFlow API → 不排序 |
| OCR | `services/ocr.py` | 图片文字识别 | SiliconFlow API → Tesseract(预留) |
| WebScraper | `services/web_scraper.py` | Playwright 网页抓取 | Playwright → httpx 静态抓取 |
| Cache | `services/cache.py` | Redis 缓存 + 滑动窗口限流(100次/时) | Redis 不可用→不限流 |
| Guard | `services/guard.py` | Prompt 注入检测（正则规则） | — |
| Citation | `services/citation.py` | 引用编号合法性验证，去除幻觉引用 | — |
| Evaluator | `services/evaluator.py` | RAG 评估：Recall@5/10, MRR, NDCG@10 | — |
| Factory | `services/factory.py` | 7 个 Protocol 接口定义 + Adapter 类 + 工厂方法 | — |

---

## 9. 接口抽象层

`app/services/factory.py` 定义 7 个 `typing.Protocol` 接口 + 对应的 Adapter 类和工厂方法。

`VectorStoreBase` 的方法名已与实际实现对齐（`insert` / `search` / `delete_by_document`）。

**当前状态：Protocol 方法名已对齐，但调用方仍直接 import 具体模块。** 所有业务代码（包括 Agent 工具层）直接 import `vector_store.get_vector_store()` 而非通过 `factory.get_vector_store()`。

| Protocol | 定义方法 | 实际实现方法 | 调用方是否走 Protocol |
|----------|---------|-------------|---------------------|
| `VectorStoreBase` | insert / search / delete_by_document | insert / search / delete_by_document | 否，4 处直接 import |
| `FullTextSearchBase` | search / index_chunk / delete_chunk | — | 否 |
| `EmbeddingServiceBase` | encode / encode_query | encode / encode_query | 否 |
| `RerankServiceBase` | rerank | rerank | 否 |
| `OcrServiceBase` | recognize | recognize | 否 |
| `LlmServiceBase` | stream_generate | stream_generate | 否 |
| `ObjectStorageBase` | save / load / delete | — | 否 |

**后续计划（详见 OPTIMIZATION-PLAN.md #7）**：调用方改走工厂方法 → 编写 Protocol 参数化测试。

---

## 10. Agent 架构详细设计（v2.0）

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
    plan: list[dict]                        # 直接覆盖（无 reducer）
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

**已解决的问题：**
- `chunks` 改用 `_replace_list` reducer（last-write-wins），每轮只保留最新检索结果
- 新增 `original_query` 字段，保留用户原始输入用于展示
- 新增 `reflection_scores` 字段，记录分维度评分

### 10.2 LangGraph 状态图

```
                      ┌─────────────┐
                      │  START      │
                      └──────┬──────┘
                             │
                      ┌──────▼──────┐
                      │ intent_     │  纯规则（QueryAnalyzer）
                      │ classify    │  + Prometheus 指标
                      └──────┬──────┘
                             │
                ┌────────────┼────────────┐
                │ simple     │ complex     │
                ▼            ▼             │
        ┌──────────┐  ┌──────────┐        │
        │ 走 v1.x  │  │generate_ │        │
        │ 检索+生成│  │  plan    │        │
        │ (END)    │  │(轻量LLM) │        │
        └──────────┘  └────┬─────┘        │
                           │              │
                    ┌──────▼──────┐        │
                    │  execute_   │        │
                    │   tools     │ ← 复用 SearchService.search_with_weights()
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
                    └──────┬──────┘        │
                           │              │
                ┌──────────┼──────────┐
                │ pass     │ fail &   │
                │          │ retry<N  │
                ▼          ▼          │
            ┌──────┐  ┌──────────┐    │
            │ END  │  │adjust_   │    │
            │      │  │  params  │────┘  按最低分维度选择策略
            └──────┘  └──────────┘  (回到 execute_tools)
```

### 10.3 工具层

| 工具名 | 实现 | 功能 | 参数 |
|--------|------|------|------|
| `hybrid_search` | 调用 `SearchService.search_with_weights()` | 向量+全文混合检索（复用完整 v1.x 管线：查询改写→RRF→rerank） | query, user_id, collection_id, top_k(40), vector_weight(0.7), bm25_weight(0.3) |
| `fulltext_search` | 调用 `SearchService._bm25_search()` | BM25 全文检索 | query, user_id, top_k(20) |

`hybrid_search` 已改为复用 `SearchService.search_with_weights()`，零重复代码，查询改写步骤保留。

### 10.4 Checkpoint 状态持久化

**当前状态：未实现。** `app/models/agent_checkpoint.py` 表结构已定义，但 `app/agent/` 目录下没有 `checkpoint.py` 文件。LangGraph graph 使用默认的内存状态管理，进程重启后状态丢失。

设计目标（待实现）：

| 层级 | 存储 | Key | TTL | 用途 |
|------|------|-----|-----|------|
| 热层 | Redis | `agent:ckpt:{thread_id}` | 1 小时 | 正在执行的任务状态 |
| 冷层 | PostgreSQL `agent_checkpoints` | `thread_id` (PK) | 30 天 | 历史回溯、进程重启恢复 |

### 10.5 降级机制

```python
# app/agent/degrade.py — 系统水位检测
def should_degrade() -> bool:
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory().percent
    if cpu > settings.agent_degrade_cpu_threshold:   # 默认 80%
        return True
    if mem > settings.agent_degrade_mem_threshold:   # 默认 85%
        return True
    return False
```

**降级路径（三层）：**

| 触发点 | 代码位置 | 行为 | 状态 |
|--------|---------|------|------|
| 系统过载 | `router.py:38-44` | SSE 构建前降级，发送 `agent_step` 降级通知 | ✅ 已实现 |
| Agent 构建异常 | `router.py:46-54` | `except Exception` 兜底，发送降级通知 | ✅ 已实现 |
| SSE 流内异常 | `router.py:364-390` | 流内降级：先发降级通知，再用 v1.x 逻辑检索生成 | ✅ 已实现 |
| graph 超时(60s) | `router.py:276-308` | `asyncio.wait_for` 超时后流内降级到 v1.x | ✅ 已实现 |
| 反思耗尽 | `router.py:342-344` | answer 末尾追加质量警告 `[注：此回答的质量校验未完成]` | ✅ 已实现 |

降级通知通过 SSE `agent_step` 事件发送（`tool: "system"`），前端可区分展示。

### 10.6 成本控制策略

| 策略 | 状态 | 说明 |
|------|------|------|
| 轻量 LLM 中间步骤 | ✅ | 规划 + 反思用 glm-4.5-air；意图分类为纯规则零成本 |
| Embedding 缓存 | ✅ | 复用 v1.x Redis 缓存层 |
| 用户限流 | ✅ | v1.x 和 Agent 共享 100次/小时 |
| 重试上限 | ✅ | `agent_max_retries=2`（总尝试 3 次） |
| 本地 Reranker 禁用 | ✅ | 已移除本地 CrossEncoder fallback，防止 4G OOM |
| httpx 连接池复用 | ✅ | `_get_http_client()` 单例复用 |
| API 调用计数 | ✅ | `agent_api_calls_total` 在 `_call_lightweight_llm` 中 `.inc()` |
| 规划缓存 | ❌ | 未实现 |
| 检索结果缓存 | ❌ | 未实现 |
| Agent 独立限流 | ❌ | 未实现 |

成本增量尚无线上数据验证。

---

## 11. 可观测性

### 11.1 v1.x Prometheus 指标

| 指标 | 类型 | 含义 | 是否已接入 |
|------|------|------|-----------|
| `rag_retrieval_duration_seconds` | Histogram | 混合检索总耗时 | ✅ |
| `rag_results_count` | Histogram | Rerank 后结果数量 | ✅ |
| `rag_rerank_duration_seconds` | Histogram | Rerank 耗时 | ✅ |
| `rag_llm_duration_seconds` | Histogram | LLM 生成耗时 | ✅ |
| `embedding_api_duration_seconds` | Histogram | Embedding API 调用耗时 | ✅ |
| `rerank_api_duration_seconds` | Histogram | Rerank API 调用耗时 | ✅ |
| `ocr_api_duration_seconds` | Histogram | OCR API 调用耗时 | ✅ |
| `api_error_total` | Counter | API 调用失败次数（按 service 标签） | ✅ |
| `model_memory_bytes` | Gauge | 本地模型内存占用 | ❌ 已定义但未调用 `.set()` |

### 11.2 v2.0 新增 Prometheus 指标

| 指标 | 类型 | 含义 | 是否已接入 | 告警阈值 |
|------|------|------|-----------|----------|
| `agent_execution_duration_seconds` | Histogram | Agent 总执行时长 | ✅ | P95 > 2s |
| `agent_tool_call_duration_seconds` | Histogram | 单次工具调用时长 | ❌ 已定义未使用 | P95 > 1s |
| `agent_retry_total` | Counter | 反思重试次数 | ✅ reflect 节点触发时 `.inc()` | 1小时 > 100 |
| `agent_degrade_total` | Counter | 降级到 v1.x 次数 | ✅ | 1小时 > 50 |
| `intent_classify_total` | Counter | 意图分类结果分布 | ✅ `intent_classify` 节点内 `.inc()` | — |
| `intent_classify_duration_seconds` | Histogram | 意图分类耗时 | ✅ | — |
| `agent_api_calls_total` | Counter | Agent API 调用次数 | ✅ `_call_lightweight_llm` 内 `.inc()` | — |

### 11.3 Agent Trace

**当前状态：部分实现。** `messages` 表有 `agent_trace` JSONB 字段（表结构已定义），但 `router.py` 和 `nodes.py` 中未写入 trace 数据。

设计目标（待实现）：每条 Agent 执行记录完整过程，存储到 `messages.agent_trace` JSONB 字段，供离线分析和问题排查。

### 11.4 告警规则

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
| Agent 无限循环 | LLM 持续生成无效 Plan | 重试上限 2 次 + 全局超时 30s |

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

# ── Agent (v2.0) ──
USE_AGENT=true
AGENT_LIGHTWEIGHT_LLM=glm-4-flash
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

### 13.4 v2.0 部署步骤

```bash
# 1. 安装新依赖
pip install langgraph langchain-core langchain psutil

# 2. 数据库迁移（仅新增 1 张表 + 1 列）
alembic revision --autogenerate -m "add_agent_checkpoints_and_trace"
alembic upgrade head

# 3. 配置 .env 新增 Agent 相关变量
# 4. 重启服务（与 v1.x 完全相同）
sudo systemctl restart knspace

# 5. 验证 v1.x 接口正常（默认 use_agent=false）
curl http://localhost:8000/api/v1/auth/me -H "Authorization: Bearer <token>"

# 6. 灰度开启 Agent（按用户 hash 比例控制）
# 在 .env 中设置 USE_AGENT=true
```

---

## 14. 演进路线

| 版本 | 核心改动 | 架构影响 |
|------|----------|----------|
| **v1.x** (已上线) | 固定 RAG 管线：解析→分块→混合检索→Rerank→LLM 生成 | 单实例部署 |
| **v2.0** (本次) | 新增 `app/agent/` LangGraph 编排层，混合路由，双层 Checkpoint | 无侵入，兼容 v1.x，新增 <50MB |
| **v2.1** | Agent 无状态化，Redis Cluster 替换单机 Redis，水平扩展 | 可扩展至 100+ 实例 |
| **v2.2** | 多 Agent 协作（检索 Agent + 写作 Agent + 审核 Agent） | 复杂任务拆分为 Agent 流水线 |
| **v3.0** | Milvus Cluster、ES Cluster、PG 读写分离 | 支撑百万级用户 |

**平滑设计保障**：

- 接口兼容：`POST /api/v1/chat` 新增 `use_agent` 参数，默认 `false`
- 组件可替换：基于 `factory.py` 的 7 个 Protocol 抽象，替换 Milvus/ES/LLM 时 Agent 层零改动
- 数据零迁移：v2.0 只新增 `agent_checkpoints` 表，不修改任何 v1.x 表结构
- 灰度能力：可按用户 ID hash 控制灰度比例（10% → 50% → 100%）

---

## 15. 延迟预算

| 路径 | 节点 | 目标延迟 |
|------|------|----------|
| v1.x 固定链路 | 注入检测→限流→指代消解→查询分析→混合检索→Rerank→LLM生成→引用校验 | <500ms |
| Agent 简单路径 | 意图分类(规则预筛)→simple→降级到 v1.x | <500ms |
| Agent 复杂路径 | 意图分类→规划→工具执行→生成→反思 | <1.5s |
| Agent 重试路径 | 上述 + 1 次反思重试（adjust_params→execute_tools→generate→reflect） | <2.5s |

---

## 16. 开发计划（v2.0 三周 MVP）

| 阶段 | 时间 | 任务 | 交付物 |
|------|------|------|--------|
| 第一周 | D1-D7 | `state.py` 状态定义 + `graph.py` 状态图 + `tools.py` 工具封装 + `nodes.py` 4个节点 | Agent 最小闭环可运行 |
| 第二周 | D8-D14 | `checkpoint.py` 双层持久化 + `router.py` 混合路由 + `degrade.py` 降级 + `api/chat.py` 集成 | 完整链路联调通过 |
| 第三周 | D15-D21 | `metrics` 新增 4 个指标 + `agent_trace` JSONB + 单元测试 + 集成测试 + 灰度上线 | 生产环境就绪 |

### 每日里程碑

```
D1  state.py + graph.py 骨架
D2  nodes.py — intent_classification + generate_plan
D3  tools.py — HybridSearchTool + FullTextSearchTool
D4  nodes.py — execute_tools + generate_answer
D5  nodes.py — reflect + 条件边
D6  graph.py 串联 + 本地联调
D7  第一周末检收：最小闭环跑通

D8  checkpoint.py — DualCheckpointSaver
D9  config.py 新增配置 + 数据库迁移
D10 router.py — route_query + v1.x 降级
D11 api/chat.py 集成 use_agent 分支
D12 SSE 流式输出适配
D13 多轮对话 + collection 过滤
D14 第二周末检收：完整链路联调

D15 metrics — 4 个新增 Prometheus 指标
D16 agent_trace — messages 表新增 JSONB 列
D17 单元测试（tools / nodes / checkpoint）
D18 集成测试（端到端 Agent 流程）
D19 压测 + 调优
D20 灰度部署（10% 用户）
D21 全量上线
```

---

## 17. 设计现状总结

### 已落地

1. **渐进式架构演进**：v2.0 = v1.x + Agent 层。v1.x 的 16 个 service 模块原封不动复用，v2.0 新增 `app/agent/` 目录（6 个文件），改动量仅 3 个文件约 20 行代码。

2. **LangGraph 状态图编排**：6 节点状态图（intent_classify → generate_plan → execute_tools → generate_answer → reflect → adjust_params），条件路由 + 重试循环，进程内执行零额外服务依赖。

3. **RRF 混合检索 + 规则查询分析**：向量检索 + BM25 全文检索 → RRF 融合 → Rerank 精排。纯规则查询分类器支持 keyword/semantic/compare/multi_hop 四种模式，零 LLM 调用成本。

4. **多层回退链**：Embedding（Redis→API→Local→Dummy）、FTS（ES→PG FTS）、Rerank（API→Local→不排序）、VectorStore（Milvus→Pickle）。任何单点故障不影响系统可用性。

5. **结构化父子分块**：按标题层级分组，父 chunk 提供完整段落上下文，子 chunk 精确检索。检索结果自动附带 `parent_content`。

### 待优化（详见 OPTIMIZATION-PLAN.md）

6. **Protocol 接口层**：方法名已对齐，但调用方仍直接 import 具体模块，未通过工厂方法。需编写参数化测试后才能保证替换安全。

7. **成本可观测性**：`agent_api_calls_total` 已接入，但缺少 token 成本指标和按路径拆分的成本 dashboard。

8. **规划缓存 / 检索结果缓存**：设计文档提到的规划缓存（Redis TTL=1d）和检索结果缓存均未实现。

9. **Agent 独立限流**：v1.x 和 Agent 共享同一限流桶，缺少 Agent 路径的独立并发控制。

10. **Checkpoint 状态持久化**：表结构已定义，`app/agent/checkpoint.py` 未实现。
