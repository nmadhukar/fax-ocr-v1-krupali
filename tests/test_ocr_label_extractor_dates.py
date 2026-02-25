"""Regression tests for OCR label date normalization."""

from libs.shared.extraction import ocr_label_extractor as ocr


def test_effective_date_keeps_first_date_in_range():
    value = "8/16/2025-9/14/2025"
    assert ocr._normalize_date_value_for_field("auth_effective_date", value) == "8/16/2025"


def test_expiration_date_keeps_last_date_in_range():
    value = "8/16/2025-9/14/2025"
    assert ocr._normalize_date_value_for_field("auth_expiration_date", value) == "9/14/2025"


def test_invalid_year_first_tokens_are_rejected():
    value = "5122-29-03 5122-27-03"
    assert ocr._normalize_date_value_for_field("auth_expiration_date", value) == ""


def test_year_first_iso_date_is_allowed_for_dob():
    value = "1981-02-23"
    assert ocr._normalize_date_value_for_field("patient_dob", value) == "1981-02-23"
