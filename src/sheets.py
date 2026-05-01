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

import os
from datetime import datetime, timezone

import gspread
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.schemas.pack import PackData
from src.validator import ValidationResult


PACK_DATA_TAB = "pack_data"
REVIEW_QUEUE_TAB = "review_queue"
RUN_METADATA_TAB = "run_metadata"


class SheetConfigError(Exception):
    pass


def get_sheet_client() -> gspread.Client:
    """
    Return an authenticated gspread client.
    Reads GOOGLE_SERVICE_ACCOUNT_JSON from the environment.
    """
    path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not path:
        raise SheetConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set. "
            "Set it to the path of a valid service account JSON key file."
        )
    if not os.path.isfile(path):
        raise SheetConfigError(
            f"Service account key file not found at path: {path!r}. "
            "Ensure GOOGLE_SERVICE_ACCOUNT_JSON points to an existing file."
        )
    return gspread.service_account(filename=path)


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

    row = [datetime.now(timezone.utc).isoformat(), filename, outcome, latency_ms, error or ""]
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


def _row_exists(sheet: gspread.Spreadsheet, tab_name: str, ean: str) -> bool:
    """Return True if a row with the given EAN already exists in tab_name."""
    try:
        worksheet = sheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return False

    all_values = worksheet.get_all_values()
    if not all_values:
        return False

    # codice_ean is the first column; skip the header row if present
    for row in all_values:
        if row and row[0] == ean:
            return True
    return False


def _pack_to_row(pack: PackData) -> list:
    """
    Flatten PackData to a list of cell values in Sheet column order.
    Order is derived from pack.content_fields() which mirrors model_fields.
    """
    fields = pack.content_fields()
    row = []
    for value in fields.values():
        if value is None:
            row.append("")
        elif value is True:
            row.append("Sì")
        elif value is False:
            row.append("No")
        else:
            row.append(value)
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


def _get_or_create_worksheet(
    spreadsheet: gspread.Spreadsheet, title: str
) -> gspread.Worksheet:
    """
    Return the worksheet with the given title, creating it if absent.
    Separated from _ensure_headers so each caller controls header schema independently.
    """
    try:
        return spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        logger.info("Worksheet '{}' not found — creating it.", title)
        return spreadsheet.add_worksheet(title=title, rows=1000, cols=50)
