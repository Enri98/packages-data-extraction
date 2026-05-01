"""
Text extraction from packaging PDFs using pdfplumber.

Responsibilities:
- Parse EAN, dimensions, and product name from the filename.
- Extract selectable text from the PDF and apply regex / spatial rules
  to populate all text_in_pdf and deterministic fields.
- Return a partially-filled PackData with confidence=1.0 for deterministic
  fields and confidence derived from match quality for regex fields.
- Never makes network calls; all logic is local and fully unit-testable.

Not responsible for:
- Visual / icon fields (delegated to vlm.py).
- Cross-field validation or derived fields (delegated to validator.py).
"""

from pathlib import Path

from loguru import logger

from src.schemas.pack import ExtractedField, PackData


def parse_filename(filename: str) -> tuple[str, str, str]:
    """
    Extract (ean, dimensions, product_name) from the standard filename pattern.

    Pattern: {EAN}_{W}x{H}x{D}_{ProductName}.pdf
    Raises ValueError for filenames that do not match the pattern.
    """
    raise NotImplementedError("Session 2 deliverable")


def extract_text_fields(pdf_path: Path) -> PackData:
    """
    Run the full text-extraction pipeline on a single PDF.

    1. Parse deterministic fields from the filename.
    2. Open the PDF with pdfplumber and extract selectable text + bounding boxes.
    3. Apply regex and spatial rules to populate text_in_pdf fields.
    4. Return a PackData with all available fields set; missing fields
       retain the default ExtractedField(value=None, confidence=0.0).
    """
    raise NotImplementedError("Session 2 deliverable")
