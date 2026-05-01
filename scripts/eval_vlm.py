#!/usr/bin/env python3
"""
A/B eval: run parser-only, parser+VLM(image), parser+VLM(image+OCR) over the
3 sample PDFs and print a side-by-side comparison against goldens.

This script makes REAL Gemini API calls and costs money. It refuses to run
unless GEMINI_LIVE=1 is set. It also prompts for confirmation with an
estimated cost before each batch.

Usage:
    GEMINI_LIVE=1 GEMINI_API_KEY=... uv run python scripts/eval_vlm.py
"""

import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

# Repo root on sys.path when invoked directly.
_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from loguru import logger  # noqa: E402

from src.ocr import extract_ocr_text  # noqa: E402
from src.parsing import extract_text_fields  # noqa: E402
from src.schemas.pack import PackData  # noqa: E402
from src.validator import validate  # noqa: E402
from src.vlm import VLMNotAuthorizedError, extract_visual_fields  # noqa: E402
from tests._eval_utils import (  # noqa: E402
    PDF_TO_GOLDEN,
    Outcome,
    _SAMPLES_DIR,
    compare_field,
    extract_actual,
)


# Rough cost estimate per VLM call. Gemini 2.5 Pro on a small fustella PDF
# is well under 1 cent at current pricing; padded here for safety.
_EST_COST_PER_CALL_USD = 0.03

MODES: tuple[tuple[str, bool, bool], ...] = (
    # (label, run_vlm, ocr_grounding)
    ("parser",          False, False),
    ("vlm-image",       True,  False),
    ("vlm-image+ocr",   True,  True),
)


# ---------------------------------------------------------------------------
# Mode runner
# ---------------------------------------------------------------------------


def run_mode(pdf_path: Path, *, run_vlm: bool, ocr_grounding: bool) -> PackData:
    """Run one extraction mode and return the final merged PackData."""
    parser_output = extract_text_fields(pdf_path)

    if not run_vlm:
        # Stub VLM — same shape as eval_deterministic uses.
        vlm_stub = PackData(
            codice_ean=parser_output.codice_ean,
            dimensioni=parser_output.dimensioni,
        )
        return validate(parser_output, vlm_stub).pack

    ocr_text = extract_ocr_text(pdf_path) if ocr_grounding else None
    vlm_output = extract_visual_fields(pdf_path, parser_output, ocr_text=ocr_text)
    return validate(parser_output, vlm_output).pack


# ---------------------------------------------------------------------------
# Diff against golden
# ---------------------------------------------------------------------------


def diff_against_golden(pack: PackData, golden_path: Path) -> dict[str, dict]:
    """Per-field diff: {field: {expected, actual, outcome}}."""
    with golden_path.open(encoding="utf-8") as fh:
        golden: dict = json.load(fh)

    fields: dict[str, dict] = {}
    for key, expected in golden.items():
        if key.startswith("_"):
            continue
        actual = extract_actual(pack, key)
        outcome = compare_field(expected, actual)
        fields[key] = {"expected": expected, "actual": actual, "outcome": outcome}
    return fields


def summarize(fields: dict[str, dict]) -> dict[str, Any]:
    counts = {"correct": 0, "wrong": 0, "missing": 0, "unexpected": 0, "not_in_schema": 0}
    for info in fields.values():
        counts[info["outcome"].value.lower()] += 1
    denom = counts["correct"] + counts["wrong"] + counts["missing"] + counts["unexpected"]
    counts["accuracy"] = counts["correct"] / denom if denom else 0.0
    return counts


# ---------------------------------------------------------------------------
# Comparison rendering
# ---------------------------------------------------------------------------


def _value_str(v: Any) -> str:
    if v is None:
        return "—"
    s = str(v)
    return s if len(s) <= 24 else s[:21] + "..."


