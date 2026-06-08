"""Query analyzer: LLM-first classification with rule fast path."""

import re
import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class AnalyzedQuery:
    original: str
    query_type: str  # keyword / semantic / compare / multi_hop
    sub_types: list[str]
    has_keyword: bool
    rewritten: str
    sub_queries: list[str]
    vector_weight: float
    bm25_weight: float


# Rule fast path: unambiguous patterns only
_FAST_KEYWORD = re.compile(
    r'".+?"|\'.+?\'|'            # Quoted strings
    r'[\w\d]{8,}-[\w\d]{4,}'     # UUID format
)

# Noise words to strip during rewrite
_NOISE_WORDS = re.compile(r"^(请问|请教一下|我想知道|帮我|告诉我|能说下|可以说下|说说|讲讲)\s*")
_FILLER = re.compile(r"\s+(吗|呢|吧|啊|呀|哈|嘛|哦)\s*$")

# Fallback patterns (used only when LLM fails)
_COMPARE_PATTERNS = re.compile(r"(对比|区别|不同|差异|比较|分别|各自|vs|versus|和.*的区)")
_MULTI_ENTITY_PATTERNS = re.compile(r"(和|与|以及|跟|同).*(的|之间|关系|联系)")


class QueryAnalyzer:
    async def analyze(self, query: str, history: list[str] | None = None) -> AnalyzedQuery:
        q = query.strip()

        # 1. Resolve references if history exists
        if history:
            from app.services.multi_turn import _rule_based_resolve
            history_dicts = [{"role": "user", "content": h} for h in history[-3:]]
            resolved = _rule_based_resolve(q, history_dicts)
            if resolved != q:
                q = resolved

        # 2. Rewrite (noise removal)
        rewritten = self._rewrite(q)

        # 3. Classify: rule fast path → LLM main path
        query_type, sub_types, has_keyword = await self._classify(q)

        # 4. Decompose
        if query_type in ("compare", "multi_hop") and len(sub_types) > 1:
            sub_queries = self._decompose(q, query_type)
        else:
            sub_queries = [rewritten]

        # 5. Weights
        if has_keyword:
            vw, bw = 0.3, 0.7
        else:
            vw, bw = 0.7, 0.3

        return AnalyzedQuery(
            original=query, query_type=query_type, sub_types=sub_types,
            has_keyword=has_keyword, rewritten=rewritten,
            sub_queries=sub_queries, vector_weight=vw, bm25_weight=bw,
        )

    async def _classify(self, query: str) -> tuple[str, list[str], bool]:
        # Rule fast path: unambiguous keyword patterns
        if _FAST_KEYWORD.search(query):
            return "keyword", ["keyword"], True

        # LLM main path
        return await self._llm_classify(query)

    async def _llm_classify(self, query: str) -> tuple[str, list[str], bool]:
        """Call lightweight LLM for classification."""
        try:
            from app.config import get_settings
            import httpx

            settings = get_settings()
            prompt = f"""分析以下用户查询，输出 JSON。

用户查询：{query}

判断维度：
1. query_type（主类型，必选其一）：
   - keyword: 包含精确 ID/编号/错误码等需要精确匹配的内容
   - semantic: 开放式问答（为什么/怎么/什么是）
   - compare: 对比类查询（A vs B、区别、差异）
   - multi_hop: 需要多步推理或跨实体关联
2. sub_types（所有命中的类型，可多选）：keyword/semantic/compare/multi_hop
3. has_keyword: 是否包含需要精确匹配的关键词（ID/编号/错误码）

输出：{{"query_type": "...", "sub_types": [...], "has_keyword": true/false}}"""

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{settings.llm_api_url}/chat/completions",
                    headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                    json={
                        "model": settings.agent_lightweight_llm,
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 150,
                        "temperature": 0.1,
                    },
                )
                resp.raise_for_status()
                raw = resp.json()["choices"][0]["message"]["content"].strip()

            return self._parse_classify(raw, query)
        except Exception:
            return self._fallback_classify(query)

    def _parse_classify(self, raw: str, query: str) -> tuple[str, list[str], bool]:
        try:
            text = raw.strip()
            if "```" in text:
                m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
                if m:
                    text = m.group(1).strip()
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                qt = parsed.get("query_type", "semantic")
                st = parsed.get("sub_types", [qt])
                hk = parsed.get("has_keyword", False)
                if qt not in {"keyword", "semantic", "compare", "multi_hop"}:
                    qt = "semantic"
                return qt, st, hk
        except (json.JSONDecodeError, AttributeError):
            pass
        return self._fallback_classify(query)

    def _fallback_classify(self, query: str) -> tuple[str, list[str], bool]:
        """Rule-based fallback when LLM is unavailable."""
        if _COMPARE_PATTERNS.search(query) and _MULTI_ENTITY_PATTERNS.search(query):
            return "multi_hop", ["compare", "multi_hop"], False
        if _COMPARE_PATTERNS.search(query):
            return "compare", ["compare"], False
        if _MULTI_ENTITY_PATTERNS.search(query):
            return "multi_hop", ["multi_hop"], False
        return "semantic", ["semantic"], False

    def _rewrite(self, query: str) -> str:
        q = _NOISE_WORDS.sub("", query)
        q = _FILLER.sub("", q)
        return q.strip()

    def _decompose(self, query: str, query_type: str) -> list[str]:
        if query_type == "compare":
            for connector in ["对比", "比较", "vs", "versus", "和", "与", "跟"]:
                parts = query.split(connector, 1)
                if len(parts) == 2 and len(parts[0].strip()) > 1 and len(parts[1].strip()) > 1:
                    return [p.strip() for p in parts]
        elif query_type == "multi_hop":
            for connector in ["和", "与", "以及", "跟"]:
                parts = query.split(connector, 1)
                if len(parts) == 2 and len(parts[0].strip()) > 1 and len(parts[1].strip()) > 1:
                    return [p.strip() for p in parts]
        return [query]
