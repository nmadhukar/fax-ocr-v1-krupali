"""
Stage 4: Post-processing — Field merge, smart corrections, OCR scanners.

Steps 10–10d of the fax processing pipeline.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.extraction.field_builder import FieldBuilder

from . import PipelineContext

logger = logging.getLogger(__name__)


def merge_fields(ctx: PipelineContext) -> None:
    """Step 10: Multi-source merge via FieldBuilder."""
    field_builder = FieldBuilder(
        vlm_multiplier=ctx.settings.vlm.layoutlm_score_multiplier,
    )

    for field_key, candidates in ctx.candidates_by_field.items():
        built = field_builder.build_field(field_key, candidates)
        ctx.extracted_fields[field_key] = {
            "value": built.value,
            "confidence": built.confidence,
            "method": built.method.value,
            "evidence_bbox": built.evidence_bbox,
            "evidence_text": built.evidence_text,
            "candidates": [c.to_dict() for c in built.candidates],
        }

    logger.info(
        "FieldBuilder merged %d fields from %d total candidates",
        len(ctx.extracted_fields),
        sum(len(c) for c in ctx.candidates_by_field.values()),
    )


def smart_corrections(ctx: PipelineContext) -> None:
    """Step 10b: Smart post-extraction corrections."""
    fields = ctx.extracted_fields
    _donly = lambda s: re.sub(r"\D", "", s)

    _member_id_val = (fields.get("member_id") or {}).get("value") or ""
    _dob_val = (fields.get("patient_dob") or {}).get("value") or ""
    _eff_val = (fields.get("auth_effective_date") or {}).get("value") or ""
    _exp_val = (fields.get("auth_expiration_date") or {}).get("value") or ""

    # units_requested that duplicates member_id → clear
    _units_data = fields.get("units_requested")
    if _units_data and _member_id_val:
        _uval = (_units_data.get("value") or "").replace(" ", "")
        if _uval == _member_id_val.replace(" ", "") and len(_uval) >= 6:
            logger.info("Auto-corrected units_requested: value duplicates member_id → cleared")
            fields.pop("units_requested", None)

    # next_review_date that equals patient_dob → DOB bleed
    _review_data = fields.get("next_review_date")
    if _review_data and _dob_val:
        if _dob_val and _donly(_review_data.get("value") or "") == _donly(_dob_val):
            logger.info("Auto-corrected next_review_date: value equals patient_dob → cleared")
            fields.pop("next_review_date", None)

    # auth_effective_date == auth_expiration_date both VLM, matches another date → confusion
    _next_val = (fields.get("next_review_date") or {}).get("value") or ""
    if _eff_val and _exp_val and _eff_val == _exp_val:
        _eff_method = (fields.get("auth_effective_date") or {}).get("method", "")
        _exp_method = (fields.get("auth_expiration_date") or {}).get("method", "")
        _both_from_vlm = (
            _eff_method not in ("TEMPLATE_OCR", "HYBRID") and
            _exp_method not in ("TEMPLATE_OCR", "HYBRID")
        )
        _also_matches_dob = _dob_val and _donly(_eff_val) == _donly(_dob_val)
        _also_matches_review = _next_val and _donly(_eff_val) == _donly(_next_val)
        if _both_from_vlm and (_also_matches_dob or _also_matches_review):
            logger.info(
                "Auto-corrected: auth dates both from VLM with same value '%s' — matches another date field; cleared",
                _eff_val,
            )
            fields.pop("auth_effective_date", None)
            fields.pop("auth_expiration_date", None)

    # Refresh after potential pops
    _eff_val = (fields.get("auth_effective_date") or {}).get("value") or ""
    _exp_val = (fields.get("auth_expiration_date") or {}).get("value") or ""
    _dob_val = (fields.get("patient_dob") or {}).get("value") or ""

    # auth_expiration_date == patient_dob → VLM confused DOB for expiry
    if _exp_val and _dob_val and _donly(_exp_val) == _donly(_dob_val):
        _exp_data = fields.get("auth_expiration_date") or {}
        _alt_exp = None
        for _cand in (_exp_data.get("candidates") or []):
            _cval = _cand.get("value") or ""
            if _cval and _donly(_cval) != _donly(_dob_val):
                _alt_exp = _cand
                break
        if _alt_exp:
            logger.info("auth_expiration_date was DOB bleed; recovered template candidate")
            fields["auth_expiration_date"] = {
                "value": _alt_exp["value"],
                "confidence": float(_alt_exp.get("confidence") or 0.70),
                "method": _alt_exp.get("method", "TEMPLATE_OCR"),
                "evidence_bbox": _alt_exp.get("evidence_bbox"),
                "evidence_text": _alt_exp.get("evidence_text"),
                "candidates": [],
            }
        else:
            logger.info("Auto-corrected auth_expiration_date: value equals patient_dob → cleared")
            fields.pop("auth_expiration_date", None)

    # auth_effective_date == patient_dob → clear
    if _eff_val and _dob_val and _donly(_eff_val) == _donly(_dob_val):
        logger.info("Auto-corrected auth_effective_date: value equals patient_dob → cleared")
        fields.pop("auth_effective_date", None)

    # patient_dob: impossible birth year (too recent) → not valid adult DOB
    _dob_data = fields.get("patient_dob")
    if _dob_data:
        _dob_yr_m = re.search(r"\b(20\d{2}|19\d{2}|18\d{2})\b", _dob_data.get("value") or "")
        if _dob_yr_m:
            _dob_year = int(_dob_yr_m.group(1))
            # Dynamic cutoff: patients in prior-auth are typically adults (18+).
            # Use current_year - 8 as a lenient floor for pediatric patients.
            _min_valid_year = datetime.now(timezone.utc).year - 8
            if _dob_year > _min_valid_year:
                logger.info("Auto-corrected patient_dob: birth year too recent → cleared")
                fields.pop("patient_dob", None)

    # units_requested: must be simple integer 1-9999
    _units_data2 = fields.get("units_requested")
    if _units_data2:
        _uval2 = (_units_data2.get("value") or "").strip()
        _units_invalid = (
            not _uval2 or
            not re.match(r"^\d{1,4}$", _uval2) or
            not (1 <= int(_uval2) <= 9999)
        )
        if _units_invalid:
            logger.info("Auto-corrected units_requested: value '%s' is not a valid unit count → cleared", _uval2)
            fields.pop("units_requested", None)

    # patient_name = provider contamination
    _pn_data = fields.get("patient_name")
    _prvn_data = fields.get("provider_name")
    if _pn_data and _prvn_data:
        _pn_val = (_pn_data.get("value") or "").lower()
        _prvn_val = (_prvn_data.get("value") or "").lower()
        _pn_words = set(re.sub(r"[^a-z]", " ", _pn_val).split())
        _prvn_words = set(re.sub(r"[^a-z]", " ", _prvn_val).split())
        if _pn_words and _prvn_words and _pn_words.issubset(_prvn_words):
            _pn_method = _pn_data.get("method", "")
            if _pn_method not in ("TEMPLATE_OCR",):
                logger.info("Auto-corrected patient_name: subset of provider_name → cleared")
                fields.pop("patient_name", None)

    # When template verified AND patient_name from VLM, prefer template candidate
    _pn_data2 = fields.get("patient_name")
    if ctx.template_verified and _pn_data2 and _pn_data2.get("method") == "LAYOUTLM":
        for _cand in (_pn_data2.get("candidates") or []):
            if _cand.get("method") == "TEMPLATE_OCR" and _cand.get("value"):
                logger.info("patient_name: VLM source overridden, promoting template candidate")
                fields["patient_name"] = {
                    "value": _cand["value"],
                    "confidence": float(_cand.get("confidence") or 0.75),
                    "method": "TEMPLATE_OCR",
                    "evidence_bbox": _cand.get("evidence_bbox"),
                    "evidence_text": _cand.get("evidence_text"),
                    "candidates": [],
                }
                break


def ocr_scanners(ctx: PipelineContext) -> None:
    """Steps 10c-10d: OCR pattern scanners for recovery."""
    fields = ctx.extracted_fields
    scan_ocr = ctx.full_doc_ocr_text or ctx.all_ocr_text or ""

    _service_code_scanner(fields, scan_ocr, ctx.raw_ocr_tokens)
    _auth_date_range_recovery(fields, scan_ocr)
    _service_dates_scanner(fields, scan_ocr)
    _units_scanner(fields, scan_ocr)
    _prior_auth_validation(fields, scan_ocr)
    _patient_name_recovery(fields, scan_ocr, ctx)
    _following_member_scan(fields, scan_ocr)
    _next_review_to_dob_transfer(fields, scan_ocr)

    # Update payer_str in case template match changed detected_payer
    ctx.update_payer_str()


# ── Scanner Helpers ──────────────────────────────────────────────────

def _service_code_scanner(fields: dict, scan_ocr: str, raw_ocr_tokens: list) -> None:
    """Step 10c: Scan for HCPCS/CPT codes."""
    if (fields.get("service_code") or {}).get("value"):
        return

    _HCPCS_PAT = re.compile(r"\b([A-Z]\d{4}[A-Z]?)\b")
    extra_token_text = " ".join(
        t["text"] for pt in raw_ocr_tokens for t in pt if t.get("text")
    )
    combined_text = (scan_ocr or "") + " " + extra_token_text
    hcpcs_candidates = []
    for m in _HCPCS_PAT.finditer(combined_text):
        code = m.group(1)
        if len(code) >= 5 and not code.startswith(("19", "18", "20")):
            hcpcs_candidates.append(code)

    seen: set[str] = set()
    unique_codes = []
    for c in hcpcs_candidates:
        if c not in seen:
            seen.add(c)
            unique_codes.append(c)

    if unique_codes:
        logger.info("Service code fallback scanner found: %s", unique_codes[0])
        fields["service_code"] = {
            "value": unique_codes[0],
            "confidence": 0.65,
            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
            "evidence_bbox": None,
            "evidence_text": f"OCR scan: {' '.join(unique_codes[:3])}",
            "candidates": [],
        }


def _auth_date_range_recovery(fields: dict, scan_ocr: str) -> None:
    """Step 10d: Auth date range recovery."""
    eff_val = (fields.get("auth_effective_date") or {}).get("value") or ""
    exp_val = (fields.get("auth_expiration_date") or {}).get("value") or ""

    if (not eff_val) or exp_val:
        return

    eff_dig = re.sub(r"\D", "", eff_val)
    range_re = re.compile(
        r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s*[-–]+\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})"
    )
    for rm in range_re.finditer(scan_ocr):
        if re.sub(r"\D", "", rm.group(1)) == eff_dig:
            end_d = rm.group(2)
            logger.info("Auth date range recovery: effective=%s, found expiration=%s", eff_val, end_d)
            fields["auth_expiration_date"] = {
                "value": end_d,
                "confidence": 0.70,
                "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                "evidence_bbox": None,
                "evidence_text": f"OCR range: {rm.group(0)}",
                "candidates": [],
            }
            break


def _service_dates_scanner(fields: dict, scan_ocr: str) -> None:
    """Service dates scanner (Buckeye / Molina)."""
    if (fields.get("auth_effective_date") or {}).get("value"):
        return

    sd_re = re.compile(
        r"(?:Service|Authorization)\s+[Dd]ates?\s*:?\s*"
        r"(\d{1,2}/\d{1,2}/\d{4})\s*[-–]+\s*(\d{1,2}/\d{1,2}/\d{4})",
        re.IGNORECASE,
    )
    sd_m = sd_re.search(scan_ocr)
    if sd_m:
        logger.info("Service dates scanner found: %s - %s", sd_m.group(1), sd_m.group(2))
        fields["auth_effective_date"] = {
            "value": sd_m.group(1),
            "confidence": 0.65,
            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
            "evidence_bbox": None,
            "evidence_text": f"OCR: {sd_m.group(0)}",
            "candidates": [],
        }
        if not (fields.get("auth_expiration_date") or {}).get("value"):
            fields["auth_expiration_date"] = {
                "value": sd_m.group(2),
                "confidence": 0.65,
                "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                "evidence_bbox": None,
                "evidence_text": f"OCR: {sd_m.group(0)}",
                "candidates": [],
            }


def _units_scanner(fields: dict, scan_ocr: str) -> None:
    """Units/visits scanner (Buckeye narrative)."""
    if (fields.get("units_requested") or {}).get("value"):
        return

    units_re = re.compile(
        r"(?:units?|visits?)\s+(?:authorized|approved|requested)\s*:?\s*(\d+)",
        re.IGNORECASE,
    )
    units_m = units_re.search(scan_ocr)
    if units_m:
        unit_val = units_m.group(1)
        if 1 <= int(unit_val) <= 9999:
            logger.info("Units scanner found: %s", unit_val)
            fields["units_requested"] = {
                "value": unit_val,
                "confidence": 0.65,
                "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                "evidence_bbox": None,
                "evidence_text": f"OCR: {units_m.group(0)}",
                "candidates": [],
            }


def _prior_auth_validation(fields: dict, scan_ocr: str) -> None:
    """Prior auth number validation and recovery."""
    auth_data = fields.get("prior_auth_number")
    if not auth_data:
        return

    av = (auth_data.get("value") or "").strip()
    av_method = auth_data.get("method") or ""
    av_conf = float(auth_data.get("confidence") or 0)

    hyphen_junk = "-" in av and av_conf < 0.5 and av_method == "LAYOUTLM"
    if " " not in av and len(av) >= 5 and not hyphen_junk:
        return  # Valid

    logger.info("prior_auth_number '%s' invalid — trying recovery", av)
    alt_auth = None
    for cand in (auth_data.get("candidates") or []):
        cv = (cand.get("value") or "").strip()
        if cv and " " not in cv and "-" not in cv and len(cv) >= 5 and cv != av:
            alt_auth = cand
            break

    if alt_auth:
        logger.info("prior_auth_number recovered from candidate: %s", alt_auth.get("value"))
        fields["prior_auth_number"] = {
            "value": alt_auth["value"],
            "confidence": float(alt_auth.get("confidence") or 0.65),
            "method": alt_auth.get("method", "TEMPLATE_OCR"),
            "evidence_bbox": alt_auth.get("evidence_bbox"),
            "evidence_text": alt_auth.get("evidence_text"),
            "candidates": [],
        }
    else:
        ref_re = re.compile(r"Reference\s*#\s*:?\s*([A-Za-z0-9]{5,25})", re.IGNORECASE)
        ref_m = ref_re.search(scan_ocr)
        if ref_m:
            logger.info("prior_auth_number Reference# scan found: %s", ref_m.group(1))
            fields["prior_auth_number"] = {
                "value": ref_m.group(1),
                "confidence": 0.65,
                "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                "evidence_bbox": None,
                "evidence_text": f"OCR: {ref_m.group(0)}",
                "candidates": [],
            }
        else:
            logger.info("prior_auth_number cleared (no valid candidate found)")
            fields.pop("prior_auth_number", None)


def _patient_name_recovery(fields: dict, scan_ocr: str, ctx: PipelineContext) -> None:
    """Patient name recovery from candidates and OCR scan."""
    if (fields.get("patient_name") or {}).get("value"):
        return

    # Try candidates from previously cleared patient_name
    _prvn_val_now = ((fields.get("provider_name") or {}).get("value") or "").lower()
    _prvn_words_now = set(re.sub(r"[^a-z]", " ", _prvn_val_now).split())

    # Note: in the original, _pn_data and _pn_data2 were old variables.
    # In the refactored version we no longer have them; the candidates are gone after
    # smart_corrections above. This scanner focuses on the OCR-based recovery.

    # Member Name scan from OCR text (Molina)
    mn_label_re = re.compile(r"Member\s+Name\s*:?\s*(.+)", re.IGNORECASE)
    for mn_line in scan_ocr.split("\n"):
        mn_m = mn_label_re.match(mn_line.strip())
        if mn_m:
            mn_val = mn_m.group(1).strip()
            mn_val = re.sub(
                r"\s+(?:Requesting|Servicing|Ordering|Attending|Referring|Primary)\s+Provider.*$",
                "", mn_val, flags=re.IGNORECASE,
            ).strip()
            # Drop trailing long numeric IDs but preserve ordinal suffixes (e.g., "John 3rd").
            mn_val = re.sub(r"\s+\d{4,}.*$", "", mn_val).strip()
            mn_val = mn_val.strip(",").strip()
            if len(mn_val) >= 3 and any(c.isalpha() for c in mn_val):
                logger.info("Member Name OCR scan found patient_name (len=%d)", len(mn_val))
                fields["patient_name"] = {
                    "value": mn_val,
                    "confidence": 0.60,
                    "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                    "evidence_bbox": None,
                    "evidence_text": f"OCR: {mn_line.strip()}",
                    "candidates": [],
                }
                break


def _following_member_scan(fields: dict, scan_ocr: str) -> None:
    """'Following member' scan (Buckeye/payer letter format)."""
    fm_re = re.compile(
        r"following\s+member\s*:[ \t]*\n\s*([^\n]+)", re.IGNORECASE
    )
    fm_m = fm_re.search(scan_ocr)
    if not fm_m:
        return

    fm_name = fm_m.group(1).strip().rstrip(",").strip()
    # Drop trailing long numeric IDs but preserve ordinal suffixes in names.
    fm_name = re.sub(r"\s+\d{4,}.*$", "", fm_name).strip()
    pn_current = (fields.get("patient_name") or {}).get("value") or ""
    if len(fm_name) >= 3 and any(c.isalpha() for c in fm_name):
        if not pn_current or pn_current.upper() != fm_name.upper():
            logger.info("patient_name 'following member' scan overrides previous value (len=%d → %d)", len(pn_current), len(fm_name))
            fields["patient_name"] = {
                "value": fm_name,
                "confidence": 0.65,
                "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                "evidence_bbox": None,
                "evidence_text": f"OCR: following member: {fm_name}",
                "candidates": [],
            }


def _next_review_to_dob_transfer(fields: dict, scan_ocr: str) -> None:
    """Transfer next_review_date → patient_dob if it's 18+ years in the past."""
    if (fields.get("patient_dob") or {}).get("value"):
        return

    nrd_data = fields.get("next_review_date") or {}
    nrd_val = (nrd_data.get("value") or "").strip()
    if not nrd_val:
        return

    nrd_parsed = None
    for fmt in ["%m/%d/%Y", "%m/%d/%y"]:
        try:
            nrd_parsed = datetime.strptime(nrd_val, fmt)
            break
        except Exception:
            pass

    if nrd_parsed is None:
        nrd_norm = re.sub(r"[-.]", "/", nrd_val)
        for fmt in ["%m/%d/%Y", "%m/%d/%y"]:
            try:
                nrd_parsed = datetime.strptime(nrd_norm, fmt)
                break
            except Exception:
                pass

    if nrd_parsed:
        years_ago = (datetime.now(timezone.utc) - nrd_parsed.replace(tzinfo=timezone.utc)).days / 365.25
        if years_ago >= 18:
            logger.info(
                "next_review_date is %d years in the past → transferring to patient_dob",
                int(years_ago),
            )
            fields["patient_dob"] = {
                "value": nrd_val,
                "confidence": 0.65,
                "method": nrd_data.get("method", ExtractionMethodEnum.TEMPLATE_OCR.value),
                "evidence_bbox": nrd_data.get("evidence_bbox"),
                "evidence_text": nrd_data.get("evidence_text", f"OCR: {nrd_val}"),
                "candidates": [],
            }
            fields.pop("next_review_date", None)
