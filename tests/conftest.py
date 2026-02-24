"""
Shared pytest fixtures for fax OCR unit tests.
"""

import pytest

from libs.shared.extraction.validators import FieldValidator
from libs.shared.extraction.field_builder import FieldBuilder
from libs.shared.extraction.cross_field_validator import CrossFieldValidator


@pytest.fixture
def field_validator():
    """Return a fresh FieldValidator."""
    return FieldValidator()


@pytest.fixture
def field_builder():
    """Return a FieldBuilder with default settings."""
    return FieldBuilder()


@pytest.fixture
def cross_validator():
    """Return a fresh CrossFieldValidator."""
    return CrossFieldValidator()
