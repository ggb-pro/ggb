"""Web scraper: fetch URL content with Playwright (JS rendering) fallback."""

import re
import logging

logger = logging.getLogger(__name__)


async def scrape_url(url: str) -> dict:
    """Fetch a URL and extract clean text content.

    Strategy: httpx (fast, static) → Playwright (JS rendering) if content is thin.
    Returns: {"title": str, "content": str, "raw_html": str}
    """
    import httpx

    # Try httpx first (fast, low memory)
    try:
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

        # If content is substantial, return immediately
        if len(content) > 500:
            return {"title": title, "content": content, "raw_html": html}
    except Exception as e:
        logger.info(f"httpx fetch failed: {e}, trying Playwright")
        html = None
        content = ""

    # Fallback to Playwright for JS-rendered pages
    try:
        return await _scrape_playwright(url)
    except Exception as e:
        logger.warning(f"Playwright also failed: {e}")
        if html:
            return {"title": title or "Untitled", "content": content, "raw_html": html}
        raise RuntimeError(f"Failed to scrape {url}: {e}")


async def _scrape_playwright(url: str) -> dict:
    """Render page with Playwright headless Chromium."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = await browser.new_page()
        await page.goto(url, wait_until="networkidle", timeout=30000)
        html = await page.content()
        title = await page.title()
        await browser.close()

    content = _extract_content(html)
    return {"title": title or "Untitled", "content": content, "raw_html": html}


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if m:
        title = m.group(1).strip()
        if title:
            return title[:500]
    return "Untitled"


def _extract_content(html: str) -> str:
    """Extract main text content from HTML."""
    clean = re.sub(
        r"<(script|style|nav|header|footer|aside|noscript)[^>]*>.*?</\1>",
        "", html, flags=re.IGNORECASE | re.DOTALL,
    )
    article_match = re.search(
        r"<(?:article|main)[^>]*>(.*?)</(?:article|main)>",
        clean, re.IGNORECASE | re.DOTALL,
    )
    if article_match:
        clean = article_match.group(1)

    clean = re.sub(r"<(p|div|br|h[1-6]|li|tr)[^>]*>", "\n", clean, flags=re.IGNORECASE)
    clean = re.sub(r"<[^>]+>", "", clean)

    import html as html_mod
    clean = html_mod.unescape(clean)

    clean = re.sub(r"[ \t]+", " ", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)

    return clean.strip()
