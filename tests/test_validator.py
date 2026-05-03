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
#
# New contract (reads from codice_smaltimento_scatola / _sacchetto / _doypack
# plus simboli_materiali_smaltimento; returns ExtractedField):
#   - Match  → value = human label (e.g. "scatola + sacchetto"), confidence=0.9
#   - Mismatch → value = None, confidence=0.2
#   - Insufficient data → value = None, confidence=0.0 (default ExtractedField)
# ---------------------------------------------------------------------------

class TestCheckTrimanConsistency:
    def test_match_returns_label(self) -> None:
        # scatola=FR 7, sacchetto=CPE 21 → text prefixes {FR, CPE}
        # visual "FR CPE recycling icons" → visual prefixes {FR, CPE} → match
        pack = _minimal_pack(
            codice_smaltimento_scatola=_extracted("FR 7", 0.9),
            codice_smaltimento_sacchetto=_extracted("CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE recycling icons", 0.9),
        )
        result = _check_triman_consistency(pack)
        assert isinstance(result, ExtractedField)
        assert result.value == "scatola + sacchetto"
        assert result.confidence == 0.9

    def test_mismatch_returns_none_value_low_confidence(self) -> None:
        # scatola=FR 7, sacchetto=CPE 21 → text prefixes {FR, CPE}
        # visual "ABS PE recycling symbols" → visual prefixes {ABS, PE} → mismatch
        pack = _minimal_pack(
            codice_smaltimento_scatola=_extracted("FR 7", 0.9),
            codice_smaltimento_sacchetto=_extracted("CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("ABS PE recycling symbols", 0.9),
        )
        result = _check_triman_consistency(pack)
        assert isinstance(result, ExtractedField)
        assert result.value is None
        assert result.confidence == 0.2

    def test_missing_text_fields_returns_default(self) -> None:
        # No disposal code fields set → insufficient data → default ExtractedField
        pack = _minimal_pack(
            simboli_materiali_smaltimento=_extracted("FR recycling icon", 0.9),
        )
        result = _check_triman_consistency(pack)
        assert isinstance(result, ExtractedField)
        assert result.value is None
        assert result.confidence == 0.0

    def test_missing_visual_field_returns_default(self) -> None:
        # No visual field → cannot verify → default ExtractedField
        pack = _minimal_pack(
            codice_smaltimento_scatola=_extracted("FR 7", 0.9),
        )
        result = _check_triman_consistency(pack)
        assert isinstance(result, ExtractedField)
        assert result.value is None
        assert result.confidence == 0.0

    def test_both_fields_missing_returns_default(self) -> None:
        # Nothing set — all defaults
        pack = _minimal_pack()
        result = _check_triman_consistency(pack)
        assert isinstance(result, ExtractedField)
        assert result.value is None
        assert result.confidence == 0.0

    def test_numeric_suffixes_ignored(self) -> None:
        # "FR 7" in text vs bare "FR" in visual → both normalise to prefix "FR" → match
        pack = _minimal_pack(
            codice_smaltimento_scatola=_extracted("FR 7", 0.9),
            simboli_materiali_smaltimento=_extracted("FR", 0.9),
        )
        result = _check_triman_consistency(pack)
        assert isinstance(result, ExtractedField)
        assert result.value == "scatola"
        assert result.confidence == 0.9

    def test_case_insensitive(self) -> None:
        # Lower-case codes in scatola + sacchetto vs upper-case in visual → match
        pack = _minimal_pack(
            codice_smaltimento_scatola=_extracted("fr 7", 0.9),
            codice_smaltimento_sacchetto=_extracted("cpe", 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE", 0.9),
        )
        result = _check_triman_consistency(pack)
        assert isinstance(result, ExtractedField)
        assert result.value == "scatola + sacchetto"
        assert result.confidence == 0.9


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
        """A pack with high confidence on all envelope fields present in the schema."""
        return _minimal_pack(
            tipo_o_modello=_extracted("Model A", 0.9),
            numero_di_serie_lotto=_extracted("LOT: L2024001", 0.9),
            lotto=_extracted("L2024001", 0.9),
            codice_asin=_extracted("B0ABC12345", 0.9),
            materiale=_extracted("Silicone", 0.92),
            impermeabilita=_extracted("IPX6", 0.9),
            modalita_di_ricarica=_extracted("Ricarica magnetica", 0.9),
            dimensioni=_extracted("17cm x Ø5.7cm", 0.9),
            capacita_batteria_e_tensione_nominale=_extracted("3.7V 800mAh", 0.9),
            n_vibrazioni=_extracted("10", 0.9),
            n_velocita=_extracted("3", 0.9),
            n_modalita_suzione=_extracted("0", 0.9),
            n_modalita_tapping=_extracted("0", 0.9),
            n_modalita_rotazione=_extracted("0", 0.9),
            codice_smaltimento_scatola=_extracted("FR 7", 0.9),
            codice_smaltimento_sacchetto=_extracted("CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE icons", 0.9),
            simbolo_ce=_presence(True, 0.95),
            simbolo_raee=_presence(True, 0.9),
            simbolo_ukca=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.88),
            simbolo_smaltimento_spagnolo=_presence(False, 0.9),
            qr_code_junker=_presence(True, 0.9),
            simbolo_garanzia_2_anni=_presence(True, 0.9),
            simbolo_libretto_informativo=_presence(True, 0.9),
            strap_on_compatibile=_presence(False, 0.9),
            funzione_riscaldante=_presence(False, 0.9),
            sexy_ideas=_presence(True, 0.9),
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
            tipo_o_modello=_extracted("Vibrator X", 0.98),
            simbolo_ce=_presence(False, 0.95),   # CE explicitly absent
            simbolo_raee=_presence(True, 0.95),
            simbolo_triman=_presence(True, 0.95),
        )
        vlm = _minimal_pack(
            tipo_o_modello=_extracted("Vibrator X", 0.98),
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
        # scatola=FR 7, sacchetto=CPE 21 and visual "FR CPE icons" → match
        # Expected label: "scatola + sacchetto"
        parser = _minimal_pack(
            codice_smaltimento_scatola=_extracted("FR 7", 0.9),
            codice_smaltimento_sacchetto=_extracted("CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE icons", 0.9),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        vlm = _minimal_pack(
            codice_smaltimento_scatola=_extracted("FR 7", 0.9),
            codice_smaltimento_sacchetto=_extracted("CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("FR CPE icons", 0.9),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        result = validate(parser, vlm)
        assert isinstance(result.pack.contenuto_triman_corretto, ExtractedField)
        assert result.pack.contenuto_triman_corretto.value == "scatola + sacchetto"
        assert result.pack.contenuto_triman_corretto.confidence == 0.9

    def test_triman_mismatch_sets_none_value(self) -> None:
        # scatola=FR 7, sacchetto=CPE 21 vs visual "ABS PE icons" → mismatch
        # Expected: value=None, confidence=0.2
        parser = _minimal_pack(
            codice_smaltimento_scatola=_extracted("FR 7", 0.9),
            codice_smaltimento_sacchetto=_extracted("CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("ABS PE icons", 0.9),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        vlm = _minimal_pack(
            codice_smaltimento_scatola=_extracted("FR 7", 0.9),
            codice_smaltimento_sacchetto=_extracted("CPE 21", 0.9),
            simboli_materiali_smaltimento=_extracted("ABS PE icons", 0.9),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        result = validate(parser, vlm)
        assert isinstance(result.pack.contenuto_triman_corretto, ExtractedField)
        assert result.pack.contenuto_triman_corretto.value is None
        assert result.pack.contenuto_triman_corretto.confidence == 0.2

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
        # tipo_o_modello replaces the removed nome_prodotto field; same role.
        parser = _minimal_pack(
            tipo_o_modello=_extracted("Product", 0.2),   # low confidence
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        vlm = _minimal_pack(
            tipo_o_modello=_extracted("Product", 0.2),
            simbolo_ce=_presence(True, 0.9),
            simbolo_raee=_presence(True, 0.9),
            simbolo_triman=_presence(True, 0.9),
        )
        result = validate(parser, vlm)
        assert "tipo_o_modello" in result.flagged_fields


class TestBackfillDisposalDigitsFromSimboli:
    """Validator back-fill: per-field disposal codes recover missing digits
    from the joined-list `simboli_materiali_smaltimento`."""

    def _empty_pack(self) -> PackData:
        return PackData(codice_ean="1234567890123")

    def test_patches_bare_prefix_from_simboli(self) -> None:
        from src.validator import _backfill_disposal_digits_from_simboli

        pack = self._empty_pack()
        pack.simboli_materiali_smaltimento = ExtractedField(
            value="PAP21 / CPE07", confidence=0.9, evidence="back panel"
        )
        pack.codice_smaltimento_sacchetto = ExtractedField(
            value="CPE", confidence=0.8, evidence="vlm read prefix only"
        )
        _backfill_disposal_digits_from_simboli(pack)
        assert pack.codice_smaltimento_sacchetto.value == "CPE07"
        assert "back-filled" in (pack.codice_smaltimento_sacchetto.evidence or "")

    def test_leaves_full_code_untouched(self) -> None:
        from src.validator import _backfill_disposal_digits_from_simboli

        pack = self._empty_pack()
        pack.simboli_materiali_smaltimento = ExtractedField(
            value="PAP21 / CPE07", confidence=0.9
        )
        pack.codice_smaltimento_scatola = ExtractedField(value="PAP21", confidence=0.9)
        _backfill_disposal_digits_from_simboli(pack)
        assert pack.codice_smaltimento_scatola.value == "PAP21"
        assert "back-filled" not in (pack.codice_smaltimento_scatola.evidence or "")

    def test_noop_when_simboli_empty(self) -> None:
        from src.validator import _backfill_disposal_digits_from_simboli

        pack = self._empty_pack()
        pack.codice_smaltimento_sacchetto = ExtractedField(value="CPE", confidence=0.8)
        _backfill_disposal_digits_from_simboli(pack)
        assert pack.codice_smaltimento_sacchetto.value == "CPE"

    def test_fills_empty_field_when_unique_prefix_class(self) -> None:
        """If the per-field is empty and simboli has exactly one plastic
        prefix, fill the sacchetto field. Conservative: don't guess between
        multiple."""
        from src.validator import _backfill_disposal_digits_from_simboli

        pack = self._empty_pack()
        pack.simboli_materiali_smaltimento = ExtractedField(
            value="PAP21 / CPE07", confidence=0.9
        )
        # sacchetto starts empty.
        _backfill_disposal_digits_from_simboli(pack)
        assert pack.codice_smaltimento_sacchetto.value == "CPE07"
        assert pack.codice_smaltimento_scatola.value == "PAP21"
