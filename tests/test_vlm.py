"""Unit tests for src.vlm — pure, no network, no GEMINI_API_KEY required."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.schemas.pack import ExtractedField, PackData, PresenceField
from src.vlm import (
    _PRESENCE_FIELDS,
    _VLM_OWNS_VISUAL,
    _VLM_VERIFIES_TEXT,
    ExtractedFieldResponse,
    PresenceFieldResponse,
    VLMNotAuthorizedError,
    _build_response_model,
    _build_schema_fragment,
    _filename_context,
    _format_ocr_block,
    _merge_vlm_response,
    _require_live_gate,
    extract_visual_fields,
)

# ---------------------------------------------------------------------------
# Authorization gate
# ---------------------------------------------------------------------------


class TestLiveGate:
    def test_raises_when_gemini_live_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GEMINI_LIVE", raising=False)
        with pytest.raises(VLMNotAuthorizedError):
            _require_live_gate()

    def test_raises_when_gemini_live_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_LIVE", "0")
        with pytest.raises(VLMNotAuthorizedError):
            _require_live_gate()

    def test_raises_when_live_set_but_no_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_LIVE", "1")
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        with pytest.raises(VLMNotAuthorizedError, match="GEMINI_API_KEY"):
            _require_live_gate()

    def test_passes_when_both_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_LIVE", "1")
        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        _require_live_gate()  # no exception

    def test_extract_visual_fields_blocks_without_gate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("GEMINI_LIVE", raising=False)
        pdf = tmp_path / "fake.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        with pytest.raises(VLMNotAuthorizedError):
            extract_visual_fields(pdf, PackData(codice_ean="1234567890123"))


# ---------------------------------------------------------------------------
# Field-set partitioning
# ---------------------------------------------------------------------------


class TestFieldPartitioning:
    def test_visual_and_text_sets_are_disjoint(self) -> None:
        assert set(_VLM_OWNS_VISUAL).isdisjoint(set(_VLM_VERIFIES_TEXT))

    def test_visual_fields_match_presence_set(self) -> None:
        # Every visual presence-style field is in _PRESENCE_FIELDS, and only
        # visual fields appear there.
        assert _PRESENCE_FIELDS.issubset(set(_VLM_OWNS_VISUAL))

    def test_all_requested_fields_exist_on_packdata(self) -> None:
        for name in list(_VLM_OWNS_VISUAL) + list(_VLM_VERIFIES_TEXT):
            assert name in PackData.model_fields, f"unknown PackData field: {name}"


# ---------------------------------------------------------------------------
# Schema fragment
# ---------------------------------------------------------------------------


class TestSchemaFragment:
    def test_emits_one_line_per_field(self) -> None:
        fragment = _build_schema_fragment(["simbolo_ce", "tipo_o_modello"])
        lines = fragment.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("- simbolo_ce (PresenceField)")
        assert lines[1].startswith("- tipo_o_modello (ExtractedField)")

    def test_includes_descriptions(self) -> None:
        fragment = _build_schema_fragment(["simbolo_ce"])
        assert "CE conformity" in fragment


# ---------------------------------------------------------------------------
# Filename context
# ---------------------------------------------------------------------------


class TestFilenameContext:
    def test_parses_known_pattern(self, tmp_path: Path) -> None:
        pdf = tmp_path / "8055712771306_220x80x45_Thomas Turbato.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        ctx = _filename_context(pdf)
        assert "8055712771306" in ctx
        assert "220x80x45" in ctx
        assert "Thomas Turbato" in ctx

    def test_returns_empty_on_unparseable_name(self, tmp_path: Path) -> None:
        pdf = tmp_path / "not-a-pack.pdf"
        pdf.write_bytes(b"%PDF-1.4\n")
        assert _filename_context(pdf) == ""


# ---------------------------------------------------------------------------
# OCR block formatter
# ---------------------------------------------------------------------------


class TestOcrBlock:
    def test_empty_when_none(self) -> None:
        assert _format_ocr_block(None) == ""

    def test_empty_when_blank(self) -> None:
        assert _format_ocr_block("   \n  ") == ""

    def test_wraps_in_fence_when_present(self) -> None:
        out = _format_ocr_block("hello")
        assert "```" in out
        assert "hello" in out


# ---------------------------------------------------------------------------
# Dynamic response model
# ---------------------------------------------------------------------------


class TestResponseModel:
    def test_presence_field_uses_presence_submodel(self) -> None:
        model = _build_response_model(["simbolo_ce"])
        ann = model.model_fields["simbolo_ce"].annotation
        assert ann is PresenceFieldResponse

    def test_extracted_field_uses_extracted_submodel(self) -> None:
        model = _build_response_model(["materiale"])
        ann = model.model_fields["materiale"].annotation
        assert ann is ExtractedFieldResponse

    def test_validates_a_full_payload(self) -> None:
        model = _build_response_model(["simbolo_ce", "materiale"])
        instance = model.model_validate(
            {
                "simbolo_ce": {"present": True, "confidence": 0.95, "evidence": "front panel"},
                "materiale": {"value": "Silicone/ABS", "confidence": 0.9, "evidence": "back text"},
            }
        )
        assert instance.simbolo_ce.present is True
        assert instance.materiale.value == "Silicone/ABS"

    def test_rejects_missing_field(self) -> None:
        model = _build_response_model(["simbolo_ce", "materiale"])
        with pytest.raises(Exception):
            model.model_validate(
                {"simbolo_ce": {"present": True, "confidence": 0.9, "evidence": "x"}}
            )


# ---------------------------------------------------------------------------
# Merge into PackData
# ---------------------------------------------------------------------------


class TestMerge:
    def test_overlays_presence_field(self) -> None:
        model = _build_response_model(["simbolo_ce"])
        parsed = model.model_validate(
            {"simbolo_ce": {"present": True, "confidence": 0.97, "evidence": "back panel"}}
        )
        pack = PackData(codice_ean="1234567890123")
        result = _merge_vlm_response(parsed, pack)
        assert result.simbolo_ce.present is True
        assert result.simbolo_ce.confidence == pytest.approx(0.97)
        assert result.simbolo_ce.evidence == "back panel"

    def test_overlays_extracted_field(self) -> None:
        model = _build_response_model(["materiale"])
        parsed = model.model_validate(
            {"materiale": {"value": "Silicone", "confidence": 0.8, "evidence": "side"}}
        )
        pack = PackData(codice_ean="1234567890123")
        result = _merge_vlm_response(parsed, pack)
        assert result.materiale.value == "Silicone"
        assert result.materiale.confidence == pytest.approx(0.8)

    def test_does_not_mutate_input_pack(self) -> None:
        model = _build_response_model(["materiale"])
        parsed = model.model_validate(
            {"materiale": {"value": "X", "confidence": 0.5, "evidence": None}}
        )
        pack = PackData(codice_ean="1234567890123")
        pack.materiale = ExtractedField(value="ORIGINAL", confidence=0.9, evidence="parser")
        _ = _merge_vlm_response(parsed, pack)
        assert pack.materiale.value == "ORIGINAL"

    def test_returns_fresh_pack_without_parser_data(self) -> None:
        """The returned PackData represents only what the VLM saw — parser
        fields are NOT carried over. The validator does the merge."""
        model = _build_response_model(["simbolo_ce"])
        parsed = model.model_validate(
            {"simbolo_ce": {"present": True, "confidence": 0.9, "evidence": "x"}}
        )
        pack = PackData(codice_ean="1234567890123")
        pack.simbolo_raee = PresenceField(present=False, confidence=0.7, evidence="parser")
        result = _merge_vlm_response(parsed, pack)
        # codice_ean carries through (identity).
        assert result.codice_ean == "1234567890123"
        # Parser-set field is at schema default in the VLM output.
        assert result.simbolo_raee.present is None
        assert result.simbolo_raee.confidence == 0.0

    def test_skips_overlay_when_vlm_returns_no_signal_presence(self) -> None:
        """A null/zero-confidence VLM envelope must not clobber the schema
        default. The validator reads vlm_output as 'no data here' so the
        parser side wins through reconciliation."""
        model = _build_response_model(["simbolo_ce"])
        parsed = model.model_validate(
            {"simbolo_ce": {"present": None, "confidence": 0.0, "evidence": None}}
        )
        pack = PackData(codice_ean="1234567890123")
        result = _merge_vlm_response(parsed, pack)
        assert result.simbolo_ce.present is None
        assert result.simbolo_ce.confidence == 0.0
        assert result.simbolo_ce.evidence is None

    def test_skips_overlay_when_vlm_returns_no_signal_extracted(self) -> None:
        model = _build_response_model(["materiale"])
        parsed = model.model_validate(
            {"materiale": {"value": None, "confidence": 0.0, "evidence": None}}
        )
        pack = PackData(codice_ean="1234567890123")
        result = _merge_vlm_response(parsed, pack)
        assert result.materiale.value is None
        assert result.materiale.confidence == 0.0

    def test_keeps_explicit_false_with_confidence(self) -> None:
        """A confident `present=False` is real signal and must be retained."""
        model = _build_response_model(["simbolo_ce"])
        parsed = model.model_validate(
            {"simbolo_ce": {"present": False, "confidence": 0.95, "evidence": "scanned all panels"}}
        )
        pack = PackData(codice_ean="1234567890123")
        result = _merge_vlm_response(parsed, pack)
        assert result.simbolo_ce.present is False
        assert result.simbolo_ce.confidence == pytest.approx(0.95)


# ---------------------------------------------------------------------------
# Module import safety
# ---------------------------------------------------------------------------


def test_module_imports_without_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-importing src.vlm without GEMINI_API_KEY must not raise."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_LIVE", raising=False)
    import importlib

    import src.vlm as vlm_module

    importlib.reload(vlm_module)
    assert hasattr(vlm_module, "extract_visual_fields")
    # Sanity: env was clean during reload
    assert os.environ.get("GEMINI_API_KEY") is None
