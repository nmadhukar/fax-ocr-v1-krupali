"""
Field validation utilities.

Validates extracted values against payer-specific rules.
"""

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from libs.shared.config.payer_rules import get_payer_rules


@dataclass
class ValidationResult:
    """Result of field validation."""

    field_key: str
    is_valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    normalized_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "field_key": self.field_key,
            "is_valid": self.is_valid,
            "errors": self.errors,
            "warnings": self.warnings,
            "normalized_value": self.normalized_value,
        }


class FieldValidator:
    """
    Validates extracted field values.

    Uses payer-specific rules and common validation patterns.
    """

    def __init__(self):
        """Initialize validator."""
        self.payer_rules = get_payer_rules()

    def validate(
        self,
        field_key: str,
        value: str | None,
        payer_name: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> ValidationResult:
        """
        Validate a field value.

        Args:
            field_key: Field identifier.
            value: Value to validate.
            payer_name: Optional payer for specific rules.
            context: Optional context with other field values.

        Returns:
            ValidationResult.
        """
        errors: list[str] = []
        warnings: list[str] = []
        normalized_value = value

        # Check for empty required field
        if not value or not value.strip():
            if self._is_required(field_key, payer_name):
                errors.append(f"Required field '{field_key}' is empty")
            return ValidationResult(
                field_key=field_key,
                is_valid=len(errors) == 0,
                errors=errors,
            )

        # Apply payer-specific validation
        if payer_name:
            is_valid, payer_errors = self.payer_rules.validate_field(
                payer_name, field_key, value, context
            )
            errors.extend(payer_errors)

        # Apply type-specific validation
        type_result = self._validate_by_type(field_key, value)
        errors.extend(type_result["errors"])
        warnings.extend(type_result["warnings"])
        if type_result["normalized"]:
            normalized_value = type_result["normalized"]

        return ValidationResult(
            field_key=field_key,
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
            normalized_value=normalized_value,
        )

    def validate_all(
        self,
        fields: dict[str, str | None],
        payer_name: str | None = None,
    ) -> dict[str, ValidationResult]:
        """
        Validate all fields.

        Args:
            fields: Dictionary of field_key -> value.
            payer_name: Optional payer for specific rules.

        Returns:
            Dictionary of field_key -> ValidationResult.
        """
        results = {}
        for field_key, value in fields.items():
            results[field_key] = self.validate(
                field_key,
                value,
                payer_name,
                context=fields,
            )
        return results

    def _is_required(self, field_key: str, payer_name: str | None) -> bool:
        """Check if field is required."""
        if payer_name:
            return self.payer_rules.is_critical_field(payer_name, field_key)

        # Default critical fields (aligned with hitl.py _CRITICAL_FIELDS)
        critical_fields = [
            "member_id",
            "prior_auth_number",
            "patient_name",
            "patient_dob",
        ]
        return field_key in critical_fields

    def _validate_by_type(self, field_key: str, value: str) -> dict[str, Any]:
        """
        Apply type-specific validation.

        Args:
            field_key: Field identifier.
            value: Value to validate.

        Returns:
            Dict with errors, warnings, and normalized value.
        """
        errors: list[str] = []
        warnings: list[str] = []
        normalized = None

        # Determine field type from key
        field_type = self._infer_field_type(field_key)

        if field_type == "date":
            result = self._validate_date(value)
            errors.extend(result["errors"])
            normalized = result.get("normalized")

        elif field_type == "phone":
            result = self._validate_phone(value)
            errors.extend(result["errors"])

        elif field_type == "ssn":
            result = self._validate_ssn(value)
            errors.extend(result["errors"])

        elif field_type == "npi":
            result = self._validate_npi(value)
            errors.extend(result["errors"])

        return {
            "errors": errors,
            "warnings": warnings,
            "normalized": normalized,
        }

    def _infer_field_type(self, field_key: str) -> str:
        """Infer field type from key name."""
        key_lower = field_key.lower()

        if "date" in key_lower or "dob" in key_lower:
            return "date"
        elif "phone" in key_lower or key_lower in ("fax_number", "fax_phone", "provider_fax") or re.search(r"(?:^|_)fax$", key_lower):
            return "phone"
        elif "ssn" in key_lower or "social" in key_lower:
            return "ssn"
        elif "npi" in key_lower:
            return "npi"

        return "text"

    def _validate_date(self, value: str) -> dict[str, Any]:
        """Validate date field."""
        errors = []

        # Try common date formats; single-digit month/day handled by normalizing first
        date_formats = [
            "%Y-%m-%d",
            "%m/%d/%Y",
            "%m-%d-%Y",
            "%m/%d/%y",
            "%m.%d.%Y",
            "%m.%d.%y",
        ]

        parsed = None
        # Normalize separators to "/" and try standard formats
        _norm_val = re.sub(r"[-.]", "/", value.strip())
        for fmt in date_formats:
            for _v in (_norm_val, value.strip()):
                try:
                    parsed = datetime.strptime(_v, fmt).date()
                    break
                except (ValueError, TypeError):
                    continue
            if parsed:
                break

        if parsed is None:
            errors.append(f"Invalid date format: '{value}'")
            return {"errors": errors, "normalized": None}

        # Check for reasonable date range
        today = date.today()
        min_date = date(1900, 1, 1)
        max_date = date(today.year + 5, 12, 31)

        if parsed < min_date or parsed > max_date:
            errors.append(f"Date '{value}' is outside valid range")

        return {
            "errors": errors,
            "normalized": parsed.strftime("%Y-%m-%d") if parsed else None,
        }

    def _validate_phone(self, value: str) -> dict[str, Any]:
        """Validate phone number."""
        errors = []

        # Extract digits
        digits = re.sub(r"\D", "", value)

        # US phone numbers
        if len(digits) == 10:
            pass  # Valid
        elif len(digits) == 11 and digits[0] == "1":
            pass  # Valid with country code
        else:
            errors.append(f"Invalid phone number format: '{value}'")

        return {"errors": errors}

    def _validate_ssn(self, value: str) -> dict[str, Any]:
        """Validate SSN."""
        errors = []

        digits = re.sub(r"\D", "", value)

        if len(digits) != 9:
            errors.append(f"SSN must be 9 digits: '{value}'")
        elif (digits[0:3] == "000" or digits[3:5] == "00" or digits[5:9] == "0000"
              or digits[0:3] == "666" or digits[0] == "9"):
            errors.append(f"Invalid SSN format: '{value}'")

        return {"errors": errors}

    def _validate_npi(self, value: str) -> dict[str, Any]:
        """Validate NPI using CMS Luhn algorithm (80840 prefix)."""
        errors = []

        digits = re.sub(r"\D", "", value)

        if len(digits) != 10:
            errors.append(f"NPI must be 10 digits: '{value}'")
            return {"errors": errors}

        # CMS NPI spec: prefix with "80840" before applying Luhn check
        # See: https://www.cms.gov/Regulations-and-Guidance/Administrative-Simplification/NationalProvIdentStand
        if not self._luhn_check("80840" + digits):
            errors.append(f"Invalid NPI check digit: '{value}'")

        return {"errors": errors}

    def _luhn_check(self, digits: str) -> bool:
        """Validate using Luhn algorithm."""
        def digits_of(n: str) -> list[int]:
            return [int(d) for d in n]

        check = digits_of(digits)
        odd = check[-1::-2]
        even = check[-2::-2]

        total = sum(odd)
        for d in even:
            total += sum(digits_of(str(d * 2)))

        return total % 10 == 0
