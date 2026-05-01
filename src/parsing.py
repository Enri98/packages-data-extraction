"""
Text extraction from packaging PDFs.

Responsibilities:
- Parse EAN, dimensions, and product name from the filename.
- Extract text from the PDF via OCR (default) or pdfplumber (fallback for
  PDFs with live selectable text) and apply regex / spatial rules to populate
  all text_in_pdf and deterministic fields.
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

from src.schemas.pack import ExtractedField, PackData, PresenceField

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

# Dimensional string, e.g. "17cm x Ø5.7cm" or "25cm x 03cm" (OCR may render Ø as 0).
# We keep the regex permissive and normalise the captured value in post-processing.
_DIMENSIONI_RE = re.compile(
    r"(\d+(?:[.,]\d+)?\s*cm\s*x?\s*[Ø0O]\s*\d+(?:[.,]\d+)?\s*cm)",
    re.IGNORECASE,
)

# Lot number — require explicit label prefix + letter-starting value to avoid
# matching stray numeric tokens elsewhere on the package.
_LOTTO_RE = re.compile(r"(?:Lotto|LOT)\s*[:#]\s*([A-Z][A-Z0-9\-]{1,15})")

# Battery capacity + nominal voltage. Capture the mAh numeric and V numeric
# separately so we can reformat to canonical "420mAh / 3.7V".
_BATTERY_RE = re.compile(
    r"Capacit[aà]\s+batteria[:\s]+(\d+(?:[.,]\d+)?)\s*mAh[^\n]{0,60}?(\d+(?:[.,]\d+)?)\s*V",
    re.IGNORECASE,
)

# Vibration count — capture number + word so output matches golden "10 vibrazioni".
_VIBRAZIONI_RE = re.compile(r"(\d+\s*(?:vibrazioni|vibration\w*))", re.IGNORECASE)

# Waterproofing — try IPX/IPW first; fall back to "Non impermeabile" / "Not waterproof".
_IMPERMEABILITA_IPX_RE = re.compile(r"(IP[XW]\d+)", re.IGNORECASE)
_IMPERMEABILITA_NON_RE = re.compile(
    r"(Non\s+impermeabile|Not\s+waterproof)", re.IGNORECASE
)

# Charging mode — match only known charging-method keywords that follow "Ricarica".
# Using an explicit allowlist avoids false positives from "Ricarica energetica",
# "ricarica" in disposal instructions, or other non-charging context on the packaging.
_RICARICA_MODES = (
    "magnetica", "USB", "minijack", "mini-jack", "Type-C", "type-c",
    "wireless", "induttiva", "micro-USB", "microusb",
)
_RICARICA_MODES_PATTERN = "|".join(re.escape(m) for m in _RICARICA_MODES)
# Match "Ricarica <mode>" exactly; no optional trailing word to avoid absorbing
# bilingual repetitions (e.g. "Ricarica minijack Minijack") or other context.
_RICARICA_RE = re.compile(
    rf"\b(Ricarica\s+(?:{_RICARICA_MODES_PATTERN}))\b",
    re.IGNORECASE,
)

# Paper code prefixes (go to scatola / doypack).
_PAPER_PREFIXES: frozenset[str] = frozenset({"PAP", "FR"})
# Plastic code prefixes (go to sacchetto).
_PLASTIC_PREFIXES: frozenset[str] = frozenset({"CPE", "PE", "HDPE", "LDPE", "PP", "PS", "PET"})

# Material code tokens that form disposal codes, e.g. "FR 7", "CPE 21", "PAP"
_MATERIAL_CODE_RE = re.compile(r"^(?:[A-Z]{2,3}\s*\d*|\d+)$")

# Known material token set for materiale extraction (case-insensitive matching).
_MATERIAL_TOKENS: list[str] = [
    "Silicone", "ABS", "PVC", "Metallo", "PU", "TPE", "TPR", "Plastica", "Acciaio",
]
_MATERIAL_TOKENS_LOWER: set[str] = {t.lower() for t in _MATERIAL_TOKENS}
# Canonical casing map: lowercase → display form.
_MATERIAL_CANONICAL: dict[str, str] = {t.lower(): t for t in _MATERIAL_TOKENS}

# Regex for splitting an OCR line into individual material token candidates.
# Splits on whitespace, slashes, commas, and dashes so that combined OCR lines
# like "Silicone/ABS" or "PVC / Metallo" are handled correctly.
_MATERIAL_TOKEN_SPLIT_RE: re.Pattern[str] = re.compile(r"[\s/,\-]+")

# Known product-type sequences, longest-first so "Vibratore rabbit" beats "Vibratore".
_PRODUCT_TYPES: list[str] = [
    "Vibratore rabbit",
    "Vibratore lay-on",
    "Kit Bondage",
    "Stimolatore",
    "Vibratore",
    "Manette",
    "Plug",
    "Anello",
]

# ---------------------------------------------------------------------------
# Compatibility shim — consumed by scripts/ocr_experiment.py which imports
# this list directly. The list maps field names to their primary regex for use
# in the experiment's per-field outcome table.  Not used by the production
# extraction path (which calls each regex inline in extract_text_fields).
# ---------------------------------------------------------------------------

_FIELD_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("dimensioni", _DIMENSIONI_RE),
    ("lotto", _LOTTO_RE),
    ("capacita_batteria_e_tensione_nominale", _BATTERY_RE),
    ("n_vibrazioni", _VIBRAZIONI_RE),
    ("impermeabilita", _IMPERMEABILITA_IPX_RE),
    ("modalita_di_ricarica", _RICARICA_RE),
]


# ---------------------------------------------------------------------------
# Field-level helper functions
# ---------------------------------------------------------------------------

def _normalise_dimensioni(raw: str) -> str:
    """
    Normalise a raw dimension capture to canonical form "25cm x Ø3cm".

    OCR sometimes renders Ø as "0" or "O". We detect this by checking whether
    the character before the second digit group is Ø or not, and correct it.
    """
    # Collapse spaces and normalise separator
    normalised = re.sub(r"\s+", " ", raw).strip()

    # Replace "x 0 N" or "x O N" (OCR artifact) → "x ØN"
    # Pattern: digits cm [space] x [space] (0|O) [space] digits cm
    normalised = re.sub(
        r"(\d+(?:[.,]\d+)?\s*cm)\s*x?\s*[0O]\s*(\d+(?:[.,]\d+)?\s*cm)",
        r"\1 x Ø\2",
        normalised,
        flags=re.IGNORECASE,
    )

    # If Ø is already present but spacing is off, clean it up.
    normalised = re.sub(
        r"(\d+(?:[.,]\d+)?\s*cm)\s*x\s*Ø\s*(\d+(?:[.,]\d+)?\s*cm)",
        r"\1 x Ø\2",
        normalised,
        flags=re.IGNORECASE,
    )

    return normalised


def _extract_materiale(full_text: str) -> str | None:
    """
    Locate consecutive known material tokens in the OCR text and join with '/'.

    Searches line by line, splitting each line on whitespace and punctuation
    (slash, comma, dash) so that OCR lines like "Silicone/ABS" or
    "PVC / Metallo" are handled correctly.  Collects adjacent matches
    (within 3 lines of each other) into a single material string.
    Returns canonical casing e.g. "Silicone/ABS", "PVC/Metallo".
    Returns None if no known tokens found.
    """
    lines = full_text.splitlines()
    found: list[tuple[int, str]] = []  # (line_index, canonical_token)

    for idx, line in enumerate(lines):
        # Tokenise the line to handle combined forms like "Silicone/ABS".
        raw_tokens = _MATERIAL_TOKEN_SPLIT_RE.split(line.strip())
        for raw_tok in raw_tokens:
            cleaned = raw_tok.strip().lower()
            if cleaned and cleaned in _MATERIAL_TOKENS_LOWER:
                found.append((idx, _MATERIAL_CANONICAL[cleaned]))

    if not found:
        return None

    # Group tokens that appear within 3 lines of each other.
    groups: list[list[str]] = []
    current_group: list[str] = [found[0][1]]
    prev_idx = found[0][0]

    for line_idx, token in found[1:]:
        if line_idx - prev_idx <= 3:
            current_group.append(token)
        else:
            groups.append(current_group)
            current_group = [token]
        prev_idx = line_idx

    groups.append(current_group)

    # Return the largest group (most tokens), then first occurrence.
    best_group = max(groups, key=len)
    # Dedup while preserving order — OCR often duplicates the material line
    # (Italian + bold variant rendered as two separate text regions).
    seen: set[str] = set()
    deduped: list[str] = []
    for tok in best_group:
        if tok not in seen:
            seen.add(tok)
            deduped.append(tok)
    return "/".join(deduped)


def _extract_tipo_o_modello(full_text: str) -> str | None:
    """
    Find the first known product-type sequence in the OCR text.

    Searches case-insensitively; returns the canonical casing from _PRODUCT_TYPES.
    Longest entries are checked first so "Vibratore rabbit" beats "Vibratore".
    """
    text_lower = full_text.lower()
    for product_type in _PRODUCT_TYPES:
        if product_type.lower() in text_lower:
            return product_type
    return None


def _extract_disposal_codes_from_text(full_text: str) -> dict[str, str]:
    """
    Extract material disposal codes from OCR text using a prefix-proximity strategy.

    Strategy (robust to OCR separating prefix and digits onto different lines):
    1. For each known prefix in ``_PAPER_PREFIXES | _PLASTIC_PREFIXES``, locate
       every occurrence in the text (whole-word, case-insensitive).
    2. For each prefix occurrence, look in the 80-character window immediately
       following it for a 1-2-digit number.  This tolerates a newline or a few
       spaces between prefix and digits but avoids false pairings with distant
       numbers elsewhere in the document.
    3. Categorise: paper prefix (PAP preferred over FR) → scatola,
       plastic prefix → sacchetto.
    4. Return canonical formatted codes like "PAP21" (no space, preserve digits
       as they appear — e.g. "07" not "7").

    Prefers PAP over FR for scatola because PAP is the primary packaging code
    used by this brand; FR is a secondary/alternative paper code and should
    only fill scatola when PAP is absent.
    """
    # Ordered preference for paper prefixes: PAP binds first, FR is fallback.
    _PAPER_PREFIX_RANK: dict[str, int] = {"PAP": 0, "FR": 1}

    # All known prefixes to scan for.
    all_known_prefixes: frozenset[str] = _PAPER_PREFIXES | _PLASTIC_PREFIXES

    # Window size (chars) after each prefix occurrence in which to look for digits.
    # Tight: only accept digits immediately adjacent to the prefix (e.g. "PAP21"
    # joined, or "PAP\n21" with single newline). RapidOCR on these packaging
    # PDFs rarely captures the small disposal-code digits at all — so when the
    # prefix appears alone we MUST NOT scan further and grab unrelated numbers
    # (lot, EAN). Conservative: digits must be within 5 chars of the prefix end.
    _WINDOW = 5

    candidates: list[tuple[str, str, int]] = []  # (prefix, formatted_code, rank)

    # Match prefix either as a standalone word (\b...\b) OR immediately followed by
    # digits (e.g. "PAP21" joined by OCR).  The two alternatives are ordered so
    # the joined form is tried first (longer match wins).
    all_prefixes_sorted = sorted(all_known_prefixes, key=len, reverse=True)
    prefix_re = re.compile(
        r"(?<![A-Za-z])("
        + "|".join(re.escape(p) for p in all_prefixes_sorted)
        + r")(?:\b|(?=\d))",
        re.IGNORECASE,
    )
    digit_re = re.compile(r"(\d{1,2})(?!\d)")

    # Regex for normalising common OCR artifact: isolated "O" or "o" that should
    # be digit "0".  We only substitute when the O is surrounded by digits or
    # positioned at the start of a numeric token, e.g. "O7" → "07".
    _ocr_o_to_zero = re.compile(r"\bO(\d)\b", re.IGNORECASE)

    for m_prefix in prefix_re.finditer(full_text):
        prefix = m_prefix.group(1).upper()
        # Look in the window after the prefix for the first 1-2-digit number.
        window_start = m_prefix.end()
        window_end = window_start + _WINDOW
        window_text = full_text[window_start:window_end]

        # Normalise OCR O/o → 0 before extracting digits.
        window_text = _ocr_o_to_zero.sub(r"0\1", window_text)

        m_digit = digit_re.search(window_text)
        if m_digit is None:
            continue  # no digit in window — skip

        digit_str = m_digit.group(1)
        formatted = f"{prefix}{digit_str}"  # e.g. "PAP21", "CPE07"
        rank = _PAPER_PREFIX_RANK.get(prefix, 99)
        candidates.append((prefix, formatted, rank))

    # Select the best paper candidate (lowest rank = highest preference).
    paper_candidates = sorted(
        [(rank, fmt) for (pfx, fmt, rank) in candidates if pfx in _PAPER_PREFIXES],
        key=lambda t: t[0],
    )
    plastic_candidates = [
        fmt for (pfx, fmt, _rank) in candidates if pfx in _PLASTIC_PREFIXES
    ]

    result: dict[str, str] = {}
    if paper_candidates:
        result["codice_smaltimento_scatola"] = paper_candidates[0][1]
    if plastic_candidates:
        result["codice_smaltimento_sacchetto"] = plastic_candidates[0]

    return result


# ---------------------------------------------------------------------------
# Spatial extraction helpers (pdfplumber path only)
# ---------------------------------------------------------------------------

def _extract_disposal_codes(page: object) -> list[str]:
    """
    Extract material disposal codes from the bottom band of the first page.

    On these packaging PDFs the disposal code tokens (e.g. "PAP", "21",
    "CPE", "FR", "7") appear as selectable text near the bottom of the page,
    typically within the last ~130 PDF points.  Tokens are matched against a
    material-code pattern and joined into individual code strings like
    "PAP 21", "CPE 07", "FR 7".

    Returns a list of code strings (may be empty).
    Only used in the pdfplumber extraction path.
    """
    words = page.extract_words()  # type: ignore[attr-defined]
    page_height: float = float(page.height)  # type: ignore[attr-defined]
    bottom_threshold = page_height - 130

    bottom_words: list[str] = [
        w["text"] for w in words if float(w["top"]) >= bottom_threshold
    ]

    # Collect consecutive material-code tokens
    tokens: list[str] = []
    for word in bottom_words:
        if _MATERIAL_CODE_RE.match(word.strip()):
            tokens.append(word.strip())

    if not tokens:
        return []

    # Pair alphabetic prefix with following numeric token when adjacent
    # e.g. ["FR", "7", "CPE", "21", "PAP"] → ["FR 7", "CPE 21", "PAP"]
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

    return codes


def _assign_disposal_codes(pack: PackData, codes: list[str]) -> None:
    """
    Split a list of disposal code strings into the three per-material fields.

    Heuristic:
    - First paper-prefixed code (PAP*, FR*) → codice_smaltimento_scatola.
    - First plastic-prefixed code (CPE*, PE*, etc.) → codice_smaltimento_sacchetto.
    - Any third code that doesn't fit the first two buckets → codice_smaltimento_doypack.

    If only one code is found it always goes to scatola.
    Only used in the pdfplumber extraction path.
    """
    scatola: str | None = None
    sacchetto: str | None = None
    doypack: str | None = None
    extras: list[str] = []

    for code in codes:
        prefix = code.split()[0].upper()
        if scatola is None and prefix in _PAPER_PREFIXES:
            scatola = code
        elif sacchetto is None and prefix in _PLASTIC_PREFIXES:
            sacchetto = code
        else:
            extras.append(code)

    # Fallback: if no categorised scatola but codes exist, put first code there.
    if scatola is None and codes:
        scatola = codes[0]
        remaining = codes[1:]
    else:
        remaining = extras

    # Third code (rare doypack) — only set when a remaining code exists.
    if doypack is None and remaining:
        doypack = remaining[0]

    if scatola is not None:
        pack.codice_smaltimento_scatola = ExtractedField(value=scatola, confidence=0.85)
    if sacchetto is not None:
        pack.codice_smaltimento_sacchetto = ExtractedField(value=sacchetto, confidence=0.85)
    if doypack is not None:
        pack.codice_smaltimento_doypack = ExtractedField(value=doypack, confidence=0.85)


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_text_fields(pdf_path: Path, *, source: str = "ocr") -> PackData:
    """
    Run the full text-extraction pipeline on a single PDF.

    1. Parse deterministic fields from the filename.
    2. Obtain full text via *source* (``"ocr"`` renders and OCRs; ``"pdfplumber"``
       uses selectable text).
    3. Apply regex and code-level rules to populate text_in_pdf fields.
    4. Return a PackData with all available fields set; missing fields
       retain the default ExtractedField(value=None, confidence=0.0).

    Args:
        pdf_path: Path to the PDF file.
        source: ``"ocr"`` (default) or ``"pdfplumber"``.

    Raises:
        ValueError: If *source* is not one of the accepted values.
    """
    if source not in ("ocr", "pdfplumber"):
        raise ValueError(f"Invalid source '{source}'. Must be 'ocr' or 'pdfplumber'.")

    ean, _dimensions, _product_name = parse_filename(pdf_path.name)
    logger.info(
        "Parsed filename | ean={} | dimensions={} | source={}", ean, _dimensions, source
    )

    pack = PackData(codice_ean=ean)

    # ------------------------------------------------------------------
    # Obtain full text
    # ------------------------------------------------------------------
    if source == "ocr":
        from src.ocr import extract_ocr_text  # local import to defer cold start
        full_text = extract_ocr_text(pdf_path)
        logger.info("OCR text obtained | ean={} | chars={}", ean, len(full_text))
    else:
        import pdfplumber  # local import keeps the module import-time cost minimal

        full_text_parts: list[str] = []
        disposal_codes: list[str] = []

        with pdfplumber.open(pdf_path) as pdf:
            for page_index, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                full_text_parts.append(page_text)

                if page_index == 0:
                    disposal_codes = _extract_disposal_codes(page)

        full_text = "\n".join(full_text_parts)
        logger.info(
            "pdfplumber text obtained | ean={} | chars={}", ean, len(full_text)
        )

    # ------------------------------------------------------------------
    # Apply regex and code-level rules
    # ------------------------------------------------------------------
    fields_found: int = 1  # codice_ean is deterministic

    # --- dimensioni ---
    m = _DIMENSIONI_RE.search(full_text)
    if m:
        raw = m.group(1).strip()
        value = _normalise_dimensioni(raw)
        pack.dimensioni = ExtractedField(
            value=value, confidence=0.9, evidence=raw
        )
        fields_found += 1

    # --- lotto ---
    m = _LOTTO_RE.search(full_text)
    if m:
        pack.lotto = ExtractedField(
            value=m.group(1).strip(), confidence=0.9, evidence=m.group(0).strip()
        )
        fields_found += 1

    # --- capacita_batteria_e_tensione_nominale ---
    m = _BATTERY_RE.search(full_text)
    if m:
        mah = m.group(1).replace(",", ".")
        volt = m.group(2).replace(",", ".")
        value = f"{mah}mAh / {volt}V"
        pack.capacita_batteria_e_tensione_nominale = ExtractedField(
            value=value, confidence=0.9, evidence=m.group(0).strip()
        )
        fields_found += 1

    # --- n_vibrazioni ---
    m = _VIBRAZIONI_RE.search(full_text)
    if m:
        pack.n_vibrazioni = ExtractedField(
            value=m.group(1).strip(), confidence=0.9, evidence=m.group(0).strip()
        )
        fields_found += 1

    # --- impermeabilita ---
    m = _IMPERMEABILITA_IPX_RE.search(full_text)
    if m:
        pack.impermeabilita = ExtractedField(
            value=m.group(1).strip(), confidence=0.9, evidence=m.group(0).strip()
        )
        fields_found += 1
    else:
        m = _IMPERMEABILITA_NON_RE.search(full_text)
        if m:
            pack.impermeabilita = ExtractedField(
                value="Non Impermeabile",
                confidence=0.85,
                evidence=m.group(0).strip(),
            )
            fields_found += 1

    # --- modalita_di_ricarica ---
    m = _RICARICA_RE.search(full_text)
    if m:
        value = m.group(1).strip()
        # Exclude degenerate matches where captured value is just "Ricarica"
        if len(value.split()) >= 2:
            pack.modalita_di_ricarica = ExtractedField(
                value=value, confidence=0.85, evidence=m.group(0).strip()
            )
            fields_found += 1

    # --- materiale (code-level, not a single regex) ---
    mat = _extract_materiale(full_text)
    if mat:
        pack.materiale = ExtractedField(value=mat, confidence=0.85, evidence=mat)
        fields_found += 1

    # --- tipo_o_modello (code-level) ---
    tipo = _extract_tipo_o_modello(full_text)
    if tipo:
        pack.tipo_o_modello = ExtractedField(
            value=tipo, confidence=0.85, evidence=tipo
        )
        fields_found += 1

    # ------------------------------------------------------------------
    # Presence checks: substring markers in text.
    # ------------------------------------------------------------------
    text_lower = full_text.lower()

    if "sexy ideas" in text_lower:
        pack.sexy_ideas = PresenceField(
            present=True,
            confidence=0.85,
            evidence="found 'sexy ideas' in text",
        )
        fields_found += 1

    if "garanzia 2 anni" in text_lower or "2 years warranty" in text_lower:
        pack.simbolo_garanzia_2_anni = PresenceField(
            present=True,
            confidence=0.85,
            evidence="found warranty text in OCR output",
        )
        fields_found += 1

    if "libretto" in text_lower:
        pack.simbolo_libretto_informativo = PresenceField(
            present=True,
            confidence=0.7,
            evidence="found 'libretto' in text",
        )
        fields_found += 1

    if "junker" in text_lower:
        pack.qr_code_junker = PresenceField(
            present=True,
            confidence=0.6,
            evidence="found 'junker' in text",
        )
        fields_found += 1

    # ------------------------------------------------------------------
    # Disposal codes
    # ------------------------------------------------------------------
    if source == "ocr":
        disposal_map = _extract_disposal_codes_from_text(full_text)
        if disposal_map:
            for field_name, code_value in disposal_map.items():
                setattr(pack, field_name, ExtractedField(value=code_value, confidence=0.85))
            fields_found += 1
    else:
        # pdfplumber path: disposal_codes was populated above.
        if disposal_codes:
            _assign_disposal_codes(pack, disposal_codes)
            fields_found += 1

    logger.info(
        "Text extraction complete | ean={} | fields_found={}", ean, fields_found
    )

    return pack
