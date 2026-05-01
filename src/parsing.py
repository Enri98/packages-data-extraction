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

import re
from pathlib import Path

from loguru import logger

from src.schemas.pack import ExtractedField, PackData

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

_FILENAME_RE = re.compile(r"^(\d{13})_(\d+x\d+x\d+)_(.+)\.pdf$")


def parse_filename(filename: str) -> tuple[str, str, str]:
    """
    Extract (ean, dimensions, product_name) from the standard filename pattern.

    Pattern: {EAN}_{W}x{H}x{D}_{ProductName}.pdf
    Underscores in the product name segment are replaced with spaces.
    Raises ValueError for filenames that do not match the pattern.
    """
    match = _FILENAME_RE.match(filename)
    if not match:
        raise ValueError(
            f"Filename '{filename}' does not match expected pattern "
            r"'{EAN}_{W}x{H}x{D}_{ProductName}.pdf' "
            "(EAN must be 13 digits, dimensions as WxHxD)."
        )
    ean = match.group(1)
    dimensions = match.group(2)
    product_name = match.group(3).replace("_", " ")
    return ean, dimensions, product_name


# ---------------------------------------------------------------------------
# Regex patterns for text_in_pdf fields
# ---------------------------------------------------------------------------

_FIELD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "dimensioni",
        re.compile(
            r"(\d+(?:[.,]\d+)?\s*cm\s*x\s*Ø\s*\d+(?:[.,]\d+)?\s*cm)",
            re.IGNORECASE,
        ),
    ),
    ("materiale", re.compile(r"(?:Materiale|Material)[:\s]+([^\n]+)", re.IGNORECASE)),
    ("lotto", re.compile(r"(?:Lotto|Lot)[:\s#]+([A-Z0-9\-]+)", re.IGNORECASE)),
    ("paese_di_produzione", re.compile(r"(?:Prodotto|Made)\s+in\s+([^\n,\.]+)", re.IGNORECASE)),
    (
        "capacita_batteria_e_tensione_nominale",
        re.compile(r"(\d+[\.,]\d+\s*V[^\n]*(?:mAh|mah)[^\n]*)", re.IGNORECASE),
    ),
    ("tempo_di_carica", re.compile(r"(?:Tempo di carica|Charging time)[:\s]+([^\n]+)", re.IGNORECASE)),
    ("n_vibrazioni", re.compile(r"(\d+)\s*(?:vibrazioni|vibration)", re.IGNORECASE)),
    ("livello_impermeabilita", re.compile(r"(IP[XW]\d+)", re.IGNORECASE)),
    ("sito_web", re.compile(r"(www\.[a-zA-Z0-9\-\.]+\.[a-z]{2,})")),
    ("eta_minima", re.compile(r"(\+?18\+?)")),
]

# Material code tokens that form disposal codes, e.g. "FR 7", "CPE 21", "PAP"
_MATERIAL_CODE_RE = re.compile(r"^(?:[A-Z]{2,3}\s*\d*|\d+)$")


# ---------------------------------------------------------------------------
# Spatial extraction helpers
# ---------------------------------------------------------------------------

def _extract_disposal_codes(page: object) -> str | None:
    """
    Extract material disposal codes from the top band of the first page.

    Looks for words whose top-edge is above y=80 (PDF points) and whose
    text matches a material-code token pattern.  Adjacent tokens are joined
    with " | " to produce strings like "FR 7 | CPE 21 | PAP".
    Returns None if no tokens are found.
    """
    words = page.extract_words()  # type: ignore[attr-defined]
    top_words: list[str] = [
        w["text"] for w in words if float(w["top"]) < 80
    ]

    # Collect consecutive material-code tokens
    tokens: list[str] = []
    for word in top_words:
        if _MATERIAL_CODE_RE.match(word.strip()):
            tokens.append(word.strip())

    if not tokens:
        return None

    # Pair alphabetic prefix with following numeric token when adjacent
    # e.g. ["FR", "7", "CPE", "21", "PAP"] → "FR 7 | CPE 21 | PAP"
    codes: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.isalpha() and i + 1 < len(tokens) and tokens[i + 1].isdigit():
            codes.append(f"{tok} {tokens[i + 1]}")
            i += 2
        else:
            codes.append(tok)
            i += 1

    return " | ".join(codes)


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_text_fields(pdf_path: Path) -> PackData:
    """
    Run the full text-extraction pipeline on a single PDF.

    1. Parse deterministic fields from the filename.
    2. Open the PDF with pdfplumber and extract selectable text + bounding boxes.
    3. Apply regex and spatial rules to populate text_in_pdf fields.
    4. Return a PackData with all available fields set; missing fields
       retain the default ExtractedField(value=None, confidence=0.0).
    """
    import pdfplumber  # local import keeps the module import-time cost minimal

    ean, dimensions, product_name = parse_filename(pdf_path.name)
    logger.info(
        "Parsed filename | ean={} | dimensions={}", ean, dimensions
    )

    pack = PackData(
        codice_ean=ean,
        nome_prodotto=ExtractedField(value=product_name, confidence=0.9),
    )

    # ------------------------------------------------------------------
    # Open PDF and collect full text
    # ------------------------------------------------------------------
    full_text_parts: list[str] = []
    disposal_codes: str | None = None

    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            full_text_parts.append(page_text)

            if page_index == 0:
                disposal_codes = _extract_disposal_codes(page)

    full_text = "\n".join(full_text_parts)

    # ------------------------------------------------------------------
    # Apply regex patterns
    # ------------------------------------------------------------------
    fields_found: int = 2  # codice_ean (deterministic) + nome_prodotto already set

    for field_name, pattern in _FIELD_PATTERNS:
        match = pattern.search(full_text)
        if match:
            value = match.group(1).strip()
            setattr(
                pack,
                field_name,
                ExtractedField(value=value, confidence=0.9, evidence=match.group(0).strip()),
            )
            fields_found += 1

    # ------------------------------------------------------------------
    # Spatial: disposal codes from page-top band
    # ------------------------------------------------------------------
    if disposal_codes:
        pack.codici_smaltimento_materiali = ExtractedField(
            value=disposal_codes, confidence=0.85
        )
        fields_found += 1

    logger.info(
        "Text extraction complete | ean={} | fields_found={}", ean, fields_found
    )

    return pack
