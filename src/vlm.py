"""
Visual field extraction via Gemini 2.5 Pro (google-genai SDK).

Responsibilities:
- Accept a raw PDF file path, send it natively to the Gemini Files API
  (no rasterisation required; Gemini ingests PDFs directly).
- Load the versioned extraction prompt from prompts/extraction_v1.txt.
- Request structured JSON output validated against a Pydantic-derived schema
  built dynamically from the set of fields requested for this call.
- Optionally inject OCR text and filename context into the prompt as
  grounding signals.
- Always request all VLM-eligible fields (visual + text-in-pdf), so the
  validator can reconcile parser vs VLM and surface conflicts.
- Wrap every API call with tenacity retry (exponential backoff, 3 attempts).
- Refuse to make a real API call unless GEMINI_LIVE=1 is set in the
  environment. This protects against accidental spend during development
  and from imports in tests.

Not responsible for:
- Cross-field validation or derived fields (delegated to validator.py).
- Merging with parser output beyond overlaying VLM-returned envelopes
  onto a copy of the parser's PackData (delegated to validator.py).

Cost note (Gemini 2.5 Pro, verify against current pricing before billing
decisions): https://ai.google.dev/gemini-api/docs/pricing
"""

import os
from pathlib import Path
from typing import Any

from src.secrets import get_secret

