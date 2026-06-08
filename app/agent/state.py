"""Agent state definition for LangGraph."""

from typing import TypedDict, Annotated
import operator


def _replace_list(old: list, new: list) -> list:
    """Last-write-wins reducer: each node returns the complete list."""
    if new is ... or new is None:
        return old
    return new


class AgentState(TypedDict):
    query: str                              # resolved query (after reference resolution)
    original_query: str                     # user's raw input (for display)
    user_id: str
    conversation_id: str
    collection_id: str | None
    intent: str                             # simple / complex
    has_keyword: bool                       # D7: pass keyword flag from classify to plan
    plan: list[dict]
    tools_called: Annotated[list[dict], operator.add]  # audit log: append-only
    chunks: Annotated[list[dict], _replace_list]       # latest round only, no accumulation
    context: str
    answer: str
    reflection_result: str
    reflection_scores: dict                 # {"relevance": N, "groundedness": N, "consistency": N}
    retry_count: int
    should_retry: bool
    error: str | None
