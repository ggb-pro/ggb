# knSpace 详细设计文档

> 基于 4C/4G 腾讯云单实例部署的私有化 RAG 知识库系统。
> 本文档描述已实现的全部功能，与代码一一对应。

---

## 1. 项目概览

### 1.1 定位

knSpace 是一个私有化部署的 RAG（Retrieval-Augmented Generation）知识库系统。用户上传文档（PDF/Word/Markdown/图片/网页），系统自动解析、分块、向量化，然后基于文档内容进行智能问答。

### 1.2 核心能力

- 多格式文档解析：PDF（PyMuPDF）、Word（python-docx）、Markdown、图片（OCR）、网页（Playwright）
- 结构化父子分块：按标题层级分组，父 chunk 提供完整上下文，子 chunk 精确检索
- 混合检索：向量检索（Milvus）+ 全文检索（Elasticsearch）+ RRF 融合
- 查询智能分析：规则引擎自动分类（keyword/semantic/compare/multi_hop），动态调整检索权重
- 多轮对话指代消解：规则 + LLM 两级消解，支持"它""这个"等代词
- 流式响应：SSE 实时推送 LLM token + 检索状态
- 跨用户文档去重：相同文件只处理一次，其他用户克隆 chunk

### 1.3 技术栈

| 层级 | 技术 | 版本 | 用途 |
|------|------|------|------|
| 语言 | Python | 3.12 | 后端 |
| Web 框架 | FastAPI + Uvicorn | 0.115.6 | 异步 HTTP 服务 |
| 数据库 | PostgreSQL | 16 | 业务数据（users/documents/chunks/conversations） |
| ORM | SQLAlchemy | 2.0.36 | 异步 ORM（Mapped + mapped_column） |
| 向量库 | Milvus Standalone | 2.5.6 | 向量存储 + COSINE 检索（Docker） |
| 搜索引擎 | Elasticsearch | 8.17.0 | 全文检索 + jieba 分词（Docker） |
| 缓存 | Redis | 7.x | embedding 缓存 + 限流计数 |
| 前端 | Vue 3 + Vite + Tailwind | - | SPA，构建后由 FastAPI 静态托管 |
| 反向代理 | Nginx | - | SSL + 限流 + 静态资源 |
| Embedding | SiliconFlow API | BAAI/bge-m3 | 1024 维向量（云端 API） |
| Reranker | SiliconFlow API | BAAI/bge-reranker-v2-m3 | 精排（云端 API） |
| OCR | SiliconFlow API | DeepSeek-OCR | 图片文字识别（云端 API） |
| LLM | OpenAI 兼容 API | glm-5.1-openai | 流式生成（云端 API） |
| 中文分词 | jieba | 0.42.1 | 全文检索分词 |
| 监控 | Prometheus + Grafana | - | 指标采集 + 可视化 |

### 1.4 硬件约束

4 核 AMD EPYC 7K62 / 4 GB 内存 / 40 GB SSD / 腾讯云轻量 4Mbps

全部 AI 模型走云端 API，服务器零模型内存占用。月成本约 43 元（API）+ 100 元（服务器）。

---

## 2. 架构总览

### 2.1 系统架构

```
                        ┌──────────────┐
                        │    Nginx     │ SSL 终结 + 限流 + 静态资源
                        └──────┬───────┘
                               │
                        ┌──────▼───────────────────────────┐
                        │  FastAPI (uvicorn) :8000           │
                        │                                    │
                        │  ┌─────┐ ┌──────┐ ┌──────┐       │
                        │  │Auth │ │Doc   │ │Chat  │       │
                        │  │API  │ │API   │ │API   │       │
                        │  └─────┘ └──────┘ └──────┘       │
                        │  ┌──────┐ ┌────┐ ┌──────┐       │
                        │  │Coll  │ │Conv│ │Eval  │       │
                        │  │API   │ │API │ │API   │       │
                        │  └──────┘ └────┘ └──────┘       │
                        └──┬───────┬──────────────────────┘
                           │       │
              ┌────────────┘       └────────────┐
              │                                  │
       ┌──────▼──────┐                   ┌──────▼──────┐
       │ PostgreSQL  │                   │ Milvus      │
       │ :5432       │                   │ Standalone  │
       │ 业务数据     │                   │ :19530      │
       └──────┬──────┘                   └─────────────┘
              │
       ┌──────▼──────┐
       │Elasticsearch│
       │ :9200       │
       │ 全文检索     │
       └─────────────┘

       ┌──────▼──────┐
       │   Redis     │ embedding 缓存 + 限流
       │ :6379       │
       └─────────────┘

      ┌──────────────────────────────────────────────────┐
      │              云端 API（零本地内存）                  │
      │  SiliconFlow: Embedding · Rerank · OCR            │
      │  OpenAI-compat: LLM (glm-5.1)                     │
      └──────────────────────────────────────────────────┘
```

