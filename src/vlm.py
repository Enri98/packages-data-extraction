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

Cost note (Gemini 2.5 Pro, as of 2025):
  ~$1.25 / 1M input tokens + $10 / 1M output tokens.
  A typical fustella PDF ≈ 800–1200 tokens input. Estimated $0.01–0.03 per pack.
  See: https://ai.google.dev/gemini-api/docs/pricing
"""

import json
import os
from pathlib import Path
from typing import Any

from google import genai
from loguru import logger
from tenacity import before_log, retry, stop_after_attempt, wait_exponential

from src.schemas.pack import ExtractedField, PackData, PresenceField


PROMPT_PATH = Path("prompts/extraction_v1.txt")

# Fields that are always sent to the VLM regardless of parser confidence.
_VISUAL_FIELDS: frozenset[str] = frozenset(
    {
        "simbolo_ce",
        "simbolo_raee",
        "simbolo_ukca",
        "simbolo_triman",
        "simbolo_eta_minima",
        "simboli_materiali_smaltimento",
        "qr_code_junker",
    }
)

# Human-readable descriptions injected into the prompt schema fragment.
_FIELD_DESCRIPTIONS: dict[str, str] = {
    # visual / presence
    "simbolo_ce": "CE conformity mark (PresenceField) — legally required",
    "simbolo_raee": "RAEE/WEEE wheelie-bin symbol (PresenceField) — legally required",
    "simbolo_ukca": "UKCA mark for UK market (PresenceField)",
    "simbolo_triman": "French TRIMAN recycling logo (PresenceField)",
    "simbolo_eta_minima": "+18 minimum age graphic (PresenceField)",
    # visual / extracted
    "simboli_materiali_smaltimento": (
        "Description of recycling triangle symbols and their codes "
        "(ExtractedField, e.g. 'FR 7 triangle, CPE 21 triangle, PAP 21 triangle')"
    ),
    "qr_code_junker": "Decoded URL from the Junker QR code on the back panel (ExtractedField)",
    # text_in_pdf — descriptions kept short to save tokens
    "nome_prodotto": "Product commercial name (ExtractedField)",
    "tipo_o_modello": "Product type or model code (ExtractedField)",
    "colore": "Product colour (ExtractedField)",
    "codice_asin": "Amazon ASIN code (ExtractedField)",
    "materiale": "Main body material, e.g. silicone (ExtractedField)",
    "codici_smaltimento_materiali": (
        "Material disposal codes as plain text, e.g. 'FR 7 | CPE 21 | PAP' (ExtractedField)"
    ),
    "lotto": "Lot / batch number (ExtractedField)",
    "paese_di_produzione": "Country of manufacture (ExtractedField)",
    "capacita_batteria_e_tensione_nominale": (
        "Battery capacity and nominal voltage, e.g. '3.7V 800mAh' (ExtractedField)"
    ),
    "tempo_di_carica": "Charging time (ExtractedField)",
    "durata_utilizzo": "Operating duration per charge (ExtractedField)",
    "istruzioni_carica": "Charging instructions text (ExtractedField)",
    "n_vibrazioni": "Number of vibration modes (ExtractedField)",
    "livello_impermeabilita": "Waterproofing level, e.g. IPX7 (ExtractedField)",
    "avvertenze": "Safety warnings text (ExtractedField)",
    "eta_minima": "Minimum age as text, e.g. '+18' (ExtractedField)",
    "lingue_sulla_confezione": "Languages present on packaging (ExtractedField)",
    "sito_web": "Brand website URL (ExtractedField)",
    "assistenza_clienti": "Customer support contact (ExtractedField)",
    "sexy_ideas": "Marketing text block from side panel — extract verbatim (ExtractedField)",
}


class VLMError(Exception):
    """Raised when the VLM returns an unparseable or structurally invalid response."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_visual_fields(pdf_path: Path, partial_pack: PackData) -> PackData:
    """
    Call Gemini 2.5 Pro on the PDF and populate visual + gap-fill fields.

    partial_pack contains fields already set by the text parser; the VLM
    fills visual fields and any text_in_pdf field still at confidence=0.0.
    Returns a new PackData with VLM-sourced fields overlaid on partial_pack.
    """
    # Client is initialised here, not at module level, so tests can import
    # this module without a live API key present.
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

    schema_fragment = _build_schema_fragment(partial_pack)
    prompt_template = _load_prompt()
    prompt = prompt_template.replace("{field_schema}", schema_fragment)

    uploaded_file = _upload_with_retry(client, pdf_path)
    logger.info(
        "Uploaded PDF to Gemini Files API | name={} | uri={}",
        uploaded_file.name,
        uploaded_file.uri,
    )

    try:
        raw_json = _generate_with_retry(client, prompt, uploaded_file)
    finally:
        # Always delete the uploaded file to avoid accumulating storage quota.
        try:
            client.files.delete(name=uploaded_file.name)
            logger.debug("Deleted uploaded file | name={}", uploaded_file.name)
        except Exception as exc:  # noqa: BLE001
            # Non-fatal: quota cleanup failure should not abort the pipeline.
            logger.warning("Failed to delete uploaded file | name={} | error={}", uploaded_file.name, exc)

    return _merge_vlm_response(raw_json, partial_pack)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _load_prompt() -> str:
    """Load and return the extraction prompt text from disk."""
    return PROMPT_PATH.read_text(encoding="utf-8")