from google import genai
from google.genai import types
from loguru import logger
from pydantic import BaseModel, Field, create_model
from tenacity import (
    RetryCallState,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from src.parsing import parse_filename
from src.schemas.pack import ExtractedField, PackData, PresenceField


PROMPT_PATH = Path("prompts/extraction_v1.txt")
MODEL_NAME = "gemini-2.5-pro"

# Fields the VLM is the sole authority on (visual / iconographic).
_VLM_OWNS_VISUAL: tuple[str, ...] = (
    "simbolo_ce",
    "simbolo_raee",
    "simbolo_ukca",
    "simbolo_triman",
    "simbolo_smaltimento_spagnolo",
    "simbolo_garanzia_2_anni",
    "simbolo_libretto_informativo",
    "qr_code_junker",
    "simboli_materiali_smaltimento",
    "strap_on_compatibile",
    "funzione_riscaldante",
    "sexy_ideas",
)

# Fields the parser also handles. The VLM extracts them too so the validator
# can reconcile and flag conflicts (gap-fill + verify contract).
_VLM_VERIFIES_TEXT: tuple[str, ...] = (
    "tipo_o_modello",
    "numero_di_serie_lotto",
    "lotto",
    "materiale",
    "capacita_batteria_e_tensione_nominale",
    "impermeabilita",
    "modalita_di_ricarica",
    "dimensioni",
    "n_vibrazioni",
    "n_velocita",
    "n_modalita_suzione",
    "n_modalita_tapping",
    "n_modalita_rotazione",
    "codice_asin",
    "codice_smaltimento_scatola",
    "codice_smaltimento_sacchetto",
    "codice_smaltimento_doypack",
)

# Human-readable hints injected into the prompt schema fragment.
_FIELD_DESCRIPTIONS: dict[str, str] = {
    # visual / presence
    "simbolo_ce": "CE conformity mark — legally required",
    "simbolo_raee": "RAEE/WEEE wheelie-bin symbol — legally required",
    "simbolo_ukca": "UKCA mark for UK market",
    "simbolo_triman": "French TRIMAN recycling logo",
    "simbolo_smaltimento_spagnolo": "Spanish disposal symbol",
    "simbolo_garanzia_2_anni": "2-year guarantee symbol",
    "simbolo_libretto_informativo": "Information booklet symbol",
    "strap_on_compatibile": "Strap-on compatible marker",
    "funzione_riscaldante": "Heating function marker",
    "sexy_ideas": (
        "Sexy Ideas marketing block present on a side panel. "
        "Set present=true if the block exists; put the verbatim block text "
        "in the evidence field. Do NOT put the text in 'value' — this is a "
        "boolean field on the Sheet."
    ),
    "qr_code_junker": "Junker QR code present on back panel",
    # visual / extracted
    "simboli_materiali_smaltimento": (
        "Text description of recycling triangle symbols and their codes "
        "(e.g. 'PAP21 / CPE07')"
    ),
    # text_in_pdf — VLM verifies parser output
    "tipo_o_modello": "Product type or model code",
    "numero_di_serie_lotto": "Full lot label as printed, e.g. 'LOT: 468'",
    "lotto": "Parsed lot value only; 'N/A' when not shown",
    "codice_asin": "Amazon ASIN code",
    "materiale": "Main body material, e.g. 'Silicone/ABS'",
    "capacita_batteria_e_tensione_nominale": (
        "Battery capacity and nominal voltage, e.g. '420mAh / 3.7V'"
    ),
    "impermeabilita": "Waterproofing level, e.g. 'IPX6' or 'Non Impermeabile'",
    "modalita_di_ricarica": (
        "Charging method, e.g. 'Ricarica magnetica', 'Ricarica minijack', or 'N/A'"
    ),
    "dimensioni": "Physical dimensions, e.g. '17cm x Ø5.7cm'",
    "n_vibrazioni": "Number of vibration modes as text, e.g. '10 vibrazioni'",
    "n_velocita": "Number of speed modes as text",
    "n_modalita_suzione": "Number of suction modes as text",
    "n_modalita_tapping": "Number of tapping / movement modes as text",
    "n_modalita_rotazione": "Number of rotation modes as text",
    "codice_smaltimento_scatola": "Disposal code for box, e.g. 'PAP21'",
    "codice_smaltimento_sacchetto": "Disposal code for bag, e.g. 'CPE07'",
    "codice_smaltimento_doypack": "Disposal code for doypack when present",
}


class VLMError(Exception):
    """Raised when the VLM returns an unparseable or structurally invalid response."""


class VLMNotAuthorizedError(VLMError):
    """Raised when GEMINI_LIVE is not enabled — protects against accidental spend."""


# ---------------------------------------------------------------------------
# Response sub-models — the dynamic per-call response model nests these.
# ---------------------------------------------------------------------------


class PresenceFieldResponse(BaseModel):
    present: bool | None = None
    confidence: float = 0.0
    evidence: str | None = None


class ExtractedFieldResponse(BaseModel):
    value: str | None = None
    confidence: float = 0.0
    evidence: str | None = None


_PRESENCE_FIELDS: frozenset[str] = frozenset(
    {
        "simbolo_ce",
        "simbolo_raee",
        "simbolo_ukca",
        "simbolo_triman",
        "simbolo_smaltimento_spagnolo",
        "simbolo_garanzia_2_anni",
        "simbolo_libretto_informativo",
        "qr_code_junker",
        "strap_on_compatibile",
        "funzione_riscaldante",
        "sexy_ideas",
    }
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_visual_fields(
    pdf_path: Path,
    partial_pack: PackData,
    *,
    ocr_text: str | None = None,
) -> PackData:
    """
    Call Gemini 2.5 Pro on the PDF and populate visual + verify text fields.

    Always requests every VLM-eligible field, regardless of parser confidence.
    The validator reconciles overlaps and flags conflicts.

    Args:
        pdf_path: path to the PDF on local disk.
        partial_pack: PackData populated by the text parser. Returned shape
            is a deep copy with VLM-sourced envelopes overlaid.
        ocr_text: optional OCR text from src.ocr; injected into the prompt
            as a grounding block when provided.

    Raises:
        VLMNotAuthorizedError: if GEMINI_LIVE != "1" in the environment.
        VLMError: on structurally invalid responses.
    """
    _require_live_gate()

    requested_fields = list(_VLM_OWNS_VISUAL) + list(_VLM_VERIFIES_TEXT)
    response_model = _build_response_model(requested_fields)

    prompt = _render_prompt(
        requested_fields=requested_fields,
        pdf_path=pdf_path,
        ocr_text=ocr_text,
    )

    client = genai.Client(api_key=get_secret("GEMINI_API_KEY"))
    uploaded_file = _upload_with_retry(client, pdf_path)
    logger.info(
        "Uploaded PDF to Gemini Files API | name={} | uri={}",
        uploaded_file.name,
        uploaded_file.uri,
    )

    try:
        parsed = _generate_with_retry(client, prompt, uploaded_file, response_model)
    finally:
        try:
            client.files.delete(name=uploaded_file.name)
            logger.debug("Deleted uploaded file | name={}", uploaded_file.name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to delete uploaded file | name={} | error={}",
                uploaded_file.name,
                exc,
            )

    return _merge_vlm_response(parsed, partial_pack)


# ---------------------------------------------------------------------------
# Prompt rendering
# ---------------------------------------------------------------------------


def _render_prompt(
    *,
    requested_fields: list[str],
    pdf_path: Path,
    ocr_text: str | None,
) -> str:
    template = PROMPT_PATH.read_text(encoding="utf-8")
    return (
        template.replace("{field_schema}", _build_schema_fragment(requested_fields))
        .replace("{filename_context}", _filename_context(pdf_path))
        .replace("{ocr_text}", _format_ocr_block(ocr_text))
    )


def _build_schema_fragment(field_names: list[str]) -> str:
    """Render the field list injected into the prompt under {field_schema}."""
    lines: list[str] = []
    for name in field_names:
        kind = "PresenceField" if name in _PRESENCE_FIELDS else "ExtractedField"
        description = _FIELD_DESCRIPTIONS.get(name, "")
        lines.append(f"- {name} ({kind}): {description}")
    return "\n".join(lines)


def _filename_context(pdf_path: Path) -> str:
    """Render filename-derived context block. Empty string on parse failure."""
    try:
        ean, dimensions, product_name = parse_filename(pdf_path.name)
    except ValueError:
        return ""
    return (
        f"EAN: {ean}\n"
        f"Dimensions hint (from filename, WxHxD mm): {dimensions}\n"
        f"Product name (from filename): {product_name}"
    )


def _format_ocr_block(text: str | None) -> str:
    """Wrap OCR text for prompt injection. Empty string when no OCR provided."""
    if not text or not text.strip():
        return ""
    return (
        "Below is OCR-recovered text from the same PDF (best-effort, may have "
        "errors). Use it as a grounding signal — prefer the visible text on "
        "the page when they disagree.\n\n"
        "```\n"
        f"{text.strip()}\n"
        "```"
    )


# ---------------------------------------------------------------------------
# Dynamic response schema
# ---------------------------------------------------------------------------


def _build_response_model(field_names: list[str]) -> type[BaseModel]:
    """
    Build a Pydantic model whose fields are the requested envelope sub-models.

    Each requested field becomes a required attribute typed as either
    PresenceFieldResponse or ExtractedFieldResponse. Gemini is asked to
    populate every field — null values are allowed inside the envelope, but
    the envelope itself must be present.
    """
    field_defs: dict[str, Any] = {}
    for name in field_names:
        sub = PresenceFieldResponse if name in _PRESENCE_FIELDS else ExtractedFieldResponse
        field_defs[name] = (sub, Field(...))
    return create_model("VLMExtractionResponse", **field_defs)


# ---------------------------------------------------------------------------
# Gemini calls
# ---------------------------------------------------------------------------


def _log_retry(state: RetryCallState) -> None:
    """Tenacity before-hook that logs via loguru (avoids stdlib logging coupling)."""
    if state.attempt_number > 1:
        logger.warning(
            "Retrying {} | attempt={}",
            state.fn.__name__ if state.fn else "<unknown>",
            state.attempt_number,
        )


def _is_transient(exc: BaseException) -> bool:
    """
    Retry only on server-side / capacity errors. Don't burn attempts on 4xx
    client mistakes (bad schema, bad request, auth) — those won't fix themselves.
    """
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if code in (429, 500, 502, 503, 504):
        return True
    # Network-layer errors (timeouts, connection drops) — also worth retrying.
    msg = str(exc).lower()
    return any(s in msg for s in ("timeout", "connection", "unavailable", "high demand"))


@retry(
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_is_transient),
    before=_log_retry,
    reraise=True,
)
def _upload_with_retry(client: genai.Client, pdf_path: Path) -> Any:
    """Upload the PDF to the Gemini Files API with exponential-backoff retry."""
    return client.files.upload(file=pdf_path)


@retry(
    wait=wait_exponential(multiplier=2, min=4, max=60),
    stop=stop_after_attempt(5),
    retry=retry_if_exception(_is_transient),
    before=_log_retry,
    reraise=True,
)
def _generate_with_retry(
    client: genai.Client,
    prompt: str,
    uploaded_file: Any,
    response_model: type[BaseModel],
) -> BaseModel:
    """
    Call generate_content with response_schema enforcement.

    Returns the parsed Pydantic instance. Raises VLMError if the SDK could
    not parse the response into the schema, including the candidate
    finish_reason and prompt_feedback for diagnosis.
    """
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=[uploaded_file, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=response_model,
            temperature=0.0,
            # HIGH resolution gives Gemini more pixels per page — important
            # because the disposal-code numeric suffixes (e.g. "21" in PAP21)
            # and small regulatory icons are <1mm on the printed pack.
            media_resolution=types.MediaResolution.MEDIA_RESOLUTION_HIGH,
        ),
    )

    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, response_model):
        return parsed

    finish_reason = None
    candidates = getattr(response, "candidates", None) or []
    if candidates:
        finish_reason = getattr(candidates[0], "finish_reason", None)
    prompt_feedback = getattr(response, "prompt_feedback", None)

    # Pull text safely — accessing .text on a blocked candidate emits a SDK
    # warning and may raise. Fall back to None.
    raw_text_head: str | None
    try:
        raw_text_head = (response.text or "")[:500] if response.text else None
    except Exception:  # noqa: BLE001
        raw_text_head = None

    raise VLMError(
        "Gemini response did not parse to the expected schema. "
        f"finish_reason={finish_reason} | prompt_feedback={prompt_feedback} | "
        f"text_head={raw_text_head!r}"
    )