### 2.2 组件职责

| 组件 | 职责 | 端口 |
|------|------|------|
| Nginx | SSL 终结、限流 10r/s、SPA 静态资源 | 443/80 |
| FastAPI | 所有业务 API、SPA fallback | 8000 |
| PostgreSQL | 用户、文档、chunk、会话、标签持久化 | 5432 |
| Milvus Standalone | 向量存储 + COSINE 检索，IVF_FLAT 索引 | 19530 |
| Elasticsearch 8.x | 全文检索，jieba 分词，content + content_jieba 双字段 | 9200 |
| Redis | Embedding 缓存（7 天 TTL）、API 限流（100 次/时） | 6379 |

### 2.3 内存预算

| 组件 | 常驻内存 | 说明 |
|------|----------|------|
| OS + 系统服务 | 500 MB | 含腾讯云监控 |
| PostgreSQL 16 | 300 MB | shared_buffers=128MB |
| Elasticsearch 8.x | 300 MB | 单节点，1 shard |
| Milvus Standalone | 200 MB | Docker，IVF_FLAT |
| FastAPI | 200 MB | 含所有业务代码 |
| Redis | 50 MB | 缓存 + 限流 |
| Nginx | 20 MB | 反向代理 |
| **常驻合计** | **~1.6 GB** | |
| **剩余** | **~2.0 GB** | 连接池 + 请求缓冲 |

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
```

### 3.3 关键设计决策

- **UUID 主键**：分布式友好。users/collections 等表用 `VARCHAR(36)`，chunks 表用原生 `UUID` 类型
- **JSON 元数据**：documents.metadata、chunks.metadata、users.settings 使用 JSON 字段，避免频繁加列
- **软删除**：documents、collections、conversations 使用 `is_deleted` 而非物理删除
- **所有查询强制带 user_id**：为未来 PostgreSQL RLS 做准备

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
| POST | `/` | 智能问答（SSE 流式返回） | JWT |

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

**入口**：`POST /api/v1/documents/upload` 或 `POST /api/v1/documents/import-url`
**核心服务**：`app/services/doc_processor.py:process_document()`

### 5.1 完整流程

```
用户上传文件
     │
     ├── 1. 读取文件内容，计算 SHA-256 content_hash
     │
     ├── 2. 用户内去重检查（content_hash + user_id）
     │     └── 重复 → 409 File already uploaded
     │
     ├── 3. 跨用户去重检查（content_hash，不限 user_id）
     │     ├── 已存在 → 克隆 chunks（_clone_chunks_from_existing）
     │     │            跳过解析/分块/向量化，直接返回 ready
     │     └── 不存在 → 继续正常处理
     │
     ├── 4. 保存文件到本地文件系统（data/files/{user_id}/{doc_id}_{filename}）
     │
     ├── 5. 创建 Document 记录（status=pending）
     │
     ├── 6. asyncio.create_task(_bg_process) 进程内异步后台任务
     │     │
     │     ├── [parsing] 解析文件
     │     │     ├── PDF → PyMuPDF 提取文本（逐页 → ParsedSection）
     │     │     ├── Word → python-docx 提取段落 + 标题层级
     │     │     ├── Markdown → 按段落分组，识别 # 标题
     │     │     ├── 图片 → OCR API → PaddleOCR fallback
     │     │     └── 输出：ParsedDocument { title, sections[], raw_text, page_count }
     │     │
     │     ├── [chunking] 结构化分块
     │     │     ├── 按标题层级分组（_group_by_headings）
     │     │     ├── 每组 1 个 parent chunk（完整段落）+ N 个 child chunk（400字，64字overlap）
     │     │     ├── 超长文本按句子边界（。！？.!?）切割
     │     │     ├── 过滤 < 20 字的无效 chunk
     │     │     └── 写入 chunks 表（含 parent_chunk_id 引用）
     │     │
     │     ├── ES 索引（主路径）
     │     │     ├── 对每个 chunk 做 jieba 分词
     │     │     ├── bulk_index_chunks() 写入 ES（content + content_jieba 双字段）
     │     │     └── 失败 → fallback PG fts_vector 列
     │     │
     │     ├── [embedding] 向量化
     │     │     ├── 批量 embed_texts（batch_size=64）
     │     │     ├── 优先 SiliconFlow API（BAAI/bge-m3，1024 维）
     │     │     ├── Redis 缓存命中跳过已计算的 embedding
     │     │     └── API 失败 → 本地 SentenceTransformer → dummy hash
     │     │
     │     ├── 写入 Milvus（IVF_FLAT 索引，COSINE 度量）
     │     │     └── snippet 按 UTF-8 字节截断到 8000 字节
     │     │
     │     └── [ready] 更新 Document.chunk_count
     │
     ├── 失败 → [failed] + processing_error 记录
     │
     └── 前端 SSE 轮询 GET /{doc_id}/status/stream（5秒间隔，5分钟超时）
