"""
Pydantic schema for one packaging unit (fustella).

Source of truth for:
- The 34 output fields and their Italian names (must match the Google Sheet
  header byte-for-byte after Italian → snake_case mapping).
- The extraction strategy tag for every field.
- The confidence/evidence envelope for non-deterministic fields.
- Default values for constant manufacturer/importer fields.

Field declaration order = Google Sheet column order (columns 1–34).
`codice_ean` is declared after the 34 sheet fields; it is the pipeline
identity key but is NOT a Sheet column.
"""

from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class ExtractionStrategy(StrEnum):
    DETERMINISTIC = "deterministic"  # filename segment or hardcoded constant
    TEXT_IN_PDF = "text_in_pdf"  # pdfplumber regex / spatial rule
    VISUAL = "visual"  # VLM (Gemini 2.5 Pro)
    DERIVED = "derived"  # computed from other fields


class ExtractedField(BaseModel):
    """Confidence envelope for text-extracted and VLM-extracted string fields."""

    value: str | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str | None = None  # source text snippet or bounding-box description

    model_config = {"frozen": False}


class PresenceField(BaseModel):
    """Confidence envelope for binary symbol-presence fields (CE, RAEE, etc.)."""

    present: bool | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: str | None = None

    model_config = {"frozen": False}


# ---------------------------------------------------------------------------
# Main schema — 34 sheet fields in Sheet column order, then identity field.
# ---------------------------------------------------------------------------


