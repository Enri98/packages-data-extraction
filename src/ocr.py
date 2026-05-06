"""
OCR text extraction from packaging PDFs.

Responsibilities:
- Lazy-load a singleton RapidOCR engine (cold-start ~1.6 s, paid once per process).
- Render each PDF page to a 300-DPI bitmap with pypdfium2, then run RapidOCR.
- Return the concatenated per-page text for the full document.
- Cache results per resolved path so a PDF processed twice in the same pipeline
  run is never re-OCR'd.

Not responsible for:
- Regex / spatial parsing of the extracted text (delegated to parsing.py).
- VLM extraction (delegated to vlm.py).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from loguru import logger

# ---------------------------------------------------------------------------
# Singleton engine
# ---------------------------------------------------------------------------

_engine: object | None = None  # RapidOCR instance; loaded on first call


def _get_engine() -> object:
    """Return the module-level RapidOCR singleton, loading it if needed."""
    global _engine
    if _engine is None:
        logger.info("Loading RapidOCR engine (cold start ~1.6 s)...")
        from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-untyped]

        _engine = RapidOCR()
        logger.info("RapidOCR engine ready.")
    return _engine


# ---------------------------------------------------------------------------
# Per-path result cache
# ---------------------------------------------------------------------------

_OCR_CACHE: dict[Path, str] = {}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _render_page_to_array(page: object, scale: float) -> np.ndarray:
    """Render a single pypdfium2 page to an RGB numpy array at *scale* factor."""
    bitmap = page.render(scale=scale)  # type: ignore[attr-defined]
    pil_image = bitmap.to_pil()
    return np.array(pil_image)


def _ocr_page(engine: object, img_array: np.ndarray) -> str:
    """
    Run RapidOCR on a single page image array and return the page text.

    Each OCR result line becomes one text line in the output.
    """
    result, _elapsed = engine(img_array)  # type: ignore[call-arg]
    if not result:
        return ""
    lines: list[str] = [text for _box, text, _conf in result]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_ocr_text(pdf_path: Path) -> str:
    """
    Render all pages of *pdf_path* at 300 DPI and return concatenated OCR text.

    Pages are joined by newline characters.  Results are cached by resolved path
    so repeated calls within the same process return instantly.

    Args:
        pdf_path: Path to the PDF file to process.

    Returns:
        A single string containing all OCR-recognised text, one page per
        paragraph separated by ``\\n``.

    Raises:
        FileNotFoundError: If *pdf_path* does not exist.
        RuntimeError: If pypdfium2 or RapidOCR fails to process the file.
    """
    resolved = pdf_path.resolve()

    if resolved in _OCR_CACHE:
        logger.debug("OCR cache hit | path={}", resolved)
        return _OCR_CACHE[resolved]

    if not resolved.exists():
        raise FileNotFoundError(f"PDF not found: {resolved}")

    logger.info("Starting OCR extraction | path={}", resolved)

    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]
    except ImportError as exc:
        raise RuntimeError(
            "pypdfium2 is required for OCR extraction. Install it with: uv add pypdfium2"
        ) from exc

    engine = _get_engine()

    dpi = 300
    scale = dpi / 72.0

    doc = pdfium.PdfDocument(str(resolved))
    page_texts: list[str] = []

    for page_index, page in enumerate(doc):
        logger.debug("Rendering page {} / {}", page_index + 1, len(doc))
        img_array = _render_page_to_array(page, scale)
        page_text = _ocr_page(engine, img_array)
        if not page_text:
            logger.warning("OCR returned no results for page {}", page_index + 1)
        page_texts.append(page_text)
        logger.debug("Page {} | chars={}", page_index + 1, len(page_text))

    full_text = "\n".join(page_texts)
    _OCR_CACHE[resolved] = full_text

    logger.info(
        "OCR extraction complete | path={} | pages={} | total_chars={}",
        resolved,
        len(doc),
        len(full_text),
    )
    return full_text