```

### 5.2 跨用户去重

当用户 B 上传了与用户 A 相同的文件（SHA-256 匹配），系统直接克隆 A 的 chunks 给 B，跳过解析/分块/向量化，大幅减少 API 调用成本。去重查询不暴露其他用户信息。

### 5.3 状态流转

```
pending → parsing → chunking → embedding → ready
                                              └→ failed（任何环节异常）
```

---

## 6. 智能问答管线

**入口**：`POST /api/v1/chat`
**请求体**：`{ query, conversation_id?, collection_id?, history? }`
**响应**：SSE 事件流

### 6.1 完整流程

```
用户提问
     │
     ├── 1. Prompt 注入检测（guard.check_injection）
     │     └── 命中模式 → 直接拒绝
     │
     ├── 2. 限流检查（cache.check_rate_limit）
     │     └── Redis INCR + EXPIRE，100 次/小时
     │
     ├── 3. 获取/创建 Conversation，保存 user Message
     │
     ├── 4. 指代消解（multi_turn.resolve_query_with_history）
     │     ├── 检测代词（它/这个/那/前面...）
     │     ├── Rule-based：从历史提取实体替换代词（<1ms）
     │     └── LLM fallback：改写为独立查询（~1s）
     │
     ├── 5. 查询分析（query_analyzer.analyze）
     │     ├── 分类：keyword / semantic / compare / multi_hop
     │     ├── 改写：去掉口语噪声
     │     ├── 分解：compare 拆为 2 个子查询
     │     └── 权重：keyword → 0.3/0.7，其他 → 0.7/0.3
     │
     ├── 6. 混合检索（search.py）
     │     ├── 向量检索：embed_query → Milvus.search（top_k=40）
     │     ├── 全文检索：ES.search（jieba + 双字段匹配）
     │     │           └── ES 失败 → PG FTS fallback
     │     ├── RRF 融合：weight / (60 + rank)
     │     └── 多子查询时合并 RRF 分数
     │
     ├── 7. 拉取 chunk 内容（_fetch_chunks）
     │     ├── 从 PG 查 chunk 内容
     │     └── 级联拉取 parent_chunk 的内容（parent_content）
     │
     ├── 8. Rerank（_rerank）
     │     ├── 候选 top 40 → 精排 top 10
     │     ├── API（SiliconFlow）优先
     │     └── 本地 CrossEncoder fallback
     │
     ├── 9. 上下文构建（build_context）
     │     ├── 优先使用 parent_content（完整段落）
     │     ├── 按 char 数截断（max=8000）
     │     └── 格式：[1] 内容 \n\n [2] 内容
     │
     ├── 10. LLM 流式生成
     │     ├── System Prompt：基于参考信息回答，标注 [1][2]
     │     ├── 历史：最近 6 条消息
     │     └── max_tokens=2000, temperature=0.3
     │
     ├── 11. 引用校验（citation.validate_citations）
     │
     └── 12. 保存 assistant Message（含 citations），更新 Conversation
