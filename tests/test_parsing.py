"""Unit tests for src/parsing.py.

All filename tests are pure-function and require no network or file I/O.
Tests that open a real PDF use the `sample_pdf_path` fixture and are
automatically skipped when no sample PDFs are present in samples/.
"""

import pytest
from pathlib import Path

from src.parsing import parse_filename, extract_text_fields, _extract_disposal_codes_from_ocr


# ---------------------------------------------------------------------------
# parse_filename — pure function tests (no I/O, no network)
# ---------------------------------------------------------------------------

def test_parse_filename_valid() -> None:
    ean, dims, name = parse_filename("8055712771306_220x80x45_Thomas Turbato.pdf")
    assert ean == "8055712771306"
    assert dims == "220x80x45"
    assert name == "Thomas Turbato"


def test_parse_filename_underscores_replaced() -> None:
    _, _, name = parse_filename("8055712771306_220x80x45_My_Product_Name.pdf")
    assert name == "My Product Name"


def test_parse_filename_invalid() -> None:
    with pytest.raises(ValueError):
        parse_filename("not_a_valid_filename.pdf")


def test_parse_filename_ean_is_13_digits() -> None:
    ean, _, _ = parse_filename("8055712771306_220x80x45_Test.pdf")
    assert len(ean) == 13 and ean.isdigit()


def test_parse_filename_missing_extension_raises() -> None:
    with pytest.raises(ValueError):
        parse_filename("8055712771306_220x80x45_TestProduct")


def test_parse_filename_short_ean_raises() -> None:
    # EAN with 12 digits instead of 13 must not match.
    with pytest.raises(ValueError):
        parse_filename("805571277130_220x80x45_TestProduct.pdf")


def test_parse_filename_dimensions_preserved() -> None:
    _, dims, _ = parse_filename("8055712771306_150x45x180_SomeProd.pdf")
    assert dims == "150x45x180"


def test_parse_filename_single_word_product_name() -> None:
    _, _, name = parse_filename("8055712771306_220x80x45_Vibe.pdf")
    assert name == "Vibe"


def test_parse_filename_returns_tuple_of_three_strings() -> None:
    result = parse_filename("8055712771306_220x80x45_Test.pdf")
    assert isinstance(result, tuple)
    assert len(result) == 3
    assert all(isinstance(v, str) for v in result)


# ---------------------------------------------------------------------------
# extract_text_fields — integration tests (require sample PDFs)
# ---------------------------------------------------------------------------

def test_extract_text_fields_returns_packdata(sample_pdf_path: Path) -> None:
    result = extract_text_fields(sample_pdf_path)
    assert result.codice_ean
    assert len(result.codice_ean) == 13


def test_extract_text_fields_deterministic_fields(sample_pdf_path: Path) -> None:
    result = extract_text_fields(sample_pdf_path)
    assert result.nome_del_fabbricante == "MySecretCase s.r.l."



def test_extract_text_fields_confidence_bounds(sample_pdf_path: Path) -> None:
    """All confidence values in the result must be in [0.0, 1.0]."""
    result = extract_text_fields(sample_pdf_path)
    for field_name, conf in result.confidence_map().items():
        assert 0.0 <= conf <= 1.0, (
            f"Field '{field_name}' has confidence {conf} outside [0, 1]"
        )


def test_extract_text_fields_manufacturer_constant(sample_pdf_path: Path) -> None:
    result = extract_text_fields(sample_pdf_path)
    assert result.nome_del_fabbricante == "MySecretCase s.r.l."
    assert result.indirizzo_del_fabbricante == "Corso C. Colombo 7 - Milano 20144"
    assert result.nome_dell_importatore == "MySecretCase s.r.l."
    assert result.indirizzo_dell_importatore == "Corso C. Colombo 7 - Milano 20144"


def test_extract_text_fields_ean_matches_filename(sample_pdf_path: Path) -> None:
    """EAN in PackData must match the EAN parsed from the filename."""
    expected_ean, _, _ = parse_filename(sample_pdf_path.name)
    result = extract_text_fields(sample_pdf_path)
    assert result.codice_ean == expected_ean


