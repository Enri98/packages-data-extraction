# fustelle-extractor

Extracts structured data from product packaging PDFs ("fustelle") and writes rows to a Google Sheet.

## Architecture

```
Drive upload
    │
    ▼ Eventarc (Drive onCreate → Pub/Sub)
Cloud Run container
    │
    ├─ parsing.py   ── pdfplumber text extraction (filename + regex/spatial)
    ├─ vlm.py       ── Gemini 2.5 Pro (visual symbols, QR code, gap-fill)
    ├─ validator.py ── merge, cross-check, confidence scoring, TRIMAN rule
    └─ sheets.py    ── write pack_data / review_queue / run_metadata tabs
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for design decisions and trade-offs.

## Quick start

```bash
uv sync
cp .env.example .env   # fill in credentials
source .venv/bin/activate
python -m src.pipeline path/to/pack.pdf
uv run pytest
```

## Field schema

34 output fields in Italian, matching the Google Sheet header. Tagged by extraction strategy:

| Strategy | Description |
|---|---|
| `deterministic` | Filename or brand constant — confidence 1.0 |
| `text_in_pdf` | pdfplumber regex / spatial extraction |
| `visual` | Gemini 2.5 Pro VLM |
| `derived` | Computed from other fields |

Full schema: [`src/schemas/pack.py`](src/schemas/pack.py)

## Project structure

```
src/            one file = one responsibility
prompts/        versioned extraction prompts, loaded at runtime
tests/          pytest suite; VLM tests use cassettes (no live API in CI)
infra/          Dockerfile, cloudbuild.yaml, GCP runbook
samples/        sanitized PDFs for tests (real PDFs are gitignored)
notebooks/      exploratory analysis, not part of the production path
```

## Deployment

See [DEPLOYMENT.md](DEPLOYMENT.md).
