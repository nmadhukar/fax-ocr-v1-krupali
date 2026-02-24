"""
Extraction repositories.
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.db.models.fax_extraction import FaxExtractedField, FaxExtraction
from libs.shared.db.repositories.base import BaseRepository


class ExtractedFieldRepository(BaseRepository[FaxExtractedField]):
    """Repository for FaxExtractedField model."""

    def __init__(self, db: Session):
        super().__init__(db, FaxExtractedField)

    def get_by_job(self, fax_job_id: UUID) -> list[FaxExtractedField]:
        """Get all extracted fields for a job."""
        stmt = (
            select(FaxExtractedField)
            .where(FaxExtractedField.fax_job_id == fax_job_id)
            .order_by(FaxExtractedField.field_key)
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_by_job_and_method(
        self,
        fax_job_id: UUID,
        method: ExtractionMethodEnum,
    ) -> list[FaxExtractedField]:
        """Get fields extracted by a specific method."""
        stmt = (
            select(FaxExtractedField)
            .where(
                FaxExtractedField.fax_job_id == fax_job_id,
                FaxExtractedField.method == method,
            )
        )
        return list(self.db.execute(stmt).scalars().all())

    def get_field(
        self,
        fax_job_id: UUID,
        field_key: str,
        method: ExtractionMethodEnum | None = None,
    ) -> FaxExtractedField | None:
        """Get a specific field."""
        conditions = [
            FaxExtractedField.fax_job_id == fax_job_id,
            FaxExtractedField.field_key == field_key,
        ]

        if method:
            conditions.append(FaxExtractedField.method == method)

        stmt = select(FaxExtractedField).where(*conditions)
        return self.db.execute(stmt).scalar_one_or_none()

    def upsert_field(
        self,
        fax_job_id: UUID,
        field_key: str,
        method: ExtractionMethodEnum,
        field_value: str | None,
        field_conf: float | None = None,
        evidence_bbox: dict | None = None,
        evidence_text: str | None = None,
    ) -> FaxExtractedField:
        """
        Insert or update an extracted field.

        Args:
            fax_job_id: Parent job UUID.
            field_key: Field key.
            method: Extraction method.
            field_value: Extracted value.
            field_conf: Confidence score.
            evidence_bbox: Bounding box evidence.
            evidence_text: Text evidence.

        Returns:
            Created or updated FaxExtractedField.
        """
        existing = self.get_field(fax_job_id, field_key, method)

        if existing:
            existing.field_value = field_value
            existing.field_conf = field_conf
            existing.evidence_bbox = evidence_bbox
            existing.evidence_text = evidence_text
            self.db.flush()
            return existing

        field = FaxExtractedField(
            fax_job_id=fax_job_id,
            field_key=field_key,
            method=method,
            field_value=field_value,
            field_conf=field_conf,
            evidence_bbox=evidence_bbox,
            evidence_text=evidence_text,
        )
        return self.create(field)

    def update_validation(
        self,
        fax_job_id: UUID,
        field_key: str,
        validation_passed: bool,
        validation_errors: list[str] | None = None,
    ) -> None:
        """
        Update validation results on extracted field records.

        Updates ALL method variants of a field (template, vlm) for the job.

        Args:
            fax_job_id: Parent job UUID.
            field_key: Field key to update.
            validation_passed: Whether the field passed validation.
            validation_errors: List of validation error messages.
        """
        stmt = (
            select(FaxExtractedField)
            .where(
                FaxExtractedField.fax_job_id == fax_job_id,
                FaxExtractedField.field_key == field_key,
            )
        )
        records = list(self.db.execute(stmt).scalars().all())
        for record in records:
            record.validation_passed = validation_passed
            record.validation_errors = validation_errors or []
        if records:
            self.db.flush()

    def delete_by_job(self, fax_job_id: UUID) -> int:
        """Delete all fields for a job."""
        fields = self.get_by_job(fax_job_id)
        count = len(fields)
        for field in fields:
            self.db.delete(field)
        self.db.flush()
        return count


class ExtractionRepository(BaseRepository[FaxExtraction]):
    """Repository for FaxExtraction model."""

    def __init__(self, db: Session):
        super().__init__(db, FaxExtraction)

    def get_by_job(self, fax_job_id: UUID) -> FaxExtraction | None:
        """Get extraction result for a job."""
        stmt = select(FaxExtraction).where(FaxExtraction.fax_job_id == fax_job_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def update_flagged_fields(
        self,
        fax_job_id: UUID,
        flagged_fields: list[dict],
    ) -> None:
        """
        Store HITL field flags on the extraction record.

        Args:
            fax_job_id: Parent job UUID.
            flagged_fields: List of flag dicts from compute_field_flags().
        """
        extraction = self.get_by_job(fax_job_id)
        if extraction is None:
            logger.warning(
                "update_flagged_fields: no extraction record for job %s — flags dropped",
                fax_job_id,
            )
            return
        extraction.flagged_fields = flagged_fields
        self.db.flush()

    def apply_corrections(
        self,
        fax_job_id: UUID,
        corrections: dict[str, str],
    ) -> "FaxExtraction | None":
        """
        Apply human reviewer corrections to extraction_json.

        Each corrected field is updated to conf=1.0 / method=HUMAN_REVIEW.
        Flags for corrected fields are cleared (they are now accurate).

        Args:
            fax_job_id: Parent job UUID.
            corrections: {field_key: corrected_value} from the reviewer.

        Returns:
            Updated FaxExtraction, or None if no extraction record exists.
        """
        from libs.shared.extraction.hitl import apply_human_corrections

        extraction = self.get_by_job(fax_job_id)
        if extraction is None:
            return None

        extraction.extraction_json = apply_human_corrections(
            extraction.extraction_json, corrections
        )

        # Remove flags for fields that the human has now corrected
        corrected_keys = set(corrections.keys())
        extraction.flagged_fields = [
            f for f in (extraction.flagged_fields or [])
            if f.get("field_key") not in corrected_keys
        ]

        self.db.flush()
        return extraction

    def upsert(
        self,
        fax_job_id: UUID,
        extraction_json: dict,
        model_versions: dict | None = None,
        pipeline_version: str | None = None,
    ) -> FaxExtraction:
        """
        Insert or update extraction result.

        Args:
            fax_job_id: Parent job UUID.
            extraction_json: Final extracted fields.
            model_versions: Model versions used.
            pipeline_version: Pipeline version.

        Returns:
            Created or updated FaxExtraction.
        """
        existing = self.get_by_job(fax_job_id)

        if existing:
            existing.extraction_json = extraction_json
            existing.model_versions = model_versions
            existing.pipeline_version = pipeline_version
            # Reprocessing starts from fresh flags; stage 16b recomputes them.
            existing.flagged_fields = []
            self.db.flush()
            return existing

        extraction = FaxExtraction(
            fax_job_id=fax_job_id,
            extraction_json=extraction_json,
            model_versions=model_versions,
            pipeline_version=pipeline_version,
        )
        return self.create(extraction)
