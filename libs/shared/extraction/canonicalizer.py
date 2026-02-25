"""
Field value canonicalization.

Normalizes extracted values to standard formats.
"""

import re
from datetime import datetime, timezone


class FieldCanonicalizer:
    """
    Canonicalizes field values to standard formats.

    Applies payer-specific and field-type-specific normalization.
    """

    # Common date formats to recognize
    DATE_FORMATS = [
        "%m/%d/%Y",
        "%m-%d-%Y",
        "%Y-%m-%d",
        "%m/%d/%y",
        "%m-%d-%y",
        "%B %d, %Y",
        "%b %d, %Y",
    ]

    # Standard output format
    STANDARD_DATE_FORMAT = "%Y-%m-%d"

    def canonicalize(
        self,
        value: str,
        field_type: str = "text",
        payer_name: str | None = None,
    ) -> str:
        """
        Canonicalize a field value.

        Args:
            value: Raw value.
            field_type: Type of field (text, date, phone, ssn, etc.).
            payer_name: Optional payer for payer-specific rules.

        Returns:
            Canonicalized value.
        """
        if not value:
            return ""

        # Basic cleanup
        value = value.strip()

        # Apply type-specific canonicalization
        if field_type == "date":
            return self._canonicalize_date(value)
        elif field_type == "phone":
            return self._canonicalize_phone(value)
        elif field_type == "ssn":
            return self._canonicalize_ssn(value)
        elif field_type == "member_id":
            return self._canonicalize_member_id(value, payer_name)
        elif field_type == "npi":
            return self._canonicalize_npi(value)
        else:
            return self._canonicalize_text(value)

    def _canonicalize_text(self, value: str) -> str:
        """Basic text canonicalization."""
        # Remove multiple spaces
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    def _canonicalize_date(self, value: str) -> str:
        """
        Canonicalize date to YYYY-MM-DD format.

        Args:
            value: Date string in various formats.

        Returns:
            Date in YYYY-MM-DD format or original if unparseable.
        """
        value = value.strip()

        for fmt in self.DATE_FORMATS:
            try:
                parsed = datetime.strptime(value, fmt)
                return parsed.strftime(self.STANDARD_DATE_FORMAT)
            except ValueError:
                continue

        # Try to extract date components with regex
        # Pattern: MM/DD/YYYY or similar
        match = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})", value)
        if match:
            month, day, year = match.groups()
            # Fix 2-digit year using a sliding pivot based on current year.
            # Anything within the past 90 years → 19xx/20xx accordingly;
            # otherwise assume 2000s.  This avoids the fixed-30 bug that
            # would misinterpret 2030+ dates as 1930s.
            if len(year) == 2:
                current_year = datetime.now(timezone.utc).year
                y = int(year)
                # Balanced 30-year forward window: 2-digit years within
                # 30 years ahead of now stay in current century;
                # otherwise go to the previous century.  This correctly
                # maps '90' → 1990 (DOB) while keeping '25' → 2025.
                full = (current_year // 100) * 100 + y
                if full > current_year + 30:
                    full -= 100
                year = str(full)
            try:
                parsed = datetime(int(year), int(month), int(day))
                return parsed.strftime(self.STANDARD_DATE_FORMAT)
            except ValueError:
                pass

        return value

    def _canonicalize_phone(self, value: str) -> str:
        """
        Canonicalize phone number to (XXX) XXX-XXXX format.

        Args:
            value: Phone number string.

        Returns:
            Formatted phone number.
        """
        # Extract digits only
        digits = re.sub(r"\D", "", value)

        # Remove country code if present
        if len(digits) == 11 and digits[0] == "1":
            digits = digits[1:]

        if len(digits) == 10:
            return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"

        return value

    def _canonicalize_ssn(self, value: str) -> str:
        """
        Canonicalize SSN to XXX-XX-XXXX format.

        Args:
            value: SSN string.

        Returns:
            Formatted SSN.
        """
        # Extract digits only
        digits = re.sub(r"\D", "", value)

        if len(digits) == 9:
            return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"

        return value

    def _canonicalize_member_id(
        self,
        value: str,
        payer_name: str | None = None,
    ) -> str:
        """
        Canonicalize member ID based on payer rules.

        Args:
            value: Member ID string.
            payer_name: Payer identifier.

        Returns:
            Canonicalized member ID.
        """
        # Default: uppercase, remove spaces
        value = value.upper()
        value = value.replace(" ", "")

        # Payer-specific format enforcement (case-insensitive matching)
        payer_upper = (payer_name or "").upper()
        if payer_upper == "ANTHEM":
            # Anthem member IDs: typically 3 letters + 9 digits (e.g., YAW123456789)
            # Dashes are not meaningful — strip them
            cleaned = re.sub(r"[^A-Z0-9]", "", value)
            if len(cleaned) >= 12 and cleaned[:3].isalpha() and cleaned[3:].isdigit():
                value = cleaned
        elif payer_upper == "UNITED_HEALTH":
            # UHC member IDs: typically 9-11 digits — dashes not meaningful
            cleaned = re.sub(r"[^0-9]", "", value)
            if 9 <= len(cleaned) <= 11:
                value = cleaned
        else:
            # For other payers, preserve dashes as they may be semantically
            # meaningful (e.g., Medicaid dependent suffix "82353822-01").
            # Only strip truly extraneous whitespace (already done above).
            pass

        return value

    def _canonicalize_npi(self, value: str) -> str:
        """
        Canonicalize NPI (National Provider Identifier).

        Args:
            value: NPI string.

        Returns:
            10-digit NPI.
        """
        digits = re.sub(r"\D", "", value)

        if len(digits) == 10:
            return digits

        return value

    def normalize_for_comparison(self, value: str) -> str:
        """
        Normalize value for fuzzy comparison.

        Args:
            value: Value to normalize.

        Returns:
            Normalized value for comparison.
        """
        if not value:
            return ""

        # Lowercase
        value = value.lower()
        # Remove all non-alphanumeric
        value = re.sub(r"[^a-z0-9]", "", value)
        return value

    def are_equivalent(
        self,
        value1: str,
        value2: str,
        field_type: str = "text",
    ) -> bool:
        """
        Check if two values are equivalent.

        Args:
            value1: First value.
            value2: Second value.
            field_type: Type of field.

        Returns:
            True if values are equivalent.
        """
        # Canonicalize both
        canon1 = self.canonicalize(value1, field_type)
        canon2 = self.canonicalize(value2, field_type)

        # Compare normalized versions
        norm1 = self.normalize_for_comparison(canon1)
        norm2 = self.normalize_for_comparison(canon2)

        return norm1 == norm2
