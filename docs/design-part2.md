# 第二部分：核心模块详细设计

---

## 4. 多模态内容处理模块

### 4.1 处理架构总览

文档处理采用 **事件驱动的流水线架构**，通过 Kafka 串联各处理阶段：

```
用户上传 ──→ API Svc ──→ MinIO(存文件) ──→ Kafka[doc.uploaded]
                                                │
                    ┌───────────────────────────┘
                    ▼
            Document Worker ──→ 格式检测 ──→ 路由到解析器 ──→ Kafka[doc.parsed]
                                                                │
                    ┌───────────────────────────────────────────┘
                    ▼
            Chunk Worker ──→ 结构化分块 ──→ Kafka[doc.chunked]
                                                │
                    ┌───────────────────────────┘
                    ▼
            Embed Worker ──→ 批量向量化 ──→ Kafka[doc.embedded]
                                                │
                    ┌───────────────────────────┘
                    ▼
            Index Worker ──→ 写入 Milvus + PG ──→ Kafka[doc.completed]
                                                      │
                    ┌─────────────────────────────────┘
                    ▼
            通知用户（WebSocket / 轮询）
```

**【设计理由】为什么用 Kafka 事件驱动而非同步链式调用：**

1. **故障隔离**：解析阶段崩溃不影响向量化阶段，各阶段独立重试
2. **弹性伸缩**：每个 Worker 独立扩缩容。上传高峰时扩展解析 Worker，不影响 Embedding Worker
3. **回溯重放**：如果发现 Embedding 模型升级了，可以重新消费 `doc.chunked` topic，重新向量化全部数据，不影响线上服务
4. **背压控制**：Embedding GPU 资源有限时，Kafka 作为缓冲区自然形成背压，不会压垮 GPU 服务

### 4.2 文档处理流水线

#### Stage 1：格式检测与路由

```python
# 设计模式：策略模式 + 注册表
class ParserRegistry:
    """解析器注册表，按 MIME 类型路由"""
    _parsers: dict[str, BaseParser] = {}

    @classmethod
    def register(cls, mime_types: list[str]):
        def decorator(parser_cls):
            for mt in mime_types:
                cls._parsers[mt] = parser_cls()
            return parser_cls
        return decorator

    @classmethod
    def get_parser(cls, mime_type: str) -> BaseParser:
        parser = cls._parsers.get(mime_type)
        if not parser:
            raise UnsupportedFormatError(mime_type)
        return parser

@ParserRegistry.register([
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/markdown",
    "text/html",
])
class DocumentParser(BaseParser): ...
```

#### Stage 2：结构化解析

| 文件类型 | 解析器 | 结构保留策略 |
|----------|--------|-------------|
| PDF（文本型） | PyMuPDF (fitz) | 按页提取，保留标题层级（字号推断） |
| PDF（扫描型） | PyMuPDF 检测 → PaddleOCR | 识别为扫描型后走 OCR 路径，提取文字+表格 |
| Word (.docx) | python-docx | 保留 heading level、段落、表格、图片引用 |
| Excel (.xlsx) | openpyxl | 每个 sheet 每个表格单独提取，保留行列结构 |
| PPT (.pptx) | python-pptx | 按幻灯片提取，文本+备注+图表 |
| Markdown | 自定义 AST 解析器 | 保留 heading tree、代码块、列表、表格 |
| 网页 HTML | Trafilatura + Playwright | 提取正文，过滤导航/广告/评论 |

**解析输出统一格式：**

```python
@dataclass
class ParsedDocument:
    doc_id: str
    title: str
    language: str  # 自动检测
    structure_tree: DocumentNode  # 文档结构树
    raw_text: str  # 纯文本
    tables: list[Table]  # 提取的表格
    images: list[ImageRef]  # 图片引用（指向 MinIO 路径）
    metadata: DocumentMetadata  # 作者、日期、页数等

@dataclass
class DocumentNode:
    node_type: str  # "heading" | "paragraph" | "list" | "table" | "code" | "image"
    level: int  # heading level (1-6)
    content: str
    children: list[DocumentNode]
    page_number: int
    char_offset: int  # 在原文中的字符偏移
```

#### Stage 3：智能分块（核心）

**基础策略：结构感知的递归分块**

