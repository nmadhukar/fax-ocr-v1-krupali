"""
Pipeline context and stage definitions for fax processing.

The PipelineContext is a mutable state bag that flows through each
pipeline stage, replacing the 20+ local variables that previously
lived inside the monolithic process_fax_task function.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy.orm import Session

from libs.shared.config import get_settings
from libs.shared.db.models.enums import (
    DocTypeEnum,
    ExtractionMethodEnum,
    FaxJobStatusEnum,
    PayerNameEnum,
)
from libs.shared.db.repositories.extraction_repo import (
    ExtractedFieldRepository,
    ExtractionRepository,
)
from libs.shared.db.repositories.fax_job_repo import FaxJobRepository
from libs.shared.db.repositories.fax_page_repo import FaxPageRepository
from libs.shared.db.repositories.ocr_token_repo import OcrTokenRepository
from libs.shared.db.repositories.review_repo import ReviewRepository
from libs.shared.extraction.field_builder import ExtractionCandidate, FieldBuilder
from libs.shared.extraction.template_extractor import TemplateExtractor
from libs.shared.monitoring.pipeline_metrics import PipelineMetrics
from libs.shared.ocr.paddle_client import PaddleOcrClient
from libs.shared.scoring.confidence_scorer import ConfidenceScorer
from libs.shared.storage.s3_adapter import S3StorageAdapter
from libs.shared.template.matcher import TemplateMatcher
from libs.shared.utils.image_utils import ImagePreprocessor

logger = logging.getLogger(__name__)


@dataclass
class PipelineContext:
    """Mutable state that flows through all pipeline stages."""

    # ── Identifiers ──────────────────────────────────────────────────
    fax_job_id: str
    job_uuid: UUID
    tenant_id: str

    # ── Database ─────────────────────────────────────────────────────
    db: Session
    settings: Any  # AppSettings

    # ── Job record ───────────────────────────────────────────────────
    job: Any = None  # FaxJob ORM model

    # ── Repositories (created once) ──────────────────────────────────
    job_repo: FaxJobRepository | None = None
    page_repo: FaxPageRepository | None = None
    token_repo: OcrTokenRepository | None = None
    field_repo: ExtractedFieldRepository | None = None
    extraction_repo: ExtractionRepository | None = None
    review_repo: ReviewRepository | None = None

    # ── Infrastructure ───────────────────────────────────────────────
    storage: S3StorageAdapter | None = None
    preprocessor: ImagePreprocessor | None = None
    ocr_client: PaddleOcrClient | None = None
    template_matcher: TemplateMatcher | None = None
    field_extractor: TemplateExtractor | None = None

    # ── Timing ───────────────────────────────────────────────────────
    pipeline_start: float = 0.0
    metrics: PipelineMetrics | None = None

    # ── Page data ────────────────────────────────────────────────────
    pages: list[np.ndarray] = field(default_factory=list)
    page_id_map: dict[int, UUID] = field(default_factory=dict)
    content_page_images: dict[int, np.ndarray] = field(default_factory=dict)

    # ── OCR text ─────────────────────────────────────────────────────
    all_page_text: str = ""
    all_ocr_text: str = ""
    first_content_page: Any = None
    first_content_page_text: str = ""
    full_doc_ocr_text: str = ""

    # ── Document splitting ───────────────────────────────────────────
    split_meta: dict[str, Any] | None = None
    active_page_numbers: set[int] | None = None

    # ── Payer detection ──────────────────────────────────────────────
    detected_payer: PayerNameEnum = PayerNameEnum.UNKNOWN
    payer_str: str | None = None
    payer_detection_meta: dict[str, Any] | None = None

    # ── Template matching ────────────────────────────────────────────
    match_result: Any = None
    template_verified: bool = False

    # ── Document classification ──────────────────────────────────────
    doc_class_meta: dict[str, Any] | None = None
    decision_value: str | None = None
    page_sections: dict[int, str] = field(default_factory=dict)
    page_section_scores: dict[int, dict[str, float]] = field(default_factory=dict)
    field_allowed_pages: dict[str, set[int]] = field(default_factory=dict)

    # ── Extraction ───────────────────────────────────────────────────
    candidates_by_field: dict[str, list[ExtractionCandidate]] = field(default_factory=dict)
    raw_candidates_by_field: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    extraction_results: list = field(default_factory=list)
    extracted_fields: dict[str, dict[str, Any]] = field(default_factory=dict)
    adjudication_meta: dict[str, Any] | None = None
    template_drift_meta: dict[str, Any] | None = None

    # ── LayoutLM / VLM metadata ──────────────────────────────────────
    vlm_extraction_meta: dict[str, Any] | None = None
    layoutlm_extraction_meta: dict[str, Any] | None = None
    raw_ocr_tokens: list[list[dict]] = field(default_factory=list)

    # ── Scoring and review ───────────────────────────────────────────
    overall_conf: float = 0.0
    needs_review: bool = False
    scoring_result: Any = None
    cross_result: Any = None
    hitl_flags: list[dict] = field(default_factory=list)

    # ── OCR quality ──────────────────────────────────────────────────
    ocr_quality: dict[str, float] | None = None

    def init_repos(self) -> None:
        """Initialise all repository instances."""
        self.job_repo = FaxJobRepository(self.db)
        self.page_repo = FaxPageRepository(self.db)
        self.token_repo = OcrTokenRepository(self.db)
        self.field_repo = ExtractedFieldRepository(self.db)
        self.extraction_repo = ExtractionRepository(self.db)
        self.review_repo = ReviewRepository(self.db)

    def init_components(self) -> None:
        """Initialise processing components."""
        self.storage = S3StorageAdapter()
        self.preprocessor = ImagePreprocessor()
        self.ocr_client = PaddleOcrClient()
        self.template_matcher = TemplateMatcher()
        self.field_extractor = TemplateExtractor()

    def update_payer_str(self) -> None:
        """Sync ``payer_str`` from ``detected_payer``."""
        self.payer_str = (
            self.detected_payer.value
            if self.detected_payer != PayerNameEnum.UNKNOWN
            else None
        )
