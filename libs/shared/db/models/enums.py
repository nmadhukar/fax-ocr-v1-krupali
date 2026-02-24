"""
Database enum types.

These match the PostgreSQL ENUM types defined in migrations.
"""

import enum


class PayerNameEnum(str, enum.Enum):
    """Supported payer/insurance provider names."""

    ANTHEM = "ANTHEM"
    CARESOURCE = "CARESOURCE"
    BUCKEYE = "BUCKEYE"
    MOLINA = "MOLINA"
    UNITED_HEALTH = "UNITED_HEALTH"
    HUMANA = "HUMANA"
    AMERIHEALTH = "AMERIHEALTH"
    AETNA = "AETNA"
    PARAMOUNT = "PARAMOUNT"
    PROMEDICA = "PROMEDICA"
    UNKNOWN = "UNKNOWN"


class DocTypeEnum(str, enum.Enum):
    """Document type classification."""

    PRIOR_AUTH_FORM = "PRIOR_AUTH_FORM"
    PRIOR_AUTH_APPROVAL = "PRIOR_AUTH_APPROVAL"
    PRIOR_AUTH_DENIAL = "PRIOR_AUTH_DENIAL"
    PEER_TO_PEER_DENIAL = "PEER_TO_PEER_DENIAL"
    FAX_COVER_SHEET = "FAX_COVER_SHEET"
    HIPAA_RELEASE = "HIPAA_RELEASE"
    CLINICAL_NOTES = "CLINICAL_NOTES"
    LAB_RESULTS = "LAB_RESULTS"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class FaxJobStatusEnum(str, enum.Enum):
    """Fax job processing status."""

    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NEEDS_REVIEW = "NEEDS_REVIEW"


class ExtractionMethodEnum(str, enum.Enum):
    """Method used for field extraction."""

    TEMPLATE_OCR = "TEMPLATE_OCR"   # Label-anchored template extraction
    LAYOUTLM = "LAYOUTLM"           # LayoutLM Document QA model (sole VLM)
    DONUT = "DONUT"                 # READ-ONLY legacy value — Donut removed, do NOT use for new extractions
    VLM = "VLM"                     # Generic VLM fallback
    HUMAN_REVIEW = "HUMAN_REVIEW"   # Human reviewer correction (authoritative)
    HYBRID = "HYBRID"               # Multiple sources agreed
    LLM = "LLM"                     # Reserved — not in use
