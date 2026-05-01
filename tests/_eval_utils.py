"""Shared utilities for the deterministic extraction evaluation harness."""

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any

from src.parsing import extract_text_fields
from src.schemas.pack import ExtractedField, PackData, PresenceField
from src.validator import validate

# ---------------------------------------------------------------------------
# PDF → golden mapping (matched by EAN prefix in filename)
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_SAMPLES_DIR = Path(__file__).parent.parent / "samples"

PDF_TO_GOLDEN: dict[str, Path] = {
    "8055712771306_220x80x45_Thomas Turbato.pdf":       _FIXTURES_DIR / "8055712771306_thomas_turbato.json",
    "8055712772860_180x80x50_OSA_Fairy-Handcuffs.pdf":  _FIXTURES_DIR / "8055712772860_fairy_handcuffs.json",
    "8055712772907_150x45x180_OSA_Gothic-Love.pdf":      _FIXTURES_DIR / "8055712772907_gothic_love.json",
}

# Sentinel returned when the field name does not exist on PackData.
NOT_IN_SCHEMA = object()


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------

class Outcome(str, Enum):
    CORRECT      = "CORRECT"
    WRONG        = "WRONG"
    MISSING      = "MISSING"       # expected value present, actual is None/empty
    UNEXPECTED   = "UNEXPECTED"    # expected empty/absent, actual has a value
    NOT_IN_SCHEMA = "NOT_IN_SCHEMA"


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def run_deterministic(pdf_path: Path) -> PackData:
    """Run parser → stub-VLM → validator, return the merged PackData."""
    parser_output = extract_text_fields(pdf_path)

    # Minimal stub so Pydantic validation passes (codice_ean is required).
    vlm_stub = PackData(
        codice_ean=parser_output.codice_ean,
        dimensioni=parser_output.dimensioni,
    )

    result = validate(parser_output, vlm_stub)
    return result.pack


def normalize(s: Any) -> Any:
    """Strip and collapse internal whitespace for strings; return other types as-is."""
    if isinstance(s, str):
        return re.sub(r"\s+", " ", s).strip()
    return s


def extract_actual(pack: PackData, field_name: str) -> Any:
    """
    Pull the comparable scalar from a PackData field.
    Returns NOT_IN_SCHEMA sentinel when the field does not exist on the model.
    """
    if field_name not in PackData.model_fields:
        return NOT_IN_SCHEMA

    val = getattr(pack, field_name)

    if isinstance(val, ExtractedField):
        return val.value
    if isinstance(val, PresenceField):
        return val.present
    # Plain str / bool / None (deterministic or derived).
    return val


def _is_empty(v: Any) -> bool:
    """True when a value represents absence: None, False, empty string, or "N/A"."""
    if v is None or v is False:
        return True
    if isinstance(v, str) and (v.strip() == "" or v.strip().upper() == "N/A"):
        return True
    return False


def compare_field(expected: Any, actual: Any) -> Outcome:
    """
    Determine the Outcome for a single field comparison.

    Handles:
    - Schema gap (actual is NOT_IN_SCHEMA sentinel).
    - "N/A" / null / False expected → treat as absence.
    - Boolean expected → exact compare.
    - String expected → normalize() then case-sensitive compare.
    """
    if actual is NOT_IN_SCHEMA:
        return Outcome.NOT_IN_SCHEMA

    expected_empty = _is_empty(expected)
    actual_empty   = _is_empty(actual)

    if expected_empty and actual_empty:
        return Outcome.CORRECT
    if not expected_empty and actual_empty:
        return Outcome.MISSING
    if expected_empty and not actual_empty:
        return Outcome.UNEXPECTED

    # Both non-empty — compare values.
    if isinstance(expected, bool):
        # PresenceField.present is bool; golden booleans compare exactly.
        return Outcome.CORRECT if actual == expected else Outcome.WRONG

    # Normalise both sides to strings for comparison.
    norm_expected = normalize(str(expected))
    norm_actual   = normalize(str(actual))
    return Outcome.CORRECT if norm_expected == norm_actual else Outcome.WRONG


# ---------------------------------------------------------------------------
# Per-PDF evaluation
# ---------------------------------------------------------------------------

def evaluate_pdf(pdf_path: Path, golden_path: Path) -> dict:
    """
    Run the deterministic pipeline on pdf_path, diff against golden_path.

    Returns:
        {
          "pdf": str,
          "fields": {field_name: {"expected": ..., "actual": ..., "outcome": Outcome}},
          "summary": {"correct": n, "wrong": n, "missing": n,
                      "unexpected": n, "not_in_schema": n, "accuracy": float},
        }
    """
    pack = run_deterministic(pdf_path)

    with golden_path.open(encoding="utf-8") as fh:
        golden: dict = json.load(fh)

    fields: dict[str, dict] = {}
    counts: dict[str, int] = {
        "correct": 0, "wrong": 0, "missing": 0, "unexpected": 0, "not_in_schema": 0
    }

    for key, expected in golden.items():
        if key.startswith("_"):   # metadata — skip
            continue

        actual  = extract_actual(pack, key)
        outcome = compare_field(expected, actual)

        fields[key] = {"expected": expected, "actual": actual, "outcome": outcome}
        counts[outcome.value.lower()] += 1

    # Accuracy excludes NOT_IN_SCHEMA from denominator (schema-evolution gaps).
    denominator = counts["correct"] + counts["wrong"] + counts["missing"] + counts["unexpected"]
    accuracy = counts["correct"] / denominator if denominator > 0 else 0.0

    return {
        "pdf":     pdf_path.name,
        "fields":  fields,
        "summary": {**counts, "accuracy": accuracy},
    }
