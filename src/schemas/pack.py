"""
Pydantic schema for one packaging unit (fustella).

Source of truth for:
- The 34 output fields and their Italian names (must match the Google Sheet
  header byte-for-byte after Italian → snake_case mapping).
- The extraction strategy tag for every field.
- The confidence/evidence envelope for non-deterministic fields.
- Default values for constant manufacturer/importer fields.

IMPORTANT — field names were inferred from CLAUDE.md and project context.
Verify each name against the actual Sheet header before Session 5 (sheets.py).
Fields marked # TODO need confirmation.
"""

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class ExtractionStrategy(str, Enum):
    DETERMINISTIC = "deterministic"  # filename segment or hardcoded constant
    TEXT_IN_PDF = "text_in_pdf"      # pdfplumber regex / spatial rule
    VISUAL = "visual"                # VLM (Gemini 2.5 Pro)
    DERIVED = "derived"              # computed from other fields


class ExtractedField(BaseModel):
    """Confidence envelope for text-extracted and VLM-extracted string fields."""

    value: Optional[str] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: Optional[str] = None  # source text snippet or bounding-box description

    model_config = {"frozen": False}


class PresenceField(BaseModel):
    """Confidence envelope for binary symbol-presence fields (CE, RAEE, etc.)."""

    present: Optional[bool] = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: Optional[str] = None

    model_config = {"frozen": False}


# ---------------------------------------------------------------------------
# Main schema
# Field declaration order = Google Sheet column order.
# ---------------------------------------------------------------------------

class PackData(BaseModel):
    """
    Structured data for one packaging unit.

    Deterministic fields are plain str (confidence = 1.0 by definition).
    All other fields wrap their value in ExtractedField or PresenceField
    so that every cell in the Sheet carries provenance and confidence.
    """

    # -- DETERMINISTIC: manufacturer / importer constants ----------------
    # These are always the same for this brand; VLM should still flag
    # disagreements if the PDF text differs.
    nome_del_fabbricante: str = "MySecretCase s.r.l."
    indirizzo_del_fabbricante: str = "Corso C. Colombo 7 - Milano 20144"
    nome_dell_importatore: str = "MySecretCase s.r.l."
    indirizzo_dell_importatore: str = "Corso C. Colombo 7 - Milano 20144"

    # -- DETERMINISTIC: from filename ------------------------------------
    # Filename pattern: {EAN}_{W}x{H}x{D}_{ProductName}.pdf
    codice_ean: str
    dimensioni: str  # "{W}x{H}x{D}" mm, parsed directly from filename

    # -- TEXT_IN_PDF: product identity ----------------------------------
    nome_prodotto: ExtractedField = Field(default_factory=ExtractedField)
    tipo_o_modello: ExtractedField = Field(default_factory=ExtractedField)
    colore: ExtractedField = Field(default_factory=ExtractedField)
    codice_asin: ExtractedField = Field(default_factory=ExtractedField)  # TODO: confirm field name vs Sheet header

    # -- TEXT_IN_PDF: materials -----------------------------------------
    materiale: ExtractedField = Field(default_factory=ExtractedField)
    # Selectable text material codes, e.g. "FR 7 | CPE 21 | PAP"
    # Context: these appear as plain text at top of page — cross-check
    # against the visual recycling icons (simboli_materiali_smaltimento).
    codici_smaltimento_materiali: ExtractedField = Field(default_factory=ExtractedField)

    # -- TEXT_IN_PDF: traceability --------------------------------------
    lotto: ExtractedField = Field(default_factory=ExtractedField)
    paese_di_produzione: ExtractedField = Field(default_factory=ExtractedField)

    # -- TEXT_IN_PDF: electrical / battery ------------------------------
    capacita_batteria_e_tensione_nominale: ExtractedField = Field(default_factory=ExtractedField)
    tempo_di_carica: ExtractedField = Field(default_factory=ExtractedField)
    durata_utilizzo: ExtractedField = Field(default_factory=ExtractedField)
    istruzioni_carica: ExtractedField = Field(default_factory=ExtractedField)

    # -- TEXT_IN_PDF: product features ----------------------------------
    n_vibrazioni: ExtractedField = Field(default_factory=ExtractedField)
    livello_impermeabilita: ExtractedField = Field(default_factory=ExtractedField)

    # -- TEXT_IN_PDF: regulatory text -----------------------------------
    avvertenze: ExtractedField = Field(default_factory=ExtractedField)
    eta_minima: ExtractedField = Field(default_factory=ExtractedField)
    lingue_sulla_confezione: ExtractedField = Field(default_factory=ExtractedField)

    # -- TEXT_IN_PDF: contact / marketing -------------------------------
    sito_web: ExtractedField = Field(default_factory=ExtractedField)
    assistenza_clienti: ExtractedField = Field(default_factory=ExtractedField)
    sexy_ideas: ExtractedField = Field(default_factory=ExtractedField)

    # -- VISUAL: regulatory symbols -------------------------------------
    # Interesting signal is *absence*, not presence — legally required on every pack.
    simbolo_ce: PresenceField = Field(default_factory=PresenceField)
    simbolo_raee: PresenceField = Field(default_factory=PresenceField)
    simbolo_ukca: PresenceField = Field(default_factory=PresenceField)
    simbolo_triman: PresenceField = Field(default_factory=PresenceField)
    simbolo_eta_minima: PresenceField = Field(default_factory=PresenceField)  # +18 graphic

    # -- VISUAL: recycling icons + QR -----------------------------------
    # simboli_materiali_smaltimento: textual description of the visual
    # recycling symbols (triangle + codes), used to validate TRIMAN consistency.
    simboli_materiali_smaltimento: ExtractedField = Field(default_factory=ExtractedField)
    # qr_code_junker: decoded URL from the Junker QR code on the back panel.
    qr_code_junker: ExtractedField = Field(default_factory=ExtractedField)

    # -- DERIVED --------------------------------------------------------
    # True  = TRIMAN icon materials match codici_smaltimento_materiali text
    # False = mismatch (human review required)
    # None  = not computable (one or both source fields missing)
    contenuto_triman_corretto: Optional[bool] = None

    model_config = {"frozen": False}

    @model_validator(mode="after")
    def _ean_not_empty(self) -> "PackData":
        if not self.codice_ean:
            raise ValueError("codice_ean must not be empty")
        return self

    def content_fields(self) -> dict:
        """Return {field_name: value_str} for all 34 fields, flattened for Sheet writing."""
        result: dict = {}
        for name, field_info in self.model_fields.items():
            val = getattr(self, name)
            if isinstance(val, ExtractedField):
                result[name] = val.value
            elif isinstance(val, PresenceField):
                result[name] = val.present
            else:
                result[name] = val
        return result

    def confidence_map(self) -> dict[str, float]:
        """Return {field_name: confidence} for every field that carries confidence."""
        result: dict[str, float] = {}
        for name in self.model_fields:
            val = getattr(self, name)
            if isinstance(val, (ExtractedField, PresenceField)):
                result[name] = val.confidence
        return result
