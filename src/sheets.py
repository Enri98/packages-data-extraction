"""
Google Sheets writer using gspread.

Responsibilities:
- Authenticate with the Sheets API via a service account credential file
  (path from GOOGLE_SERVICE_ACCOUNT_JSON env var).
- Write finalized PackData rows to the `pack_data` tab. Column order is
  derived from PackData.model_fields declaration order so it stays in sync
  with the schema automatically.
- Write flagged packs (overall_confidence < REVIEW_THRESHOLD) to the
  `review_queue` tab with field-level conflict explanations.
- Write per-run metadata (timestamp, filename, outcome, latency_ms) to the
  `run_metadata` tab.
- Enforce idempotency: check whether a row with the same codice_ean already
  exists before writing; log and skip duplicates.
- Retry all gspread API calls with tenacity on transient errors (quota,
  network).

Not responsible for:
- Extracting or validating data (delegated to parsing.py, vlm.py, validator.py).
- Any Drive operations (delegated to pipeline.py / server.py).

Tabs required in the target Sheet:
  pack_data     — one row per processed pack
  review_queue  — subset of pack_data rows that need human review
  run_metadata  — one row per pipeline run (including errors)
"""

from datetime import UTC, datetime

import gspread
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.schemas.pack import ExtractedField, PackData, PresenceField
from src.secrets import load_service_account_info
from src.validator import ValidationResult

PACK_DATA_TAB = "pack_data"
REVIEW_QUEUE_TAB = "review_queue"
RUN_METADATA_TAB = "run_metadata"

# Cell rendering — match the example row in the deliverable Sheet exactly:
#   PresenceField True/False    -> "✅" / "❌"
#   ExtractedField with value   -> the string value
#   ExtractedField absent (None or empty), in COUNT_LIKE_FIELDS -> "❌"
#   ExtractedField absent (None or empty), all other string fields -> "N/A"
CELL_TRUE = "✅"
CELL_FALSE = "❌"
CELL_NA = "N/A"

# String-typed fields where absence semantically means "doesn't have this
# feature" rather than "not applicable / not printed". Per the Fairy-Handcuffs
# golden fixture: these fields encode absence as the boolean `false`, which
# the example row renders as ❌.
COUNT_LIKE_FIELDS: frozenset[str] = frozenset(
    {
        "n_vibrazioni",
        "n_velocita",
        "n_modalita_suzione",
        "n_modalita_tapping",
        "n_modalita_rotazione",
        "codice_smaltimento_doypack",
    }
)


class SheetConfigError(Exception):
    pass


def get_sheet_client() -> gspread.Client:
    """
    Return an authenticated gspread client.

    Reads GOOGLE_SERVICE_ACCOUNT_JSON from the environment via
    ``load_service_account_info``, which handles both a local file path
    (local dev) and an inline JSON string (Cloud Run ``--update-secrets``).
    """
    try:
        sa_info = load_service_account_info("GOOGLE_SERVICE_ACCOUNT_JSON")
    except ValueError as exc:
        raise SheetConfigError(str(exc)) from exc
    return gspread.service_account_from_dict(sa_info)


def write_pack(result: ValidationResult, sheet_id: str) -> None:
    """
    Write a validated pack to pack_data (always) and review_queue
    (if result.needs_review). Idempotent on codice_ean.
    """
    client = get_sheet_client()
    spreadsheet = client.open_by_key(sheet_id)

    if _row_exists(spreadsheet, PACK_DATA_TAB, result.pack.codice_ean):
        logger.info(
            "Skipping duplicate pack: codice_ean={} already exists in {}",
            result.pack.codice_ean,
            PACK_DATA_TAB,
        )
        return

    row = _pack_to_row(result.pack)
    headers = list(PackData.model_fields.keys())

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type(gspread.exceptions.APIError),
    )
    def _write_pack_data() -> None:
        ws = _get_or_create_worksheet(spreadsheet, PACK_DATA_TAB)
        _ensure_headers(ws, headers)
        ws.append_row(row, value_input_option="USER_ENTERED")

    _write_pack_data()
    logger.info(
        "Written pack to {}: codice_ean={}",
        PACK_DATA_TAB,
        result.pack.codice_ean,
    )

    if result.needs_review:
        review_row = row + [
            ", ".join(result.flagged_fields),
            "\n".join(result.review_reasons),
        ]
        review_headers = headers + ["flagged_fields", "review_reasons"]

        @retry(
            wait=wait_exponential(multiplier=1, min=2, max=60),
            stop=stop_after_attempt(5),
            retry=retry_if_exception_type(gspread.exceptions.APIError),
        )
        def _write_review_queue() -> None:
            ws = _get_or_create_worksheet(spreadsheet, REVIEW_QUEUE_TAB)
            _ensure_headers(ws, review_headers)
            ws.append_row(review_row, value_input_option="USER_ENTERED")

        _write_review_queue()
        logger.info(
            "Flagged pack added to {}: codice_ean={}, flagged_fields={}",
            REVIEW_QUEUE_TAB,
            result.pack.codice_ean,
            result.flagged_fields,
        )


