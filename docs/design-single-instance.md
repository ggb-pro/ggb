# knSpace 单实例设计方案（云端 API 版）

> 面向 4C/4G 服务器的最小可行架构，所有 AI 模型通过云端 API 调用。
> 保证与百万用户版本（design-final.md）平滑迁移。
> 设计原则：**接口一致，实现可替换。**

---

## 1. 设计目标与约束

### 1.1 硬件约束

| 资源 | 规格 | 说明 |
|------|------|------|
| CPU | 4 核 AMD EPYC 7K62 | 无 AVX-512 |
| 内存 | 4 GB（可用 ~3.6 GB） | 模型全走 API，不再是瓶颈 |
| 磁盘 | 40 GB SSD | 含 OS + 数据 |
| GPU | 无 | 不需要，全部 API 调用 |
| 网络 | 腾讯云轻量 4Mbps | 上行带宽受限，API 调走内网/公网 |

### 1.2 容量目标

| 指标 | 单实例目标 | 百万用户版本 |
|------|-----------|-------------|
| 注册用户 | 1,000 | 1,000,000 |
| DAU | 50 | 100,000 |
| 文档总量 | 10,000 | 10,000,000 |
| 向量总量 | 500 万 | 10 亿 |
| QPS | 5 | 150 |
| 存储 | ~20 GB | ~20 TB |

### 1.3 核心架构决策：全部模型走云端 API

| 模型 | 本地加载内存 | 云端 API | API 延迟 | API 成本 |
|------|-------------|---------|---------|---------|
| **bge-m3**（embedding） | ~2.5 GB | OpenAI 兼容 Embedding API | ~200ms/批 | ~¥0.7/百万 token |
| **bge-reranker-v2-m3** | ~1.2 GB | Jina/Cohere Rerank API 或自建 | ~300ms | ~¥0.5/千次 |
| **PaddleOCR** | ~1.5 GB | 腾讯云 OCR / 百度 OCR API | ~1s/页 | ~¥0.01/次（免费额度内） |
| **LLM** | N/A | glm-5.1 / DeepSeek API | ~2s 首 token | ~¥1/百万 token |

**关键结论：4 GB 内存完全足够。** 无需加载任何模型，服务器只运行业务逻辑 + 数据库。

---

## 2. 架构总览

### 2.1 单实例 vs 百万用户版本对照

```
百万用户版本（design-final.md）              单实例版本（云端 API）
─────────────────────────────            ─────────────────────────
APISIX API Gateway                  →    Nginx（已有）
Kubernetes                          →    systemd（已有）
12 个微服务（gRPC 通信）              →    单 FastAPI 进程（模块化）
Kafka 异步流水线                     →    Celery + Redis
PostgreSQL + Citus（32 shard）        →    单 PostgreSQL 实例
Milvus Cluster（HNSW）               →    Milvus Lite（IVF_FLAT）
Elasticsearch（BM25）                 →    PostgreSQL FTS（tsvector）
Redis Cluster                        →    单 Redis
MinIO / S3                           →    本地文件系统
GPU Embedding Service                →    Embedding API（OpenAI 兼容）
GPU Rerank Service                   →    Rerank API（Jina / 自建）
GPU LLM（Qwen2-72B）                 →    LLM API（glm-5.1 / DeepSeek）
GPU OCR（PaddleOCR Server）          →    OCR API（腾讯云 / 百度）
HashiCorp Vault                      →    .env 文件
Grafana + Loki + Jaeger              →    Prometheus + 日志文件
```

### 2.2 单实例架构图

