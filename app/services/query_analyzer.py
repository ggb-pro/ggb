"""Query analyzer: classify, rewrite, decompose — no LLM, pure rules."""

import re
from dataclasses import dataclass


@dataclass
class AnalyzedQuery:
    original: str
    query_type: str  # keyword / semantic / compare / multi_hop
    rewritten: str
    sub_queries: list[str]
    vector_weight: float
    bm25_weight: float


# Patterns for classification
_SEMANTIC_PATTERNS = re.compile(
    r"(为什么|怎么|什么是|如何|怎样|为什么|为何|怎么样的|什么样|有什么|有哪些|能否|可以|能不能|是否能)"
)
_COMPARE_PATTERNS = re.compile(
    r"(对比|区别|不同|差异|比较|分别|各自|vs|versus|和.*的区)"
)
_EXACT_PATTERNS = re.compile(
    r'".+?"|\'.+?\'|[\w\d]{8,}-[\w\d]{4,}'  # quoted strings or UUID-like IDs
)
_MULTI_ENTITY_PATTERNS = re.compile(
    r"(和|与|以及|跟|同).*(的|之间|关系|联系)"
)

# Noise words to strip during rewrite
_NOISE_WORDS = re.compile(r"^(请问|请教一下|我想知道|帮我|告诉我|能说下|可以说下|说说|讲讲)\s*")
_FILLER = re.compile(r"\s+(吗|呢|吧|啊|呀|哈|嘛|哦)\s*$")


class QueryAnalyzer:
    def analyze(self, query: str, history: list[str] | None = None) -> AnalyzedQuery:
        q = query.strip()

        # 1. Resolve references if history exists (rule-based)
        if history:
            q = self._resolve_references(q, history[-3:])

        # 2. Classify
        query_type = self._classify(q)

        # 3. Rewrite
        rewritten = self._rewrite(q)

        # 4. Decompose (compare/multi_hop only)
        if query_type == "compare":
            sub_queries = self._decompose_compare(q)
        elif query_type == "multi_hop":
            sub_queries = self._decompose_multi(q)
        else:
            sub_queries = [rewritten]

        # 5. Set weights based on type
        if query_type == "keyword":
            vector_weight, bm25_weight = 0.3, 0.7
        else:
            vector_weight, bm25_weight = 0.7, 0.3

        return AnalyzedQuery(
            original=query,
            query_type=query_type,
            rewritten=rewritten,
            sub_queries=sub_queries,
            vector_weight=vector_weight,
            bm25_weight=bm25_weight,
        )

    def _classify(self, query: str) -> str:
        if _EXACT_PATTERNS.search(query):
            return "keyword"
        if _COMPARE_PATTERNS.search(query):
            return "compare"
        if _MULTI_ENTITY_PATTERNS.search(query):
            return "multi_hop"
        if _SEMANTIC_PATTERNS.search(query):
            return "semantic"
        # Default: treat as semantic (vector-friendly)
        return "semantic"

    def _rewrite(self, query: str) -> str:
        q = _NOISE_WORDS.sub("", query)
        q = _FILLER.sub("", q)
        return q.strip()

    def _decompose_compare(self, query: str) -> list[str]:
        """Split comparison query into two sub-queries."""
        # Try splitting on comparison connectors
        for connector in ["对比", "比较", "vs", "versus", "和", "与", "跟"]:
            parts = query.split(connector, 1)
            if len(parts) == 2 and len(parts[0].strip()) > 1 and len(parts[1].strip()) > 1:
                return [p.strip() for p in parts]
        return [query]

    def _decompose_multi(self, query: str) -> list[str]:
        """Split multi-entity query."""
        for connector in ["和", "与", "以及", "跟"]:
            parts = query.split(connector, 1)
            if len(parts) == 2 and len(parts[0].strip()) > 1 and len(parts[1].strip()) > 1:
                return [p.strip() for p in parts]
        return [query]

    def _resolve_references(self, query: str, history: list[str]) -> str:
        """Rule-based pronoun resolution using recent history."""
        pronouns = {
            "它": None, "他": None, "她": None,
            "这个": None, "那个": None, "这": None, "那": None,
            "其": None, "此": None,
        }
        # Extract nouns/entities from history (last few messages)
        recent_text = " ".join(history)
        # Simple approach: look for capitalized terms, technical terms, or quoted items
        entities = re.findall(r'[一-鿿]{2,8}|[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*', recent_text)

        if not entities:
            return query

        last_entity = entities[-1]
        resolved = query
        for pronoun in pronouns:
            if pronoun in resolved:
                resolved = resolved.replace(pronoun, last_entity, 1)
                break

        return resolved
