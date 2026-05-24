"""Web scraper: fetch URL content and extract clean text using readability."""

import re
import logging

logger = logging.getLogger(__name__)


async def scrape_url(url: str) -> dict:
    """Fetch a URL and extract clean text content.

    Returns: {"title": str, "content": str, "raw_html": str}
    """
    import httpx

    async with httpx.AsyncClient(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; knSpaceBot/1.0)"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    title = _extract_title(html)
    content = _extract_content(html)

    return {"title": title, "content": content, "raw_html": html}


def _extract_title(html: str) -> str:
    """Extract page title from HTML."""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = m.group(1).strip()
        if title:
            return title[:500]
    return "Untitled"


def _extract_content(html: str) -> str:
    """Extract main text content from HTML using simple heuristics.

    Tries readability-style extraction. Falls back to stripping all tags.
    """
    # Remove scripts, styles, nav, header, footer
    clean = re.sub(
        r"<(script|style|nav|header|footer|aside|noscript)[^>]*>.*?</\1>",
        "", html, flags=re.IGNORECASE | re.DOTALL,
    )

    # Try to find <article> or <main> content
    article_match = re.search(
        r"<(?:article|main)[^>]*>(.*?)</(?:article|main)>",
        clean, re.IGNORECASE | re.DOTALL,
    )
    if article_match:
        clean = article_match.group(1)

    # Convert common block elements to newlines
    clean = re.sub(r"<(p|div|br|h[1-6]|li|tr)[^>]*>", "\n", clean, flags=re.IGNORECASE)

    # Remove all remaining HTML tags
    clean = re.sub(r"<[^>]+>", "", clean)

    # Decode HTML entities
    import html as html_mod
    clean = html_mod.unescape(clean)

    # Collapse whitespace
    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)

    return clean.strip()