```
输入: ParsedDocument（带结构树）
     │
     ▼
Step 1: 按结构树拆分
  ├── H1 级别作为一个大段
  ├── H2 级别作为段落组
  ├── 表格作为一个整体
  └── 图片描述 + 上下文作为一个块
     │
     ▼
Step 2: 对超长段落递归切分
  ├── 目标大小: 512 tokens
  ├── 重叠: 64 tokens（~12.5%）
  ├── 切分边界优先级: 句号 > 换行 > 分号 > 逗号
  └── 硬上限: 不超过 768 tokens（bge-m3 最佳输入长度）
     │
     ▼
Step 3: 生成父子关系
  ├── 父块: H2 级别的完整段落
  ├── 子块: 512 token 的分片
  └── 检索命中子块 → 返回时自动附加父块上下文
```

**分块参数设计：**

| 参数 | 默认值 | 可配置范围 | 说明 |
|------|--------|-----------|------|
| chunk_size | 512 tokens | 256-1024 | bge-m3 最优输入 512，超过 8192 会被截断 |
| chunk_overlap | 64 tokens | 0-128 | 12.5% 重叠率，避免跨块语义断裂 |
| max_chunk_size | 768 tokens | 固定 | 硬上限，超过强制切分 |
| min_chunk_size | 50 tokens | 32-128 | 过短的块合并到相邻块 |

**【设计理由】为什么 chunk_size = 512 tokens：**
- bge-m3 的训练数据以 512 token 为主，超过 512 后 embedding 质量开始下降（信息被压缩）
- 512 token 约等于 300-400 个中文字符，是一个段落的自然长度，语义完整性好
- 更小的 chunk（256）提高检索精度但丢失上下文；更大的 chunk（1024）保留上下文但降低检索精度和 embedding 质量
- 512 是召回率和精确率的甜蜜点，多个 RAG benchmark 验证过

**【设计理由】为什么不用语义分块作为默认策略：**
1. 语义分块需要额外的 embedding 调用来计算相邻句子的相似度，成本增加 3-5x
2. 语义分块的结果不稳定——换一个 embedding 模型，分块结果完全不同
3. 在多个 RAG benchmark 上，结构感知递归分块的效果与语义分块差距 < 5%，但成本和复杂度低一个数量级
4. 可以作为 Phase 2 的可选增强，在特定文档类型上（学术论文）开启

#### Stage 4：向量化

**批量嵌入流程：**

```python
class EmbeddingWorker:
    def __init__(self):
        self.batch_buffer: list[Chunk] = []
        self.batch_size = 64  # A10 GPU 最优 batch
        self.flush_interval = 0.5  # 500ms 强制刷新

    async def process_chunk(self, chunk: Chunk):
        # 1. 检查缓存（内容 hash）
        cache_key = f"emb:{hashlib.md5(chunk.content.encode()).hexdigest()}"
        cached = await redis.get(cache_key)
        if cached:
            chunk.embedding = pickle.loads(cached)
            return

        # 2. 加入批次缓冲区
        self.batch_buffer.append(chunk)

        # 3. 达到批次大小或超时则刷新
        if len(self.batch_buffer) >= self.batch_size:
            await self.flush_batch()

    async def flush_batch(self):
        if not self.batch_buffer:
            return

        # 批量推理
        texts = [c.content for c in self.batch_buffer]
        embeddings = await embedding_model.encode(texts, batch_size=64)

        # 写入缓存（TTL 7天）
        pipe = redis.pipeline()
        for chunk, emb in zip(self.batch_buffer, embeddings):
            chunk.embedding = emb
            pipe.set(f"emb:{chunk.content_hash}", pickle.dumps(emb), ex=7*86400)
        await pipe.execute()

        # 发送到下一阶段
        await kafka_produce("doc.embedded", self.batch_buffer)
        self.batch_buffer.clear()
```

**【设计理由】批量 + 缓存策略：**
- GPU 推理的 batch 效率：batch=64 比 batch=1 快 ~30x（GPU 并行度充分利用）
- 嵌入缓存命中率预估 15-20%（用户重复收藏、同一文档被多人上传）。10 亿分块 × 15% 命中率 = 节省 1.5 亿次 embedding 调用
- 500ms 刷新间隔保证低流量时不会无限等待，同时不频繁触发小批量推理

### 4.3 图片处理流水线