class PackData(BaseModel):
    """
    Structured data for one packaging unit.

    Fields 1–34 match the Google Sheet header in column order.
    Deterministic fields are plain str (confidence = 1.0 by definition).
    All other fields wrap their value in ExtractedField or PresenceField
    so that every cell in the Sheet carries provenance and confidence.
    """

    # -- Column 1-4: DETERMINISTIC manufacturer / importer constants -----
    # These are always the same for this brand; VLM should still flag
    # disagreements if the PDF text differs.
    nome_del_fabbricante: str = "MySecretCase s.r.l."
    indirizzo_del_fabbricante: str = "Corso C. Colombo 7 - Milano 20144"
    nome_dell_importatore: str = "MySecretCase s.r.l."
    indirizzo_dell_importatore: str = "Corso C. Colombo 7 - Milano 20144"

    # -- Column 5: product identity -------------------------------------
    tipo_o_modello: ExtractedField = Field(default_factory=ExtractedField)

    # -- Column 6-7: traceability ---------------------------------------
    # numero_di_serie_lotto: the full lot label as it appears, e.g. "LOT: 468".
    numero_di_serie_lotto: ExtractedField = Field(default_factory=ExtractedField)
    # lotto: the parsed lot value only (often "N/A" when no lot is shown).
    lotto: ExtractedField = Field(default_factory=ExtractedField)

    # -- Column 8-12: VISUAL regulatory symbols -------------------------
    simbolo_ce: PresenceField = Field(default_factory=PresenceField)
    simbolo_raee: PresenceField = Field(default_factory=PresenceField)
    simbolo_ukca: PresenceField = Field(default_factory=PresenceField)
    simbolo_triman: PresenceField = Field(default_factory=PresenceField)
    simbolo_smaltimento_spagnolo: PresenceField = Field(default_factory=PresenceField)

    # -- Column 13: recycling icons description -------------------------
    # Text description of the visual recycling symbols, e.g. "PAP21 / CPE07".
    simboli_materiali_smaltimento: ExtractedField = Field(default_factory=ExtractedField)

    # -- Column 14-16: VISUAL presence markers --------------------------
    qr_code_junker: PresenceField = Field(default_factory=PresenceField)
    simbolo_garanzia_2_anni: PresenceField = Field(default_factory=PresenceField)
    simbolo_libretto_informativo: PresenceField = Field(default_factory=PresenceField)

    # -- Column 17-20: TEXT_IN_PDF electrical / product features --------
    capacita_batteria_e_tensione_nominale: ExtractedField = Field(default_factory=ExtractedField)
    # impermeabilita: e.g. "IPX6" or "Non Impermeabile".
    impermeabilita: ExtractedField = Field(default_factory=ExtractedField)
    # materiale: e.g. "Silicone/ABS".
    materiale: ExtractedField = Field(default_factory=ExtractedField)
    # modalita_di_ricarica: e.g. "Ricarica magnetica", "Ricarica minijack", or "N/A".
    modalita_di_ricarica: ExtractedField = Field(default_factory=ExtractedField)

    # -- Column 21: dimensions ------------------------------------------
    # e.g. "17cm x Ø5.7cm"
    dimensioni: ExtractedField = Field(default_factory=ExtractedField)

    # -- Column 22-26: feature counts -----------------------------------
    # String value or None when absent (golden uses false for absent).
    n_vibrazioni: ExtractedField = Field(default_factory=ExtractedField)
    n_velocita: ExtractedField = Field(default_factory=ExtractedField)
    n_modalita_suzione: ExtractedField = Field(default_factory=ExtractedField)
    n_modalita_tapping: ExtractedField = Field(default_factory=ExtractedField)
    n_modalita_rotazione: ExtractedField = Field(default_factory=ExtractedField)

    # -- Column 27-28: VISUAL feature presence markers ------------------
    strap_on_compatibile: PresenceField = Field(default_factory=PresenceField)
    funzione_riscaldante: PresenceField = Field(default_factory=PresenceField)

    # -- Column 29: Amazon identifier -----------------------------------
    codice_asin: ExtractedField = Field(default_factory=ExtractedField)

    # -- Column 30-32: disposal codes (split from visual recycling band) -
    # codice_smaltimento_scatola: first paper code, e.g. "PAP21".
    codice_smaltimento_scatola: ExtractedField = Field(default_factory=ExtractedField)
    # codice_smaltimento_sacchetto: first plastic code, e.g. "CPE07".
    codice_smaltimento_sacchetto: ExtractedField = Field(default_factory=ExtractedField)
    # codice_smaltimento_doypack: third code when present; None otherwise.
    codice_smaltimento_doypack: ExtractedField = Field(default_factory=ExtractedField)

    # -- Column 33: DERIVED triman description --------------------------
    # Text description of matched materials, e.g. "scatola + sacchetto".
    # None when undetermined; set to a low-confidence value on mismatch.
    contenuto_triman_corretto: ExtractedField = Field(default_factory=ExtractedField)

    # -- Column 34: marketing presence marker ---------------------------
    sexy_ideas: PresenceField = Field(default_factory=PresenceField)

    # -----------------------------------------------------------------------
    # Identity field — NOT a Sheet column.
    # Used by pipeline.py and sheets.py for idempotency checks (EAN = row key).
    # -----------------------------------------------------------------------
    codice_ean: str

    model_config = {"frozen": False}

    @model_validator(mode="after")
    def _ean_not_empty(self) -> "PackData":
        if not self.codice_ean:
            raise ValueError("codice_ean must not be empty")
        return self

    @model_validator(mode="after")
    def _sheet_fields_in_sync(self) -> "PackData":
        declared = set(PackData.model_fields) - {"codice_ean"}
        listed = set(self._SHEET_FIELDS)
        missing_from_tuple = declared - listed
        extra_in_tuple = listed - declared
        if missing_from_tuple or extra_in_tuple:
            parts: list[str] = []
            if missing_from_tuple:
                parts.append(f"missing from _SHEET_FIELDS: {sorted(missing_from_tuple)}")
            if extra_in_tuple:
                parts.append(f"extra in _SHEET_FIELDS (not in model): {sorted(extra_in_tuple)}")
            raise ValueError(
                "_SHEET_FIELDS is out of sync with PackData fields — " + "; ".join(parts)
            )
        return self

    # Sheet column names in declaration order (excludes codice_ean).
    _SHEET_FIELDS: tuple[str, ...] = (
        "nome_del_fabbricante",
        "indirizzo_del_fabbricante",
        "nome_dell_importatore",
        "indirizzo_dell_importatore",
        "tipo_o_modello",
        "numero_di_serie_lotto",
        "lotto",
        "simbolo_ce",
        "simbolo_raee",
        "simbolo_ukca",
        "simbolo_triman",
        "simbolo_smaltimento_spagnolo",
        "simboli_materiali_smaltimento",
        "qr_code_junker",
        "simbolo_garanzia_2_anni",
        "simbolo_libretto_informativo",
        "capacita_batteria_e_tensione_nominale",
        "impermeabilita",
        "materiale",
        "modalita_di_ricarica",
        "dimensioni",
        "n_vibrazioni",
        "n_velocita",
        "n_modalita_suzione",
        "n_modalita_tapping",
        "n_modalita_rotazione",
        "strap_on_compatibile",
        "funzione_riscaldante",
        "codice_asin",
        "codice_smaltimento_scatola",
        "codice_smaltimento_sacchetto",
        "codice_smaltimento_doypack",
        "contenuto_triman_corretto",
        "sexy_ideas",
    )

    def content_fields(self) -> dict:
        """Return {field_name: scalar_value} for the 34 Sheet columns only, in order.

        Excluded: codice_ean (identity key, not a Sheet column).
        """
        result: dict = {}
        for name in self._SHEET_FIELDS:
            val = getattr(self, name)
            if isinstance(val, ExtractedField):
                result[name] = val.value
            elif isinstance(val, PresenceField):
                result[name] = val.present
            else:
                result[name] = val
        return result

    def confidence_map(self) -> dict[str, float]:
        """Return {field_name: confidence} for the 34 Sheet fields that carry confidence.

        Excluded: codice_ean and plain-str deterministic fields (confidence = 1.0 implicitly).
        """
        result: dict[str, float] = {}
        for name in self._SHEET_FIELDS:
            val = getattr(self, name)
            if isinstance(val, (ExtractedField, PresenceField)):
                result[name] = val.confidence
        return result
