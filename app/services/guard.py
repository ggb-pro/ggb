"""Prompt injection guard: rule-based detection."""

import re
import logging

logger = logging.getLogger(__name__)

# Patterns commonly used in prompt injection attacks
_BLOCKED_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I),
    re.compile(r"forget\s+(all\s+)?previous", re.I),
    re.compile(r"you\s+are\s+now\s+", re.I),
    re.compile(r"system\s*:\s*", re.I),
    re.compile(r"jailbreak", re.I),
    re.compile(r"pretend\s+you\s+are", re.I),
    re.compile(r"act\s+as\s+if\s+you", re.I),
    re.compile(r"override\s+(your\s+)?(instructions|rules|guidelines)", re.I),
    re.compile(r"reveal\s+your\s+(system|prompt|instructions)", re.I),
    re.compile(r"</?(system|user|assistant)>", re.I),
]

_MAX_INPUT_LENGTH = 10000


def check_injection(user_input: str) -> tuple[bool, str]:
    """Check user input for prompt injection attempts.

    Returns (is_safe, reason). If is_safe is False, the input should be rejected.
    """
    if not user_input:
        return True, ""

    if len(user_input) > _MAX_INPUT_LENGTH:
        return False, "Input too long"

    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(user_input):
            logger.warning(f"Blocked prompt injection attempt: matched {pattern.pattern}")
            return False, "Input contains disallowed patterns"

    return True, ""
