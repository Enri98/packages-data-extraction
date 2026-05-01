"""
Visual field extraction via Gemini 2.5 Pro (google-genai SDK).

Responsibilities:
- Accept a raw PDF file path, send it natively to the Gemini Files API
  (no rasterisation required; Gemini ingests PDFs directly).
- Load the versioned extraction prompt from prompts/extraction_v1.txt.
- Request structured JSON output validated against the PackData schema.
- Return per-field {value, confidence, evidence} for all visual fields
  and any text_in_pdf fields the text parser left unfilled.
- Wrap every API call with tenacity retry (exponential backoff, 3 attempts).
- Upload the PDF once, cache the file URI for the duration of the request.
- Never writes to disk beyond the temp upload; never touches Sheet or Drive.

Not responsible for:
- Cross-field validation or derived fields (delegated to validator.py).
- Merging with parser output (delegated to validator.py).

Cost note (Gemini 2.5 Pro, as of 2025-Q1):
  ~$0.00125 / 1k input tokens + $0.01 / 1k output tokens.
  A typical fustella PDF ≈ 800–1200 tokens. Estimated $0.01–0.03 per pack.
  See: https://ai.google.dev/gemini-api/docs/pricing
"""

from pathlib import Path

from loguru import logger

from src.schemas.pack import PackData


PROMPT_PATH = Path("prompts/extraction_v1.txt")


def extract_visual_fields(pdf_path: Path, partial_pack: PackData) -> PackData:
    """
    Call Gemini 2.5 Pro on the PDF and merge visual + gap-fill results into
    partial_pack.

    partial_pack contains fields already set by the text parser; the VLM
    fills visual fields and any text_in_pdf field still at confidence=0.0.
    Returns a new PackData with VLM-sourced fields populated.
    """
    raise NotImplementedError("Session 3 deliverable")


def _load_prompt() -> str:
    """Load and return the extraction prompt text from disk."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def _build_schema_fragment(pack: PackData) -> str:
    """
    Generate a concise JSON schema description of PackData for injection
    into the prompt. Only visual and unfilled text_in_pdf fields are included
    to keep token usage minimal.
    """
    raise NotImplementedError("Session 3 deliverable")