```

### 6.2 SSE 事件格式

```
data: {"type": "conversation", "conversation_id": "xxx"}
data: {"type": "status", "message": "Searching..."}
data: {"type": "citations", "data": [{chunk_id, score, snippet}]}
data: {"type": "token", "content": "每"}
data: {"type": "token", "content": "个"}
...
data: {"type": "done", "conversation_id": "xxx"}
data: {"type": "error", "message": "处理失败"}
```

---

## 7. 混合检索引擎

### 7.1 双通道架构

```
查询 ──┬── 向量通道 ── embed_query() ── Milvus.search() ── [{chunk_id, score}]
        │
        └── 全文通道 ── jieba tokenize ── ES.search() ── [{chunk_id, score}]
                                         └── fallback ── PG FTS ── [{chunk_id, score}]

两路结果 → RRF 融合 → 按 score 排序 → top 40 候选
```

### 7.2 RRF 融合

```python
scores = {}
for rank, r in enumerate(vector_results):
    scores[cid] += vector_weight / (60 + rank + 1)
for rank, r in enumerate(bm25_results):
    scores[cid] += bm25_weight / (60 + rank + 1)
```

RRF 无需调参，自动平衡两路检索的质量差异。K=60 是经典值，兼顾头部精度和长尾覆盖。

### 7.3 查询分析器

纯规则引擎，零 LLM 开销，<1ms。

| 分类 | 触发条件 | 检索策略 |
|------|----------|----------|
| keyword | 引号字符串、UUID、长 ID | vector=0.3, bm25=0.7 |
| semantic | 疑问词（为什么/怎么/什么是） | vector=0.7, bm25=0.3 |
| compare | "对比""区别""vs" | 拆为 2 个子查询，合并 RRF |
| multi_hop | "A 和 B 的关系" | 拆为 2 个子查询，合并 RRF |

### 7.4 中文分词一致性

PostgreSQL `simple` 分词器对中文完全无效。使用 jieba 应用层分词，写入和查询同一套分词器：

- **写入**：`tokenize(text)` → jieba 切词 → 存入 ES `content_jieba` 字段
- **查询**：`tokenize_query(query)` → jieba 切词 → ES match 查询

ES 同时搜索 `content`（原始）和 `content_jieba`（分词后），提升召回率。

### 7.5 ES 索引设计

```json
{
  "mappings": {
    "properties": {
      "chunk_id":      {"type": "keyword"},
      "document_id":   {"type": "keyword"},
      "user_id":       {"type": "keyword"},
      "content":       {"type": "text", "analyzer": "standard"},
      "content_jieba": {"type": "text", "analyzer": "standard"}
    }
  }
}
```

查询：`bool.must=term(user_id) + bool.should=[match(content_jieba), match(content)]`

---

## 8. 各服务模块详解

### 8.1 Parser

文件：`app/services/parser.py`

从多种格式提取文本，输出 `ParsedDocument(title, sections, raw_text, page_count)`。

| 格式 | 库 | 处理逻辑 |
|------|----|----------|
| PDF | PyMuPDF | 逐页提取文本，每页一个 ParsedSection |
| Word | python-docx | 遍历段落，从 style 识别 Heading 层级 |
| Markdown | 内置 | 按空行分段，`#` 识别 heading level |
| TXT | 内置 | 按双换行分段 |
| 图片 | OCR 服务 | 调 ocr_image() → 按行分段 |

### 8.2 Chunking

文件：`app/services/chunking.py`

结构化父子分块算法：

1. 按标题分组（_group_by_headings）
2. Parent chunk：每组全部文本合并（完整上下文）
3. Child chunks：400 字、64 字 overlap 切分
4. 超长按句子边界切割
5. 过滤 < 20 字的 chunk

参数：CHUNK_SIZE=400, CHUNK_OVERLAP=64, MIN_CHUNK_SIZE=20

### 8.3 Embedding

文件：`app/services/embedding.py`

Fallback 链：Redis 缓存 → SiliconFlow API → 本地 SentenceTransformer → dummy hash

- Redis 缓存：key=`emb:{md5(text)}`，TTL=7 天
- API 批量：batch_size=64
- Dummy：SHA-256 种子确定性随机向量

