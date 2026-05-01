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
        "simbolo_smaltimento_spagnolo",
        "simboli_materiali_smaltimento",
        "qr_code_junker",
        "simbolo_garanzia_2_anni",
        "simbolo_libretto_informativo",
        "strap_on_compatibile",
        "funzione_riscaldante",
        "sexy_ideas",
    }
)

# Human-readable descriptions injected into the prompt schema fragment.
_FIELD_DESCRIPTIONS: dict[str, str] = {
    # visual / presence
    "simbolo_ce": "CE conformity mark (PresenceField) — legally required",
    "simbolo_raee": "RAEE/WEEE wheelie-bin symbol (PresenceField) — legally required",
    "simbolo_ukca": "UKCA mark for UK market (PresenceField)",
    "simbolo_triman": "French TRIMAN recycling logo (PresenceField)",
    "simbolo_smaltimento_spagnolo": "Spanish disposal symbol (PresenceField)",
    "simbolo_garanzia_2_anni": "2-year guarantee symbol (PresenceField)",
    "simbolo_libretto_informativo": "Information booklet symbol (PresenceField)",
    "strap_on_compatibile": "Strap-on compatible marker (PresenceField)",
    "funzione_riscaldante": "Heating function marker (PresenceField)",
    "sexy_ideas": "Sexy Ideas marketing marker present on pack (PresenceField)",
    # visual / extracted
    "simboli_materiali_smaltimento": (
        "Text description of recycling triangle symbols and their codes "
        "(ExtractedField, e.g. 'PAP21 / CPE07')"
    ),
    "qr_code_junker": "Junker QR code present on back panel (PresenceField)",
    # text_in_pdf — descriptions kept short to save tokens
    "tipo_o_modello": "Product type or model code (ExtractedField)",
    "numero_di_serie_lotto": "Full lot label as printed, e.g. 'LOT: 468' (ExtractedField)",
    "lotto": "Parsed lot value only; 'N/A' when not shown (ExtractedField)",
    "codice_asin": "Amazon ASIN code (ExtractedField)",
    "materiale": "Main body material, e.g. 'Silicone/ABS' (ExtractedField)",
    "capacita_batteria_e_tensione_nominale": (
        "Battery capacity and nominal voltage, e.g. '420mAh / 3.7V' (ExtractedField)"
    ),
    "impermeabilita": "Waterproofing level, e.g. 'IPX6' or 'Non Impermeabile' (ExtractedField)",
    "modalita_di_ricarica": (
        "Charging method, e.g. 'Ricarica magnetica', 'Ricarica minijack', or 'N/A' (ExtractedField)"
    ),
    "dimensioni": "Physical dimensions, e.g. '17cm x Ø5.7cm' (ExtractedField)",
    "n_vibrazioni": "Number of vibration modes as text, e.g. '10 vibrazioni' (ExtractedField)",
    "n_velocita": "Number of speed modes as text (ExtractedField)",
    "n_modalita_suzione": "Number of suction modes as text (ExtractedField)",
    "n_modalita_tapping": "Number of tapping / movement modes as text (ExtractedField)",
    "n_modalita_rotazione": "Number of rotation modes as text (ExtractedField)",
    "codice_smaltimento_scatola": "Disposal code for box, e.g. 'PAP21' (ExtractedField)",
    "codice_smaltimento_sacchetto": "Disposal code for bag, e.g. 'CPE07' (ExtractedField)",
    "codice_smaltimento_doypack": "Disposal code for doypack when present (ExtractedField)",
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
