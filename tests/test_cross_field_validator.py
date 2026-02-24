"""
Unit tests for CrossFieldValidator.

Tests date ordering, decision consistency, DOB checks, medical code validation,
units-member_id dedup, next_review_date!=DOB, VLM date confusion, and prose auth#.
"""

import pytest

from libs.shared.extraction.cross_field_validator import (
    CrossFieldResult,
    CrossFieldValidator,
)


def _field(value, conf=0.9, method="TEMPLATE_OCR"):
    return {"value": value, "confidence": conf, "method": method}


class TestDateOrdering:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cv = CrossFieldValidator()

    def test_valid_ordering(self):
        fields = {
            "auth_effective_date": _field("01/01/2024"),
            "auth_expiration_date": _field("12/31/2024"),
        }
        result = self.cv.validate(fields)
        assert result.is_consistent
        assert not result.errors

    def test_effective_after_expiration(self):
        fields = {
            "auth_effective_date": _field("12/31/2024"),
            "auth_expiration_date": _field("01/01/2024"),
        }
        result = self.cv.validate(fields)
        assert not result.is_consistent
        assert any("before" in e.lower() or "after" in e.lower() for e in result.errors)

    def test_missing_one_date_passes(self):
        fields = {
            "auth_effective_date": _field("01/01/2024"),
        }
        result = self.cv.validate(fields)
        # Missing expiration should not cause an error
        assert not any("expiration" in e.lower() for e in result.errors)


class TestDecisionConsistency:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cv = CrossFieldValidator()

    def test_denied_is_consistent(self):
        """DENIED without additional fields is valid (denial_reason is not an extracted field)."""
        fields = {
            "decision": _field("DENIED"),
        }
        result = self.cv.validate(fields)
        assert result.is_consistent

    def test_approved_is_consistent(self):
        fields = {
            "decision": _field("APPROVED"),
            "units_requested": _field("10"),
        }
        result = self.cv.validate(fields)
        # Approved + units is totally fine
        assert result.is_consistent


class TestDobInPast:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cv = CrossFieldValidator()

    def test_future_dob(self):
        fields = {
            "patient_dob": _field("01/01/2099"),
        }
        result = self.cv.validate(fields)
        assert not result.is_consistent
        assert any("past" in e.lower() or "dob" in e.lower() for e in result.errors)

    def test_past_dob(self):
        fields = {
            "patient_dob": _field("01/01/1990"),
        }
        result = self.cv.validate(fields)
        assert not any("dob" in e.lower() for e in result.errors)


class TestDiagnosisCodes:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cv = CrossFieldValidator()

    def test_valid_icd10(self):
        fields = {"diagnosis_codes": _field("F32.1")}
        result = self.cv.validate(fields)
        assert not any("diagnosis" in e.lower() for e in result.errors)
        assert not any("icd" in w.lower() for w in result.warnings)

    def test_valid_icd10_with_alpha_extension(self):
        """ICD-10 codes like T21.01XA (burn codes with extension) are valid."""
        fields = {"diagnosis_codes": _field("T21.01XA")}
        result = self.cv.validate(fields)
        assert not any("icd" in w.lower() for w in result.warnings)

    def test_valid_icd10_no_dot(self):
        """3-character ICD-10 codes without dot portion are valid."""
        fields = {"diagnosis_codes": _field("F32")}
        result = self.cv.validate(fields)
        assert not any("icd" in w.lower() for w in result.warnings)

    def test_invalid_code(self):
        fields = {"diagnosis_codes": _field("123ABC")}
        result = self.cv.validate(fields)
        assert any("diagnosis" in w.lower() or "icd" in w.lower() for w in result.warnings)


class TestProcedureCodes:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cv = CrossFieldValidator()

    def test_valid_cpt(self):
        fields = {"service_code": _field("99213")}
        result = self.cv.validate(fields)
        assert not any("procedure" in e.lower() or "service" in e.lower() for e in result.errors)

    def test_valid_hcpcs(self):
        fields = {"service_code": _field("H0031")}
        result = self.cv.validate(fields)
        assert not any("procedure" in e.lower() or "service" in e.lower() for e in result.errors)


class TestUnitsNeMemberId:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cv = CrossFieldValidator()

    def test_units_equals_member_id(self):
        fields = {
            "units_requested": _field("123456789"),
            "member_id": _field("123456789"),
        }
        result = self.cv.validate(fields)
        assert not result.is_consistent
        assert any("member" in e.lower() or "units" in e.lower() for e in result.errors)


class TestNextReviewNeDob:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cv = CrossFieldValidator()

    def test_same_values(self):
        fields = {
            "next_review_date": _field("01/15/1985"),
            "patient_dob": _field("01/15/1985"),
        }
        result = self.cv.validate(fields)
        assert any("next_review" in e.lower() or "dob" in e.lower() for e in result.errors)


class TestPriorAuthNotProse:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cv = CrossFieldValidator()

    def test_prose_auth_number(self):
        fields = {
            "prior_auth_number": _field("This is an approved authorization for the patient"),
        }
        result = self.cv.validate(fields)
        assert any("auth" in w.lower() or "prose" in w.lower() for w in result.warnings)

    def test_valid_auth_number(self):
        fields = {
            "prior_auth_number": _field("PA12345678"),
        }
        result = self.cv.validate(fields)
        assert not any("prose" in w.lower() for w in result.warnings)


class TestVlmDateConfusion:
    @pytest.fixture(autouse=True)
    def setup(self):
        self.cv = CrossFieldValidator()

    def test_same_date_in_three_fields_warns(self):
        """Same date across 3+ date fields should trigger a warning."""
        fields = {
            "patient_dob": _field("01/15/1990"),
            "auth_effective_date": _field("01/15/1990"),
            "auth_expiration_date": _field("01/15/1990"),
        }
        result = self.cv.validate(fields)
        assert any("date" in w.lower() and "confusion" in w.lower() for w in result.warnings)

    def test_same_date_in_two_fields_ok(self):
        """Same date in only 2 fields should NOT trigger a confusion warning."""
        fields = {
            "auth_effective_date": _field("01/15/2024"),
            "auth_expiration_date": _field("01/15/2024"),
        }
        result = self.cv.validate(fields)
        assert not any("confusion" in w.lower() for w in result.warnings)


class TestEmptyFields:
    def test_empty_dict(self):
        cv = CrossFieldValidator()
        result = cv.validate({})
        assert result.is_consistent
        assert not result.errors
