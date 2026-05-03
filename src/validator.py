"""
Cross-validation and confidence modelling for extracted pack data.

Responsibilities:
- Reconcile text-parser output vs VLM output for overlapping fields:
  keep the higher-confidence value, populate the evidence field with
  a conflict note when both sources disagree.
- Apply business rules:
  - Required regulatory symbols (CE, RAEE, TRIMAN) — flag if absent.
  - TRIMAN material consistency — compute contenuto_triman_corretto by
    comparing simboli_materiali_smaltimento (visual) against
    codice_smaltimento_scatola / _sacchetto / _doypack (text).
- Compute a final per-field confidence and a scalar overall_confidence.
- Mark packs below REVIEW_THRESHOLD for human review with a structured
  list of flagged fields and reasons.
- Pure functions only — no I/O, no network, fully unit-testable.

Not responsible for:
- Writing to the Sheet (delegated to sheets.py).
- Calling the VLM or text parser (delegated to their modules).
"""

import re
from dataclasses import dataclass

from src.schemas.pack import ExtractedField, PackData, PresenceField


REVIEW_THRESHOLD = 0.75  # packs below this overall confidence go to review_queue

# Symbols that must appear on every pack by EU regulation.
_REQUIRED_SYMBOL_FIELDS: list[str] = ["simbolo_ce", "simbolo_raee", "simbolo_triman"]

# Regex to extract material code prefixes (letters only, ignore numeric suffixes).
# Examples: "FR 7" → "FR", "CPE 21" → "CPE", "PAP" → "PAP"
_MATERIAL_CODE_RE = re.compile(r"\b([A-Z]{1,4})\b")


@dataclass
class ValidationResult:
    pack: PackData
    overall_confidence: float
    flagged_fields: list[str]
    needs_review: bool
    review_reasons: list[str]


def validate(parser_output: PackData, vlm_output: PackData) -> ValidationResult:
    """
    Merge parser and VLM outputs, apply business rules, return a
    validated PackData with a review flag.
    """
    # Build merged pack starting from parser deterministic fields.
    merged_data: dict = {}

    for field_name in PackData.model_fields:
        parser_val = getattr(parser_output, field_name)
        vlm_val = getattr(vlm_output, field_name)

        if isinstance(parser_val, (ExtractedField, PresenceField)):
            merged_data[field_name] = _merge_field(parser_val, vlm_val)
        else:
            # Deterministic (str): parser is authoritative.
            merged_data[field_name] = parser_val

    merged_pack = PackData(**merged_data)

    # Cheap recovery step: when a per-field disposal code is missing its
    # numeric suffix (e.g. just "CPE" because the digit inside the recycling
    # triangle was too small to read), back-fill it from the joined-list field
    # `simboli_materiali_smaltimento` if that field carries a fully-formed code
    # with the same letter prefix. Saves the ~1-cell-per-pack accuracy loss
    # without any extra API call.
    _backfill_disposal_digits_from_simboli(merged_pack)

    # Derived field: TRIMAN consistency requires merged visual + text data.
    merged_pack.contenuto_triman_corretto = _check_triman_consistency(merged_pack)

    # Business rule: required regulatory symbols.
    absent_symbols = _required_symbols_present(merged_pack)

    # Aggregate confidence from all envelope fields (Sheet fields only).
    confidences: list[float] = [
        val.confidence
        for field_name in merged_pack._SHEET_FIELDS
        for val in [getattr(merged_pack, field_name)]
        if isinstance(val, (ExtractedField, PresenceField))
    ]

    overall_confidence = sum(confidences) / len(confidences) if confidences else 0.0

    # Absent required symbols cap the overall score — the pack cannot be trusted.
    if absent_symbols:
        overall_confidence = min(overall_confidence, 0.5)

    # Collect low-confidence fields (excluding plain deterministic str fields).
    low_confidence_fields: list[str] = [
        field_name
        for field_name in merged_pack._SHEET_FIELDS
        for val in [getattr(merged_pack, field_name)]
        if isinstance(val, (ExtractedField, PresenceField)) and val.confidence < 0.5
    ]

    flagged_fields: list[str] = list(dict.fromkeys(low_confidence_fields + absent_symbols))

    review_reasons: list[str] = []
    for field_name in low_confidence_fields:
        val = getattr(merged_pack, field_name)
        review_reasons.append(
            f"Low confidence ({val.confidence:.2f}) on field '{field_name}'"
        )
    for field_name in absent_symbols:
        review_reasons.append(
            f"Required symbol absent or uncertain: '{field_name}'"
        )

    needs_review = overall_confidence < REVIEW_THRESHOLD or bool(absent_symbols)

    return ValidationResult(
        pack=merged_pack,
        overall_confidence=overall_confidence,
        flagged_fields=flagged_fields,
        needs_review=needs_review,
        review_reasons=review_reasons,
    )


