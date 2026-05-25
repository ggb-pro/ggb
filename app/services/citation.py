"""Citation validator: verify LLM output citations against actual results."""

import re


def validate_citations(answer: str, max_index: int) -> str:
    """Replace invalid citation markers in LLM output.

    LLM should use [1], [2], etc. referencing the numbered context chunks.
    This removes any citation numbers outside the valid range.
    """
    def _replace(m):
        num = int(m.group(1))
        if 1 <= num <= max_index:
            return m.group(0)
        return f"[invalid-{num}]"

    return re.sub(r"\[(\d+)\]", _replace, answer)
