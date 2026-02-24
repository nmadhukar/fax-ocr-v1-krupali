"""
Unit tests for review/workflow guardrails without API/runtime dependencies.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

from libs.shared.db.models.enums import FaxJobStatusEnum
from libs.shared.db.models.fax_review import FaxReview
from libs.shared.db.repositories.extraction_repo import ExtractionRepository
from libs.shared.db.repositories.fax_job_repo import FaxJobRepository
from libs.shared.db.repositories.review_repo import ReviewRepository
from libs.shared.db.repositories.template_repo import TemplateRepository


class _ExecResult:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount


class _WriteCaptureSession:
    def __init__(self, rowcount: int):
        self.rowcount = rowcount
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        return _ExecResult(self.rowcount)

    def flush(self):
        return None


class _ScalarChainResult:
    def __init__(self, values=None):
        self._values = values or []

    def unique(self):
        return self

    def scalars(self):
        return self

    def all(self):
        return self._values


class _ReadCaptureSession:
    def __init__(self, values=None):
        self.values = values or []
        self.statements = []

    def execute(self, stmt):
        self.statements.append(stmt)
        return _ScalarChainResult(self.values)

    def flush(self):
        return None


def test_claim_review_atomic_includes_submitted_guard():
    db = _WriteCaptureSession(rowcount=1)
    repo = ReviewRepository(db)

    claimed = repo.claim_review_atomic(uuid4(), "reviewer-1")

    assert claimed is True
    sql = str(db.statements[0]).lower()
    assert "submitted_at" in sql


def test_claim_review_atomic_other_user_takeover_requires_expired_claim():
    db = _WriteCaptureSession(rowcount=1)
    repo = ReviewRepository(db)

    claimed = repo.claim_review_atomic(
        uuid4(),
        reviewer_id="reviewer-2",
        expected_claimed_by="reviewer-1",
    )

    assert claimed is True
    sql = str(db.statements[0]).lower()
    assert "claim_expires_at" in sql


def test_submit_review_atomic_includes_expiry_and_claim_guards():
    db = _WriteCaptureSession(rowcount=0)
    repo = ReviewRepository(db)

    submitted = repo.submit_review(uuid4(), "reviewer-1", {"member_id": {"corrected_value": "X"}})

    assert submitted is None
    sql = str(db.statements[0]).lower()
    assert "submitted_at" in sql
    assert "claim_expires_at" in sql
    assert "claimed_by" in sql


def test_model_submit_rejects_expired_claim():
    review = FaxReview(
        fax_job_id=uuid4(),
        review_packet={"fields": []},
    )
    review.claim("reviewer-1", expiry_minutes=30)
    review.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    ok = review.submit("reviewer-1", corrections={"member_id": {"corrected_value": "X"}})

    assert ok is False


def test_update_status_completed_clears_needs_review_and_preserves_completed_at():
    db = SimpleNamespace(flush=lambda: None)
    repo = FaxJobRepository(db)

    completed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    job = SimpleNamespace(
        status=FaxJobStatusEnum.NEEDS_REVIEW,
        needs_review=True,
        processing_started_at=datetime.now(timezone.utc) - timedelta(minutes=7),
        processing_completed_at=completed_at,
    )
    repo.get_by_id = lambda _fax_job_id: job

    repo.update_status(uuid4(), FaxJobStatusEnum.COMPLETED)

    assert job.status == FaxJobStatusEnum.COMPLETED
    assert job.needs_review is False
    assert job.processing_completed_at == completed_at


def test_template_list_query_only_filters_active_when_requested():
    db = _ReadCaptureSession(values=[])
    repo = TemplateRepository(db)

    repo.list_templates(active_only=False)
    repo.list_templates(active_only=True)

    sql_all = str(db.statements[0]).lower()
    sql_active_only = str(db.statements[1]).lower()

    assert " where " not in sql_all or "fax_template.is_active = true" not in sql_all
    assert "fax_template.is_active = true" in sql_active_only


def test_extraction_upsert_clears_stale_hitl_flags():
    db = SimpleNamespace(flush=lambda: None)
    repo = ExtractionRepository(db)

    existing = SimpleNamespace(
        extraction_json={"member_id": {"value": "OLD"}},
        model_versions={"pipeline": "old"},
        pipeline_version="old",
        flagged_fields=[{"field_key": "member_id", "reason": "LOW_CONFIDENCE"}],
    )
    repo.get_by_job = lambda _fax_job_id: existing

    updated = repo.upsert(
        fax_job_id=uuid4(),
        extraction_json={"member_id": {"value": "NEW"}},
        model_versions={"pipeline": "new"},
        pipeline_version="new",
    )

    assert updated is existing
    assert existing.extraction_json["member_id"]["value"] == "NEW"
    assert existing.flagged_fields == []
