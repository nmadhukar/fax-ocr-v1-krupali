"""
Payer-specific validation rules loader.

Loads and provides access to payer-specific field validation rules,
canonicalization settings, and confidence thresholds.
"""

import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class FieldValidation(BaseModel):
    """Field validation rules."""

    regex: str | None = None
    description: str | None = None
    example: str | None = None
    format: str | None = None
    validate_not_future: bool = False
    validate_after_effective: bool = False
    min_length: int | None = None
    max_length: int | None = None


class Canonicalization(BaseModel):
    """Field canonicalization rules."""

    uppercase: bool = False
    lowercase: bool = False
    remove_spaces: bool = False
    remove_dashes: bool = False
    remove_special: bool = False
    trim: bool = True
    standard_format: str | None = None
    accept_formats: list[str] = Field(default_factory=list)


class PayerConfig(BaseModel):
    """Configuration for a specific payer."""

    display_name: str
    critical_fields: list[str] = Field(default_factory=list)
    field_validations: dict[str, FieldValidation] = Field(default_factory=dict)
    confidence_thresholds: dict[str, float] = Field(default_factory=dict)
    canonicalization: dict[str, Canonicalization] = Field(default_factory=dict)


class FuzzyMatchingConfig(BaseModel):
    """Fuzzy matching configuration."""

    levenshtein_threshold: int = 2
    min_token_length: int = 3
    case_sensitive: bool = False


class CoverPageConfig(BaseModel):
    """Cover page detection configuration."""

    keywords: list[str] = Field(default_factory=list)
    min_text_density: float = 0.05


class PayerRulesConfig(BaseModel):
    """Complete payer rules configuration."""

    payers: dict[str, PayerConfig] = Field(default_factory=dict)
    fuzzy_matching: FuzzyMatchingConfig = Field(default_factory=FuzzyMatchingConfig)
    cover_page: CoverPageConfig = Field(default_factory=CoverPageConfig)