```
图片上传 ──→ 存储 MinIO
    │
    ├──→ PaddleOCR → 提取文字 → bge-m3 嵌入 → Milvus (modality="image_text")
    │
    ├──→ CLIP → 视觉语义向量 → Milvus (modality="image_visual", dim=768)
    │
    └──→ Qwen-VL → 图片描述文本 → bge-m3 嵌入 → Milvus (modality="image_desc")
```

**双向量 + 描述向量三重索引：**
- `image_text`：图片中的文字内容（OCR 提取），支持"图片里写了什么"
- `image_visual`：图片的视觉语义（CLIP），支持"以图搜图"、"找类似图片"
- `image_desc`：图片的语义描述（Qwen-VL 生成），支持"展示 XX 的图片"

**【设计理由】为什么需要三种向量：**
单一向量无法覆盖所有检索场景。用户可能搜"合同签署页的截图"（需要 visual）、"带公司 logo 的图片"（需要 visual + OCR）、"包含风险条款的图片"（需要 OCR + desc）。三种向量通过 document_id 关联，检索时按 modality 分别搜索后融合。

### 4.4 网页处理流水线

```python
class WebProcessor:
    async def process(self, url: str, user_id: str) -> ParsedDocument:
        # 1. 智能抓取
        html = await self.fetch_with_fallback(url)

        # 2. 内容提取
        article = trafilatura.extract(html, include_tables=True, favor_precision=True)

        # 3. 元数据提取
        metadata = trafilatura.extract(html, output_format="json")

        # 4. 转为统一的 ParsedDocument
        return self.to_parsed_document(article, metadata, url)

    async def fetch_with_fallback(self, url: str) -> str:
        # 优先用 Trafilatura（快，纯 HTTP）
        try:
            result = trafilatura.fetch_url(url)
            if self.is_content_rich(result):
                return result
        except Exception:
            pass

        # Fallback: Playwright（处理动态 JS 页面）
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            return await page.content()

    def is_content_rich(self, html: str) -> bool:
        """简单启发式：正文长度 > 500 字符且包含段落标签"""
        ...
```

**增量更新策略：**
- 每条网页收藏记录存储 `content_hash`（SHA-256 of extracted text）
- 定时任务（CronJob，每小时一批）抽查用户收藏的网页
- 重新抓取 → 提取正文 → 计算 hash → 与存储的 hash 比较
- hash 不同 → 内容有变化 → 触发重新处理流水线
- 记录历史版本（保留最近 5 个），用户可查看变更

### 4.5 处理队列设计

**Kafka Topic 配置：**

| Topic | Partitions | Replication | Retention | 说明 |
|-------|-----------|-------------|-----------|------|
| doc.uploaded | 32 | 3 | 7 天 | 触发解析 |
| doc.parsed | 32 | 3 | 7 天 | 触发分块 |
| doc.chunked | 32 | 3 | 7 天 | 触发向量化 |
| doc.embedded | 32 | 3 | 3 天 | 触发索引写入 |
| doc.completed | 32 | 3 | 3 天 | 通知完成 |
| doc.failed | 8 | 3 | 30 天 | 死信队列，重试源 |

**【设计理由】32 partitions：**
- 32 = Citus shard count，Kafka partition 和 DB shard 按 user_id hash 取模对齐，保证同一用户的数据在同一组 worker 和同一组 shard 上处理，最大化缓存局部性
- 32 个 partition 支持 32 个并行 consumer，峰值 500K docs/day 时每个 consumer 处理 ~15K docs/day，完全够用

**消费策略：**
- 每个 Worker 服务部署 8-16 个 consumer 实例
- Consumer group 名称：`{stage}_worker`（如 `parse_worker`、`embed_worker`）
- 提交策略：**处理成功后手动提交 offset**（非自动提交），避免处理失败时消息丢失

---

## 5. 检索增强模块（RAG 核心）

> 这是决定系统问答质量的最关键模块。80% 的优化精力应投入于此。

### 5.1 查询理解

```
用户问题 ──→ Query Analyzer ──→ 分类 + 改写 + 分解
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              query_type      rewritten_q      sub_queries[]
              (查询类型)       (改写后)         (子查询列表)
```

**查询分类（轻量规则 + 小模型，非 LLM）：**

| 类型 | 判断规则 | 检索策略调整 |
|------|----------|-------------|
| 精确关键词 | 包含引号、特定术语、ID/编号 | BM25 权重提升到 0.7 |
| 语义问题 | 自然语言问句、包含"为什么/怎么/什么是" | 向量检索权重 0.7 |
| 对比型 | 包含"对比/区别/不同" | 拆分为两个子查询分别检索 |
| 多跳推理 | 包含多个实体且关系隐含 | 拆分子查询 + 知识图谱辅助 |

