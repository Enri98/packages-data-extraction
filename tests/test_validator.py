"""
Unit tests for src/validator.py.

All tests are pure — no PDFs, no network, no disk access.
"""

import pytest

from src.schemas.pack import ExtractedField, PackData, PresenceField
from src.validator import (
    REVIEW_THRESHOLD,
    ValidationResult,
    _check_triman_consistency,
    _merge_field,
    _required_symbols_present,
    validate,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _minimal_pack(**overrides) -> PackData:
    """Return a PackData with all envelope fields defaulted to empty."""
    base = dict(codice_ean="8055712771306")
    base.update(overrides)
    return PackData(**base)


def _extracted(value: str | None, confidence: float, evidence: str | None = None) -> ExtractedField:
    return ExtractedField(value=value, confidence=confidence, evidence=evidence)


def _presence(present: bool | None, confidence: float, evidence: str | None = None) -> PresenceField:
    return PresenceField(present=present, confidence=confidence, evidence=evidence)


# ---------------------------------------------------------------------------
# _merge_field — ExtractedField
# ---------------------------------------------------------------------------

class TestMergeFieldExtracted:
    def test_higher_confidence_wins_parser(self) -> None:
        parser = _extracted("parsed value", confidence=0.9)
        vlm = _extracted("vlm value", confidence=0.6)
        result = _merge_field(parser, vlm)
        assert isinstance(result, ExtractedField)
        assert result.value == "parsed value"
        assert result.confidence == 0.9

    def test_vlm_wins_higher_confidence(self) -> None:
        parser = _extracted("parsed value", confidence=0.5)
        vlm = _extracted("vlm value", confidence=0.95)
        result = _merge_field(parser, vlm)
        assert isinstance(result, ExtractedField)
        assert result.value == "vlm value"
        assert result.confidence == 0.95

    def test_tie_goes_to_parser(self) -> None:
        parser = _extracted("parsed value", confidence=0.8)
        vlm = _extracted("same value", confidence=0.8)
        result = _merge_field(parser, vlm)
        # Tie — parser wins; values differ so conflict penalty applies
        assert result.value == "parsed value"

    def test_conflict_recorded_when_both_confident_and_disagree(self) -> None:
        parser = _extracted("parsed value", confidence=0.9, evidence="page 1 line 3")
        vlm = _extracted("vlm different", confidence=0.7)
        result = _merge_field(parser, vlm)
        # Parser wins (higher confidence).
        assert result.value == "parsed value"
        # Confidence penalised by 0.1.
        assert abs(result.confidence - 0.8) < 1e-9
        # Conflict note appended to evidence.
        assert "[CONFLICT: other source said: vlm different]" in result.evidence

    def test_conflict_note_appended_to_existing_evidence(self) -> None:
        parser = _extracted("A", confidence=0.85, evidence="existing evidence")
        vlm = _extracted("B", confidence=0.75)  # both >= 0.7 threshold → conflict fires
        result = _merge_field(parser, vlm)
        assert result.evidence.startswith("existing evidence")
        assert "[CONFLICT:" in result.evidence

    def test_no_conflict_when_values_agree(self) -> None:
        parser = _extracted("same", confidence=0.9)
        vlm = _extracted("same", confidence=0.8)
        result = _merge_field(parser, vlm)
        assert result.confidence == 0.9
        assert result.evidence is None or "[CONFLICT" not in (result.evidence or "")

    def test_one_side_missing_parser_wins(self) -> None:
        parser = _extracted("only parser", confidence=0.7)
        vlm = _extracted(None, confidence=0.0)
        result = _merge_field(parser, vlm)
        assert result.value == "only parser"

    def test_one_side_missing_vlm_wins(self) -> None:
        parser = _extracted(None, confidence=0.0)
        vlm = _extracted("only vlm", confidence=0.85)
        result = _merge_field(parser, vlm)
        assert result.value == "only vlm"

    def test_both_missing_returns_empty_extracted(self) -> None:
        parser = _extracted(None, confidence=0.0)
        vlm = _extracted(None, confidence=0.0)
        result = _merge_field(parser, vlm)
        assert isinstance(result, ExtractedField)
        assert result.value is None

    def test_none_inputs_return_empty_extracted(self) -> None:
        result = _merge_field(None, None)
        assert isinstance(result, ExtractedField)
        assert result.value is None

    def test_does_not_mutate_inputs(self) -> None:
        parser = _extracted("A", confidence=0.9, evidence="orig")
        vlm = _extracted("B", confidence=0.7)
        original_evidence = parser.evidence
        _merge_field(parser, vlm)
        assert parser.evidence == original_evidence


# ---------------------------------------------------------------------------
# _merge_field — PresenceField
# ---------------------------------------------------------------------------

class TestMergeFieldPresence:
    def test_higher_confidence_wins(self) -> None:
        parser = _presence(True, confidence=0.9)
        vlm = _presence(False, confidence=0.6)
        result = _merge_field(parser, vlm)
        assert isinstance(result, PresenceField)
        assert result.present is True

    def test_conflict_recorded_for_presence_fields(self) -> None:
        parser = _presence(True, confidence=0.8)
        vlm = _presence(False, confidence=0.75)
        result = _merge_field(parser, vlm)
        assert result.present is True
        assert abs(result.confidence - 0.7) < 1e-9
        assert "[CONFLICT: other source said: False]" in result.evidence

    def test_one_side_missing_presence(self) -> None:
        parser = _presence(None, confidence=0.0)
        vlm = _presence(True, confidence=0.9)
        result = _merge_field(parser, vlm)
        assert result.present is True

    def test_both_missing_returns_empty_presence(self) -> None:
        result = _merge_field(
            _presence(None, 0.0),
            _presence(None, 0.0),
        )
        assert isinstance(result, PresenceField)
        assert result.present is None


# ---------------------------------------------------------------------------
# _check_triman_consistency
# ---------------------------------------------------------------------------

class TestCheckTrimanConsistency:
    def test_match_returns_true(self) -> None:
        pack = _minimal_pack(
            codici_smaltimento_materiali=_extracted("FR 7 | CPE 21 | PAP", 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE PAP recycling icons", 0.9),
        )
        assert _check_triman_consistency(pack) is True

    def test_mismatch_returns_false(self) -> None:
        pack = _minimal_pack(
            codici_smaltimento_materiali=_extracted("FR 7 | CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("ABS PE recycling symbols", 0.9),
        )
        assert _check_triman_consistency(pack) is False

    def test_missing_text_field_returns_none(self) -> None:
        pack = _minimal_pack(
            codici_smaltimento_materiali=_extracted(None, 0.0),
            simboli_materiali_smaltimento=_extracted("FR recycling icon", 0.9),
        )
        assert _check_triman_consistency(pack) is None

    def test_missing_visual_field_returns_none(self) -> None:
        pack = _minimal_pack(
            codici_smaltimento_materiali=_extracted("FR 7", 0.9),
            simboli_materiali_smaltimento=_extracted(None, 0.0),
        )
        assert _check_triman_consistency(pack) is None

    def test_both_fields_missing_returns_none(self) -> None:
        pack = _minimal_pack()
        assert _check_triman_consistency(pack) is None

    def test_numeric_suffixes_ignored(self) -> None:
        # "FR 7" and "FR" should be treated as the same code.
        pack = _minimal_pack(
            codici_smaltimento_materiali=_extracted("FR 7", 0.9),
            simboli_materiali_smaltimento=_extracted("FR", 0.9),
        )
        assert _check_triman_consistency(pack) is True

    def test_case_insensitive(self) -> None:
        pack = _minimal_pack(
            codici_smaltimento_materiali=_extracted("fr 7 | cpe", 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE", 0.9),
        )
        assert _check_triman_consistency(pack) is True


# ---------------------------------------------------------------------------
# _required_symbols_present
# ---------------------------------------------------------------------------

class TestRequiredSymbolsPresent:
    def test_all_present_returns_empty_list(self) -> None:
        pack = _minimal_pack(
            simbolo_ce=_presence(True, 0.95),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.88),
        )
        assert _required_symbols_present(pack) == []

    def test_ce_absent_flagged(self) -> None:
        pack = _minimal_pack(
            simbolo_ce=_presence(False, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.88),
        )
        absent = _required_symbols_present(pack)
        assert "simbolo_ce" in absent
        assert "simbolo_raee" not in absent
        assert "simbolo_triman" not in absent

    def test_raee_absent_flagged(self) -> None:
        pack = _minimal_pack(
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(False, 0.85),
            simbolo_triman=_presence(True, 0.88),
        )
        assert _required_symbols_present(pack) == ["simbolo_raee"]

    def test_uncertain_low_confidence_flagged(self) -> None:
        # present=None with confidence < 0.5 is treated as uncertain → flag
        pack = _minimal_pack(
            simbolo_ce=_presence(None, 0.3),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.88),
        )
        absent = _required_symbols_present(pack)
        assert "simbolo_ce" in absent

    def test_uncertain_high_confidence_not_flagged(self) -> None:
        # present=None but confidence >= 0.5 means the VLM saw something — do not flag
        pack = _minimal_pack(
            simbolo_ce=_presence(None, 0.6),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.88),
        )
        assert _required_symbols_present(pack) == []

    def test_ukca_not_required(self) -> None:
        # UKCA is optional — absent UKCA must not trigger a flag.
        pack = _minimal_pack(
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.88),
            simbolo_ukca=_presence(False, 0.95),
        )
        assert _required_symbols_present(pack) == []


