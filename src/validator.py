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
    codici_smaltimento_materiali (text).
- Compute a final per-field confidence and a scalar overall_confidence.
- Mark packs below REVIEW_THRESHOLD for human review with a structured
  list of flagged fields and reasons.
- Pure functions only — no I/O, no network, fully unit-testable.

Not responsible for:
- Writing to the Sheet (delegated to sheets.py).
- Calling the VLM or text parser (delegated to their modules).
"""

from dataclasses import dataclass

from src.schemas.pack import PackData


REVIEW_THRESHOLD = 0.75  # packs below this overall confidence go to review_queue


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
    raise NotImplementedError("Session 4 deliverable")


def _merge_field(parser_value, vlm_value):
    """
    Choose between two ExtractedField or PresenceField instances.
    Higher confidence wins; ties go to the parser (deterministic preferred).
    If values disagree and both confidence >= 0.5, record a conflict note.
    """
    raise NotImplementedError("Session 4 deliverable")


def _check_triman_consistency(pack: PackData) -> bool | None:
    """
    Compare material codes from text extraction against VLM-described
    recycling icons. Returns True (match), False (mismatch), or None
    (insufficient data to decide).
    """
    raise NotImplementedError("Session 4 deliverable")


def _required_symbols_present(pack: PackData) -> list[str]:
    """
    Return a list of field names for required symbols that are absent
    or have confidence below 0.5.
    """
    raise NotImplementedError("Session 4 deliverable")