```python
class QueryAnalyzer:
    """轻量查询分析器，不调用 LLM"""
    def analyze(self, query: str, history: list[str] | None = None) -> AnalyzedQuery:
        # 1. 上下文补全：如果 history 存在，补全代词和省略
        if history:
            query = self.resolve_references(query, history[-3:])

        # 2. 分类
        query_type = self.classify(query)

        # 3. 改写：去掉无意义词、标准化术语
        rewritten = self.rewrite(query)

        # 4. 子查询分解（仅对比型和多跳型）
        sub_queries = self.decompose(query) if query_type in ("compare", "multi_hop") else [rewritten]

        return AnalyzedQuery(
            original=query, type=query_type,
            rewritten=rewritten, sub_queries=sub_queries
        )
```

**【设计理由】为什么不用 LLM 做查询理解：**
- 100K DAU × 10 次/天 = 100 万次/天查询理解调用
- 用 LLM 做查询理解：每次 ~500 token input，¥0.5/天 → ¥15K/月
- 用规则 + 小模型（<100M 参数）：零额外成本，延迟 < 10ms
- 查询理解不需要创造性，规则方法在结构化任务上效果不逊于 LLM

### 5.2 混合检索

#### 通道 1：稠密向量检索（Milvus）

```python
async def dense_search(query: str, user_id: str, top_k: int = 30) -> list[SearchResult]:
    query_vector = await embedding_model.encode([query])  # [1, 1024]

    results = await milvus_collection.search(
        data=query_vector,
        anns_field="dense_vector",
        param={
            "metric_type": "COSINE",
            "params": {"ef": 128}  # HNSW 搜索参数，ef 越大越精确但越慢
        },
        limit=top_k,
        expr=f'user_id == "{user_id}"',  # 分区过滤
        output_fields=["chunk_id", "document_id", "modality", "content_snippet"]
    )
    return [SearchResult(chunk_id=r.id, score=r.distance, ...) for r in results[0]]
```

**HNSW 索引参数：**

| 参数 | 值 | 说明 |
|------|-----|------|
| M | 16 | 每个节点的邻居数，影响索引大小和召回率 |
| efConstruction | 256 | 构建时的搜索宽度，越大索引质量越好 |
| ef (search) | 128 | 查询时的搜索宽度，128 在 99% 召回率和延迟间取得平衡 |

**【设计理由】HNSW vs IVF_FLAT：**
- HNSW：查询延迟更稳定（< 10ms），召回率高（99%+），但内存占用大
- IVF_FLAT：内存占用小，但需要调 nprobe 参数，小 nprobe 召回率低，大 nprobe 延迟高
- 在单用户分区搜索场景下（每个分区 < 100 万向量），HNSW 的内存开销可控，选择 HNSW

#### 通道 2：稀疏检索 BM25（Elasticsearch）

```python
async def sparse_search(query: str, user_id: str, top_k: int = 30) -> list[SearchResult]:
    results = await es.search(
        index="chunks",
        body={
            "query": {
                "bool": {
                    "must": [{"match": {"content": {"query": query, "analyzer": "ik_max_word"}}}],
                    "filter": [{"term": {"user_id": user_id}}]
                }
            },
            "size": top_k,
            "_source": ["chunk_id", "document_id", "content"]
        }
    )
    return [SearchResult(chunk_id=h["_id"], score=h["_score"], ...) for h in results["hits"]["hits"]]
```

**【设计理由】为什么用 Elasticsearch 而不是 PostgreSQL FTS：**
- PostgreSQL FTS 的中文分词需要安装 zhparser/jieba 扩展，质量和维护性都不如 Elasticsearch 的 ik 分词器
- Elasticsearch 在千万级文档上 BM25 检索延迟 < 50ms，PostgreSQL FTS 在百万级就开始退化
- Elasticsearch 的聚合能力更强，支持按标签/时间/来源的分组统计

#### 通道 3：元数据预过滤

```python
@dataclass
class SearchFilter:
    user_id: str          # 必填，安全隔离
    tags: list[str] | None = None
    date_range: tuple[str, str] | None = None  # (start, end)
    file_types: list[str] | None = None
    collections: list[str] | None = None
```