def _merge_field(
    parser_value: ExtractedField | PresenceField | None,
    vlm_value: ExtractedField | PresenceField | None,
) -> ExtractedField | PresenceField:
    """
    Choose between two ExtractedField or PresenceField instances.
    Higher confidence wins; ties go to the parser (deterministic preferred).
    If values disagree and both confidence >= 0.5, record a conflict note.
    """
    # Determine whether either side is effectively empty.
    def _has_data(field: ExtractedField | PresenceField | None) -> bool:
        if field is None:
            return False
        if isinstance(field, ExtractedField):
            return field.value is not None
        return field.present is not None  # PresenceField

    parser_has = _has_data(parser_value)
    vlm_has = _has_data(vlm_value)

    if parser_has and not vlm_has:
        return parser_value.model_copy()
    if vlm_has and not parser_has:
        return vlm_value.model_copy()
    if not parser_has and not vlm_has:
        # Both empty — return a fresh default of the correct type.
        if isinstance(parser_value, PresenceField) or isinstance(vlm_value, PresenceField):
            return PresenceField()
        return ExtractedField()

    # Both have data — pick the higher-confidence source; ties go to parser.
    parser_conf = parser_value.confidence
    vlm_conf = vlm_value.confidence

    winner, loser = (
        (parser_value, vlm_value) if parser_conf >= vlm_conf else (vlm_value, parser_value)
    )

    # Extract the actual value from each source for comparison.
    def _get_value(field: ExtractedField | PresenceField) -> str | bool | None:
        if isinstance(field, ExtractedField):
            return field.value
        return field.present

    winner_val = _get_value(winner)
    loser_val = _get_value(loser)
    values_agree = winner_val == loser_val

    if not values_agree and parser_conf >= 0.7 and vlm_conf >= 0.7:
        # Conflict: both confident but disagree — penalise and annotate.
        new_confidence = max(0.0, winner.confidence - 0.1)
        conflict_note = f" [CONFLICT: other source said: {loser_val}]"
        new_evidence = (winner.evidence or "") + conflict_note

        return winner.model_copy(
            update={"confidence": new_confidence, "evidence": new_evidence}
        )

    return winner.model_copy()


def _collect_code_prefixes(raw: str | None) -> set[str]:
    """Extract uppercase letter-only code prefixes from a raw code string."""
    if raw is None:
        return set()
    return {m.group(1) for m in _MATERIAL_CODE_RE.finditer(raw.upper())}


# Pattern used by the back-fill: letters (with optional internal "/" e.g. C/PAP),
# optional whitespace, then 1-2 digits. Captures both halves.
_FULL_DISPOSAL_CODE_RE = re.compile(r"\b([A-Z]{1,5}(?:/[A-Z]{1,5})?)\s*0*(\d{1,2})\b")
# Pattern matching a "bare prefix" — letters only, no digit anywhere.
_BARE_PREFIX_RE = re.compile(r"^\s*([A-Z]{1,5}(?:/[A-Z]{1,5})?)\s*$")


