"""
Text extraction from packaging PDFs.

Responsibilities:
- Parse EAN, dimensions, and product name from the filename.
- Extract text from the PDF via OCR and apply regex / spatial rules to populate
  all text_in_pdf and deterministic fields. The supplied PDFs are vector with
  text converted to outlines (Adobe Illustrator "Crea contorni"), so
  pdfplumber.extract_text returns ~10 chars/page; OCR is the only viable source.
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


def _extract_disposal_codes_from_ocr(text: str) -> dict[str, ExtractedField]:
    """
    Scan OCR text for recycling-triangle codes (e.g. "PAP 21", "CPE 7"),
    canonicalise to letters + 2-digit zero-padded number, and assign them
    to the box / bag / doypack disposal fields based on material type.

    Strategy — line-cluster approach:
    1. Scan lines for those that are exclusively a known material prefix
       (e.g. a line containing only "PAP" or "CPE" after stripping whitespace).
    2. For each such prefix line, look at the immediately following 2 lines
       for a token that is 1-2 digits only.  If found, pair them.
       The digit search is confined to "pure digit" lines to avoid grabbing
       EAN codes, lot numbers, or other numeric context.
    3. Additionally detect "joined" forms such as "PAP21" or "CPE07" that OCR
       may produce when the label and digit are rendered in a single text block.
    4. Assign by material type:
       - PAP, FR  → codice_smaltimento_scatola (PAP preferred over FR).
       - CPE, LDPE, HDPE, PE, PP, PS, PET → codice_smaltimento_sacchetto.
       - A third plastic code, when present alongside a sacchetto code →
         codice_smaltimento_doypack (conservative: only set for 3+ codes).
    5. Populate ``simboli_materiali_smaltimento`` with ALL found codes joined
       by " / " in the order they appear in the OCR text.

    Returns a dict with any subset of:
        codice_smaltimento_scatola, codice_smaltimento_sacchetto,
        codice_smaltimento_doypack, simboli_materiali_smaltimento
    populated as ExtractedField envelopes.  Confidence is 0.7 for all
    parser-extracted disposal codes (high enough to beat a VLM response
    lacking digits; low enough to yield to a confident VLM match).
    """
    _CONF = 0.7
    _PAPER_PREFIX_RANK: dict[str, int] = {"PAP": 0, "FR": 1, "C/PAP": 2}
    all_known_prefixes: frozenset[str] = _PAPER_PREFIXES | _PLASTIC_PREFIXES

    # Pre-build a fast lookup set (uppercase) for line-exact prefix matching.
    _prefix_upper: frozenset[str] = frozenset(p.upper() for p in all_known_prefixes)

    # Regex: joined form "PAP21", "CPE07", "C/PAP21" — prefix immediately
    # followed by 1-2 digits with no whitespace between them.
    _joined_re = re.compile(
        r"(?<![A-Za-z/])("
        + "|".join(re.escape(p) for p in sorted(all_known_prefixes, key=len, reverse=True))
        + r")(\d{1,2})(?!\d)",
        re.IGNORECASE,
    )
    # Regex: standalone digit line — entire stripped line is 1-2 digits.
    _pure_digit_re = re.compile(r"^\s*(\d{1,2})\s*$")

    lines = text.splitlines()
    # Ordered list of (prefix_upper, digit_str_or_None) found in scan order.
    raw_codes: list[tuple[str, str | None]] = []

    # --- Pass 1: joined forms anywhere in the text (e.g. "PAP21" in one OCR block).
    seen_joins: set[str] = set()
    for m in _joined_re.finditer(text):
        prefix = m.group(1).upper()
        digit_str = m.group(2)
        key = f"{prefix}{digit_str}"
        if key not in seen_joins:
            seen_joins.add(key)
            raw_codes.append((prefix, digit_str))

    # --- Pass 2: line-by-line cluster scan for split prefix / digit pairs.
    # Only run when pass 1 did not already produce a code for this prefix.
    joined_prefixes: set[str] = {pfx for pfx, _ in raw_codes}
    for i, line in enumerate(lines):
        stripped = line.strip().upper()
        if stripped not in _prefix_upper:
            continue
        prefix = stripped
        if prefix in joined_prefixes:
            continue  # already have a paired code from pass 1

        # Look ahead up to 2 lines for a pure-digit line.
        digit_str: str | None = None
        for lookahead in range(1, 3):
            if i + lookahead >= len(lines):
                break
            m_digit = _pure_digit_re.match(lines[i + lookahead])
            if m_digit:
                digit_str = m_digit.group(1)
                break

        raw_codes.append((prefix, digit_str))

    if not raw_codes:
        return {}

    # Canonicalise: letters + 2-digit zero-padded number.
    # If digit is absent, the code value stays None (no guessing).
    canonical_codes: list[tuple[str, str | None]] = []
    for prefix, digit_str in raw_codes:
        if digit_str is not None:
            formatted: str | None = f"{prefix}{int(digit_str):02d}"
        else:
            formatted = None
        canonical_codes.append((prefix, formatted))

    # --- Assignment ---
    scatola_candidates: list[tuple[int, str]] = []   # (rank, formatted)
    sacchetto_candidates: list[str] = []
    extra_plastic_candidates: list[str] = []

    for prefix, formatted in canonical_codes:
        if formatted is None:
            continue  # only assign fields when we have an actual code
        if prefix in _PAPER_PREFIXES:
            rank = _PAPER_PREFIX_RANK.get(prefix, 99)
            scatola_candidates.append((rank, formatted))
        elif prefix in _PLASTIC_PREFIXES:
            sacchetto_candidates.append(formatted)

    scatola_candidates.sort(key=lambda t: t[0])

    result: dict[str, ExtractedField] = {}
    if scatola_candidates:
        result["codice_smaltimento_scatola"] = ExtractedField(
            value=scatola_candidates[0][1],
            confidence=_CONF,
            evidence="disposal-code cluster in OCR text",
        )
    if sacchetto_candidates:
        result["codice_smaltimento_sacchetto"] = ExtractedField(
            value=sacchetto_candidates[0],
            confidence=_CONF,
            evidence="disposal-code cluster in OCR text",
        )
        # Third code only when there is a clear second plastic and 3+ total codes.
        if len(sacchetto_candidates) >= 2 and len(canonical_codes) >= 3:
            result["codice_smaltimento_doypack"] = ExtractedField(
                value=sacchetto_candidates[1],
                confidence=_CONF,
                evidence="disposal-code cluster in OCR text (third code)",
            )

    # simboli_materiali_smaltimento: all codes with known formatted values,
    # in scan order, joined by " / ".
    all_formatted = [fmt for _, fmt in canonical_codes if fmt is not None]
    # Deduplicate while preserving order.
    seen_fmts: set[str] = set()
    ordered_fmts: list[str] = []
    for fmt in all_formatted:
        if fmt not in seen_fmts:
            seen_fmts.add(fmt)
            ordered_fmts.append(fmt)
    if ordered_fmts:
        result["simboli_materiali_smaltimento"] = ExtractedField(
            value=" / ".join(ordered_fmts),
            confidence=_CONF,
            evidence="disposal-code cluster in OCR text",
        )

    return result


def _extract_disposal_codes_from_text(text: str) -> dict[str, str]:
    """Thin wrapper kept for backward compatibility with ``_FIELD_PATTERNS`` consumers.

    Delegates to ``_extract_disposal_codes_from_ocr`` and strips the
    ``ExtractedField`` envelope, returning plain ``str`` values keyed by
    field name.  Only ``codice_smaltimento_scatola`` and
    ``codice_smaltimento_sacchetto`` are exposed (the wrapper predates
    the doypack / simboli fields).
    """
    ocr_result = _extract_disposal_codes_from_ocr(text)
    return {
        k: v.value
        for k, v in ocr_result.items()
        if k in ("codice_smaltimento_scatola", "codice_smaltimento_sacchetto")
        and v.value is not None
    }


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------

def extract_text_fields(pdf_path: Path) -> PackData:
    """
    Run the full text-extraction pipeline on a single PDF.

    1. Parse deterministic fields from the filename.
    2. OCR the PDF (renders all pages with pypdfium2, runs RapidOCR).
    3. Apply regex and code-level rules to populate text_in_pdf fields.
    4. Return a PackData with all available fields set; missing fields
       retain the default ExtractedField(value=None, confidence=0.0).
    """
    ean, _dimensions, _product_name = parse_filename(pdf_path.name)
    logger.info("Parsed filename | ean={} | dimensions={}", ean, _dimensions)

    pack = PackData(codice_ean=ean)

    from src.ocr import extract_ocr_text  # local import to defer cold start
    full_text = extract_ocr_text(pdf_path)
    logger.info("OCR text obtained | ean={} | chars={}", ean, len(full_text))

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
    # Disposal codes (from OCR text)
    # ------------------------------------------------------------------
    disposal_map = _extract_disposal_codes_from_ocr(full_text)
    for field_name, extracted_field in disposal_map.items():
        current: ExtractedField = getattr(pack, field_name, ExtractedField())
        if current.confidence < extracted_field.confidence:
            setattr(pack, field_name, extracted_field)
    if disposal_map:
        fields_found += 1

    logger.info(
        "Text extraction complete | ean={} | fields_found={}", ean, fields_found
    )

    return pack
