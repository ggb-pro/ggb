"""OCR service: API-first (SiliconFlow vision) with local PaddleOCR fallback."""

import base64
import logging

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

_ocr_engine = None


def _load_local_ocr():
    """Lazy-load PaddleOCR engine (local fallback only)."""
    global _ocr_engine
    if _ocr_engine is not None:
        return _ocr_engine
    try:
        from paddleocr import PaddleOCR
        _ocr_engine = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=False, show_log=False)
        logger.info("PaddleOCR loaded (local fallback)")
        return _ocr_engine
    except ImportError:
        logger.warning("PaddleOCR not installed")
        return None
    except Exception as e:
        logger.warning(f"PaddleOCR load failed: {e}")
        return None


def _ocr_api(image_path: str) -> str | None:
    """Call SiliconFlow vision API for OCR."""
    if not settings.ocr_api_url or not settings.ocr_api_key:
        return None
    try:
        import httpx
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        with httpx.Client(timeout=60) as client:
            resp = client.post(
                settings.ocr_api_url.rstrip("/") + "/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.ocr_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": settings.ocr_model,
                    "messages": [{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "请完整识别并输出图片中的所有文字，保持原始格式和排版。"},
                            {"type": "image_url", "image_url": {
                                "url": f"data:image/jpeg;base64,{img_b64}"
                            }},
                        ],
                    }],
                    "max_tokens": 4096,
                    "temperature": 0.0,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        content = data["choices"][0]["message"]["content"]
        logger.info(f"OCR API returned {len(content)} chars")
        return content
    except Exception as e:
        logger.warning(f"OCR API failed: {e}")
        return None


def _ocr_local(image_path: str) -> str | None:
    """Local PaddleOCR inference."""
    engine = _load_local_ocr()
    if engine is None:
        return None
    try:
        result = engine.ocr(image_path, cls=True)
        if not result or not result[0]:
            return ""
        lines = [line[1][0] for line in result[0]]
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"Local OCR failed for {image_path}: {e}")
        return None


def ocr_image(image_path: str) -> str:
    """Extract text from an image file. Strategy: API → local → empty string."""
    if settings.ocr_backend == "api":
        text = _ocr_api(image_path)
        if text is not None:
            return text
        logger.info("OCR API failed, falling back to local")

    text = _ocr_local(image_path)
    if text is not None:
        return text

    if settings.ocr_backend == "local" and settings.ocr_api_url:
        text = _ocr_api(image_path)
        if text is not None:
            return text

    return ""
