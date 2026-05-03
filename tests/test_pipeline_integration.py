"""
Integration test for the end-to-end pipeline.

Tests the full orchestration path:
  parsing → (mocked) VLM → validation → (mocked) Sheet write

No network calls are made — Gemini and Google Sheets are both mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from src.schemas.pack import PackData

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SAMPLES_DIR = Path(__file__).parent.parent / "samples"
_CACHE_DIR = Path(__file__).parent.parent / "eval_vlm_cache"

_THOMAS_PDF = _SAMPLES_DIR / "8055712771306_220x80x45_Thomas Turbato.pdf"
_THOMAS_CACHE = _CACHE_DIR / "8055712771306_220x80x45_Thomas Turbato__vlm-image+ocr.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cached_vlm(cache_path: Path) -> PackData:
    """Parse the eval_vlm_cache JSON into a PackData instance."""
    with cache_path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    return PackData.model_validate(data["vlm_pack"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def thomas_pdf() -> Path:
    if not _THOMAS_PDF.exists():
        pytest.skip(f"Sample PDF not found: {_THOMAS_PDF}")
    return _THOMAS_PDF


@pytest.fixture
def thomas_vlm_pack() -> PackData:
    if not _THOMAS_CACHE.exists():
        pytest.skip(f"VLM cache not found: {_THOMAS_CACHE}")
    return _load_cached_vlm(_THOMAS_CACHE)


# ---------------------------------------------------------------------------
# Mock builders
# ---------------------------------------------------------------------------

def _make_mock_spreadsheet() -> tuple[MagicMock, dict[str, list]]:
    """
    Return a (mock_spreadsheet, captured_rows) pair.

    captured_rows["pack_data"]     — rows appended to pack_data tab
    captured_rows["review_queue"]  — rows appended to review_queue tab
    captured_rows["run_metadata"]  — rows appended to run_metadata tab

    The mock spreadsheet is wired so:
    - worksheet(name) raises WorksheetNotFound the first time (triggering
      add_worksheet) then returns a mock worksheet that records append_row calls.
    - _row_exists → False (no duplicate) so the pack always gets written.
    """
    import gspread.exceptions

    captured_rows: dict[str, list] = {
        "pack_data": [],
        "review_queue": [],
        "run_metadata": [],
    }

    def _make_worksheet(title: str) -> MagicMock:
        ws = MagicMock(name=f"worksheet_{title}")
        ws.title = title
        ws.get_all_values.return_value = []  # empty → _row_exists returns False

        def _append_row(row: list, **kwargs: Any) -> None:
            captured_rows[title].append(list(row))

        ws.append_row.side_effect = _append_row
        return ws

    worksheets: dict[str, MagicMock] = {}

    def _worksheet(title: str) -> MagicMock:
        if title not in worksheets:
            raise gspread.exceptions.WorksheetNotFound(title)
        return worksheets[title]

    def _add_worksheet(title: str, **kwargs: Any) -> MagicMock:
        ws = _make_worksheet(title)
        worksheets[title] = ws
        return ws

    mock_spreadsheet = MagicMock(name="spreadsheet")
    mock_spreadsheet.worksheet.side_effect = _worksheet
    mock_spreadsheet.add_worksheet.side_effect = _add_worksheet

    return mock_spreadsheet, captured_rows


# ---------------------------------------------------------------------------
# Integration test
# ---------------------------------------------------------------------------

class TestPipelineIntegration:
    def test_end_to_end_thomas_turbato(
        self,
        thomas_pdf: Path,
        thomas_vlm_pack: PackData,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Full pipeline run on Thomas Turbato PDF with mocked VLM and Sheets.

        Verifies:
        1. Return value is PackData with correct EAN.
        2. Exactly one row written to pack_data tab.
        3. Row has length == len(PackData._SHEET_FIELDS) == 34.
        4. First cell == "MySecretCase s.r.l." (column order preserved).
        5. write_run_metadata called with outcome == "success".
        """
        from src.pipeline import run_single

        # Set up env vars for Sheet write path.
        sa_json = '{"type": "service_account"}'  # content irrelevant — gspread is mocked
        monkeypatch.setenv("GOOGLE_SHEET_ID", "test-sheet-id")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", sa_json)

        mock_spreadsheet, captured_rows = _make_mock_spreadsheet()
        mock_client = MagicMock(name="gspread_client")
        mock_client.open_by_key.return_value = mock_spreadsheet

        with (
            patch("src.pipeline.extract_visual_fields", return_value=thomas_vlm_pack),
            patch("gspread.service_account_from_dict", return_value=mock_client),
        ):
            result: PackData = run_single(thomas_pdf)

        # 1. Correct EAN.
        assert result.codice_ean == "8055712771306"

        # _SHEET_FIELDS is a Pydantic PrivateAttr — must be read from an instance.
        # Use the result pack since we already have it (or any PackData instance).
        sheet_fields: tuple[str, ...] = result._SHEET_FIELDS  # type: ignore[attr-defined]
        expected_col_count = len(sheet_fields)
        assert expected_col_count == 34  # sanity: schema hasn't drifted

        # 2. Exactly one data row written to pack_data (beyond the header row).
        # _ensure_headers appends the header as row 0 (len == model_fields == 35);
        # the data row (from _pack_to_row) has len == _SHEET_FIELDS == 34.
        data_rows = [r for r in captured_rows["pack_data"] if len(r) == expected_col_count]
        assert len(data_rows) == 1, (
            f"Expected 1 data row in pack_data (34 cols), got {len(data_rows)}. "
            f"All rows: {[len(r) for r in captured_rows['pack_data']]}"
        )

        data_row = data_rows[0]

        # 3. Row length matches the 34-column schema.
        assert len(data_row) == expected_col_count, (
            f"Row has {len(data_row)} cells, expected {expected_col_count}"
        )

        # 4. First cell is the constant manufacturer name.
        assert data_row[0] == "MySecretCase s.r.l.", (
            f"First cell should be manufacturer name, got: {data_row[0]!r}"
        )

        # 5. write_run_metadata was called with outcome == "success".
        # run_metadata rows: first is the header, second is the data row.
        # Row layout: [timestamp, filename, outcome, latency_ms, error]
        metadata_data_rows = [
            r for r in captured_rows["run_metadata"] if len(r) == 5 and r[0] != "timestamp"
        ]
        assert len(metadata_data_rows) >= 1, "No run_metadata data row was written"
        metadata_row = metadata_data_rows[0]
        assert metadata_row[2] == "success", (
            f"Expected outcome='success', got {metadata_row[2]!r}"
        )
