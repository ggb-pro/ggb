"""Chinese text tokenizer using jieba for FTS."""

import logging

logger = logging.getLogger(__name__)
_jieba_initialized = False


def _ensure_jieba():
    global _jieba_initialized
    if _jieba_initialized:
        return
    import jieba
    jieba.setLogLevel(logging.WARNING)
    _jieba_initialized = True


def tokenize(text: str) -> str:
    """Tokenize Chinese text into space-separated words for tsvector/tsquery."""
    _ensure_jieba()
    import jieba
    words = jieba.cut(text)
    # Filter single-char noise and join with spaces
    return " ".join(w.strip() for w in words if len(w.strip()) > 1)


def tokenize_query(query: str) -> str:
    """Tokenize a search query for tsquery construction."""
    _ensure_jieba()
    import jieba
    words = jieba.cut(query)
    # Keep all meaningful tokens
    tokens = [w.strip() for w in words if w.strip() and len(w.strip()) > 0]
    return " & ".join(tokens)
