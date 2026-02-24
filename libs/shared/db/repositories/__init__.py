"""Repository layer for database access."""

from libs.shared.db.repositories.base import BaseRepository
from libs.shared.db.repositories.fax_job_repo import FaxJobRepository
from libs.shared.db.repositories.fax_page_repo import FaxPageRepository
from libs.shared.db.repositories.ocr_token_repo import OcrTokenRepository
from libs.shared.db.repositories.template_repo import (
    TemplateFieldRepository,
    TemplateRepository,
    TemplateSampleRepository,
    TemplateVersionRepository,
)
from libs.shared.db.repositories.extraction_repo import (
    ExtractedFieldRepository,
    ExtractionRepository,
)
from libs.shared.db.repositories.review_repo import FeedbackRepository, ReviewRepository

__all__ = [
    "BaseRepository",
    "FaxJobRepository",
    "FaxPageRepository",
    "OcrTokenRepository",
    "TemplateRepository",
    "TemplateVersionRepository",
    "TemplateSampleRepository",
    "TemplateFieldRepository",
    "ExtractedFieldRepository",
    "ExtractionRepository",
    "ReviewRepository",
    "FeedbackRepository",
]