# ---------------------------------------------------------------------------
# Merge VLM result onto parser PackData
# ---------------------------------------------------------------------------


def _merge_vlm_response(parsed: BaseModel, partial_pack: PackData) -> PackData:
    """
    Build a fresh PackData populated ONLY with VLM-returned envelopes.

    Returns a PackData whose only carried-over data from partial_pack is the
    identity field (codice_ean) — every other field starts at its schema
    default and is filled in only when the VLM returned actual signal for it.

    This separation matters: the validator does its own merge between
    parser_output and vlm_output and relies on each side accurately
    representing what THAT source saw. If we copied parser data into the
    returned vlm_output, validator could never flag a real conflict.

    "Actual signal" means: confidence > 0.0, OR a non-null value/present.
    A pure-default envelope from the VLM (everything zero/null) is treated
    as "VLM declined to answer" and left at the schema default.
    """
    result = PackData(codice_ean=partial_pack.codice_ean)

    for field_name in type(parsed).model_fields:
        sub = getattr(parsed, field_name)
        current = getattr(result, field_name, None)

        if isinstance(sub, PresenceFieldResponse) and isinstance(current, PresenceField):
            if sub.present is None and sub.confidence <= 0.0:
                continue
            current.present = sub.present
            current.confidence = float(sub.confidence)
            current.evidence = sub.evidence
        elif isinstance(sub, ExtractedFieldResponse) and isinstance(current, ExtractedField):
            if sub.value is None and sub.confidence <= 0.0:
                continue
            current.value = sub.value
            current.confidence = float(sub.confidence)
            current.evidence = sub.evidence
        else:
            logger.warning(
                "VLM response field '{}' has type incompatible with PackData "
                "(response={}, pack={}) — skipped",
                field_name,
                type(sub).__name__,
                type(current).__name__,
            )

    return result


# ---------------------------------------------------------------------------
# Authorization gate
# ---------------------------------------------------------------------------


def _require_live_gate() -> None:
    """Refuse to make a real Gemini call unless GEMINI_LIVE=1 is set."""
    if os.environ.get("GEMINI_LIVE") != "1":
        raise VLMNotAuthorizedError(
            "Refusing to call Gemini: GEMINI_LIVE != '1'. "
            "Set GEMINI_LIVE=1 in the environment to authorize a real API call. "
            "This guard exists to prevent accidental spend during development."
        )
    if not os.environ.get("GEMINI_API_KEY"):
        raise VLMNotAuthorizedError(
            "GEMINI_LIVE=1 is set but GEMINI_API_KEY is empty."
        )


__all__ = [
    "extract_visual_fields",
    "VLMError",
    "VLMNotAuthorizedError",
    "PresenceFieldResponse",
    "ExtractedFieldResponse",
    "MODEL_NAME",
    "PROMPT_PATH",
]
