"""
Unit tests for FieldValidator.

Tests type inference, date/phone/SSN/NPI validation, and the H3 fix
(fax_number is phone, but fax_cover_sheet is not).
"""

import pytest

from libs.shared.extraction.validators import FieldValidator, ValidationResult


class TestInferFieldType:
    """Test _infer_field_type (H3 regression)."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.v = FieldValidator()

    def test_phone_field(self):
        assert self.v._infer_field_type("phone_number") == "phone"

    def test_fax_number_is_phone(self):
        """H3 fix: fax_number should still be classified as phone."""
        assert self.v._infer_field_type("fax_number") == "phone"

    def test_fax_phone_is_phone(self):
        assert self.v._infer_field_type("fax_phone") == "phone"

    def test_fax_cover_sheet_is_text(self):
        """H3 fix: generic 'fax' substring should NOT infer phone type."""
        assert self.v._infer_field_type("fax_cover_sheet") == "text"

    def test_fax_job_id_is_text(self):
        assert self.v._infer_field_type("fax_job_id") == "text"

    def test_date_field(self):
        assert self.v._infer_field_type("auth_effective_date") == "date"

    def test_dob_field(self):
        assert self.v._infer_field_type("patient_dob") == "date"

    def test_ssn_field(self):
        assert self.v._infer_field_type("ssn") == "ssn"

    def test_npi_field(self):
        assert self.v._infer_field_type("provider_npi") == "npi"

    def test_generic_text(self):
        assert self.v._infer_field_type("patient_name") == "text"


class TestValidateDate:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.v = FieldValidator()

    def test_valid_mmddyyyy(self):
        result = self.v.validate("auth_effective_date", "01/15/2024")
        assert result.is_valid

    def test_valid_yyyymmdd(self):
        result = self.v.validate("auth_effective_date", "2024-01-15")
        assert result.is_valid

    def test_invalid_date(self):
        result = self.v.validate("auth_effective_date", "not-a-date")
        assert not result.is_valid
        assert any("date" in e.lower() for e in result.errors)

    def test_empty_string(self):
        result = self.v.validate("auth_effective_date", "")
        assert result.is_valid  # empty value should pass (not required)

    def test_none_value(self):
        result = self.v.validate("auth_effective_date", None)
        assert result.is_valid


class TestValidatePhone:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.v = FieldValidator()

    def test_valid_10digit(self):
        result = self.v.validate("phone_number", "6145551234")
        assert result.is_valid

    def test_valid_formatted(self):
        result = self.v.validate("phone_number", "(614) 555-1234")
        assert result.is_valid

    def test_too_short(self):
        result = self.v.validate("phone_number", "123")
        assert not result.is_valid


class TestValidateNpi:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.v = FieldValidator()

    def test_valid_npi(self):
        # NPI 1234567893 passes CMS Luhn check (80840 prefix)
        result = self.v.validate("provider_npi", "1234567893")
        assert result.is_valid

    def test_invalid_npi_too_short(self):
        result = self.v.validate("provider_npi", "12345")
        assert not result.is_valid

    def test_invalid_npi_luhn(self):
        # NPI 1234567897 passes raw Luhn but fails CMS Luhn (80840 prefix)
        result = self.v.validate("provider_npi", "1234567897")
        assert not result.is_valid


class TestValidateAll:
    def test_validate_all_returns_all_keys(self):
        v = FieldValidator()
        fields = {
            "patient_name": "John Doe",
            "auth_effective_date": "01/01/2024",
        }
        results = v.validate_all(fields)
        assert set(results.keys()) == set(fields.keys())
        assert all(isinstance(r, ValidationResult) for r in results.values())
