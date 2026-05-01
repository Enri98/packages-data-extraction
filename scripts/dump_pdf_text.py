#!/usr/bin/env python3
"""
Dump selectable text and word bounding boxes for every PDF in samples/.

Usage:
    .venv/Scripts/python.exe scripts/dump_pdf_text.py

For each PDF, prints:
- Per-page raw text (page.extract_text())
- Word count and the first 30 words with (x0, top, text) for spatial inspection.

Plain print() is intentional — this is a diagnostic CLI script, not production code.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pdfplumber  # noqa: E402 — after sys.path fix

_SAMPLES_DIR = _REPO_ROOT / "samples"

_PDFS = [
    "8055712771306_220x80x45_Thomas Turbato.pdf",
    "8055712772860_180x80x50_OSA_Fairy-Handcuffs.pdf",
    "8055712772907_150x45x180_OSA_Gothic-Love.pdf",
]


def dump_pdf(pdf_path: Path) -> None:
    print("=" * 80)
    print(f"FILE: {pdf_path.name}")
    print("=" * 80)

    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages, start=1):
            print(f"\n--- PAGE {page_num} ---")

            text = page.extract_text() or ""
            if text.strip():
                print(text)
            else:
                print("(no selectable text on this page)")

            words = page.extract_words()
            print(f"\n[words on page {page_num}: {len(words)}]")
            for w in words[:30]:
                print(f"  x0={w['x0']:6.1f}  top={w['top']:6.1f}  text={w['text']!r}")

    print()


def main() -> None:
    if not _SAMPLES_DIR.exists():
        print(f"[ERROR] samples/ directory not found at {_SAMPLES_DIR}")
        sys.exit(1)

    found = 0
    for name in _PDFS:
        path = _SAMPLES_DIR / name
        if not path.exists():
            print(f"[SKIP] not found: {name}")
            continue
        dump_pdf(path)
        found += 1

    if found == 0:
        print("[ERROR] No PDFs were found in samples/.")
        sys.exit(1)


if __name__ == "__main__":
    main()
