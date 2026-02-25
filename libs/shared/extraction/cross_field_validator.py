"""
Cross-field consistency validation.

Checks logical relationships between extracted fields:
- Date ordering (effective before expiration)
- Decision/denial_reason consistency
- Medical code format validation (ICD-10, CPT/HCPCS)
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class CrossFieldResult:
    """Result of cross-field validation."""

    is_consistent: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_consistent": self.is_consistent,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class CrossFieldValidator:
    """
    Validates logical consistency across extracted fields.
    """

    # Date formats to try when parsing
    DATE_FORMATS = [
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%m/%d/%y",
    ]

    def validate(self, fields: dict[str, dict[str, Any]]) -> CrossFieldResult:
        """
        Run all cross-field consistency checks.

        Args:
            fields: Extracted fields {field_key: {value, confidence, ...}}.

        Returns:
            CrossFieldResult with errors and warnings.
        """
        result = CrossFieldResult()

        self._check_date_ordering(fields, result)
        self._check_decision_consistency(fields, result)
        self._check_dob_in_past(fields, result)
        self._check_diagnosis_codes(fields, result)
        self._check_procedure_codes(fields, result)
        # Intelligence checks
        self._check_units_ne_member_id(fields, result)
        self._check_next_review_ne_dob(fields, result)
        self._check_auth_dates_ne_dob(fields, result)
        self._check_vlm_date_confusion(fields, result)
        self._check_prior_auth_not_prose(fields, result)

        result.is_consistent = len(result.errors) == 0
        return result

    def _get_value(self, fields: dict, key: str) -> str | None:
        """Extract value string from fields dict."""
        field_data = fields.get(key)
        if isinstance(field_data, dict):
            return field_data.get("value")
        return None

    def _parse_date(self, value: str) -> date | None:
        """Try to parse a date string."""
        for fmt in self.DATE_FORMATS:
            try:
                return datetime.strptime(value.strip(), fmt).date()
            except ValueError:
                continue
        return None

    def _check_date_ordering(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """Auth expiration must be after effective date."""
        eff_str = self._get_value(fields, "auth_effective_date")
        exp_str = self._get_value(fields, "auth_expiration_date")

        if not eff_str or not exp_str:
            return

        eff_date = self._parse_date(eff_str)
        exp_date = self._parse_date(exp_str)

        # Flag unparseable dates when values exist but can't be parsed
        if eff_str and eff_date is None:
            result.warnings.append(
                f"auth_effective_date ('{eff_str}') could not be parsed as a valid date"
            )
        if exp_str and exp_date is None:
            result.warnings.append(
                f"auth_expiration_date ('{exp_str}') could not be parsed as a valid date"
            )

        if eff_date and exp_date:
            if exp_date < eff_date:
                result.errors.append(
                    f"Auth expiration date ({exp_str}) is before "
                    f"effective date ({eff_str})"
                )
            elif exp_date == eff_date:
                result.warnings.append(
                    "Auth effective and expiration dates are the same"
                )

    def _check_decision_consistency(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """If APPROVED, units_requested should exist."""
        decision = self._get_value(fields, "decision")
        if not decision:
            return

        decision_upper = decision.upper().strip()

        if decision_upper == "APPROVED":
            # units_requested is nice to have but not required
            units = self._get_value(fields, "units_requested")
            if not units:
                result.warnings.append(
                    "Decision is APPROVED but no units_requested was extracted"
                )

    def _check_dob_in_past(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """Patient date of birth should be in the past."""
        dob_str = self._get_value(fields, "patient_dob")
        if not dob_str:
            return

        dob = self._parse_date(dob_str)
        if dob and dob > date.today():
            result.errors.append(
                f"Patient date of birth ({dob_str}) is not in the past"
            )

    def _check_diagnosis_codes(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """Diagnosis codes should match ICD-10 format (letter + digits/dot)."""
        codes_str = self._get_value(fields, "diagnosis_codes")
        if not codes_str:
            return

        # Split by comma, semicolon, or space
        codes = re.split(r"[,;\s]+", codes_str)
        icd10_pattern = re.compile(r"^[A-Z]\d{2}(\.[A-Za-z0-9]{1,4})?$", re.IGNORECASE)

        for code in codes:
            code = code.strip()
            if not code:
                continue
            if not icd10_pattern.match(code):
                result.warnings.append(
                    f"Diagnosis code '{code}' does not match ICD-10 format"
                )

    def _check_procedure_codes(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """Procedure codes should match CPT (5 digits) or HCPCS (letter+4 digits) format."""
        codes_str = self._get_value(fields, "service_code")
        if not codes_str:
            return

        codes = re.split(r"[,;\s]+", codes_str)
        # CPT: 5 digits  |  HCPCS: letter + 4 digits
        cpt_pattern = re.compile(r"^(\d{5}|[A-Z]\d{4})$", re.IGNORECASE)

        for code in codes:
            code = code.strip()
            if not code:
                continue
            if not cpt_pattern.match(code):
                result.warnings.append(
                    f"Procedure code '{code}' does not match CPT/HCPCS format"
                )

    # ── Intelligence cross-field checks ─────────────────────────────────────

    def _check_units_ne_member_id(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """units_requested should never equal member_id (VLM contamination)."""
        units = self._get_value(fields, "units_requested")
        member_id = self._get_value(fields, "member_id")
        if not units or not member_id:
            return
        if re.sub(r"\D", "", units) == re.sub(r"\D", "", member_id) and len(re.sub(r"\D", "", units)) >= 6:
            result.errors.append(
                f"units_requested ('{units}') duplicates member_id — "
                "VLM confused the member number with unit count"
            )

    def _check_next_review_ne_dob(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """next_review_date should never equal patient_dob."""
        review = self._get_value(fields, "next_review_date")
        dob = self._get_value(fields, "patient_dob")
        if not review or not dob:
            return
        # Normalize to digits only for comparison
        if re.sub(r"\D", "", review) == re.sub(r"\D", "", dob):
            result.errors.append(
                f"next_review_date ('{review}') equals patient_dob — "
                "VLM returned DOB for review date"
            )

    def _check_auth_dates_ne_dob(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """auth_effective_date / auth_expiration_date should never equal patient_dob."""
        dob = self._get_value(fields, "patient_dob")
        if not dob:
            return
        dob_norm = re.sub(r"\D", "", dob)
        if not dob_norm:
            return
        for fk in ("auth_effective_date", "auth_expiration_date"):
            val = self._get_value(fields, fk)
            if val and re.sub(r"\D", "", val) == dob_norm:
                result.errors.append(
                    f"{fk} ('{val}') equals patient_dob — "
                    "VLM likely returned DOB for auth date"
                )

    def _check_vlm_date_confusion(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """Detect when the same date appears across 3+ different date fields."""
        date_fields = [
            "patient_dob", "auth_effective_date", "auth_expiration_date",
            "next_review_date", "service_start_date", "service_end_date",
        ]
        date_values: list[str] = []
        for fk in date_fields:
            v = self._get_value(fields, fk)
            if v:
                # Canonicalize to YYYYMMDD so different formats compare equal
                parsed = self._parse_date(v)
                if parsed:
                    date_values.append(parsed.isoformat())  # YYYY-MM-DD
                else:
                    # Fallback to digits-only for unparseable dates
                    date_values.append(re.sub(r"\D", "", v))

        # Count unique normalized values
        counts = Counter(date_values)
        for norm_val, cnt in counts.items():
            if cnt >= 3 and norm_val:
                result.warnings.append(
                    f"Same date value (normalized: {norm_val}) appears in {cnt} date fields — "
                    "possible VLM date confusion; verify manually"
                )

    def _check_prior_auth_not_prose(
        self,
        fields: dict[str, dict[str, Any]],
        result: CrossFieldResult,
    ) -> None:
        """prior_auth_number should not be a prose sentence."""
        auth_num = self._get_value(fields, "prior_auth_number")
        if not auth_num:
            return
        words = auth_num.split()
        if len(words) > 4:
            result.warnings.append(
                f"prior_auth_number ('{auth_num}') appears to be prose text, not an auth number"
            )
