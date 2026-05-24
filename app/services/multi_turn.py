"""Multi-turn retrieval: resolve references and rewrite queries using conversation history."""

import re
import logging
from app.services.llm import LLMService

logger = logging.getLogger(__name__)

# Pronouns that need resolution
_PRONOUNS_ZH = re.compile(r"(它|他|她|这个|那个|这|那|其|此|上面|前面|刚刚)")
_SUBJECT_PATTERN = re.compile(r'["“](.+?)["”]')

# Extract potential entities: Chinese nouns (2-8 chars) and English terms
_ENTITY_PATTERN = re.compile(r'[一-鿿]{2,8}|[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*')


async def resolve_query_with_history(
    query: str,
    history_messages: list[dict],
    use_llm: bool = True,
) -> str:
    """Rewrite query using conversation history for reference resolution.

    history_messages: list of {"role": "user"|"assistant", "content": "..."}
    """
    if not history_messages or not _PRONOUNS_ZH.search(query):
        return query

    # Rule-based attempt first (fast, free)
    resolved = _rule_based_resolve(query, history_messages)
    if resolved != query:
        logger.info(f"Rule-based resolve: '{query}' -> '{resolved}'")
        return resolved

    # Fallback to LLM-based resolution if rule-based didn't help
    if use_llm:
        resolved = await _llm_resolve(query, history_messages[-6:])
        if resolved and resolved != query:
            logger.info(f"LLM resolve: '{query}' -> '{resolved}'")
            return resolved

    return query


def _rule_based_resolve(query: str, history: list[dict]) -> str:
    """Simple rule-based pronoun resolution."""
    # Collect entities from recent assistant messages (they contain context about what was discussed)
    entities = []
    for msg in history[-3:]:
        content = msg.get("content", "")
        entities.extend(_ENTITY_PATTERN.findall(content))

    if not entities:
        return query

    # Use the last mentioned entity as the replacement
    last_entity = entities[-1]

    resolved = query
    for pronoun in ["它", "他", "她", "这", "那", "这个", "那个", "其", "此"]:
        if pronoun in resolved:
            resolved = resolved.replace(pronoun, last_entity, 1)
            return resolved

    return query


async def _llm_resolve(query: str, history: list[dict]) -> str | None:
    """Use LLM to resolve references (lightweight, max 100 tokens)."""
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

        result = ""
        llm = LLMService()
        async for token in llm.stream_generate(
            query=prompt,
            context="",
            history=None,
        ):
            result += token
            if len(result) > 200:
                break

        # Clean up: take first line only
        resolved = result.strip().split("\n")[0].strip()
        if resolved and len(resolved) > 2:
            return resolved
    except Exception as e:
        logger.warning(f"LLM resolve failed: {e}")

    return None
