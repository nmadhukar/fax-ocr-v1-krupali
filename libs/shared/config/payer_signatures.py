"""
Payer identification signatures for keyword-based detection.

Each payer has a set of text patterns that appear on their documents.
Patterns are ordered by specificity (most specific first).  The
detector scores matches using primary > secondary weighting and
uses negative keywords to prevent cross-payer false positives.

These signatures were built from real payer documents:
  - CareSource: "CareSource" header, "CareSource.com"
  - Humana: "Humana Healthy Horizons" header
  - Anthem: "Anthem Blue Cross and Blue Shield Medicaid"
  - Molina: "Molina Healthcare" header
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PayerSignature:
    """Immutable keyword-based signature for a payer."""

    payer_key: str
    display_name: str
    primary_keywords: tuple[str, ...]
    secondary_keywords: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()


PAYER_SIGNATURES: dict[str, PayerSignature] = {
    "ANTHEM": PayerSignature(
        payer_key="ANTHEM",
        display_name="Anthem Blue Cross Blue Shield",
        primary_keywords=(
            "anthem blue cross and blue shield",
            "anthem blue cross blue shield",
            "anthem blue cross",
            "anthem bcbs",
            "anthem, inc",
        ),
        secondary_keywords=(
            "anthem",
            "providers.anthem.com",
            "medicaid id #",
            "aohpec",
            "866-577-2184",
            "anthem healthkeepers",
        ),
        negative_keywords=(
            "aetna",
        ),
    ),
    "CARESOURCE": PayerSignature(
        payer_key="CARESOURCE",
        display_name="CareSource",
        primary_keywords=(
            "caresource",
            "care source",
        ),
        secondary_keywords=(
            "caresource.com",
            "caresource ohio",
            "caresource utilization management",
        ),
    ),
    "MOLINA": PayerSignature(
        payer_key="MOLINA",
        display_name="Molina Healthcare",
        primary_keywords=(
            "molina healthcare",
            "molina health plan",
        ),
        secondary_keywords=(
            "molina",
            "molinahealthcare.com",
            "health plan id",
            "3000 corporate exchange",
            "855-322-4079",
        ),
    ),
    "BUCKEYE": PayerSignature(
        payer_key="BUCKEYE",
        display_name="Buckeye Health Plan",
        primary_keywords=(
            "buckeye health plan",
            "buckeye community health plan",
        ),
        secondary_keywords=(
            "buckeye",
            "buckeyehealthplan.com",
        ),
    ),
    "HUMANA": PayerSignature(
        payer_key="HUMANA",
        display_name="Humana",
        primary_keywords=(
            "humana healthy horizons",
            "humana health plan",
            "humana behavioral health",
            "humana inc",
        ),
        secondary_keywords=(
            "humana",
            "humana.com",
            "humana authorization number",
            "notice of days approved",
        ),
    ),
    "UNITED_HEALTH": PayerSignature(
        payer_key="UNITED_HEALTH",
        display_name="UnitedHealthcare",
        primary_keywords=(
            "unitedhealthcare",
            "united healthcare",
            "unitedhealth group",
            "uhc community plan",
        ),
        secondary_keywords=(
            "uhc",
            "optum",
            "uhcprovider.com",
            "uhccommunityplan.com",
        ),
    ),
    "AMERIHEALTH": PayerSignature(
        payer_key="AMERIHEALTH",
        display_name="AmeriHealth Caritas",
        primary_keywords=(
            "amerihealth caritas",
        ),
        secondary_keywords=(
            "amerihealth",
            "amerihealthcaritas.com",
        ),
    ),
    "AETNA": PayerSignature(
        payer_key="AETNA",
        display_name="Aetna",
        primary_keywords=(
            "aetna better health",
            "aetna health",
            "aetna inc",
        ),
        secondary_keywords=(
            "aetna",
            "aetna.com",
        ),
        negative_keywords=(
            "anthem",
        ),
    ),
    "PARAMOUNT": PayerSignature(
        payer_key="PARAMOUNT",
        display_name="Paramount Advantage",
        primary_keywords=(
            "paramount advantage",
            "paramount health care",
        ),
        secondary_keywords=(
            "paramount",
            "paramounthealthcare.com",
            # "affiliate of promedica" removed — causes ProMedica docs to score as Paramount
        ),
        negative_keywords=(
            "promedica health plan",
            "promedica insurance",
        ),
    ),
    "PROMEDICA": PayerSignature(
        payer_key="PROMEDICA",
        display_name="ProMedica",
        primary_keywords=(
            "promedica health plan",
            "promedica insurance",
            "promedica medicaid",
        ),
        secondary_keywords=(
            "promedica",
            "promedica.org",
        ),
        negative_keywords=(
            "paramount advantage",
        ),
    ),
}