### 8.4 Vector Store

文件：`app/services/vector_store.py`

**MilvusStandaloneStore**：Collection `chunks`，IVF_FLAT 索引，COSINE 度量。user_id 作为 partition_key。snippet 按 UTF-8 字节截断到 8000 字节。

**PickleStore**（fallback）：numpy 数组 + pickle 持久化。

### 8.5 ES 全文检索

文件：`app/services/es.py`

- `bulk_index_chunks()`：jieba 分词后写入 ES（content + content_jieba）
- `search()`：双字段匹配，返回 chunk_id + score
- `delete_by_document()`：按 document_id 清理
- `_ensure_index()`：首次自动创建索引

### 8.6 LLM

文件：`app/services/llm.py`

OpenAI 兼容接口，流式 SSE 解析。支持 glm-5.1 的 reasoning_content（思维链）。System Prompt 强制基于参考信息回答，标注来源编号。

### 8.7 OCR

文件：`app/services/ocr.py`

API（SiliconFlow Vision，base64 → chat/completions）→ PaddleOCR fallback。

### 8.8 Web Scraper

文件：`app/services/web_scraper.py`

httpx 优先（<1s）→ Playwright headless Chromium fallback（~5s）。只在 httpx 返回 < 500 字时触发 Playwright。

### 8.9 Cache

文件：`app/services/cache.py`

- `get_cached_embedding` / `cache_embedding`：Redis GET/SET，TTL=7d
- `check_rate_limit`：Redis INCR + EXPIRE，100 次/小时

### 8.10 Security

文件：`app/utils/security.py` + `app/services/guard.py`

- 密码：bcrypt（cost=12）
- Token：JWT HS256，access 15min + refresh 7d
- 注入检测：10 个正则模式
- 输入长度限制：10,000 字符

### 8.11 Citation

文件：`app/services/citation.py`

`validate_citations(answer, max_index)` 替换超出范围的 [N] 引用，防止 LLM 幻觉。

### 8.12 Evaluator

文件：`app/services/evaluator.py`

Golden dataset + Recall@5/10 + MRR + NDCG@10。API 触发评估。

---

## 9. 接口抽象层

文件：`app/services/factory.py`

7 个 Protocol 接口，业务代码只依赖抽象：

| Protocol | 当前实现 | 百万版实现 |
|----------|----------|-----------|
| VectorStoreBase | MilvusStandaloneStore | Milvus Cluster |
| FullTextSearchBase | ES (jieba) | ES Cluster (ik) |
| ObjectStorageBase | 本地文件系统 | MinIO/S3 |
| EmbeddingServiceBase | SiliconFlow API | GPU 自建 |
| RerankServiceBase | SiliconFlow API | GPU 自建 |
| OcrServiceBase | SiliconFlow Vision | GPU 自建 |
| LlmServiceBase | glm-5.1 API | DeepSeek API |

迁移路径：新增实现类 → 修改工厂函数 return → 业务代码零改动。

---

## 10. 前端

Vue 3 + Vite + Tailwind CSS。构建输出到 `app/static/`，FastAPI 托管 SPA 静态文件，不匹配的路由 fallback 到 `index.html`。

---

## 11. 可观测性

### 11.1 JSON 日志

```json
{"ts": "2026-05-26T17:36:46", "level": "INFO", "logger": "app.services.doc_processor", "msg": "Committed 561 chunks to DB"}
```

### 11.2 Prometheus 指标

| 指标 | 类型 | 含义 |
|------|------|------|
| rag_retrieval_duration_seconds | Histogram | 检索总耗时 |
| rag_rerank_duration_seconds | Histogram | 重排序耗时 |
| rag_llm_duration_seconds | Histogram | LLM 生成耗时 |
| rag_results_count | Histogram | 返回结果数 |
| embedding_api_duration_seconds | Histogram | Embedding API 耗时 |
| api_error_total | Counter | API 失败数 |

端点：`GET /metrics`

---

## 12. 安全设计

### 12.1 认证

```
注册 → bcrypt(password) → users 表
登录 → bcrypt.verify → JWT {sub, exp} → {access_token, refresh_token}
请求 → Bearer {token} → jwt.decode → get_current_user
```