```
                    ┌──────────────┐
                    │    Nginx     │ SSL 终结 + 限流 + 静态资源
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │  FastAPI     │ 单进程，~200MB 内存
                    │  (uvicorn)   │ Auth / Document / Chat / Collection
                    └──┬───────┬───┘
                       │       │
          ┌────────────┘       └────────────┐
          │                                  │
   ┌──────▼──────┐                   ┌──────▼──────┐
   │ PostgreSQL  │                   │  Milvus Lite │
   │ users/docs/ │                   │  向量存储     │
   │ chunks/fts  │                   │  (嵌入式)     │
   │ convs/tags  │                   └─────────────┘
   └─────────────┘
          │
   ┌──────▼──────┐
   │   Redis     │ 缓存 + Celery broker
   └─────────────┘

   ┌──────▼──────┐
   │  本地文件系统 │ /data/knspace/files/
   └─────────────┘

  ┌──────────────────────────────────────────────────┐
  │              云端 API（零本地内存）                  │
  │                                                    │
  │  Embedding API  ──→  向量化（bge-m3 兼容）          │
  │  Rerank API     ──→  重排序                        │
  │  OCR API        ──→  图片/扫描件文字提取            │
  │  LLM API        ──→  问答生成（SSE 流式）           │
  └──────────────────────────────────────────────────┘
```

### 2.3 为什么全走 API 是最优选择

| 维度 | 本地 CPU 推理 | 云端 API |
|------|-------------|---------|
| **内存** | 占 2.5-5.2 GB（无法在 4G 运行） | 0 MB |
| **延迟** | embedding ~30s，reranker ~15s（CPU 慢） | embedding ~200ms，reranker ~300ms |
| **成本** | 0（但需要升级到 8G+ 内存） | ¥10-50/月（当前规模） |
| **运维** | 需管理模型加载/卸载/崩溃 | API 稳定性由供应商保证 |
| **迁移** | 需改代码切到 API | 已是 API，百万版无缝衔接 |
| **磁盘** | 模型文件 ~8 GB | 0 GB |

**成本测算（1,000 用户 / 50 DAU）：**

| API | 月调用量 | 单价 | 月成本 |
|-----|---------|------|--------|
| Embedding（文档向量化） | ~50 万次 × 512 token = 2.5 亿 token | ¥0.7/百万 token | ~¥0.2 |
| Embedding（查询） | ~1.5 万次 | 可忽略 | ~¥0 |
| Reranker | ~1.5 万次 × 40 pairs | ¥0.5/千次 | ~¥7.5 |
| OCR | ~500 页 | ¥0.01/次 | ~¥5 |
| LLM | ~1.5 万次 × 2000 token = 3000 万 token | ¥1/百万 token | ~¥30 |
| **合计** | | | **~¥43/月** |

升级到 8G 内存的服务器月费约 ¥100，全走 API 月费 ~¥43，**更便宜且更快**。

---

## 3. 迁移友好设计：接口抽象层

> **核心原则：业务代码只依赖抽象接口，不依赖具体实现。**
> 迁移时只需替换实现类，业务逻辑零改动。

### 3.1 关键抽象接口

```python
# ===== 存储抽象 =====

class VectorStoreBase(Protocol):
    """向量存储接口 — 单实例用 Milvus Lite，百万版用 Milvus Cluster"""
    async def upsert(self, vectors: list[VectorRecord]): ...
    async def search(self, query_vector: list[float], user_id: str,
                     top_k: int, filters: dict | None = None) -> list[SearchResult]: ...
    async def delete_by_document(self, doc_id: str): ...

class FullTextSearchBase(Protocol):
    """全文检索接口 — 单实例用 PG FTS，百万版用 Elasticsearch"""
    async def search(self, query: str, user_id: str,
                     top_k: int) -> list[SearchResult]: ...
    async def index_chunk(self, chunk_id: str, content: str, user_id: str): ...
    async def delete_chunk(self, chunk_id: str): ...

class ObjectStorageBase(Protocol):
    """文件存储接口 — 单实例用本地 FS，百万版用 MinIO/S3"""
    async def save(self, key: str, data: bytes) -> str: ...
    async def load(self, key: str) -> bytes: ...
    async def delete(self, key: str): ...
    async def get_url(self, key: str) -> str: ...

class MessageQueueBase(Protocol):
    """消息队列接口 — 单实例用 Celery+Redis，百万版用 Kafka"""
    async def publish(self, topic: str, message: dict): ...
    async def consume(self, topic: str, handler: Callable): ...

# ===== AI 服务抽象 =====

class EmbeddingServiceBase(Protocol):
    """
    嵌入服务接口
    单实例：调云端 API（OpenAI 兼容）
    百万版：调自建 GPU 服务（vLLM/Triton）
    接口签名完全一致，切换零改动
    """
    async def encode(self, texts: list[str]) -> list[list[float]]: ...

class RerankServiceBase(Protocol):
    """
    重排序接口
    单实例：调 Jina/Cohere Rerank API
    百万版：调自建 GPU 服务
    """
    async def rerank(self, query: str, documents: list[str],
                     top_n: int) -> list[RerankResult]: ...

class OcrServiceBase(Protocol):
    """
    OCR 接口
    单实例：调腾讯云/百度 OCR API
    百万版：调自建 PaddleOCR GPU 服务
    """
    async def recognize(self, image: bytes) -> str: ...

class LlmServiceBase(Protocol):
    """
    LLM 接口
    单实例和百万版都是 API 调用，只是模型/供应商不同
    """
    async def stream_generate(self, query: str, context: str,
                               history: list | None = None) -> AsyncIterator[str]: ...
```

