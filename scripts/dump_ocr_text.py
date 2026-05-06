"""
Throwaway debug script: dump OCR text for each sample PDF to stdout.
Usage: .venv\Scripts\python.exe scripts/dump_ocr_text.py
"""

import json
import sys
import tempfile
from pathlib import Path

# Make src importable from repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ocr import extract_ocr_text  # noqa: E402

SAMPLES = Path(__file__).parent.parent / "samples"

pdfs = sorted(SAMPLES.glob("*.pdf"))
if not pdfs:
    print("No PDFs found in samples/")
    sys.exit(1)

output: dict[str, list[str]] = {}

for pdf in pdfs:
    text = extract_ocr_text(pdf)
    lines = text.splitlines()
    output[pdf.name] = lines
    print(f"\n{'=' * 80}")
    print(f"PDF: {pdf.name}")
    print(f"{'=' * 80}")
    for i, line in enumerate(lines, start=1):
        print(f"{i:4d} | {repr(line)}")
    print(f"--- END ({len(text)} chars, {len(lines)} lines) ---")

# Also dump as JSON to a temp file for easy parsing
out_path = Path(tempfile.gettempdir()) / "ocr_dump.json"
with out_path.open("w", encoding="utf-8") as f:
    json.dump(output, f, ensure_ascii=False, indent=2)
print(f"\n[JSON dump written to: {out_path}]")