def write_run_metadata(
    filename: str,
    outcome: str,
    latency_ms: int,
    error: str | None,
    sheet_id: str,
) -> None:
    """Append one row to run_metadata."""
    client = get_sheet_client()
    spreadsheet = client.open_by_key(sheet_id)

    row = [datetime.now(UTC).isoformat(), filename, outcome, latency_ms, error or ""]
    headers = ["timestamp", "filename", "outcome", "latency_ms", "error"]

    @retry(
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type(gspread.exceptions.APIError),
    )
    def _write() -> None:
        ws = _get_or_create_worksheet(spreadsheet, RUN_METADATA_TAB)
        _ensure_headers(ws, headers)
        ws.append_row(row, value_input_option="USER_ENTERED")

    _write()
    logger.info(
        "Run metadata written: filename={}, outcome={}, latency_ms={}",
        filename,
        outcome,
        latency_ms,
    )


def _ean_in_run_metadata_success(spreadsheet: gspread.Spreadsheet, ean: str) -> bool:
    """Return True if run_metadata records a successful run for this EAN.

    `run_metadata` is the source of truth for "have we processed this EAN":
    each successful pipeline writes a row with filename in column index 1
    and outcome="success" in column index 2. Filenames always start with
    `{EAN}_` (deterministic from the upload convention).
    """
    try:
        ws = spreadsheet.worksheet(RUN_METADATA_TAB)
    except gspread.exceptions.WorksheetNotFound:
        return False

    rows = ws.get_all_values()
    if len(rows) <= 1:  # header only or empty
        return False

    prefix = f"{ean}_"
    for row in rows[1:]:
        # Defensive: rows may be shorter than expected if columns were edited.
        if len(row) >= 3 and row[1].startswith(prefix) and row[2] == "success":
            return True
    return False


def _row_exists(sheet: gspread.Spreadsheet, tab_name: str, ean: str) -> bool:
    """Defense-in-depth dedup before appending to pack_data.

    Delegates to ``_ean_in_run_metadata_success``. The ``tab_name`` parameter
    is preserved for API compatibility but ignored — run_metadata is the
    canonical source for "already processed", because EAN is not a column
    in pack_data (it's the identity key, excluded from _SHEET_FIELDS).

    The earlier check in ``pipeline.run_single`` saves the OCR+Gemini cost on
    duplicates; this second check catches races where two concurrent pipelines
    both passed the early check before either wrote run_metadata.
    """
    return _ean_in_run_metadata_success(sheet, ean)


def has_ean_been_processed(sheet_id: str, ean: str) -> bool:
    """Public idempotency check. Opens the sheet and consults run_metadata.

    Used by pipeline.run_single to skip the heavy work (OCR + Gemini) when
    a successful run already exists for this EAN.
    """
    client = get_sheet_client()
    spreadsheet = client.open_by_key(sheet_id)
    return _ean_in_run_metadata_success(spreadsheet, ean)