def test_extract_text_fields_dimensioni_is_product_format(sample_pdf_path: Path) -> None:
    """If dimensioni is extracted from PDF text, its value must use the product format."""
    result = extract_text_fields(sample_pdf_path)
    if result.dimensioni.value is not None:
        # Product format: e.g. "17cm x Ø5.7cm" — must contain "cm" and "Ø".
        assert "cm" in result.dimensioni.value
        assert "Ø" in result.dimensioni.value
        assert result.dimensioni.confidence > 0.0


# ---------------------------------------------------------------------------
# _extract_disposal_codes_from_ocr — pure function tests (no I/O)
# ---------------------------------------------------------------------------

def test_disposal_codes_joined_form() -> None:
    """Joined forms like 'PAP21' and 'CPE07' in one OCR block are extracted correctly."""
    text = "some text\nPAP21\nCPE07\nmore text"
    result = _extract_disposal_codes_from_ocr(text)
    assert result["codice_smaltimento_scatola"].value == "PAP21"
    assert result["codice_smaltimento_sacchetto"].value == "CPE07"
    assert result["simboli_materiali_smaltimento"].value == "PAP21 / CPE07"


def test_disposal_codes_digit_on_next_line() -> None:
    """Prefix on one line and digit on the next line are paired correctly."""
    text = "some text\nPAP\n21\nCPE\n7\nmore text"
    result = _extract_disposal_codes_from_ocr(text)
    assert result["codice_smaltimento_scatola"].value == "PAP21"
    assert result["codice_smaltimento_sacchetto"].value == "CPE07"


def test_disposal_codes_pap_preferred_over_fr_for_scatola() -> None:
    """PAP ranks higher than FR; even if FR comes first, PAP wins scatola."""
    text = "FR\n20\nPAP\n21\n"
    result = _extract_disposal_codes_from_ocr(text)
    assert result["codice_smaltimento_scatola"].value == "PAP21"


def test_disposal_codes_digit_zero_padded() -> None:
    """Single-digit codes are zero-padded to two digits: '7' → '07'."""
    text = "CPE\n7\n"
    result = _extract_disposal_codes_from_ocr(text)
    assert result["codice_smaltimento_sacchetto"].value == "CPE07"


def test_disposal_codes_no_digits_returns_empty() -> None:
    """Prefix with no adjacent digits produces no output (no guessing)."""
    text = "some text\nPAP\nCPE\nsome other text far away 21\n"
    result = _extract_disposal_codes_from_ocr(text)
    # PAP has no adjacent digit line; '21' appears on a later non-adjacent line
    assert result.get("codice_smaltimento_scatola") is None
    assert result.get("codice_smaltimento_sacchetto") is None


def test_disposal_codes_multi_digit_line_not_paired() -> None:
    """Lines with 3+ digits (e.g. EAN prefix) are NOT treated as code digits."""
    text = "FR\n0420\nPAP\n21\n"
    result = _extract_disposal_codes_from_ocr(text)
    # '0420' has 4 digits — should not pair with FR; PAP21 should still be found
    scatola = result.get("codice_smaltimento_scatola")
    assert scatola is not None and scatola.value == "PAP21"


def test_disposal_codes_confidence_is_0_7() -> None:
    """Parser-extracted disposal codes carry confidence=0.7."""
    text = "PAP21\nCPE07\n"
    result = _extract_disposal_codes_from_ocr(text)
    assert result["codice_smaltimento_scatola"].confidence == 0.7
    assert result["codice_smaltimento_sacchetto"].confidence == 0.7


def test_disposal_codes_empty_text_returns_empty() -> None:
    """Empty input returns empty dict."""
    result = _extract_disposal_codes_from_ocr("")
    assert result == {}


def test_disposal_codes_simboli_join_order() -> None:
    """simboli_materiali_smaltimento lists codes in scan order, separated by ' / '."""
    text = "PAP21\nCPE07\n"
    result = _extract_disposal_codes_from_ocr(text)
    assert result["simboli_materiali_smaltimento"].value == "PAP21 / CPE07"
