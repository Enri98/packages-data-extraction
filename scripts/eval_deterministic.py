#!/usr/bin/env python3
"""CLI report: run the deterministic pipeline against all sample PDFs and print results."""

import sys
from pathlib import Path

# Ensure the repo root is on sys.path when the script is invoked directly.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tests._eval_utils import _SAMPLES_DIR, PDF_TO_GOLDEN, Outcome, evaluate_pdf  # noqa: E402

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _divider(width: int = 100) -> str:
    return "-" * width


def _print_per_pdf(report: dict) -> None:
    summary = report["summary"]
    print(f"\n{'=' * 100}")
    print(f"PDF: {report['pdf']}")
    print(
        f"  correct={summary['correct']}  wrong={summary['wrong']}  "
        f"missing={summary['missing']}  unexpected={summary['unexpected']}  "
        f"not_in_schema={summary['not_in_schema']}  "
        f"accuracy={summary['accuracy']:.1%}"
    )

    non_correct = {
        field: info
        for field, info in report["fields"].items()
        if info["outcome"] != Outcome.CORRECT
    }

    if not non_correct:
        print("  (all evaluated fields are CORRECT)")
        return

    print(f"\n  {'FIELD':<44} {'EXPECTED':<28} {'ACTUAL':<28} {'OUTCOME'}")
    print(f"  {_divider(96)}")
    for field, info in non_correct.items():
        exp_str = str(info["expected"])[:26]
        act_str = str(info["actual"])[:26]
        print(f"  {field:<44} {exp_str:<28} {act_str:<28} {info['outcome'].value}")


def _print_aggregate(all_reports: list[dict]) -> None:
    total: dict[str, int] = {
        "correct": 0,
        "wrong": 0,
        "missing": 0,
        "unexpected": 0,
        "not_in_schema": 0,
    }
    per_field: dict[str, dict[str, int]] = {}

    for report in all_reports:
        for k in ("correct", "wrong", "missing", "unexpected", "not_in_schema"):
            total[k] += report["summary"][k]

        for field, info in report["fields"].items():
            bucket = per_field.setdefault(
                field,
                {"correct": 0, "wrong": 0, "missing": 0, "unexpected": 0, "not_in_schema": 0},
            )
            bucket[info["outcome"].value.lower()] += 1

    denominator = total["correct"] + total["wrong"] + total["missing"] + total["unexpected"]
    overall_accuracy = total["correct"] / denominator if denominator > 0 else 0.0

    def _failure_count(counts: dict[str, int]) -> int:
        return counts["wrong"] + counts["missing"] + counts["unexpected"]

    sorted_fields = sorted(per_field.items(), key=lambda kv: _failure_count(kv[1]), reverse=True)

    print(f"\n{'=' * 100}")
    print("AGGREGATE ACROSS ALL PDFs")
    print(
        f"  correct={total['correct']}  wrong={total['wrong']}  "
        f"missing={total['missing']}  unexpected={total['unexpected']}  "
        f"not_in_schema={total['not_in_schema']}  "
        f"accuracy={overall_accuracy:.1%}"
    )
    print(
        f"\n  {'FIELD':<44} {'CORRECT':>7} {'WRONG':>5} {'MISSING':>7} "
        f"{'UNEXPECTED':>10} {'NOT_IN_SCH':>10}"
    )
    print(f"  {_divider(90)}")
    for field, counts in sorted_fields:
        print(
            f"  {field:<44} {counts['correct']:>7} {counts['wrong']:>5} "
            f"{counts['missing']:>7} {counts['unexpected']:>10} {counts['not_in_schema']:>10}"
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run eval on all sample PDFs and print the report."""
    all_reports: list[dict] = []
    missing_pdfs: list[str] = []

    for pdf_name, golden_path in PDF_TO_GOLDEN.items():
        pdf_path = _SAMPLES_DIR / pdf_name
        if not pdf_path.exists():
            missing_pdfs.append(pdf_name)
            continue

        report = evaluate_pdf(pdf_path, golden_path)
        all_reports.append(report)
        _print_per_pdf(report)

    if missing_pdfs:
        print("\n[WARN] The following sample PDFs were not found and were skipped:")
        for name in missing_pdfs:
            print(f"  - {name}")

    if all_reports:
        _print_aggregate(all_reports)
    else:
        print("\n[ERROR] No PDFs were processed. Add sample PDFs to samples/.")

    # Exit 0 — this is a report, not a quality gate.
    sys.exit(0)


if __name__ == "__main__":
    main()
