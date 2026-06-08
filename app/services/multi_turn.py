"""Multi-turn retrieval: LLM-first coreference resolution with rule fast path."""

import re
import logging

logger = logging.getLogger(__name__)

_PRONOUNS_ZH = re.compile(r"(它|他|她|这个|那个|这|那|其|此|上面|前面|刚刚)")
_FAST_PRONOUN = re.compile(r'^(它|他|她|这个|那个|这|那|其|此)')


async def resolve_query_with_history(
    query: str,
    history_messages: list[dict],
    use_llm: bool = True,
) -> str:
    """Rewrite query using conversation history for reference resolution."""
    if not history_messages or not _PRONOUNS_ZH.search(query):
        return query

    # Fast path: unambiguous single-entity + single-pronoun
    fast_resolved = _fast_rule_resolve(query, history_messages)
    if fast_resolved != query:
        logger.info(f"Fast rule resolve: '{query}' -> '{fast_resolved}'")
        return fast_resolved

    # Main path: LLM resolution
    if use_llm:
        llm_resolved = await _llm_resolve(query, history_messages[-6:])
        if llm_resolved and _validate_resolution(query, llm_resolved, history_messages):
            logger.info(f"LLM resolve: '{query}' -> '{llm_resolved}'")
            return llm_resolved

    # Fallback: append last user message as context expansion
    last_user = [m for m in history_messages if m["role"] == "user"]
    if last_user:
        return f"{last_user[-1]['content']}，{query}"
    return query


def _fast_rule_resolve(query: str, history: list[dict]) -> str:
    """Rule fast path: only for unambiguous single-entity + leading pronoun."""
    if not _FAST_PRONOUN.match(query):
        return query

    recent_user = [m for m in history[-4:] if m["role"] == "user"]
    if not recent_user:
        return query

    last_user = recent_user[-1]["content"]
    entities = _extract_topic_entities(last_user)

    if len(entities) == 1:
        target = entities[0]
        for pronoun in ["它", "他", "她", "这个", "那个"]:
            if query.startswith(pronoun):
                return query.replace(pronoun, target, 1)

    return query


def _extract_topic_entities(text: str) -> list[str]:
    """Extract topic entities from user message: noun phrases before '的'."""
    entities = []
    for m in re.finditer(r'([一-鿿A-Za-z0-9\s\-\.]{2,15})(?:的|之)', text):
        candidate = m.group(1).strip()
        if len(candidate) >= 2:
            entities.append(candidate)
    return list(dict.fromkeys(entities))


def _validate_resolution(original: str, resolved: str, history: list[dict]) -> bool:
    """Validate LLM resolution: new terms must appear in history."""
    new_terms = set(re.findall(r'[一-鿿]{2,8}|[A-Z][a-zA-Z0-9\-]+', resolved)) - \
                set(re.findall(r'[一-鿿]{2,8}|[A-Z][a-zA-Z0-9\-]+', original))
    if not new_terms:
        return False
    history_text = " ".join(m.get("content", "") for m in history)
    for term in new_terms:
        if term not in history_text:
            logger.warning(f"LLM resolve hallucination: '{term}' not in history")
            return False
    if len(resolved) > len(original) * 3:
        return False
    return True


def _rule_based_resolve(query: str, history: list[dict]) -> str:
    """Legacy interface for query_analyzer reference resolution."""
    return _fast_rule_resolve(query, history)


async def _llm_resolve(query: str, history: list[dict]) -> str | None:
    from app.config import get_settings
    settings = get_settings()
    try:
        history_text = "\n".join(
            f"{'Q' if m['role'] == 'user' else 'A'}: {m['content'][:200]}"
            for m in history
        )

        prompt = f"""根据对话历史，改写用户的最新问题，使其独立可理解。只输出改写后的问题，不要解释。

对话历史:
{history_text}

用户最新问题: {query}

改写后的问题:"""

        import httpx
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{settings.llm_api_url}/chat/completions",
                headers={"Authorization": f"Bearer {settings.llm_api_key}"},
                json={
                    "model": settings.agent_lightweight_llm,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 100,
                    "temperature": 0.1,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        resolved = data["choices"][0]["message"]["content"].strip().split("\n")[0].strip()
        if resolved and len(resolved) > 2:
            return resolved
    except Exception as e:
        logger.warning(f"LLM resolve failed: {e}")

    return None