# ---------------------------------------------------------------------------
# validate (integration of the full function)
# ---------------------------------------------------------------------------

class TestValidate:
    def _high_confidence_pack(self) -> PackData:
        """A parser pack with high confidence on ALL envelope fields."""
        return _minimal_pack(
            nome_prodotto=_extracted("Vibrator X", 0.95),
            tipo_o_modello=_extracted("Model A", 0.9),
            colore=_extracted("Pink", 0.9),
            codice_asin=_extracted("B0ABC12345", 0.9),
            materiale=_extracted("Silicone", 0.92),
            codici_smaltimento_materiali=_extracted("FR 7 | CPE 21", 0.9),
            lotto=_extracted("L2024001", 0.9),
            paese_di_produzione=_extracted("China", 0.9),
            capacita_batteria_e_tensione_nominale=_extracted("3.7V 800mAh", 0.9),
            tempo_di_carica=_extracted("60 min", 0.9),
            durata_utilizzo=_extracted("90 min", 0.9),
            istruzioni_carica=_extracted("Charge before use", 0.9),
            n_vibrazioni=_extracted("10", 0.9),
            livello_impermeabilita=_extracted("IPX7", 0.9),
            avvertenze=_extracted("Keep away from children", 0.9),
            eta_minima=_extracted("+18", 0.9),
            lingue_sulla_confezione=_extracted("IT, EN, DE", 0.9),
            sito_web=_extracted("www.mysecretcase.com", 0.9),
            assistenza_clienti=_extracted("support@mysecretcase.com", 0.9),
            sexy_ideas=_extracted("Exciting experiences await", 0.9),
            simbolo_ce=_presence(True, 0.95),
            simbolo_raee=_presence(True, 0.9),
            simbolo_ukca=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.88),
            simbolo_eta_minima=_presence(True, 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE icons", 0.9),
            qr_code_junker=_extracted("https://junker.app/scan/123", 0.9),
        )

    def test_needs_review_false_when_all_confident(self) -> None:
        parser = self._high_confidence_pack()
        vlm = self._high_confidence_pack()
        result = validate(parser, vlm)
        assert isinstance(result, ValidationResult)
        assert result.overall_confidence >= REVIEW_THRESHOLD
        assert result.needs_review is False

    def test_needs_review_true_when_low_overall_confidence(self) -> None:
        parser = _minimal_pack(
            simbolo_ce=_presence(True, 0.3),
            simbolo_raee=_presence(True, 0.3),
            simbolo_triman=_presence(True, 0.3),
        )
        vlm = _minimal_pack(
            simbolo_ce=_presence(True, 0.3),
            simbolo_raee=_presence(True, 0.3),
            simbolo_triman=_presence(True, 0.3),
        )
        result = validate(parser, vlm)
        assert result.needs_review is True
        assert result.overall_confidence < REVIEW_THRESHOLD

    def test_needs_review_true_when_required_symbol_absent(self) -> None:
        parser = _minimal_pack(
            nome_prodotto=_extracted("Vibrator X", 0.98),
            simbolo_ce=_presence(False, 0.95),   # CE explicitly absent
            simbolo_raee=_presence(True, 0.95),
            simbolo_triman=_presence(True, 0.95),
        )
        vlm = _minimal_pack(
            nome_prodotto=_extracted("Vibrator X", 0.98),
            simbolo_ce=_presence(False, 0.95),
            simbolo_raee=_presence(True, 0.95),
            simbolo_triman=_presence(True, 0.95),
        )
        result = validate(parser, vlm)
        assert result.needs_review is True
        assert "simbolo_ce" in result.flagged_fields
        # Overall confidence capped at 0.5 when a required symbol is absent.
        assert result.overall_confidence <= 0.5

    def test_deterministic_fields_taken_from_parser(self) -> None:
        parser = _minimal_pack(
            dimensioni=ExtractedField(value="17cm x Ø5.7cm", confidence=0.9)
        )
        vlm = _minimal_pack(
            codice_ean="9999999999999",
            dimensioni=ExtractedField(value="25cm x Ø3cm", confidence=0.6),
        )
        result = validate(parser, vlm)
        # codice_ean is deterministic — parser is authoritative.
        assert result.pack.codice_ean == "8055712771306"
        # dimensioni is text_in_pdf; higher-confidence parser value wins.
        assert result.pack.dimensioni.value == "17cm x Ø5.7cm"

    def test_triman_derived_field_set_correctly(self) -> None:
        parser = _minimal_pack(
            codici_smaltimento_materiali=_extracted("FR 7 | CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE icons", 0.9),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        vlm = _minimal_pack(
            codici_smaltimento_materiali=_extracted("FR 7 | CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE icons", 0.9),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        result = validate(parser, vlm)
        assert result.pack.contenuto_triman_corretto is True

    def test_triman_mismatch_sets_false(self) -> None:
        parser = _minimal_pack(
            codici_smaltimento_materiali=_extracted("FR 7 | CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("ABS PE icons", 0.9),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        vlm = _minimal_pack(
            codici_smaltimento_materiali=_extracted("FR 7 | CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("ABS PE icons", 0.9),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        result = validate(parser, vlm)
        assert result.pack.contenuto_triman_corretto is False

    def test_review_reasons_populated_for_each_flag(self) -> None:
        parser = _minimal_pack(
            simbolo_ce=_presence(False, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        vlm = _minimal_pack(
            simbolo_ce=_presence(False, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        result = validate(parser, vlm)
        assert any("simbolo_ce" in r for r in result.review_reasons)

    def test_flagged_fields_contain_low_confidence_fields(self) -> None:
        parser = _minimal_pack(
            nome_prodotto=_extracted("Product", 0.2),   # low confidence
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        vlm = _minimal_pack(
            nome_prodotto=_extracted("Product", 0.2),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        result = validate(parser, vlm)
        assert "nome_prodotto" in result.flagged_fields
