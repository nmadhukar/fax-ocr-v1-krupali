"""
Stage 4: Post-processing — Field merge, smart corrections, OCR scanners.

Steps 10–10d of the fax processing pipeline.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.extraction.field_builder import FieldBuilder

from . import PipelineContext

logger = logging.getLogger(__name__)


_DATE_TOKEN_RE = re.compile(
    r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})\b"
)
_DATE_PARSE_FORMATS = (
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m/%d/%y",
    "%m-%d-%y",
    "%Y/%m/%d",
    "%Y-%m-%d",
)


def _is_valid_date_token(token: str) -> bool:
    """Return True when token parses as a real date."""
    for fmt in _DATE_PARSE_FORMATS:
        try:
            datetime.strptime(token, fmt)
            return True
        except ValueError:
            continue
    return False


def _parse_date_token(token: str) -> datetime | None:
    """Parse a date token using supported formats."""
    if not token:
        return None
    normalized = re.sub(r"[-.]", "/", token.strip())
    for fmt in _DATE_PARSE_FORMATS:
        for candidate in (normalized, token.strip()):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def _coerce_noisy_date_token(token: str) -> str | None:
    """Best-effort cleanup for noisy OCR date tokens."""
    if not token:
        return None
    raw = token.strip().strip(".,;:")
    if _is_valid_date_token(raw):
        return raw

    raw_norm = re.sub(r"[-.]", "/", raw)
    if _is_valid_date_token(raw_norm):
        return raw_norm

    if "/" in raw_norm:
        left, _, year = raw_norm.rpartition("/")
        year_digits = re.sub(r"\D", "", year)
        left_digits = re.sub(r"\D", "", left)
        if len(year_digits) == 4 and len(left_digits) >= 3:
            month = left_digits[:2] if len(left_digits) >= 4 else left_digits[:1]
            day = left_digits[-2:]
            candidate = f"{int(month):02d}/{int(day):02d}/{year_digits}"
            if _is_valid_date_token(candidate):
                return candidate

    return None


def _extract_date_tokens(value: str) -> list[str]:
    """Extract valid date tokens from free-form field text."""
    if not value:
        return []
    return [token for token in _DATE_TOKEN_RE.findall(value) if _is_valid_date_token(token)]


def _date_direction_for_field(field_key: str) -> str:
    """Date selection direction for date-range fields."""
    key = (field_key or "").lower()
    if key in ("auth_expiration_date", "service_end_date"):
        return "last"
    if "expiration" in key or "end_date" in key:
        return "last"
    return "first"


def _normalize_date_fields(fields: dict[str, dict[str, Any]]) -> None:
    """Normalize date-like fields to one semantic date value."""
    date_keys = {
        key for key in fields.keys() if key.endswith("_date")
    } | {"patient_dob", "auth_effective_date", "auth_expiration_date", "next_review_date"}

    for field_key in date_keys:
        field_data = fields.get(field_key) or {}
        raw_value = (field_data.get("value") or "").strip()
        if not raw_value:
            continue
        dates = _extract_date_tokens(raw_value)
        if not dates:
            logger.info(
                "Cleared %s: non-date value '%s'",
                field_key,
                raw_value[:80],
            )
            fields.pop(field_key, None)
            continue
        direction = _date_direction_for_field(field_key)
        normalized = dates[-1] if direction == "last" else dates[0]
        if raw_value != normalized:
            logger.info(
                "Normalized %s from '%s' to '%s'",
                field_key,
                raw_value,
                normalized,
            )
            field_data["value"] = normalized


_APPROVAL_RE = (
    re.compile(r"\bauth(?:orization)?\s+status\s*[:\s]*approv", re.IGNORECASE),
    re.compile(r"\bapproval\s+notification\b", re.IGNORECASE),
    re.compile(r"\bapproved\s+authorization\b", re.IGNORECASE),
    re.compile(r"\bauto\s+approval\b", re.IGNORECASE),
    re.compile(r"\bnurse\s+recommendation\s*[:\s]*approv", re.IGNORECASE),
    re.compile(r"\b(?:has\s+been|is)\s+approved\b", re.IGNORECASE),
    re.compile(r"\bdecision\s*[:\s]*approv", re.IGNORECASE),
)
_DENIAL_RE = (
    re.compile(r"\bauth(?:orization)?\s+status\s*[:\s]*deni", re.IGNORECASE),
    re.compile(r"\bdenial\s+(?:notification|notice)\b", re.IGNORECASE),
    re.compile(r"\b(?:has\s+been|is)\s+denied\b", re.IGNORECASE),
    re.compile(r"\bdecision\s*[:\s]*deni", re.IGNORECASE),
    re.compile(r"\badverse\s+(?:determination|decision)\b", re.IGNORECASE),
    re.compile(r"\bnot\s+(?:medically\s+)?necessary\b", re.IGNORECASE),
    re.compile(r"\bwill\s+not\s+approve\b", re.IGNORECASE),
)


def _decision_from_text(value: str) -> str | None:
    """Map free-form decision text to APPROVED/DENIED."""
    text = (value or "").strip()
    if not text:
        return None
    upper = text.upper()
    if "APPROV" in upper:
        return "APPROVED"
    if "DENI" in upper:
        return "DENIED"
    return None


def _infer_decision_from_ocr(scan_ocr: str) -> str | None:
    """Infer decision using OCR text when extracted decision is noisy."""
    text = (scan_ocr or "").strip()
    if not text:
        return None

    approval_hits = sum(1 for pat in _APPROVAL_RE if pat.search(text))
    denial_hits = sum(1 for pat in _DENIAL_RE if pat.search(text))
    if approval_hits > denial_hits and approval_hits > 0:
        return "APPROVED"
    if denial_hits > approval_hits and denial_hits > 0:
        return "DENIED"

    # Weak fallback when explicit label patterns are absent.
    approval_kw = len(re.findall(r"\bapprov(?:ed|al|e)?\b", text, flags=re.IGNORECASE))
    denial_kw = len(re.findall(r"\bdeni(?:ed|al|es)?\b", text, flags=re.IGNORECASE))
    denial_kw += len(re.findall(r"\bnot\s+approve\b", text, flags=re.IGNORECASE))
    if approval_kw >= 2 and denial_kw == 0:
        return "APPROVED"
    if denial_kw >= 2 and approval_kw == 0:
        return "DENIED"
    return None


def _decision_from_doc_type(ctx: PipelineContext) -> str | None:
    """Map classified doc type to canonical decision."""
    doc_type = getattr(getattr(ctx, "job", None), "doc_type", None)
    doc_type_value = getattr(doc_type, "value", str(doc_type or ""))
    if doc_type_value == "PRIOR_AUTH_APPROVAL":
        return "APPROVED"
    if doc_type_value in ("PRIOR_AUTH_DENIAL", "PEER_TO_PEER_DENIAL"):
        return "DENIED"
    return None


def _decision_from_filename(ctx: PipelineContext) -> str | None:
    """Weak fallback using filename keywords when other signals are absent."""
    filename = getattr(getattr(ctx, "job", None), "original_filename", "") or ""
    lower = filename.lower()
    has_approved = "approv" in lower
    has_denied = "deni" in lower or "adverse" in lower
    if has_approved and not has_denied:
        return "APPROVED"
    if has_denied and not has_approved:
        return "DENIED"
    return None


def _normalize_decision_field(ctx: PipelineContext) -> None:
    """Ensure decision field is canonical enum text."""
    fields = ctx.extracted_fields
    decision_data = fields.get("decision") or {}
    current_value = (decision_data.get("value") or "").strip()

    canonical_source = "text"
    canonical = _decision_from_text(current_value)
    if not canonical:
        canonical = _decision_from_doc_type(ctx)
        if canonical:
            canonical_source = "doc_type"
    if not canonical:
        canonical = _decision_from_filename(ctx)
        if canonical:
            canonical_source = "filename"
    if not canonical:
        scan_ocr = ctx.full_doc_ocr_text or ctx.all_ocr_text or ""
        canonical = _infer_decision_from_ocr(scan_ocr)
        if canonical:
            canonical_source = "ocr"

    if not canonical:
        if current_value:
            logger.info("Cleared decision: non-canonical value '%s'", current_value[:120])
            fields.pop("decision", None)
        return

    if not decision_data:
        inferred_conf = 0.80
        if canonical_source == "filename":
            inferred_conf = 0.60
        elif canonical_source == "ocr":
            inferred_conf = 0.70
        fields["decision"] = {
            "value": canonical,
            "confidence": inferred_conf,
            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
            "evidence_bbox": None,
            "evidence_text": f"Inferred decision={canonical} source={canonical_source}",
            "candidates": [],
        }
        return

    if current_value != canonical:
        logger.info("Normalized decision from '%s' to '%s'", current_value, canonical)
        decision_data["value"] = canonical
        if not decision_data.get("evidence_text"):
            decision_data["evidence_text"] = canonical


def _looks_like_provider_prose(value: str) -> bool:
    """Detect narrative text accidentally captured as provider_name."""
    text = (value or "").strip().lower()
    if not text:
        return False
    words = [w for w in re.split(r"\s+", text) if w]
    if len(words) > 12:
        return True
    prose_markers = (
        "you can ask",
        "service authorization appeal",
        "clinical peer",
        "reason for appealing",
        "submitted within",
        "if you disagree",
        "coverage determination",
        "benefits and coverage",
        "provider services",
        "marketplace",
        "medicaid",
        "dsnp",
        "provider portal",
        "https://",
    )
    return any(marker in text for marker in prose_markers)


def _normalize_provider_name_value(value: str) -> str:
    """Trim common OCR bleed from provider/facility names."""
    candidate = re.sub(r"\s+", " ", (value or "")).strip(" ,.;:-")
    if not candidate:
        return candidate

    stop_re = re.compile(
        r"(?:memberd[0o]b|member\s*(?:name|id|d[0o]b|date\s+of\s+birth)|"
        r"reference\s+number|requesting\s+provider(?:\s+name)?|"
        r"servicing\s+provider(?:\s+name)?|authorization\s+status|"
        r"auth\s+status|date\s+span|decision)",
        re.IGNORECASE,
    )
    stop_m = stop_re.search(candidate)
    if stop_m:
        candidate = candidate[: stop_m.start()].strip(" ,.;:-")

    tokens = candidate.split()
    if not tokens:
        return candidate

    # Drop trailing OCR code tails (e.g., "... LLCODMHAS0245051LABOTP").
    while tokens:
        tail = re.sub(r"[^A-Za-z0-9]", "", tokens[-1])
        if not tail:
            tokens.pop()
            continue
        has_digit = any(c.isdigit() for c in tail)
        long_mixed = has_digit and len(tail) >= 8
        dense_digits = sum(1 for c in tail if c.isdigit()) >= 3
        if long_mixed or dense_digits:
            tokens.pop()
            continue
        break

    suffixes = {"llc", "inc", "corp", "corporation", "ltd", "pllc", "pc", "llp"}
    for idx, token in enumerate(tokens):
        norm = re.sub(r"[^a-z]", "", token.lower())
        if norm in suffixes:
            tokens = tokens[: idx + 1]
            break

    org_markers = {"service", "services", "health", "clinical", "clinic", "hospital", "center", "care", "group"}
    if len(tokens) >= 6 and any(re.sub(r"[^a-z]", "", t.lower()) in org_markers for t in tokens):
        last_two = tokens[-2:]
        if all(re.match(r"^[A-Za-z][A-Za-z'.-]{1,}$", t or "") for t in last_two):
            tail_norm = [re.sub(r"[^a-z]", "", t.lower()) for t in last_two]
            if not any(t in suffixes for t in tail_norm):
                tokens = tokens[:-2]

    return " ".join(tokens).strip(" ,.;:-")


def _is_reasonable_provider_name(value: str) -> bool:
    """Basic provider/facility name quality gate."""
    candidate = _normalize_provider_name_value(value)
    if len(candidate) < 3:
        return False
    if not any(c.isalpha() for c in candidate):
        return False
    compact = re.sub(r"[^A-Za-z0-9]", "", candidate)
    if compact:
        digit_ratio = sum(1 for c in compact if c.isdigit()) / len(compact)
        if digit_ratio > 0.20:
            return False
    tokens = candidate.split()
    for token in tokens:
        norm = re.sub(r"[^A-Za-z0-9]", "", token)
        if not norm:
            continue
        if sum(1 for c in norm if c.isdigit()) >= 3:
            return False
        if any(c.isdigit() for c in norm) and len(norm) >= 8:
            return False
    if candidate.count(":") > 1:
        return False
    if re.search(r"https?://", candidate, re.IGNORECASE):
        return False
    if re.search(r"\b\d{3}[-)\s]\d{3}[-\s]\d{4}\b", candidate):
        return False
    lower = candidate.lower()
    if _looks_like_provider_prose(lower):
        return False
    if (
        lower.endswith(":")
        or "provider name" in lower
        or "requesting provider" in lower
        or "servicing provider" in lower
    ):
        return False
    return True


def _is_reasonable_patient_name(value: str) -> bool:
    """Basic sanity checks for patient/member names."""
    candidate = re.sub(r"\s+", " ", (value or "")).strip(" ,.;:-")
    if len(candidate) < 3:
        return False
    if not any(c.isalpha() for c in candidate):
        return False
    compact = re.sub(r"[^A-Za-z0-9]", "", candidate)
    if compact:
        digit_ratio = sum(1 for c in compact if c.isdigit()) / len(compact)
        if digit_ratio > 0.12:
            return False
    words = [w for w in re.split(r"\s+", candidate) if w]
    if len(words) > 6:
        return False
    lower = candidate.lower()
    bad_markers = (
        "alcohol",
        "treatment",
        "authorization",
        "approved services",
        "decision",
        "appeal",
        "provider",
        "reference",
        "date span",
        "member id",
        "dob",
    )
    if any(marker in lower for marker in bad_markers):
        return False
    return True


def _clean_patient_name_candidate(raw: str) -> str:
    """Remove non-name tails from OCR-captured patient name text."""
    candidate = re.sub(r"\s+", " ", (raw or "")).strip(" ,.;:-")
    stop_re = re.compile(
        r"(?:memberd[0o]b|member\s*(?:id|d[0o]b)|date\s+of\s+birth|"
        r"requesting\s+provider|servicing\s+provider|provider\s+name|"
        r"auth(?:orization)?\s*status|reference|next\s+review|approved\s+services)",
        re.IGNORECASE,
    )
    stop_m = stop_re.search(candidate)
    if stop_m:
        candidate = candidate[: stop_m.start()].strip(" ,.;:-")
    candidate = re.sub(r"\s*,\s*", ", ", candidate)
    candidate = candidate.strip(" ,.;:-")

    if "," in candidate:
        parts = [p.strip() for p in candidate.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            last = parts[0].title()
            first = parts[1]
            if first.isupper() and len(first) >= 5:
                first = f"{first[:-1].title()} {first[-1].upper()}"
            else:
                first = first.title()
            candidate = f"{last}, {first}"
    else:
        words = []
        for w in candidate.split():
            if len(w) == 1:
                words.append(w.upper())
            else:
                words.append(w.title())
        candidate = " ".join(words)

    return candidate.strip(" ,.;:-")


def merge_fields(ctx: PipelineContext) -> None:
    """Step 10: Multi-source merge via FieldBuilder."""
    adaptive_cfg = getattr(ctx.settings, "adaptive", None)
    field_builder = FieldBuilder(
        vlm_multiplier=ctx.settings.vlm.layoutlm_score_multiplier,
        ranker_enabled=bool(getattr(adaptive_cfg, "candidate_ranker_enabled", True)),
        ranker_model_path=getattr(adaptive_cfg, "candidate_ranker_model_path", None),
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
    _normalize_date_fields(fields)
    _normalize_decision_field(ctx)

    _member_id_val = (fields.get("member_id") or {}).get("value") or ""
    _dob_val = (fields.get("patient_dob") or {}).get("value") or ""
    _eff_val = (fields.get("auth_effective_date") or {}).get("value") or ""
    _exp_val = (fields.get("auth_expiration_date") or {}).get("value") or ""
    _npi_val = (fields.get("provider_npi") or {}).get("value") or ""

    # member_id bleeding from provider NPI (same numeric stem) -> clear suspect member_id
    _mid_digits = re.sub(r"\D", "", _member_id_val)
    _npi_digits = re.sub(r"\D", "", _npi_val)
    _mid_method = (fields.get("member_id") or {}).get("method") or ""
    if (
        _mid_digits
        and _npi_digits
        and len(_npi_digits) == 10
        and len(_mid_digits) in (10, 11)
        and _mid_digits.startswith(_npi_digits)
        and _mid_method in ("OCR_LABEL", "LAYOUTLM", "VLM")
    ):
        logger.info(
            "Auto-corrected member_id: value overlaps provider_npi (%s vs %s) -> cleared",
            _mid_digits,
            _npi_digits,
        )
        fields.pop("member_id", None)
        _member_id_val = ""

    # member_id that looks like a date/timestamp is almost always extraction bleed
    if _member_id_val:
        _mid_raw = _member_id_val.strip()
        _looks_like_date = bool(
            re.search(r"\b\d{4}[/\-]\d{1,2}[/\-]\d{1,2}\b", _mid_raw)
            or re.search(r"\b\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\b", _mid_raw)
        )
        if _looks_like_date:
            logger.info("Auto-corrected member_id: value looks like date/time -> cleared")
            fields.pop("member_id", None)
            _member_id_val = ""

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
        _dob_raw = (_dob_data.get("value") or "").strip()
        _dob_norm = re.sub(r"[-.]", "/", _dob_raw)
        _dob_parsed = None
        for _fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y/%m/%d", "%Y-%m-%d"):
            try:
                _dob_parsed = datetime.strptime(_dob_norm, _fmt)
                break
            except Exception:
                pass
        if _dob_raw and _dob_parsed is None:
            logger.info(
                "Auto-corrected patient_dob: invalid date format '%s' -> cleared",
                _dob_raw,
            )
            fields.pop("patient_dob", None)
            _dob_data = None

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
    _pan_data = fields.get("prior_auth_number")
    if _prvn_data:
        _prvn_val_raw = (_prvn_data.get("value") or "").strip()
        _prvn_norm = _normalize_provider_name_value(_prvn_val_raw)
        if _prvn_norm and _prvn_norm != _prvn_val_raw:
            logger.info(
                "Normalized provider_name from '%s' to '%s'",
                _prvn_val_raw[:80],
                _prvn_norm[:80],
            )
            _prvn_data["value"] = _prvn_norm
            _prvn_val_raw = _prvn_norm
        _prvn_val_raw_lower = _prvn_val_raw.lower()
        _provider_label_like = (
            _prvn_val_raw.endswith(":")
            or "provider name" in _prvn_val_raw_lower
            or "servicing provider" in _prvn_val_raw_lower
            or "requesting provider" in _prvn_val_raw_lower
        )
        _provider_prose_like = _looks_like_provider_prose(_prvn_val_raw)
        if _provider_label_like or _provider_prose_like:
            logger.info(
                "Auto-corrected provider_name: invalid value captured ('%s') -> cleared",
                _prvn_val_raw[:120],
            )
            fields.pop("provider_name", None)
            _prvn_data = None
    if _prvn_data and _pan_data:
        _prvn_compact = re.sub(r"\W", "", (_prvn_data.get("value") or "")).upper()
        _pan_compact = re.sub(r"\W", "", (_pan_data.get("value") or "")).upper()
        if _prvn_compact and _pan_compact and _prvn_compact == _pan_compact:
            logger.info("Auto-corrected provider_name: duplicates prior_auth_number -> cleared")
            fields.pop("provider_name", None)
            _prvn_data = None
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

    _date_span_scanner(fields, scan_ocr)
    _approval_date_window_scanner(fields, scan_ocr, ctx)
    _service_code_scanner(fields, scan_ocr, ctx.raw_ocr_tokens)
    _auth_date_range_recovery(fields, scan_ocr)
    _service_dates_scanner(fields, scan_ocr)
    _units_scanner(fields, scan_ocr)
    _prior_auth_validation(fields, scan_ocr)
    _provider_name_recovery(fields, scan_ocr)
    _patient_name_recovery(fields, scan_ocr, ctx)
    _following_member_scan(fields, scan_ocr)
    _next_review_to_dob_transfer(fields, scan_ocr)
    _normalize_date_fields(fields)

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


def _date_span_scanner(fields: dict, scan_ocr: str) -> None:
    """Recover effective/expiration dates from Date Span style text."""
    span_re = re.compile(
        r"(?:date\s*span|authorization\s+dates?|service\s+dates?)\s*:?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\s*[-–—]+\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
        re.IGNORECASE,
    )
    span_m = span_re.search(scan_ocr or "")
    if not span_m:
        return

    start_date, end_date = span_m.group(1), span_m.group(2)
    eff_val = (fields.get("auth_effective_date") or {}).get("value") or ""
    exp_val = (fields.get("auth_expiration_date") or {}).get("value") or ""
    eff_valid = bool(_extract_date_tokens(eff_val))
    exp_valid = bool(_extract_date_tokens(exp_val))

    if not eff_valid or eff_val != start_date:
        fields["auth_effective_date"] = {
            "value": start_date,
            "confidence": 0.72,
            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
            "evidence_bbox": None,
            "evidence_text": f"OCR date span: {span_m.group(0)}",
            "candidates": [],
        }
        logger.info("Date span scanner set auth_effective_date=%s", start_date)

    if not exp_valid or exp_val != end_date:
        fields["auth_expiration_date"] = {
            "value": end_date,
            "confidence": 0.72,
            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
            "evidence_bbox": None,
            "evidence_text": f"OCR date span: {span_m.group(0)}",
            "candidates": [],
        }
        logger.info("Date span scanner set auth_expiration_date=%s", end_date)


def _approval_date_window_scanner(fields: dict, scan_ocr: str, ctx: PipelineContext) -> None:
    """Fallback scanner for approval-style date ranges when labels drift."""
    eff_val = (fields.get("auth_effective_date") or {}).get("value") or ""
    exp_val = (fields.get("auth_expiration_date") or {}).get("value") or ""
    if _extract_date_tokens(eff_val) and _extract_date_tokens(exp_val):
        return

    text = scan_ocr or ""
    if not text:
        return

    doc_type = getattr(getattr(ctx, "job", None), "doc_type", None)
    doc_type_value = getattr(doc_type, "value", str(doc_type or ""))
    if doc_type_value not in ("PRIOR_AUTH_APPROVAL", "UNKNOWN", ""):
        return

    range_re = re.compile(
        r"(\d{1,2}[/-]\d{1,3}[/-]\d{2,4})\s*(?:to|through|thru|[-–—]{1,2})\s*([0-9/\-]{6,12})",
        re.IGNORECASE,
    )

    best: tuple[int, int, str, str | None] | None = None
    for m in range_re.finditer(text):
        d1 = _coerce_noisy_date_token(m.group(1))
        d2 = _coerce_noisy_date_token(m.group(2))
        if not d1:
            continue
        window = text[max(0, m.start() - 90): min(len(text), m.end() + 90)].lower()
        score = 0
        for kw in ("date span", "service date", "authorization date", "first", "approved", "authorization"):
            if kw in window:
                score += 1
        for bad in ("appeal", "calendar days", "state hearing", "adverse decision"):
            if bad in window:
                score -= 2
        rank = (score, -m.start())
        if best is None or rank > (best[0], best[1]):
            best = (score, -m.start(), d1, d2)

    if not best:
        return

    start_raw = best[2]
    end_raw = best[3]
    start_dt = _parse_date_token(start_raw)
    end_dt = _parse_date_token(end_raw) if end_raw else None

    if start_dt and end_dt and start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    # Additional fallback observed in approval letters:
    # "First 30 days 08/07/2025-09705/2025; Day 31 forward ... 09/06/2025"
    if start_dt and end_dt is None:
        day_after_m = re.search(
            r"day\s*\d+\s*[^0-9]{0,30}(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})",
            text,
            re.IGNORECASE,
        )
        if day_after_m:
            maybe_after = _coerce_noisy_date_token(day_after_m.group(1))
            maybe_after_dt = _parse_date_token(maybe_after) if maybe_after else None
            if maybe_after_dt and maybe_after_dt >= start_dt:
                end_dt = maybe_after_dt - timedelta(days=1)

    if not start_dt:
        return

    if not _extract_date_tokens(eff_val):
        fields["auth_effective_date"] = {
            "value": start_dt.strftime("%m/%d/%Y"),
            "confidence": 0.68,
            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
            "evidence_bbox": None,
            "evidence_text": "OCR approval date-range fallback",
            "candidates": [],
        }
        logger.info(
            "Approval range scanner set auth_effective_date=%s",
            fields["auth_effective_date"]["value"],
        )

    if end_dt and not _extract_date_tokens(exp_val):
        fields["auth_expiration_date"] = {
            "value": end_dt.strftime("%m/%d/%Y"),
            "confidence": 0.68,
            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
            "evidence_bbox": None,
            "evidence_text": "OCR approval date-range fallback",
            "candidates": [],
        }
        logger.info(
            "Approval range scanner set auth_expiration_date=%s",
            fields["auth_expiration_date"]["value"],
        )


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


def _provider_name_recovery(fields: dict, scan_ocr: str) -> None:
    """Recover provider_name from OCR lines when field is missing or invalid."""
    existing = fields.get("provider_name") or {}
    existing_value = (existing.get("value") or "").strip()
    if existing_value and _is_reasonable_provider_name(existing_value):
        return
    if existing_value:
        fields.pop("provider_name", None)

    line_patterns = (
        re.compile(
            r"\bservicing\s+provider(?:\s+name)?\s*[:.\-]?\s*(.+)$",
            re.IGNORECASE,
        ),
        re.compile(
            r"\brequesting\s+provider(?:\s+name)?\s*[:.\-]?\s*(.+?)(?:\bservicing\s+provider\b|$)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:ordering|attending|referring|rendering|treating)?\s*provider(?:\s+name)?\s*[:.\-]\s*(.+)$",
            re.IGNORECASE,
        ),
        re.compile(r"^facility\s+name\s*[:\-]\s*(.+)$", re.IGNORECASE),
    )
    alt_patterns = (
        re.compile(r"\bto\.?\s*(.+?)\s+fax\s*[:#]", re.IGNORECASE),
        re.compile(
            r"\bcc:\s*request\s+provider\s*:?\s*(.+?)\s+(?:and/or\s+)?service\s+provider",
            re.IGNORECASE,
        ),
        re.compile(
            r"\brequest\s+provider\s*:?\s*(.+?)(?:\s+and/or|\s+service\s+provider|$)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bservice\s+provider\s*:?\s*(.+?)(?:\s+n/?a\b|$)",
            re.IGNORECASE,
        ),
        re.compile(
            r"\bmember\s+name\s*:?\s*(.+?)\s+requesting\s+provider\s+name",
            re.IGNORECASE,
        ),
    )
    label_only_re = re.compile(
        r"^(?:servicing|requesting|ordering|attending|referring|rendering|treating)?\s*"
        r"provider(?:\s+name)?\s*[:\-]?\s*$",
        re.IGNORECASE,
    )
    stop_at_re = re.compile(
        r"\b(?:npi|phone|fax|member(?:\s+id)?|dob|date\s+of\s+birth|authorization|"
        r"reference|effective\s+date|start\s+date|end\s+date)\b",
        re.IGNORECASE,
    )

    def _clean_provider_candidate(raw: str) -> str:
        candidate = _normalize_provider_name_value(raw)
        candidate = re.sub(r"^(?:and/or\s+)?(?:request|service)\s+provider\s*:?\s*", "", candidate, flags=re.IGNORECASE)
        words = candidate.split()
        if len(words) >= 8 and len(words) % 2 == 0:
            half = len(words) // 2
            left = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in words[:half]]
            right = [re.sub(r"[^a-z0-9]", "", w.lower()) for w in words[half:]]
            if left == right:
                candidate = " ".join(words[:half])
        return candidate.strip(" ,.;:-")

    lines = [ln.strip() for ln in (scan_ocr or "").splitlines() if ln.strip()]
    for idx, line in enumerate(lines):
        candidate = ""
        for pat in line_patterns:
            m = pat.search(line)
            if m:
                candidate = m.group(1).strip()
                break

        if not candidate:
            for pat in alt_patterns:
                m = pat.search(line)
                if m:
                    candidate = m.group(1).strip()
                    break

        if not candidate and label_only_re.match(line) and idx + 1 < len(lines):
            candidate = lines[idx + 1].strip()

        if not candidate:
            continue

        candidate = _clean_provider_candidate(candidate)
        stop_m = stop_at_re.search(candidate)
        if stop_m:
            candidate = candidate[: stop_m.start()].strip()
        candidate = candidate.strip(",.;:-")
        if not _is_reasonable_provider_name(candidate):
            continue

        logger.info("Provider name OCR recovery selected '%s'", candidate[:80])
        fields["provider_name"] = {
            "value": candidate,
            "confidence": 0.62,
            "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
            "evidence_bbox": None,
            "evidence_text": f"OCR: {line}",
            "candidates": [],
        }
        return


def _patient_name_recovery(fields: dict, scan_ocr: str, ctx: PipelineContext) -> None:
    """Patient name recovery from candidates and OCR scan."""
    existing = fields.get("patient_name") or {}
    existing_value = (existing.get("value") or "").strip()
    if existing_value and _is_reasonable_patient_name(existing_value):
        return

    _prvn_val_now = ((fields.get("provider_name") or {}).get("value") or "").lower()
    _prvn_words_now = set(re.sub(r"[^a-z]", " ", _prvn_val_now).split())

    fallback_candidates = list(existing.get("candidates") or [])
    if existing_value:
        logger.info(
            "patient_name '%s' looked implausible; attempting recovery",
            existing_value[:80],
        )
        fields.pop("patient_name", None)

    # First, try existing alternate candidates if they look name-like.
    for cand in fallback_candidates:
        cand_val = _clean_patient_name_candidate((cand.get("value") or "").strip())
        if not _is_reasonable_patient_name(cand_val):
            continue
        cand_words = set(re.sub(r"[^a-z]", " ", cand_val.lower()).split())
        if _prvn_words_now and cand_words and cand_words.issubset(_prvn_words_now):
            continue
        fields["patient_name"] = {
            "value": cand_val,
            "confidence": max(0.62, float(cand.get("confidence") or 0.0)),
            "method": cand.get("method", ExtractionMethodEnum.TEMPLATE_OCR.value),
            "evidence_bbox": cand.get("evidence_bbox"),
            "evidence_text": cand.get("evidence_text", f"OCR candidate: {cand_val}"),
            "candidates": [],
        }
        logger.info("Recovered patient_name from fallback candidate '%s'", cand_val[:80])
        return

    # Member/Patient Name scan from OCR text.
    name_label_re = re.compile(
        r"\b(?:member|patient)\s*name\s*[:;,\-]?\s*([^\n]+)",
        re.IGNORECASE,
    )
    lines = [ln.strip() for ln in (scan_ocr or "").splitlines() if ln.strip()]
    for line in lines:
        for match in name_label_re.finditer(line):
            name_val = _clean_patient_name_candidate(match.group(1))
            # Drop long trailing numeric IDs but preserve ordinal suffixes.
            name_val = re.sub(r"\s+\d{4,}.*$", "", name_val).strip(" ,.;:-")
            if not _is_reasonable_patient_name(name_val):
                continue
            name_words = set(re.sub(r"[^a-z]", " ", name_val.lower()).split())
            if _prvn_words_now and name_words and name_words.issubset(_prvn_words_now):
                continue
            logger.info("OCR scan recovered patient_name='%s'", name_val[:80])
            fields["patient_name"] = {
                "value": name_val,
                "confidence": 0.64,
                "method": ExtractionMethodEnum.TEMPLATE_OCR.value,
                "evidence_bbox": None,
                "evidence_text": f"OCR: {line[:160]}",
                "candidates": [],
            }
            return


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