### 3.2 实现切换方式

```python
# app/services/factory.py — 通过配置切换实现

def get_embedding_service() -> EmbeddingServiceBase:
    match settings.embedding_backend:
        case "api":
            from app.services.embedding_api import ApiEmbeddingService
            return ApiEmbeddingService(
                url=settings.embedding_api_url,
                api_key=settings.embedding_api_key,
                model=settings.embedding_model,
            )
        case "local":
            from app.services.embedding_local import LocalEmbeddingService
            return LocalEmbeddingService()
        case "gpu_service":
            from app.services.embedding_gpu import GpuEmbeddingService
            return GpuEmbeddingService(endpoint=settings.gpu_embedding_url)

def get_rerank_service() -> RerankServiceBase:
    match settings.rerank_backend:
        case "api":
            from app.services.rerank_api import ApiRerankService
            return ApiRerankService(
                url=settings.rerank_api_url,
                api_key=settings.rerank_api_key,
            )
        case "local":
            from app.services.rerank_local import LocalRerankService
            return LocalRerankService()

def get_ocr_service() -> OcrServiceBase:
    match settings.ocr_backend:
        case "tencent":
            from app.services.ocr_tencent import TencentOcrService
            return TencentOcrService()
        case "baidu":
            from app.services.ocr_baidu import BaiduOcrService
            return BaiduOcrService()
        case "local":
            from app.services.ocr import LocalOcrService
            return LocalOcrService()
```

### 3.3 迁移路径对照表

| 组件 | 单实例实现 | 百万版实现 | 迁移方式 | 代码改动 |
|------|-----------|-----------|---------|---------|
| 向量存储 | Milvus Lite（嵌入式） | Milvus Cluster | 改配置 | 新增 Cluster 实现类 |
| 全文检索 | PostgreSQL tsvector | Elasticsearch 8 | 改配置 | 新增 ES 实现类 |
| 文件存储 | 本地文件系统 | MinIO/S3 | 改配置 | 新增 S3 实现类 |
| 消息队列 | Celery + Redis | Kafka | 改配置 | 新增 Kafka 实现类 |
| **嵌入服务** | **API 调用（bge-m3 兼容）** | **GPU 自建服务** | **改 URL** | **零改动** |
| **重排序** | **API 调用（Jina/Cohere）** | **GPU 自建服务** | **改 URL** | **零改动** |
| **OCR** | **API 调用（腾讯云/百度）** | **GPU 自建 PaddleOCR** | **改 URL** | **零改动** |
| LLM | API（glm-5.1） | API（DeepSeek V3） | 改 model 名 | 零改动 |
| 数据库 | 单 PostgreSQL | 分片路由 + 多实例 | 加路由层 | 路由中间件 |
| 缓存 | 单 Redis | Redis Cluster | 改连接配置 | 驱动自动支持 |
| 部署 | systemd | Kubernetes | Dockerfile | 新增 K8s manifests |

**关键优势：AI 服务接口在单实例和百万版之间只差一个 URL，代码完全相同。**

---

## 4. 云端 API 选型

### 4.1 Embedding API