def _build_schema_fragment(pack: PackData) -> str:
    """
    Generate a concise field list for injection into the prompt.

    Visual fields are always included.
    TEXT_IN_PDF fields are included only when their current confidence == 0.0
    (i.e. the parser found nothing), keeping prompt tokens minimal.
    """
    lines: list[str] = []

    for field_name in PackData.model_fields:
        value = getattr(pack, field_name)

        is_visual = field_name in _VISUAL_FIELDS
        is_unfilled_text = (
            isinstance(value, (ExtractedField, PresenceField))
            and value.confidence == 0.0
            and field_name not in _VISUAL_FIELDS
        )

        if not (is_visual or is_unfilled_text):
            continue

        description = _FIELD_DESCRIPTIONS.get(field_name, "")
        field_type = "PresenceField" if isinstance(value, PresenceField) else "ExtractedField"
        lines.append(f"- {field_name} ({field_type}): {description}")

    return "\n".join(lines)


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    before=before_log(logger, "WARNING"),
)
def _upload_with_retry(client: genai.Client, pdf_path: Path) -> Any:
    """Upload the PDF to the Gemini Files API with exponential-backoff retry."""
    return client.files.upload(path=pdf_path)


@retry(
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
    before=before_log(logger, "WARNING"),
)
def _generate_with_retry(client: genai.Client, prompt: str, uploaded_file: Any) -> dict[str, Any]:
    """
    Call generate_content and return the parsed JSON dict.

    Raises VLMError if the response cannot be parsed as JSON.
    """
    response = client.models.generate_content(
        model="gemini-2.5-pro",
        contents=[uploaded_file, prompt],
        config={
            "response_mime_type": "application/json",
        },
    )

    raw_text: str = response.text.strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise VLMError(f"Gemini returned non-JSON response: {exc}\n---\n{raw_text[:500]}") from exc


def _merge_vlm_response(vlm_data: dict[str, Any], partial_pack: PackData) -> PackData:
    """
    Overlay VLM-returned fields onto a deep copy of partial_pack.

    Fields absent from vlm_data are left at their existing values.
    Fields with unexpected shapes are logged as warnings and skipped.
    """
    result = partial_pack.model_copy(deep=True)

    for field_name, raw_value in vlm_data.items():
        if field_name not in PackData.model_fields:
            logger.warning("VLM returned unknown field '{}' — skipped", field_name)
            continue

        current = getattr(result, field_name)

        if not isinstance(raw_value, dict):
            logger.warning(
                "VLM field '{}' has unexpected shape (expected dict, got {}) — skipped",
                field_name,
                type(raw_value).__name__,
            )
            continue

        if isinstance(current, PresenceField):
            _populate_presence_field(current, raw_value, field_name)
        elif isinstance(current, ExtractedField):
            _populate_extracted_field(current, raw_value, field_name)
        # Deterministic / derived plain-str fields are intentionally not overwritten.

    return result


def _populate_presence_field(field: PresenceField, raw: dict[str, Any], name: str) -> None:
    """Mutate a PresenceField in-place from the VLM dict for that field."""
    if "present" not in raw:
        logger.warning("VLM PresenceField '{}' missing 'present' key — skipped", name)
        return
    field.present = raw.get("present")
    field.confidence = float(raw.get("confidence", 0.0))
    field.evidence = raw.get("evidence")


def _populate_extracted_field(field: ExtractedField, raw: dict[str, Any], name: str) -> None:
    """Mutate an ExtractedField in-place from the VLM dict for that field."""
    if "value" not in raw:
        logger.warning("VLM ExtractedField '{}' missing 'value' key — skipped", name)
        return
    field.value = raw.get("value")
    field.confidence = float(raw.get("confidence", 0.0))
    field.evidence = raw.get("evidence")