### 12.2 隔离

所有查询强制 `WHERE user_id = ?`。文档/Chunk/会话/消息/收藏夹/标签全部隔离。

### 12.3 防护

| 层 | 措施 |
|----|------|
| SQL 注入 | SQLAlchemy 参数化 |
| Prompt 注入 | 10 个正则模式 |
| 输入长度 | 10,000 字符上限 |
| 限流 | Redis 100 次/小时 |

### 12.4 已知风险

| 风险 | 严重度 | 状态 |
|------|--------|------|
| Milvus filter f-string 拼接 | 高 | 待修复 |
| SSE error 返回 str(e) | 中 | 待修复 |
| LLM API 默认 HTTP | 中 | 已修复 |
| pickle 反序列化 | 低 | 待修复 |

---

## 13. 环境变量与部署

### 13.1 .env

```bash
DATABASE_URL=postgresql+asyncpg://knspace:knspace123@localhost/knspace
REDIS_URL=redis://localhost:6379/0

EMBEDDING_BACKEND=api
EMBEDDING_API_URL=https://api.siliconflow.cn/v1
EMBEDDING_API_KEY=sk-xxx
EMBEDDING_MODEL=BAAI/bge-m3

RERANK_BACKEND=api
RERANK_API_URL=https://api.siliconflow.cn/v1/rerank
RERANK_API_KEY=sk-xxx
RERANK_MODEL=BAAI/bge-reranker-v2-m3

OCR_BACKEND=api
OCR_API_URL=https://api.siliconflow.cn/v1
OCR_API_KEY=sk-xxx
OCR_MODEL=deepseek-ai/DeepSeek-OCR

LLM_API_URL=https://your-llm-host/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=glm-5.1-openai

ES_URL=http://localhost:9200
ES_INDEX=chunks
MILVUS_URI=http://localhost:19530
FILE_STORAGE_PATH=/data/knspace/files
```

### 13.2 systemd

```ini
[Unit]
Description=knSpace RAG Knowledge Base
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/knSpacePro
Environment=PATH=/home/ubuntu/knSpacePro/venv/bin:/usr/bin
EnvironmentFile=/home/ubuntu/knSpacePro/.env
ExecStart=python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

### 13.3 Docker 容器

| 服务 | 镜像 | 端口 | 配置 |
|------|------|------|------|
| Milvus | milvusdb/milvus:v2.5.6 | 19530 | ETCD_USE_EMBED=true, COMMON_STORAGETYPE=local |
| Elasticsearch | elasticsearch:8.17.0 | 9200 | xpack.security.enabled=false, single-node |

---

## 14. 亮点设计总结

### 1. 结构化父子分块

按标题层级分组，每组 parent chunk（完整段落）+ 多个 child chunk（400 字切片）。检索命中 child 时拉取 parent 完整内容，解决传统固定窗口"切在句子中间"的痛点。

### 2. 双引擎混合检索 + RRF 无参融合

向量（语义）+ 全文（关键词），RRF 融合无需训练调参。`score = weight / (K + rank)` 自动平衡质量差异。

### 3. 查询类型自适应

规则引擎（<1ms）分为 keyword/semantic/compare/multi_hop，动态调整权重。compare/multi_hop 自动拆子查询分别检索后合并。

### 4. 多层 Fallback 链

每个外部依赖都有 fallback：Embedding（Redis→API→Local→Dummy）、FTS（ES→PG）、Rerank（API→Local）、OCR（API→PaddleOCR）、向量库（Milvus→Pickle）、抓取（httpx→Playwright）。

### 5. 全链路 SSE 实时反馈

每个阶段通过 SSE 实时推送状态，用户无需等待全部完成。

### 6. Protocol 接口抽象

7 个 Protocol + Adapter + Factory，迁移百万版只需新增实现类。

### 7. 中文 jieba 分词一致性

写入和查询同一套 jieba 分词器，ES 双字段（content + content_jieba）搜索。

### 8. 跨用户文档去重

SHA-256 检测重复，跨用户直接克隆 chunks，节省 API 成本。去重不暴露其他用户信息。

### 9. RAG 离线评估

Golden dataset + Recall/MRR/NDCG 指标，API 触发评估，提供检索质量优化基准。