def _backfill_disposal_digits_from_simboli(pack: PackData) -> None:
    """
    For each per-field disposal code (scatola / sacchetto / doypack), if the
    extracted value is empty or a bare letter prefix without digits, look up
    a matching fully-formed code in `simboli_materiali_smaltimento` and patch
    the per-field value in place.

    Example:
        simboli_materiali_smaltimento.value = "PAP21 / CPE07"
        codice_smaltimento_sacchetto.value  = "CPE"      ->  patched to "CPE07"
        codice_smaltimento_scatola.value    = "PAP21"    ->  unchanged

    Mutates `pack` in place. Adds a short note to the patched field's evidence.
    """
    simboli_raw = pack.simboli_materiali_smaltimento.value
    if not simboli_raw:
        return

    # Build {prefix_upper: full_code_canonical} from the joined-list field.
    prefix_to_full: dict[str, str] = {}
    for m in _FULL_DISPOSAL_CODE_RE.finditer(simboli_raw.upper()):
        prefix, digits = m.group(1), m.group(2)
        prefix_to_full.setdefault(prefix, f"{prefix}{int(digits):02d}")

    if not prefix_to_full:
        return

    for field_name in (
        "codice_smaltimento_scatola",
        "codice_smaltimento_sacchetto",
        "codice_smaltimento_doypack",
    ):
        field: ExtractedField = getattr(pack, field_name)
        current = (field.value or "").strip().upper()

        # Already a full code? Skip.
        if current and _FULL_DISPOSAL_CODE_RE.fullmatch(current):
            continue

        # Bare prefix or empty — try to back-fill.
        bare_match = _BARE_PREFIX_RE.match(current) if current else None
        candidate_full: str | None = None

        if bare_match:
            prefix = bare_match.group(1)
            candidate_full = prefix_to_full.get(prefix)
        else:
            # Field empty: try every prefix from simboli; only patch if
            # exactly one is plausible for this field-type. Keep it
            # conservative — don't guess between multiple prefixes.
            paper_prefixes = {"PAP", "FR", "C/PAP"}
            plastic_prefixes = {"CPE", "LDPE", "HDPE", "PE", "PP", "PS", "PET"}
            candidates: set[str]
            if field_name == "codice_smaltimento_scatola":
                candidates = {p for p in prefix_to_full if p in paper_prefixes}
            elif field_name == "codice_smaltimento_sacchetto":
                candidates = {p for p in prefix_to_full if p in plastic_prefixes}
            else:
                candidates = set()
            if len(candidates) == 1:
                candidate_full = prefix_to_full[next(iter(candidates))]

        if candidate_full and candidate_full != current:
            note = f" [back-filled from simboli: {candidate_full}]"
            field.value = candidate_full
            field.evidence = (field.evidence or "") + note
            # Don't bump confidence above the source field; reuse simboli's
            # confidence as a proxy, but cap at 0.85 so a confident later
            # source can still overrule.
            field.confidence = min(0.85, max(field.confidence, pack.simboli_materiali_smaltimento.confidence))


def _check_triman_consistency(pack: PackData) -> ExtractedField:
    """
    Compare material codes from per-field text extraction against
    VLM-described recycling icons.

    Returns an ExtractedField where:
    - value = human-readable description of matched materials
      (e.g. "scatola + sacchetto", "scatola", "scatola + sacchetto + doypack")
      when the text codes and visual codes agree.
    - value = None with confidence=0.2 when codes disagree.
    - value = None with confidence=0.0 (default) when data is insufficient.
    """
    visual_raw = pack.simboli_materiali_smaltimento.value
    if visual_raw is None:
        # Cannot verify without visual data.
        return ExtractedField()

    # Collect codes present in individual disposal fields.
    scatola_val = pack.codice_smaltimento_scatola.value
    sacchetto_val = pack.codice_smaltimento_sacchetto.value
    doypack_val = pack.codice_smaltimento_doypack.value

    text_codes: set[str] = set()
    label_parts: list[str] = []

    if scatola_val is not None:
        text_codes |= _collect_code_prefixes(scatola_val)
        label_parts.append("scatola")
    if sacchetto_val is not None:
        text_codes |= _collect_code_prefixes(sacchetto_val)
        label_parts.append("sacchetto")
    if doypack_val is not None:
        text_codes |= _collect_code_prefixes(doypack_val)
        label_parts.append("doypack")

    if not text_codes:
        # No text disposal codes found — cannot verify.
        return ExtractedField()

    visual_codes = _collect_code_prefixes(visual_raw)

    if text_codes == visual_codes:
        label = " + ".join(label_parts) if label_parts else "scatola"
        return ExtractedField(value=label, confidence=0.9, evidence="codes match visual icons")

    # Mismatch — flag for review with a low confidence.
    return ExtractedField(
        value=None,
        confidence=0.2,
        evidence=(
            f"Code mismatch: text prefixes={sorted(text_codes)} "
            f"vs visual prefixes={sorted(visual_codes)}"
        ),
    )


def _required_symbols_present(pack: PackData) -> list[str]:
    """
    Return a list of field names for required symbols that are absent
    or have confidence below 0.5.
    """
    absent: list[str] = []
    for field_name in _REQUIRED_SYMBOL_FIELDS:
        symbol: PresenceField = getattr(pack, field_name)
        if symbol.present is False:
            absent.append(field_name)
        elif symbol.present is None and symbol.confidence < 0.5:
            # Cannot confirm presence and low confidence — treat as uncertain/absent.
            absent.append(field_name)
    return absent
