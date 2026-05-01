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
from pathlib import Path

from loguru import logger

from src.schemas.pack import PackData
from src.validator import ValidationResult


PACK_DATA_TAB = "pack_data"
REVIEW_QUEUE_TAB = "review_queue"
RUN_METADATA_TAB = "run_metadata"


def get_sheet_client():
    """
    Return an authenticated gspread client.
    Reads GOOGLE_SERVICE_ACCOUNT_JSON from the environment.
    """
    raise NotImplementedError("Session 5 deliverable")


def write_pack(result: ValidationResult, sheet_id: str) -> None:
    """
    Write a validated pack to pack_data (always) and review_queue
    (if result.needs_review). Idempotent on codice_ean.
    """
    raise NotImplementedError("Session 5 deliverable")


def write_run_metadata(
    filename: str,
    outcome: str,
    latency_ms: int,
    error: str | None,
    sheet_id: str,
) -> None:
    """Append one row to run_metadata."""
    raise NotImplementedError("Session 5 deliverable")


def _row_exists(sheet, tab_name: str, ean: str) -> bool:
    """Return True if a row with the given EAN already exists in tab_name."""
    raise NotImplementedError("Session 5 deliverable")


def _pack_to_row(pack: PackData) -> list:
    """
    Flatten PackData to a list of cell values in Sheet column order.
    Order is derived from pack.content_fields() which mirrors model_fields.
    """
    raise NotImplementedError("Session 5 deliverable")