def _pack_to_row(pack: PackData) -> list:
    """
    Flatten PackData to a list of cell values in Sheet column order.

    Cell mapping (matches the deliverable Sheet's example row exactly):
    - PresenceField: True -> "✅", False/None -> "❌".
    - ExtractedField with non-empty value: written as-is.
    - ExtractedField absent (None or "") in COUNT_LIKE_FIELDS: "❌".
    - ExtractedField absent (None or "") elsewhere: "N/A".
    - Plain str (the 4 deterministic manufacturer/importer constants): as-is.
    """
    row = []
    for name in pack._SHEET_FIELDS:  # type: ignore[attr-defined]
        attr = getattr(pack, name)
        if isinstance(attr, PresenceField):
            row.append(CELL_TRUE if attr.present is True else CELL_FALSE)
        elif isinstance(attr, ExtractedField):
            v = attr.value
            # Trust whatever the VLM returned (a real value, or the literal
            # string "N/A" when the VLM judges the concept does not apply).
            # Only fall back when the VLM gave back nothing at all — and then
            # the default depends on field type:
            #   - count-like fields default to ❌ ("feature absent")
            #   - all other string fields default to "N/A" ("not present on pack")
            if v not in (None, ""):
                row.append(v)
            elif name in COUNT_LIKE_FIELDS:
                row.append(CELL_FALSE)
            else:
                row.append(CELL_NA)
        else:
            # Plain str (deterministic constant) — write as-is.
            row.append(attr)
    return row


def _ensure_headers(worksheet: gspread.Worksheet, headers: list[str]) -> None:
    """
    Write headers as the first row if the worksheet is empty.
    If a first row already exists and matches, do nothing.
    If a first row exists but differs, log a warning and leave it untouched
    rather than silently overwriting potentially meaningful data.
    """
    all_values = worksheet.get_all_values()
    if not all_values:
        worksheet.append_row(headers, value_input_option="USER_ENTERED")
        return

    existing_headers = all_values[0]
    if existing_headers == headers:
        return

    logger.warning(
        "Header mismatch on worksheet '{}': expected {} but found {}. "
        "Leaving existing headers in place.",
        worksheet.title,
        headers,
        existing_headers,
    )


def _get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    """
    Return the worksheet with the given title, creating it if absent.
    Separated from _ensure_headers so each caller controls header schema independently.
    """
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        logger.info("Worksheet '{}' not found — creating it.", title)
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=50)


def verify_sheet_schema(sheet_id: str) -> None:
    """Verify that the ``pack_data`` worksheet has the right column count.

    Writes are positional via ``gspread.append_row`` — column order is what
    matters, not header text. Operators commonly customise headers to a
    human-readable form (e.g. "Nome del fabbricante" instead of
    ``nome_del_fabbricante``); we accept that as long as the column *count*
    matches the schema. A count mismatch means data would land in the wrong
    columns, so we still raise on that.

    Behaviour:
    - If the tab is missing or empty: bootstrap with canonical snake_case headers.
    - If row 1 has the expected column count: log INFO and return (regardless of header text).
    - If row 1 has fewer/more columns than the schema: raise ``SheetConfigError``.
    """
    client = get_sheet_client()
    spreadsheet = client.open_by_key(sheet_id)

    # _SHEET_FIELDS is a Pydantic v2 PrivateAttr (leading-underscore auto-private).
    # Class access returns a ModelPrivateAttr descriptor, NOT the tuple — so
    # we MUST instantiate to read the value.
    _sentinel = PackData(codice_ean="__verify__")
    expected: list[str] = list(_sentinel._SHEET_FIELDS)  # type: ignore[attr-defined]

    try:
        ws = spreadsheet.worksheet(PACK_DATA_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=PACK_DATA_TAB, rows=1000, cols=50)
        ws.append_row(expected, value_input_option="USER_ENTERED")
        logger.info(
            "Created '{}' worksheet and wrote {} header columns.",
            PACK_DATA_TAB,
            len(expected),
        )
        return

    all_values = ws.get_all_values()
    if not all_values:
        ws.append_row(expected, value_input_option="USER_ENTERED")
        logger.info(
            "Wrote {} header columns to empty '{}' worksheet.",
            len(expected),
            PACK_DATA_TAB,
        )
        return

    actual = all_values[0]
    if len(actual) == len(expected):
        if actual == expected:
            logger.info(
                "Sheet header verified: '{}' matches schema exactly ({} columns).",
                PACK_DATA_TAB,
                len(expected),
            )
        else:
            logger.info(
                "Sheet header verified: '{}' has expected column count ({}); "
                "header text differs from schema (custom labels in use, OK).",
                PACK_DATA_TAB,
                len(expected),
            )
        return

    raise SheetConfigError(
        f"Sheet column count mismatch on '{PACK_DATA_TAB}': "
        f"expected {len(expected)} columns, got {len(actual)}. "
        f"Schema: {expected}. Sheet row 1: {actual}"
    )
