"""
OCR experiment: render outlined-text PDFs to images, run OCR, re-test the
existing parser regexes against the OCR output. Determines whether OCR is
viable as a deterministic-tier fallback before reaching for the VLM.

Usage:
    .venv\\Scripts\\python.exe scripts/ocr_experiment.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pypdfium2 as pdfium  # noqa: E402
from rapidocr_onnxruntime import RapidOCR  # noqa: E402

from src.parsing import _FIELD_PATTERNS  # noqa: E402

SAMPLES_DIR = REPO_ROOT / "samples"
RENDERS_DIR = REPO_ROOT / "scripts" / "ocr_renders"
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"


DPI = 300
SCALE = DPI / 72.0


def render_pdf(pdf_path: Path) -> list[Path]:
    RENDERS_DIR.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    doc = pdfium.PdfDocument(str(pdf_path))
    for i, page in enumerate(doc):
        bitmap = page.render(scale=SCALE)
        pil = bitmap.to_pil()
        png = RENDERS_DIR / f"{pdf_path.stem}_p{i + 1}.png"
        pil.save(png)
        out.append(png)
    return out


def ocr_image(ocr: RapidOCR, png: Path) -> tuple[str, float]:
    result, _elapsed = ocr(str(png))
    if not result:
        return "", 0.0
    lines: list[str] = []
    confs: list[float] = []
    for box, text, conf in result:
        lines.append(text)
        confs.append(float(conf))
    avg_conf = sum(confs) / len(confs) if confs else 0.0
    return "\n".join(lines), avg_conf


def normalize(s: object) -> str:
    if not isinstance(s, str):
        return ""
    return re.sub(r"\s+", " ", s).strip()


def load_golden(ean: str) -> dict:
    matches = list(FIXTURES_DIR.glob(f"{ean}_*.json"))
    if not matches:
        return {}
    return json.loads(matches[0].read_text(encoding="utf-8"))


def compare(actual: str | None, expected: object) -> str:
    if actual is None:
        return "MISSING" if expected and expected != "N/A" else "CORRECT"
    if not isinstance(expected, str):
        return "TYPE_MISMATCH"
    a, e = normalize(actual), normalize(expected)
    if a == e:
        return "CORRECT"
    if e and (a in e or e in a):
        return "PARTIAL"
    return "WRONG"


def run() -> None:
    pdfs = sorted(SAMPLES_DIR.glob("*.pdf"))
    print("Loading RapidOCR (downloads ~50MB models on first run)...")
    t0 = time.perf_counter()
    ocr = RapidOCR()
    print(f"OCR engine ready in {time.perf_counter() - t0:.1f}s\n")

    aggregate: dict[str, dict[str, int]] = {}
    field_names = [name for name, _ in _FIELD_PATTERNS]
    for fn in field_names:
        aggregate[fn] = {"CORRECT": 0, "PARTIAL": 0, "WRONG": 0, "MISSING": 0}

    for pdf in pdfs:
        print(f"=== {pdf.name} ===")
        ean = pdf.name.split("_")[0]
        golden = load_golden(ean)

        t_render = time.perf_counter()
        pngs = render_pdf(pdf)
        render_secs = time.perf_counter() - t_render

        t_ocr = time.perf_counter()
        full_text_parts: list[str] = []
        confs: list[float] = []
        for png in pngs:
            text, conf = ocr_image(ocr, png)
            full_text_parts.append(text)
            confs.append(conf)
        ocr_secs = time.perf_counter() - t_ocr
        full_text = "\n".join(full_text_parts)
        avg_conf = sum(confs) / len(confs) if confs else 0.0

        print(f"  render={render_secs:.1f}s  ocr={ocr_secs:.1f}s  pages={len(pngs)}")
        print(f"  ocr_avg_confidence={avg_conf:.2f}  total_chars={len(full_text)}")
        print("  --- OCR text (first 800 chars) ---")
        print(f"  {full_text[:800].replace(chr(10), ' / ')}")
        print("  --- Regex matches ---")

        for field_name, pattern in _FIELD_PATTERNS:
            m = pattern.search(full_text)
            actual = m.group(1).strip() if m else None
            expected = golden.get(field_name)
            outcome = compare(actual, expected)
            aggregate[field_name][outcome] = aggregate[field_name].get(outcome, 0) + 1
            print(
                f"    {field_name:42s}  expected={str(expected)[:30]:30s}  actual={str(actual)[:30]:30s}  {outcome}"
            )
        print()

    print("=" * 100)
    print("AGGREGATE (across 3 PDFs)")
    print(f"{'FIELD':45s} {'CORRECT':>8s} {'PARTIAL':>8s} {'WRONG':>8s} {'MISSING':>8s}")
    print("-" * 90)
    for fn in field_names:
        r = aggregate[fn]
        print(
            f"{fn:45s} {r.get('CORRECT', 0):>8d} {r.get('PARTIAL', 0):>8d} {r.get('WRONG', 0):>8d} {r.get('MISSING', 0):>8d}"
        )


if __name__ == "__main__":
    run()
