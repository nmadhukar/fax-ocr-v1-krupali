"""SQLAlchemy models for Healthcare Fax Processing System."""

from libs.shared.db.models.enums import (
    DocTypeEnum,
    ExtractionMethodEnum,
    FaxJobStatusEnum,
    PayerNameEnum,
)
from libs.shared.db.models.fax_job import FaxJob
from libs.shared.db.models.fax_page import FaxPage
from libs.shared.db.models.fax_ocr_token import FaxOcrToken
from libs.shared.db.models.fax_template import (
    FaxTemplate,
    FaxTemplateField,
    FaxTemplateSample,
    FaxTemplateVersion,
)
from libs.shared.db.models.fax_extraction import (
    FaxExtractedField,
    FaxExtraction,
)
from libs.shared.db.models.fax_embedding import FaxEmbedding
from libs.shared.db.models.fax_review import FaxFeedback, FaxReview
from libs.shared.db.models.fax_label_example import FaxLabelExample
from libs.shared.db.models.audit_log import AuditLog
from libs.shared.db.models.model_version import ModelVersion

__all__ = [
    # Enums
    "PayerNameEnum",
    "DocTypeEnum",
    "FaxJobStatusEnum",
    "ExtractionMethodEnum",
    # Core models
    "FaxJob",
    "FaxPage",
    "FaxOcrToken",
    # Template models
    "FaxTemplate",
    "FaxTemplateVersion",
    "FaxTemplateSample",
    "FaxTemplateField",
    # Extraction models
    "FaxExtractedField",
    "FaxExtraction",
    # Embedding models
    "FaxEmbedding",
    # Review models
    "FaxReview",
    "FaxFeedback",
    # Training data
    "FaxLabelExample",
    # Audit
    "AuditLog",
    # Model version tracking
    "ModelVersion",
]