| 方案 | 兼容性 | 延迟 | 成本 | 推荐 |
|------|--------|------|------|------|
| **自建代理（59.110.212.14:4000）** | OpenAI 兼容 | ~200ms | 已有 | **首选** |
| 智谱（Zhipu） Embedding-3 | OpenAI 兼容 | ~300ms | ¥0.5/百万 token | 备选 |
| 阿里通义 text-embedding-v3 | OpenAI 兼容 | ~200ms | ¥0.7/百万 token | 备选 |
| SiliconFlow bge-m3 | OpenAI 兼容 | ~150ms | 免费额度 | 免费备选 |

**实现：** 复用现有 `embedding.py` 的 API fallback 逻辑，把 API 设为首选。

```python
# app/services/embedding_api.py

class ApiEmbeddingService:
    """OpenAI 兼容 Embedding API 调用"""

    def __init__(self, url: str, api_key: str, model: str):
        self.url = url
        self.api_key = api_key
        self.model = model

    async def encode(self, texts: list[str]) -> list[list[float]]:
        import httpx
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{self.url}/v1/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "input": texts, "encoding_format": "float"},
            )
            resp.raise_for_status()
            data = resp.json()
        return [e["embedding"] for e in sorted(data["data"], key=lambda x: x["index"])]
```

### 4.2 Rerank API

| 方案 | 延迟 | 成本 | 推荐 |
|------|------|------|------|
| **Jina Reranker v2** | ~300ms | 免费额度/¥0.02/次 | **首选** |
| Cohere Rerank v3 | ~400ms | 1000 次/月免费 | 备选 |
| SiliconFlow bge-reranker | ~200ms | 免费额度 | 免费备选 |
| 自建（独立 GPU 实例） | ~50ms | ¥500/月起 | 规模化时 |

**实现：**

```python
# app/services/rerank_api.py

class ApiRerankService:
    """Jina/Cohere Rerank API 调用"""

    async def rerank(self, query: str, documents: list[str],
                     top_n: int = 10) -> list[RerankResult]:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{self.url}/rerank",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"model": self.model, "query": query,
                      "documents": documents, "top_n": top_n},
            )
            resp.raise_for_status()
            data = resp.json()
        return [
            RerankResult(index=r["index"], score=r["relevance_score"],
                         text=documents[r["index"]])
            for r in data["results"]
        ]
```

### 4.3 OCR API

| 方案 | 延迟 | 成本 | 推荐 |
|------|------|------|------|
| **腾讯云 OCR** | ~500ms | 1000 次/月免费，之后 ¥0.01/次 | **首选**（已在腾讯云） |
| 百度云 OCR | ~600ms | 50,000 次/月免费 | 备选 |
| 阿里云 OCR | ~500ms | 免费额度 | 备选 |

**实现：**

```python
# app/services/ocr_api.py

class TencentOcrService:
    """腾讯云 OCR API（通用印刷体识别）"""

    async def recognize(self, image: bytes) -> str:
        import base64
        from tencentcloud.common import credential
        from tencentcloud.ocr.v20181119 import ocr_client, models

        cred = credential.Credential(settings.tencent_secret_id, settings.tencent_secret_key)
        client = ocr_client.OcrClient(cred, "ap-guangzhou")

        req = models.GeneralBasicOCRRequest()
        req.ImageBase64 = base64.b64encode(image).decode()

        resp = client.GeneralBasicOCR(req)
        return "\n".join(item.DetectedText for item in resp.TextDetections)
```

### 4.4 LLM API（已实现，不变）

| 方案 | 延迟 | 成本 | 当前状态 |
|------|------|------|---------|
| glm-5.1-openai | ~2s 首 token | ¥1/百万 token | **已接入** |
| DeepSeek V3 | ~1.5s 首 token | ¥1/百万 token | 已配置 |
| MiniMax M2.7 | ~2s 首 token | ¥0.5/百万 token | 已配置 |

---

## 5. 内存预算（全 API 版）

### 5.1 常驻内存分布