### 5.3 融合策略：Reciprocal Rank Fusion (RRF)

**为什么不用固定加权（0.6/0.4）：**
- 固定权重需要手动调参，不同查询类型最优权重不同
- 向量检索的分数范围 [0, 1]（余弦相似度），BM25 分数范围 [0, ∞)，两者不可直接比较
- 需要归一化，但归一化方法（min-max/z-score）会引入新的调参需求

**RRF 公式：**

```
RRF_score(d) = Σ_{r ∈ rankings} 1 / (k + rank_r(d))

其中 k = 60（标准参数，对排名靠前的结果给予足够区分度）
```

```python
def reciprocal_rank_fusion(
    rankings: dict[str, list[SearchResult]],  # {"dense": [...], "sparse": [...]}
    k: int = 60
) -> list[SearchResult]:
    scores: dict[str, float] = defaultdict(float)
    result_map: dict[str, SearchResult] = {}

    for source, results in rankings.items():
        for rank, result in enumerate(results, start=1):
            scores[result.chunk_id] += 1.0 / (k + rank)
            if result.chunk_id not in result_map:
                result_map[result.chunk_id] = result

    # 按融合分数排序，取 Top 40
    sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:40]
    return [result_map[cid] for cid, _ in sorted_ids]
```

**【设计理由】RRF 的优势：**
1. **尺度无关**：只使用排名，不使用原始分数，不需要归一化
2. **自动平衡**：两个通道都有结果的 chunk 自然得分更高
3. **无需调参**：k=60 是论文验证的稳健值，几乎所有场景都不需要改
4. **简单高效**：O(n log n) 时间复杂度，10 万结果以内毫秒级完成

### 5.4 重排序

```python
class RerankService:
    def __init__(self):
        self.model = None  # bge-reranker-v2-m3, 加载到 GPU

    async def rerank(self, query: str, candidates: list[SearchResult], top_n: int = 10) -> list[SearchResult]:
        # 构造 query-document 对
        pairs = [(query, c.content) for c in candidates]

        # 批量推理（GPU 加速）
        scores = await self.model.compute_score(pairs, batch_size=32)

        # 按分数排序
        scored = list(zip(candidates, scores))
        scored.sort(key=lambda x: x[1], reverse=True)

        return [c for c, s in scored[:top_n]]
```

**【设计理由】Top 40 → Top 10：**
- Top 40 是 RRF 融合后的结果数量，覆盖了大多数相关文档
- Reranker 的推理成本与候选数量线性相关，40 是质量和成本的最佳平衡点
- 最终取 Top 10（而非 5 或 20）作为 LLM 上下文：10 个 512-token 的 chunk = 5120 token 上下文，加上 prompt 和历史约 6K token，在 DeepSeek V3 的 64K 上下文窗口中留有足够空间

### 5.5 上下文组装

```python
class ContextAssembler:
    def assemble(
        self,
        ranked_chunks: list[SearchResult],
        max_context_tokens: int = 8000,  # 留空间给 prompt + history + output
        conversation_history: list[Message] | None = None
    ) -> str:
        # 1. 计算可用 token 预算
        system_prompt_tokens = 200
        history_tokens = self.count_tokens(conversation_history or [])
        output_reserve = 2000  # 为 LLM 输出预留
        budget = max_context_tokens - system_prompt_tokens - history_tokens - output_reserve

        # 2. 填充上下文
        context_parts = []
        used_tokens = 0

        for i, chunk in enumerate(ranked_chunks):
            # 如果同一文档的多个 chunk 连续命中，合并并扩展上下文
            content = self.expand_if_needed(chunk, ranked_chunks)
            chunk_tokens = self.count_tokens(content)

            if used_tokens + chunk_tokens > budget:
                # 截断而非丢弃：部分信息总比没有好
                remaining = budget - used_tokens
                content = self.truncate(content, remaining)
                context_parts.append(f"[{i+1}] {content}")
                break

            context_parts.append(f"[{i+1}] {content}")
            used_tokens += chunk_tokens

        return "\n\n".join(context_parts)

    def expand_if_needed(self, chunk: SearchResult, all_chunks: list[SearchResult]) -> str:
        """如果同文档的连续 chunk 也命中，合并为一个更大的上下文块"""
        same_doc_chunks = [c for c in all_chunks if c.document_id == chunk.document_id]
        if len(same_doc_chunks) <= 1:
            return chunk.content
        # 合并相邻 chunk，补充父块上下文
        return self.merge_with_parent_context(chunk, same_doc_chunks)
```

