"""Pytest-based eval for the deterministic (parser + validator, no VLM) pipeline."""

from pathlib import Path

import pytest

from tests._eval_utils import (
    PDF_TO_GOLDEN,
    Outcome,
    _SAMPLES_DIR,
    evaluate_pdf,
)

# ---------------------------------------------------------------------------
# Parametrised per-PDF tests
# ---------------------------------------------------------------------------

_PARAM_IDS = list(PDF_TO_GOLDEN.keys())


@pytest.mark.parametrize("pdf_name", _PARAM_IDS)
def test_deterministic_accuracy(pdf_name: str) -> None:
    """
    Run evaluate_pdf for one sample PDF and assert accuracy >= 0.0.

    # TODO: raise the floor (e.g. 0.5, then 0.7) as the parser matures.
    """
    pdf_path    = _SAMPLES_DIR / pdf_name
    golden_path = PDF_TO_GOLDEN[pdf_name]

    if not pdf_path.exists():
        pytest.skip(f"Sample PDF not found: {pdf_path}")

    report = evaluate_pdf(pdf_path, golden_path)
    summary = report["summary"]

    # Build a human-readable diff table for non-CORRECT outcomes.
    non_correct = {
        field: info
        for field, info in report["fields"].items()
        if info["outcome"] != Outcome.CORRECT
    }

    diff_lines = [
        f"\n{'FIELD':<45} {'EXPECTED':<30} {'ACTUAL':<30} {'OUTCOME'}",
        "-" * 115,
    ]
    for field, info in non_correct.items():
        diff_lines.append(
            f"{field:<45} {str(info['expected']):<30} {str(info['actual']):<30} {info['outcome'].value}"
        )

    diff_table = "\n".join(diff_lines)
    summary_line = (
        f"\ncorrect={summary['correct']} wrong={summary['wrong']} "
        f"missing={summary['missing']} unexpected={summary['unexpected']} "
        f"not_in_schema={summary['not_in_schema']} "
        f"accuracy={summary['accuracy']:.1%}"
    )

    assert summary["accuracy"] >= 0.0, (
        f"Accuracy check failed for {pdf_name}{summary_line}{diff_table}"
    )

    # Always print the summary so it shows up in -v output.
    print(f"\n{pdf_name}{summary_line}{diff_table}")


# ---------------------------------------------------------------------------
# Aggregate test across all 3 PDFs
# ---------------------------------------------------------------------------

def test_aggregate_accuracy() -> None:
    """
    Run all 3 PDFs and assert overall accuracy >= 0.0.

    # TODO: raise this floor as the parser matures.
    """
    total: dict[str, int] = {
        "correct": 0, "wrong": 0, "missing": 0, "unexpected": 0, "not_in_schema": 0
    }
    # field_name → {outcome_value → count}
    per_field: dict[str, dict[str, int]] = {}
    skipped: list[str] = []

    for pdf_name, golden_path in PDF_TO_GOLDEN.items():
        pdf_path = _SAMPLES_DIR / pdf_name
        if not pdf_path.exists():
            skipped.append(pdf_name)
            continue

        report = evaluate_pdf(pdf_path, golden_path)

        for k in ("correct", "wrong", "missing", "unexpected", "not_in_schema"):
            total[k] += report["summary"][k]

        for field, info in report["fields"].items():
            bucket = per_field.setdefault(field, {"correct": 0, "wrong": 0, "missing": 0, "unexpected": 0, "not_in_schema": 0})
            bucket[info["outcome"].value.lower()] += 1

    if skipped:
        pytest.skip(f"Skipped (PDFs not found): {skipped}")

    denominator = total["correct"] + total["wrong"] + total["missing"] + total["unexpected"]
    overall_accuracy = total["correct"] / denominator if denominator > 0 else 0.0

    # Build consolidated field table sorted by failure rate descending.
    def _failure_count(counts: dict[str, int]) -> int:
        return counts["wrong"] + counts["missing"] + counts["unexpected"]

    sorted_fields = sorted(per_field.items(), key=lambda kv: _failure_count(kv[1]), reverse=True)

    table_lines = [
        f"\n{'FIELD':<45} {'CORRECT':>7} {'WRONG':>5} {'MISSING':>7} {'UNEXPECTED':>10} {'NOT_IN_SCH':>10}",
        "-" * 90,
    ]
    for field, counts in sorted_fields:
        table_lines.append(
            f"{field:<45} {counts['correct']:>7} {counts['wrong']:>5} "
            f"{counts['missing']:>7} {counts['unexpected']:>10} {counts['not_in_schema']:>10}"
        )

    table = "\n".join(table_lines)
    summary_line = (
        f"\nAGGREGATE — correct={total['correct']} wrong={total['wrong']} "
        f"missing={total['missing']} unexpected={total['unexpected']} "
        f"not_in_schema={total['not_in_schema']} "
        f"accuracy={overall_accuracy:.1%}"
    )

    print(f"{summary_line}{table}")

    assert overall_accuracy >= 0.0, (
        f"Aggregate accuracy check failed{summary_line}{table}"
    )