| 组件 | 常驻内存 | 说明 |
|------|----------|------|
| OS + 系统服务 | 500 MB | 含腾讯云监控 |
| PostgreSQL 16 | 300 MB | shared_buffers=128MB，work_mem=16MB |
| Redis | 50 MB | 缓存 + Celery broker + embedding 缓存 |
| Nginx | 20 MB | 反向代理 |
| FastAPI（含全部业务代码） | 200 MB | 无模型加载 |
| Milvus Lite | 200 MB | 嵌入式向量引擎 |
| Celery worker | 100 MB | 异步任务（文档处理） |
| **常驻合计** | **~1.4 GB** | |
| **剩余可用** | **~2.2 GB** | **用于连接池、请求缓冲、Milvus 数据增长** |

### 5.2 与本地模型版对比

| 指标 | 本地模型版 | 云端 API 版 |
|------|-----------|------------|
| 常驻内存 | ~1.6 GB（无模型） | ~1.4 GB |
| 峰值内存 | ~4.1 GB（embedding 加载） | ~2.0 GB |
| 内存余量 | 负值（依赖 swap） | **+1.6 GB 富余** |
| 冷启动延迟 | ~30s（模型加载） | **~0s** |
| 推理延迟 | embedding ~30s（CPU） | **~200ms** |
| 磁盘占用 | 模型 ~8 GB | **0 GB** |
| 月增成本 | ¥0（但需升级 8G+） | **~¥43** |

### 5.3 富余内存可做的事

有了 2.2 GB 的内存余量，以下变得可行：

| 用途 | 内存占用 | 说明 |
|------|----------|------|
| **Elasticsearch（轻量）** | ~1 GB | 终于可以跑了！ |
| **Prometheus + Grafana** | ~500 MB | 完整监控栈 |
| Milvus 更多数据 | 按量增长 | 500 万向量 ~500MB |
| PostgreSQL 更大缓存 | +200MB | 更多连接和排序内存 |
| Playwright Chromium | ~600 MB | 动态网页渲染 |

---

## 6. 数据模型

> **表结构与百万用户版本完全一致**，迁移时只改路由层，不改表结构。

### 6.1 表结构（与 design-final.md §7 一致）

```sql
-- 用户表（全局表，不分片）
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

-- 收藏夹
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

-- 文档
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
    content_hash      VARCHAR(64),
    processing_status VARCHAR(20) DEFAULT 'pending',
    processing_error  TEXT,
    chunk_count       INT DEFAULT 0,
    metadata          JSONB DEFAULT '{}',
    is_deleted        BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMPTZ DEFAULT now(),
    updated_at        TIMESTAMPTZ DEFAULT now()
);

-- 分块（含 FTS）
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
    fts             TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_chunks_fts ON chunks USING GIN(fts);
CREATE INDEX idx_chunks_document ON chunks(document_id);
CREATE INDEX idx_chunks_user ON chunks(user_id);

-- 会话
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

-- 消息
CREATE TABLE messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id),
    role            VARCHAR(20) NOT NULL,
    content         TEXT NOT NULL,
    citations       JSONB,
    model_name      VARCHAR(50),
    feedback        VARCHAR(20),
    token_usage     JSONB,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- 标签
CREATE TABLE tags (
    id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id  UUID NOT NULL REFERENCES users(id),
    name     VARCHAR(50) NOT NULL,
    color    VARCHAR(7),
    UNIQUE(user_id, name)
);

-- 文档标签关联
CREATE TABLE document_tags (
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    tag_id      UUID REFERENCES tags(id) ON DELETE CASCADE,
    PRIMARY KEY (document_id, tag_id)
);
```

---

## 7. 核心流程

### 7.1 文档上传处理

```
用户上传文件
     │
     ├── 1. 保存到本地 /data/knspace/files/{user_id}/
     ├── 2. 创建 Document（status=pending）
     ├── 3. content_hash 跨用户去重
     ├── 4. Celery 异步：process_document
     │     ├── 解析（PyMuPDF / python-docx / OCR API）
     │     ├── 结构化分块（512 token, overlap=64）
     │     ├── Embedding API 批量向量化 → Milvus 写入
     │     ├── 写入 chunks 表（含 tsvector）
     │     └── 更新 status=ready
     └── 5. 前端 SSE 轮询 /documents/{id}/status/stream
```