**【设计理由】动态 token 预算分配：**
- LLM 的上下文窗口是固定资源（DeepSeek V3 = 64K），需要在 system prompt、history、context、output 之间合理分配
- 不能给 context 分配太多 token，否则 output 被压缩，回答不完整
- 不能给 context 分配太少，否则检索结果被截断，回答不准确
- 8000 token 给 context 是经过实验的平衡点：~10 个 chunk × 800 token（含扩展）= 充分但不冗余

### 5.6 多轮对话检索

```
会话历史:
  User: "介绍一下 Transformer 架构"
  Assistant: "Transformer 是一种..." [引用 3 个 chunk]

  User: "它的注意力机制是怎么工作的？"  ← 这里的"它"指什么？
```

**处理流程：**

```python
class MultiTurnRetriever:
    def rewrite_with_history(self, query: str, history: list[Message]) -> str:
        """使用最近 3 轮历史重写当前查询"""
        if not history:
            return query

        recent = history[-3:]  # 最近 3 轮（user+assistant = 6 条）
        context_text = "\n".join(f"{'Q' if m.role=='user' else 'A'}: {m.content[:200]}" for m in recent)

        # 用小型 LLM（或规则方法）做指代消解
        prompt = f"""根据对话历史，改写用户的最新问题，使其独立可理解。
只输出改写后的问题，不要解释。

对话历史:
{context_text}

用户最新问题: {query}

改写后的问题:"""

        return await small_llm.generate(prompt, max_tokens=200)
```

**会话存储：**
- Redis：活跃会话上下文（TTL 24h），支持快速读写
- PostgreSQL：完整会话历史（持久化），支持历史回看
- 压缩策略：保留最近 3 轮完整内容，更早的轮次用 LLM 生成摘要

**【设计理由】3 轮窗口：**
- 大多数指代消解只需要最近 1-2 轮上下文
- 3 轮是安全余量，覆盖 99% 的场景
- 超过 3 轮的历史通过摘要保留关键信息，避免 token 浪费

---

## 6. 生成与回答模块

### 6.1 Prompt 工程

```python
SYSTEM_PROMPT = """你是一个专业的知识库助手。你的任务是基于提供的参考信息回答用户问题。

## 严格规则
1. 只使用参考信息中的内容回答，不要编造信息
2. 如果参考信息不足以回答问题，明确说明"参考信息中没有相关内容"
3. 每个事实性观点后标注来源编号，格式：[1] [2]
4. 如果不同来源有矛盾观点，都列出来并标注各自来源
5. 回答使用项目符号或数字列表组织，不要使用 Markdown 标题

## 参考信息
{context}

## 当前对话历史
{history}"""

USER_PROMPT = "{query}"
```

**Prompt 缓存策略：**
- DeepSeek V3 支持 prefix caching：System prompt 部分不变时自动缓存
- 每个用户的 system prompt 相同，只有 context 和 history 变化
- 缓存命中率预估 > 60%（system prompt ~200 token，每次请求都相同）
- **成本节省**：缓存命中的 input token 成本降低 90%

**【设计理由】prompt 结构设计：**
- "不要编造信息"是最关键的反幻觉指令，必须放在显眼位置
- 明确的引用格式 `[1]` 确保 LLM 输出可解析的引用标记
- "不要使用 Markdown 标题"避免 LLM 的回答结构与参考信息混淆
- History 放在 system prompt 内而非独立消息，确保 LLM 可以看到完整上下文

### 6.2 流式响应（SSE）

