"""OCR service: PaddleOCR wrapper for extracting text from images."""

import logging

logger = logging.getLogger(__name__)

_ocr_engine = None


def get_ocr():
    """Lazy-load PaddleOCR engine."""
    global _ocr_engine
    if _ocr_engine is not None:
        return _ocr_engine
    try:
        from paddleocr import PaddleOCR
        _ocr_engine = PaddleOCR(use_angle_cls=True, lang="ch", use_gpu=False, show_log=False)
        logger.info("PaddleOCR loaded")
        return _ocr_engine
    except ImportError:
        logger.warning("PaddleOCR not installed. Install: pip install paddlepaddle paddleocr")
        return None
    except Exception as e:
        logger.warning(f"PaddleOCR load failed: {e}")
        return None


def ocr_image(image_path: str) -> str:
    """Extract text from an image file using PaddleOCR.

    Returns extracted text as a single string.
    """
    engine = get_ocr()
    if engine is None:
        return ""

    try:
        result = engine.ocr(image_path, cls=True)
        if not result or not result[0]:
            return ""

        lines = []
        for line in result[0]:
            text = line[1][0]
            lines.append(text)

        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"OCR failed for {image_path}: {e}")
        return ""