**延迟估算（10 页 PDF，~100 chunks）：**

| 步骤 | 本地模型 | 云端 API |
|------|---------|---------|
| 解析 | ~3s | ~3s（相同） |
| 分块 | ~0.5s | ~0.5s（相同） |
| 向量化（100 chunks） | ~60s（CPU） | **~5s（API batch）** |
| Milvus 写入 | ~2s | ~2s（相同） |
| **总计** | **~65s** | **~11s** |

### 7.2 问答流程

```
用户提问
     │
     ├── 1. 多轮指代消解（规则 + LLM fallback）     ~50ms
     ├── 2. QueryAnalyzer 分类 + 改写               ~5ms
     ├── 3. Embedding API（查询向量化）              ~200ms
     ├── 4. 混合检索
     │     ├── Milvus 向量检索 top_k=40              ~50ms
     │     ├── PostgreSQL FTS top_k=40               ~30ms
     │     └── RRF 融合                              ~5ms
     ├── 5. Rerank API 重排序 top_n=10               ~300ms
     ├── 6. 上下文组装                               ~10ms
     ├── 7. LLM API 流式生成                         ~2s 首 token
     └── 8. SSE 流式返回 + 保存消息
```

**端到端延迟：**

| 阶段 | 本地模型（冷启动） | 本地模型（热） | 云端 API |
|------|------------------|--------------|---------|
| 首次请求 | ~35s | ~25s | **~3s** |
| 后续请求 | ~25s | ~10s | **~3s** |

### 7.3 无模型调度开销

全 API 版不存在模型加载/卸载：
- 无 ModelManager，无 LRU 策略
- 无内存竞争，无 swap 抖动
- 请求之间完全无状态
- 并发请求不会因内存不足被阻塞

---

## 8. 安全设计

> 与百万版保持一致的安全模型。

### 8.1 认证与授权

| 措施 | 单实例实现 | 百万版实现 |
|------|-----------|-----------|
| 密码存储 | bcrypt（cost=12） | 同 |
| Token | JWT（access 30min + refresh 7d） | 同 + Redis 黑名单 |
| 多租户隔离 | 应用层 `WHERE user_id = ?` | PostgreSQL RLS |
| HTTPS | Nginx SSL（Let's Encrypt） | APISIX SSL |
| 限流 | Nginx rate_limit（10r/s API） | APISIX 插件 |
| SQL 注入 | SQLAlchemy ORM 参数化 | 同 |
| XSS | 输入校验 + Pydantic | 同 |

### 8.2 API Key 安全

| 措施 | 说明 |
|------|------|
| .env 存储 | API key 不入代码仓库 |
| 环境变量注入 | 容器化时通过 K8s Secret / Vault 注入 |
| 最小权限 | OCR API 只开通文字识别权限 |
| 轮换策略 | 定期更换 API key |

---

## 9. 可观测性

### 9.1 监控栈

| 组件 | 实现 | 内存占用 | 百万版实现 |
|------|------|---------|-----------|
| 指标 | Prometheus（进程内） | ~20 MB | Prometheus Operator |
| 日志 | 文件日志（JSON 格式） | 0 | Loki |
| 追踪 | 无（单进程无需） | 0 | OpenTelemetry + Jaeger |
| 告警 | 脚本 + 邮件 | 0 | AlertManager |
| 仪表盘 | **现在可以跑 Grafana！** | ~300 MB | Grafana Operator |

### 9.2 自定义 RAG 指标（与百万版一致）

```python
rag_retrieval_duration = Histogram("rag_retrieval_duration_seconds", "检索耗时")
rag_rerank_duration = Histogram("rag_rerank_duration_seconds", "重排序耗时")
rag_llm_duration = Histogram("rag_llm_duration_seconds", "LLM 生成耗时")
embedding_api_duration = Histogram("embedding_api_duration_seconds", "Embedding API 耗时")
rerank_api_duration = Histogram("rerank_api_duration_seconds", "Rerank API 耗时")
ocr_api_duration = Histogram("ocr_api_duration_seconds", "OCR API 耗时")
api_error_total = Counter("api_error_total", "API 调用失败", labels=["service"])
```