```python
# 后端：FastAPI StreamingResponse
@router.post("/api/v1/chat")
async def chat(request: ChatRequest, user: User = Depends(get_current_user)):
    async def event_stream():
        # 1. 检索
        yield f"data: {json.dumps({'type': 'status', 'message': '正在检索...'})}\n\n"
        chunks = await search_service.hybrid_search(request.query, user.id)

        # 2. 重排序
        yield f"data: {json.dumps({'type': 'status', 'message': '正在分析...'})}\n\n"
        ranked = await rerank_service.rerank(request.query, chunks)

        # 3. 组装 context
        context = context_assembler.assemble(ranked, conversation_history=request.history)

        # 4. 流式生成
        yield f"data: {json.dumps({'type': 'citations', 'data': [c.to_dict() for c in ranked]})}\n\n"

        full_answer = ""
        async for token in llm_service.stream_generate(SYSTEM_PROMPT, context, request.query):
            full_answer += token
            yield f"data: {json.dumps({'type': 'token', 'content': token})}\n\n"

        # 5. 完成
        yield f"data: {json.dumps({'type': 'done', 'message_id': generate_id(), 'usage': {...}})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

```typescript
// 前端：SSE 消费
async function streamChat(query: string, onToken: (token: string) => void) {
  const response = await fetch('/api/v1/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
    body: JSON.stringify({ query, conversation_id: currentConversationId })
  });

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    const text = decoder.decode(value);
    for (const line of text.split('\n')) {
      if (!line.startsWith('data: ')) continue;
      const data = JSON.parse(line.slice(6));

      if (data.type === 'token') {
        onToken(data.content);  // 逐 token 渲染
      } else if (data.type === 'citations') {
        setCitations(data.data);  // 显示引用来源
      } else if (data.type === 'done') {
        onComplete(data.message_id);
      }
    }
  }
}
```

**【设计理由】SSE vs WebSocket：**
- SSE 是单向服务器推送，问答场景只需要服务器→客户端的单向流
- SSE 基于 HTTP，自动兼容代理/负载均衡/CDN，WebSocket 需要额外配置
- SSE 原生支持自动重连和事件 ID，WebSocket 需要手动实现
- SSE 不需要维护连接状态，服务端无状态，天然支持水平扩展

### 6.3 引用溯源机制

**数据流：**

```
检索 chunk → 携带 chunk_id + doc_id + page + char_offset
    ↓
LLM 生成回答 → 输出 [1] [2] 等引用标记
    ↓
后处理 → 解析引用标记，验证引用编号是否在检索结果范围内
    ↓
前端渲染 → 引用标记渲染为可点击链接
    ↓
用户点击 → 打开文档预览器，跳转到 page_number，高亮 char_offset 范围
```

**引用验证：**

```python
class CitationValidator:
    def validate(self, answer: str, chunks: list[SearchResult]) -> ValidatedAnswer:
        # 1. 提取 LLM 输出中的引用标记
        citations_in_answer = re.findall(r'\[(\d+)\]', answer)

        # 2. 验证引用编号是否在有效范围内
        valid_citations = [int(c) for c in citations_in_answer if 1 <= int(c) <= len(chunks)]

        # 3. 标记无效引用（LLM 幻觉）
        invalid_citations = [c for c in citations_in_answer if int(c) not in valid_citations]

        # 4. 替换无效引用为警告文本
        for c in invalid_citations:
            answer = answer.replace(f"[{c}]", "[⚠️ 无效引用]")

        return ValidatedAnswer(content=answer, citations=valid_citations)
```

### 6.4 回答质量守护

```python
class AnswerGuardrail:
    async def check(self, answer: str, chunks: list[SearchResult]) -> GuardrailResult:
        issues = []

        # 1. 无引用检查：长段落无引用可能是幻觉
        if len(answer) > 200 and not re.search(r'\[\d+\]', answer):
            issues.append("long_answer_no_citation")

        # 2. 矛盾检测：回答中的否定表述与所有 chunk 内容矛盾
        # （轻量级规则，非 LLM）
        if self.has_contradiction_signals(answer) and all(c.score < 0.3 for c in chunks):
            issues.append("possible_contradiction")

        # 3. 敏感词过滤
        if self.contains_sensitive_content(answer):
            issues.append("sensitive_content")

        return GuardrailResult(
            is_safe=len(issues) == 0,
            issues=issues,
            disclaimer="请注意：以上回答由 AI 生成，建议核实关键信息" if issues else None
        )
```

### 6.5 会话管理

| 功能 | 实现 | 存储 |
|------|------|------|
| 创建会话 | POST /api/v1/conversations | PostgreSQL |
| 自动标题 | 第一条消息 → LLM 生成 20 字标题（缓存） | PostgreSQL |
| 会话列表 | GET /api/v1/conversations?page=1&size=20 | PostgreSQL + Redis 缓存 |
| 上下文管理 | 最近 3 轮完整 + 更早摘要 | Redis（活跃）+ PG（持久） |
| 删除会话 | 软删除（is_deleted=true） | PostgreSQL |