class PayerRulesLoader:
    """
    Loads and provides access to payer validation rules.

    Supports hot-reloading of rules for development.
    """

    def __init__(self, config_path: Path | None = None):
        """
        Initialize the loader.

        Args:
            config_path: Path to payer_rules.yml. If None, uses default.
        """
        if config_path is None:
            config_path = Path(__file__).parent.parent.parent.parent / "configs" / "payer_rules.yml"
        self.config_path = config_path
        self._config: PayerRulesConfig | None = None
        self._load_time: datetime | None = None

    def load(self, force_reload: bool = False) -> PayerRulesConfig:
        """
        Load configuration from YAML file.

        Args:
            force_reload: If True, bypass cache and reload.

        Returns:
            PayerRulesConfig instance.
        """
        if self._config is not None and not force_reload:
            return self._config

        if not self.config_path.exists():
            # Return default config if file doesn't exist
            self._config = PayerRulesConfig()
            return self._config

        with open(self.config_path, "r", encoding="utf-8") as f:
            raw_config = yaml.safe_load(f)

        # Parse payers
        payers: dict[str, PayerConfig] = {}
        for payer_key, payer_data in raw_config.get("payers", {}).items():
            field_validations = {}
            for field_key, field_data in payer_data.get("field_validations", {}).items():
                field_validations[field_key] = FieldValidation(**field_data)

            canonicalization = {}
            for canon_key, canon_data in payer_data.get("canonicalization", {}).items():
                canonicalization[canon_key] = Canonicalization(**canon_data)

            payers[payer_key] = PayerConfig(
                display_name=payer_data.get("display_name", payer_key),
                critical_fields=payer_data.get("critical_fields", []),
                field_validations=field_validations,
                confidence_thresholds=payer_data.get("confidence_thresholds", {}),
                canonicalization=canonicalization,
            )

        # Parse other sections
        fuzzy_config = raw_config.get("fuzzy_matching", {})
        cover_config = raw_config.get("cover_page_keywords", [])

        self._config = PayerRulesConfig(
            payers=payers,
            fuzzy_matching=FuzzyMatchingConfig(**fuzzy_config) if fuzzy_config else FuzzyMatchingConfig(),
            cover_page=CoverPageConfig(keywords=cover_config if isinstance(cover_config, list) else []),
        )
        self._load_time = datetime.now()

        return self._config

    def get_payer(self, payer_name: str) -> PayerConfig | None:
        """Get configuration for a specific payer."""
        config = self.load()
        return config.payers.get(payer_name)

    def validate_field(
        self,
        payer_name: str,
        field_key: str,
        value: str,
        context: dict[str, Any] | None = None,
    ) -> tuple[bool, list[str]]:
        """
        Validate a field value against payer rules.

        Args:
            payer_name: Payer identifier (e.g., 'ANTHEM').
            field_key: Field key (e.g., 'member_id').
            value: Field value to validate.
            context: Additional context (e.g., other field values).

        Returns:
            Tuple of (is_valid, list of error messages).
        """
        errors: list[str] = []
        payer = self.get_payer(payer_name)

        if payer is None:
            return True, []  # No rules = always valid

        validation = payer.field_validations.get(field_key)
        if validation is None:
            return True, []

        # Regex validation
        if validation.regex:
            if not re.match(validation.regex, value):
                errors.append(
                    f"Field '{field_key}' does not match expected format. "
                    f"Expected: {validation.description or validation.regex}"
                )

        # Length validation
        if validation.min_length and len(value) < validation.min_length:
            errors.append(
                f"Field '{field_key}' is too short. Minimum: {validation.min_length}"
            )
        if validation.max_length and len(value) > validation.max_length:
            errors.append(
                f"Field '{field_key}' is too long. Maximum: {validation.max_length}"
            )

        # Date validations
        if validation.validate_not_future and validation.format:
            try:
                parsed_date = self._parse_date(value, validation.format)
                if parsed_date and parsed_date > date.today():
                    errors.append(f"Field '{field_key}' cannot be a future date")
            except ValueError:
                errors.append(f"Field '{field_key}' is not a valid date")

        if validation.validate_after_effective and context:
            effective_date_str = context.get("auth_effective_date")
            if effective_date_str and validation.format:
                try:
                    expiration = self._parse_date(value, validation.format)
                    effective = self._parse_date(effective_date_str, validation.format)
                    if expiration and effective and expiration <= effective:
                        errors.append(
                            f"Field '{field_key}' must be after effective date"
                        )
                except ValueError:
                    pass  # Date parsing error handled elsewhere

        return len(errors) == 0, errors

    def canonicalize_field(
        self, payer_name: str, field_key: str, value: str
    ) -> str:
        """
        Apply canonicalization rules to a field value.

        Args:
            payer_name: Payer identifier.
            field_key: Field key.
            value: Raw field value.

        Returns:
            Canonicalized value.
        """
        payer = self.get_payer(payer_name)
        if payer is None:
            return value.strip()

        # Check for field-specific rules
        canon = payer.canonicalization.get(field_key)

        # Check for type-based rules (e.g., 'dates')
        if canon is None and field_key.endswith("_date"):
            canon = payer.canonicalization.get("dates")

        if canon is None:
            return value.strip()

        result = value

        if canon.trim:
            result = result.strip()
        if canon.uppercase:
            result = result.upper()
        if canon.lowercase:
            result = result.lower()
        if canon.remove_spaces:
            result = result.replace(" ", "")
        if canon.remove_dashes:
            result = result.replace("-", "")
        if canon.remove_special:
            result = re.sub(r"[^a-zA-Z0-9]", "", result)

        # Date format standardization
        if canon.standard_format and canon.accept_formats:
            for fmt in canon.accept_formats:
                try:
                    parsed = self._parse_date(result, fmt)
                    if parsed:
                        result = parsed.strftime(self._format_to_strftime(canon.standard_format))
                        break
                except ValueError:
                    continue

        return result

    def get_confidence_threshold(
        self, payer_name: str, field_key: str
    ) -> float:
        """Get the minimum confidence threshold for a field."""
        payer = self.get_payer(payer_name)
        if payer is None:
            return 0.70  # Default

        return payer.confidence_thresholds.get(field_key, 0.70)

    def is_critical_field(self, payer_name: str, field_key: str) -> bool:
        """Check if a field is critical for the payer."""
        payer = self.get_payer(payer_name)
        if payer is None:
            return False
        return field_key in payer.critical_fields

    def _parse_date(self, value: str, fmt: str) -> date | None:
        """Parse a date string using the given format."""
        strftime_fmt = self._format_to_strftime(fmt)
        try:
            return datetime.strptime(value, strftime_fmt).date()
        except ValueError:
            return None

    def _format_to_strftime(self, fmt: str) -> str:
        """Convert format string to strftime format."""
        return (
            fmt.replace("YYYY", "%Y")
            .replace("MM", "%m")
            .replace("DD", "%d")
        )


@lru_cache
def get_payer_rules() -> PayerRulesLoader:
    """Get cached payer rules loader."""
    return PayerRulesLoader()