---

## 10. 4C/4G 资源评估（全 API 版）

### 10.1 可以实现的（全部核心功能）

| 功能 | 资源消耗 | 备注 |
|------|----------|------|
| Auth 注册/登录/JWT | CPU 轻微 | bcrypt 低并发，4C 足够 |
| Collection/Tag CRUD | PG IO | 简单 CRUD |
| Conversation 管理 | PG IO | 低频 |
| 文档上传 + 去重 | CPU + 磁盘 | 跨用户去重节省开销 |
| Document delete 全链路清理 | CPU + IO | 向量 + chunks + 文件 |
| 结构化分块 | CPU | 纯计算，秒级 |
| **Embedding（API）** | **零本地内存** | API ~200ms |
| **Reranker（API）** | **零本地内存** | API ~300ms |
| **OCR（API）** | **零本地内存** | API ~1s/页 |
| **LLM 生成（API）** | **零本地内存** | API SSE 流式 |
| Milvus Lite 向量检索 | CPU + 200MB | 500 万向量以内性能 OK |
| PostgreSQL FTS 全文检索 | CPU + IO | 10 万 chunks < 100ms |
| RRF 混合融合 | CPU | 毫秒级 |
| SSE 流式响应 | 网络 | 4Mbps 足够 |
| QueryAnalyzer | CPU | 纯规则，< 1ms |
| 多轮对话 | CPU + API | 规则 + LLM fallback |
| RAG 评估 | CPU | 低频 |
| Nginx 限流 | 20MB | 已部署 |
| Prometheus 指标 | 20MB | 进程内 |
| 网页导入（httpx） | CPU + 网络 | 轻量抓取 |

### 10.2 现在额外可以实现的（之前因内存不够做不了）

| 功能 | 额外内存 | 说明 |
|------|---------|------|
| **Elasticsearch（轻量单节点）** | ~1 GB | BM25 中文分词，检索质量大幅提升 |
| **Grafana 仪表盘** | ~300 MB | 可视化 RAG 指标 |
| **Playwright（动态网页渲染）** | ~600 MB | 处理 JS 渲染页面 |
| **Milvus 更多数据** | 按量 | 可支撑到 500 万+ 向量 |
| **PostgreSQL 更大缓存** | +200 MB | 更多 work_mem 加速排序 |
| **并发文档处理** | Celery worker ~100MB | 不再担心模型 OOM |

### 10.3 仍然无法实现的（需要架构升级）

| 功能 | 原因 | 解决方案 |
|------|------|---------|
| Milvus Cluster | 需要 3+ 节点 | 单机先不碰，500 万向量内 Lite 够用 |
| Kafka | 需要 1GB+ 内存 + 大量磁盘 | Celery + Redis 在当前规模完全够 |
| PostgreSQL 分片 | 需要多实例 | 单实例先不碰 |
| MinIO/S3 | 不需要（磁盘 40GB 够用） | 迁移时加 |
| K8s | 单机不需要 | 迁移时加 |
| GPU 自建推理 | 无 GPU | 规模化后再考虑 |
| 高并发（>20 QPS） | 4C CPU | 水平扩展 |
| 大数据量（>500 万向量） | Milvus Lite 限制 | Milvus Standalone 或 Cluster |

### 10.4 资源升级路线图

| 配置 | 可支撑规模 | 月成本（腾讯云） | 新增能力 |
|------|-----------|-----------------|---------|
| **4C/4G**（当前，全 API） | **1,000 用户 / 10,000 文档** | **~¥143** | **全部核心功能** |
| 4C/8G | 5,000 用户 / 50,000 文档 | ~¥300 | + ES + Grafana + Playwright |
| 8C/16G | 20,000 用户 / 200,000 文档 | ~¥600 | + Milvus Standalone + 更高并发 |
| 集群 | 100,000+ DAU | ~¥10,000+/月 | 完整百万用户架构 |

---

## 11. 部署方案

### 11.1 环境变量（.env）

