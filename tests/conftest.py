"""Shared pytest fixtures."""

from pathlib import Path

import pytest

SAMPLES_DIR = Path(__file__).parent.parent / "samples"


@pytest.fixture
def sample_pdf_path() -> Path:
    """Return the path to the first available sample PDF, skipping if none present."""
    pdfs = sorted(SAMPLES_DIR.glob("*.pdf"))
    if not pdfs:
        pytest.skip("No sample PDFs in samples/ — add sanitized PDFs to run this test")
    return pdfs[0]


@pytest.fixture
def all_sample_pdfs() -> list[Path]:
    """Return all sample PDFs, skipping if none present."""
    pdfs = sorted(SAMPLES_DIR.glob("*.pdf"))
    if not pdfs:
        pytest.skip("No sample PDFs in samples/ — add sanitized PDFs to run this test")
    return pdfs