def render_comparison(per_pdf: dict[str, dict[str, dict[str, dict]]]) -> None:
    """
    per_pdf[pdf_name][mode] = fields-dict (from diff_against_golden).
    Print one block per PDF, then aggregate per-mode accuracy.
    """
    mode_labels = [m[0] for m in MODES]

    for pdf_name, by_mode in per_pdf.items():
        print(f"\n{'=' * 110}")
        print(f"PDF: {pdf_name}")

        # Header.
        header = f"  {'FIELD':<40} {'EXPECTED':<24}"
        for label in mode_labels:
            header += f" | {label:<24}"
        print(header)
        print("  " + "-" * (40 + 25 + len(mode_labels) * 27))

        # Find every field across modes (they should all be the same set).
        all_fields = set()
        for fields in by_mode.values():
            all_fields.update(fields.keys())

        for field in sorted(all_fields):
            # Use the first mode's expected value (identical across modes).
            first_mode = mode_labels[0]
            expected = by_mode[first_mode][field]["expected"]

            # Skip rows where every mode is correct — keeps signal high.
            outcomes = {m: by_mode[m][field]["outcome"] for m in mode_labels}
            if all(o == Outcome.CORRECT for o in outcomes.values()):
                continue

            row = f"  {field:<40} {_value_str(expected):<24}"
            for m in mode_labels:
                act = by_mode[m][field]["actual"]
                oc = outcomes[m]
                cell = f"{_value_str(act)} [{oc.value[:4]}]"
                row += f" | {cell:<24}"
            print(row)

    # Aggregate.
    print(f"\n{'=' * 110}")
    print("AGGREGATE PER MODE")
    print(f"  {'MODE':<18} {'CORRECT':>8} {'WRONG':>6} {'MISS':>5} {'UNEX':>5} {'ACC':>7}")
    print("  " + "-" * 56)
    for mode in mode_labels:
        agg = defaultdict(int)
        for by_mode in per_pdf.values():
            s = summarize(by_mode[mode])
            for k in ("correct", "wrong", "missing", "unexpected"):
                agg[k] += s[k]
        denom = agg["correct"] + agg["wrong"] + agg["missing"] + agg["unexpected"]
        acc = agg["correct"] / denom if denom else 0.0
        print(
            f"  {mode:<18} {agg['correct']:>8} {agg['wrong']:>6} "
            f"{agg['missing']:>5} {agg['unexpected']:>5} {acc:>6.1%}"
        )

    # OCR delta — fields that flipped between vlm-image and vlm-image+ocr.
    print(f"\n{'=' * 110}")
    print("OCR GROUNDING DELTA (fields where vlm-image and vlm-image+ocr disagree on outcome)")
    print(f"  {'PDF':<48} {'FIELD':<36} {'IMAGE':<10} {'IMAGE+OCR':<10}")
    print("  " + "-" * 108)
    any_delta = False
    for pdf_name, by_mode in per_pdf.items():
        a = by_mode["vlm-image"]
        b = by_mode["vlm-image+ocr"]
        for field in a:
            if a[field]["outcome"] != b[field]["outcome"]:
                any_delta = True
                print(
                    f"  {pdf_name[:46]:<48} {field:<36} "
                    f"{a[field]['outcome'].value[:8]:<10} {b[field]['outcome'].value[:8]:<10}"
                )
    if not any_delta:
        print("  (no per-field outcome differences between OCR on/off)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _confirm_cost(n_pdfs: int) -> bool:
    n_calls = n_pdfs * sum(1 for _, run_vlm, _ in MODES if run_vlm)
    est = n_calls * _EST_COST_PER_CALL_USD
    print(
        f"\nAbout to make {n_calls} real Gemini calls "
        f"({n_pdfs} PDFs × 2 VLM modes). Estimated cost: ~${est:.2f}."
    )
    if not sys.stdin.isatty():
        print(
            "Refusing to proceed: stdin is not a TTY, cannot confirm interactively. "
            "Set EVAL_VLM_NONINTERACTIVE=1 to bypass (you'll own the spend)."
        )
        return os.environ.get("EVAL_VLM_NONINTERACTIVE") == "1"
    print("Type 'yes' to proceed, anything else to abort: ", end="", flush=True)
    answer = sys.stdin.readline().strip().lower()
    return answer == "yes"


def main() -> int:
    if os.environ.get("GEMINI_LIVE") != "1":
        print(
            "ERROR: GEMINI_LIVE != '1'. Refusing to run.\n"
            "Set GEMINI_LIVE=1 in the environment to authorize real API calls."
        )
        return 2
    if not os.environ.get("GEMINI_API_KEY"):
        print("ERROR: GEMINI_API_KEY is not set.")
        return 2

    pdfs: list[tuple[Path, Path]] = []
    missing: list[str] = []
    for pdf_name, golden_path in PDF_TO_GOLDEN.items():
        pdf_path = _SAMPLES_DIR / pdf_name
        if pdf_path.exists():
            pdfs.append((pdf_path, golden_path))
        else:
            missing.append(pdf_name)

    if missing:
        print("[WARN] Skipping missing PDFs:")
        for name in missing:
            print(f"  - {name}")

    if not pdfs:
        print("[ERROR] No sample PDFs found.")
        return 1

    if not _confirm_cost(len(pdfs)):
        print("Aborted.")
        return 0

    per_pdf: dict[str, dict[str, dict[str, dict]]] = {}

    for pdf_path, golden_path in pdfs:
        print(f"\n--- {pdf_path.name} ---")
        per_pdf[pdf_path.name] = {}

        for label, run_vlm, ocr_grounding in MODES:
            t0 = time.perf_counter()
            try:
                pack = run_mode(pdf_path, run_vlm=run_vlm, ocr_grounding=ocr_grounding)
            except VLMNotAuthorizedError as exc:
                print(f"  [{label}] aborted: {exc}")
                return 2
            except Exception as exc:  # noqa: BLE001
                logger.exception("mode {} failed for {}", label, pdf_path.name)
                print(f"  [{label}] FAILED: {exc}")
                # Record an empty fields dict so the report is still rendered.
                per_pdf[pdf_path.name][label] = {}
                continue

            elapsed = time.perf_counter() - t0
            fields = diff_against_golden(pack, golden_path)
            per_pdf[pdf_path.name][label] = fields
            s = summarize(fields)
            print(
                f"  [{label}] acc={s['accuracy']:.1%} "
                f"(C{s['correct']}/W{s['wrong']}/M{s['missing']}/U{s['unexpected']}) "
                f"in {elapsed:.1f}s"
            )

    render_comparison(per_pdf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