```bash
# Database
DATABASE_URL=postgresql+asyncpg://knspace:knspace123@localhost/knspace

# Redis
REDIS_URL=redis://localhost:6379/0

# Embedding API（首选 API，不再本地加载）
EMBEDDING_BACKEND=api
EMBEDDING_API_URL=http://59.110.212.14:4000/v1
EMBEDDING_API_KEY=sk-xxx
EMBEDDING_MODEL=BAAI/bge-m3

# Rerank API
RERANK_BACKEND=api
RERANK_API_URL=https://api.jina.ai/v1/rerank
RERANK_API_KEY=jina_xxx
RERANK_MODEL=jina-reranker-v2-base-multilingual

# OCR API
OCR_BACKEND=tencent
TENCENT_SECRET_ID=xxx
TENCENT_SECRET_KEY=xxx

# LLM API
LLM_API_URL=http://1239mxgn96959.vicp.fun:4009/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=glm-5.1-openai

# File Storage
FILE_STORAGE_PATH=/data/knspace/files

# Vector Store
VECTOR_STORE_URI=./milvus_data.db
```

### 11.2 systemd 服务

```ini
# /etc/systemd/system/knspace.service
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

## 12. 迁移检查清单

> 从单实例迁移到百万用户版本时，逐项确认。

### 12.1 代码层

- [ ] 所有业务代码通过 `factory.py` 获取服务实例
- [ ] EmbeddingServiceBase / RerankServiceBase / OcrServiceBase 接口完整
- [ ] 所有 SQL 查询包含 `user_id` 过滤（为 RLS 做准备）
- [ ] 无硬编码的文件路径、数据库连接、模型名称
- [ ] 配置项全部在 `config.py` 中集中管理

### 12.2 数据层

- [ ] 表结构与百万版一致（字段名、类型、约束）
- [ ] UUID 主键（分布式友好）
- [ ] JSONB 灵活元数据
- [ ] tsvector 列已创建（迁移 ES 时作为对照）

### 12.3 AI 服务层

- [ ] Embedding API 的向量维度与本地 bge-m3 一致（1024 维）
- [ ] Rerank API 返回格式与本地 CrossEncoder 一致
- [ ] OCR API 返回纯文本，格式与 PaddleOCR 一致
- [ ] LLM API SSE 流格式与现有实现一致
- [ ] 所有 API 调用有超时 + 重试 + fallback

### 12.4 基础设施层

- [ ] Dockerfile 已编写
- [ ] 环境变量通过 .env 管理
- [ ] 日志输出 JSON 格式（Loki 友好）
- [ ] Prometheus 指标已埋点

---

## 附录 A：与 design-final.md 的接口映射

| design-final.md 接口 | 单实例实现 | 百万版替换 |
|---------------------|-----------|-----------|
| `VectorStoreBase` | `vector_store.py`（Milvus Lite） | `vector_store_cluster.py` |
| `FullTextSearchBase` | `search.py`（PG FTS） | `fts_es.py` |
| `ObjectStorageBase` | `documents.py`（本地 FS） | `storage_s3.py` |
| `MessageQueueBase` | `tasks/`（Celery） | `kafka.py` |
| `EmbeddingServiceBase` | `embedding_api.py`（HTTP） | **同一文件，改 URL** |
| `RerankServiceBase` | `rerank_api.py`（HTTP） | **同一文件，改 URL** |
| `OcrServiceBase` | `ocr_api.py`（HTTP） | **同一文件，改 URL** |
| `LlmServiceBase` | `llm.py`（HTTP SSE） | **同一文件，改 URL** |
| `ShardRouter` | 不需要 | `shard_router.py` |

## 附录 B：API 延迟基准测试（待测）

| API | 样本数 | P50 | P95 | P99 | 目标 |
|-----|--------|-----|-----|-----|------|
| Embedding（单条） | 100 | — | — | — | < 500ms |
| Embedding（batch=64） | 10 | — | — | — | < 3s |
| Rerank（40 pairs） | 100 | — | — | — | < 1s |
| OCR（单页） | 50 | — | — | — | < 3s |
| LLM（首 token） | 100 | — | — | — | < 3s |
