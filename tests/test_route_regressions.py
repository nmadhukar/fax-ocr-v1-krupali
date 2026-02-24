"""
Regression tests for API route behavior.

These tests require FastAPI runtime dependencies.
"""

import asyncio
import io
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

pytest.importorskip("fastapi")
from fastapi import HTTPException

from libs.shared.db.models.enums import FaxJobStatusEnum
from services.fax_ingress_api.api.v1.routes import faxes as ingress_faxes
from services.fax_review_api.api.v1.routes import review as review_routes
from services.fax_review_api.api.v1.routes import templates as review_templates


def test_list_templates_invalid_payer_returns_400():
    admin_user = SimpleNamespace(is_admin=True)

    with pytest.raises(HTTPException) as exc:
        review_templates.list_templates(
            payer_name="not_a_real_payer",
            active_only=False,
            user=admin_user,
            db=SimpleNamespace(),
        )

    assert exc.value.status_code == 400
    assert "Invalid payer_name" in str(exc.value.detail)


def test_duplicate_failed_job_is_requeued(monkeypatch):
    settings = SimpleNamespace(
        environment="development",
        api=SimpleNamespace(max_upload_size_mb=20),
    )
    monkeypatch.setattr(ingress_faxes, "get_settings", lambda: settings)

    class _RateLimiter:
        def check(self, _request):
            return None

    monkeypatch.setattr(ingress_faxes, "get_upload_rate_limiter", lambda: _RateLimiter())
    monkeypatch.setattr(ingress_faxes, "validate_file_magic", lambda _content, _ctype: None)
    monkeypatch.setattr(ingress_faxes, "sanitize_filename", lambda name: name)

    existing = SimpleNamespace(
        fax_job_id=uuid4(),
        status=FaxJobStatusEnum.FAILED,
        created_at=datetime.now(timezone.utc),
        tenant_id="tenant-a",
    )

    class _Repo:
        def __init__(self, _db):
            pass

        def get_by_sha256(self, _file_sha256, tenant_id=None):
            assert tenant_id == "tenant-a"
            return existing

    monkeypatch.setattr(ingress_faxes, "FaxJobRepository", _Repo)

    task_calls: list[tuple[tuple, dict]] = []

    class _TaskResult:
        id = "task-1"

    class _CeleryApp:
        def send_task(self, *args, **kwargs):
            task_calls.append((args, kwargs))
            return _TaskResult()

    monkeypatch.setitem(
        sys.modules,
        "workers.fax_processing_worker.celery_app",
        SimpleNamespace(app=_CeleryApp()),
    )

    class _UploadFile:
        filename = "sample.pdf"
        content_type = "application/pdf"
        file = io.BytesIO(b"%PDF-1.4\nfake pdf body\n")

    response = ingress_faxes.upload_fax(
        request=SimpleNamespace(),
        file=_UploadFile(),
        tenant_id="tenant-a",
        payer_hint=None,
        external_fax_id=None,
        user=SimpleNamespace(tenant_id="tenant-a", is_admin=False),
        db=SimpleNamespace(),
        storage=SimpleNamespace(),
    )

    assert response.status_code == 200
    assert len(task_calls) == 1
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["fax_job_id"] == str(existing.fax_job_id)
    assert payload["status"] == FaxJobStatusEnum.FAILED.value


def test_upload_fax_dedup_race_on_commit_returns_existing_job(monkeypatch):
    settings = SimpleNamespace(
        environment="development",
        api=SimpleNamespace(max_upload_size_mb=20),
    )
    monkeypatch.setattr(ingress_faxes, "get_settings", lambda: settings)

    class _RateLimiter:
        def check(self, _request):
            return None

    monkeypatch.setattr(ingress_faxes, "get_upload_rate_limiter", lambda: _RateLimiter())
    monkeypatch.setattr(ingress_faxes, "validate_file_magic", lambda _content, _ctype: None)
    monkeypatch.setattr(ingress_faxes, "sanitize_filename", lambda name: name)

    uploaded_keys: list[str] = []
    deleted_keys: list[str] = []

    existing = SimpleNamespace(
        fax_job_id=uuid4(),
        status=FaxJobStatusEnum.COMPLETED,
        created_at=datetime.now(timezone.utc),
        tenant_id="tenant-a",
        file_storage_key=None,
    )

    class _Repo:
        def __init__(self, _db):
            self.lookup_calls = 0

        def get_by_sha256(self, _file_sha256, tenant_id=None):
            assert tenant_id == "tenant-a"
            self.lookup_calls += 1
            if self.lookup_calls == 1:
                return None
            if uploaded_keys:
                existing.file_storage_key = uploaded_keys[0]
            return existing

        def create(self, _fax_job):
            return None

    monkeypatch.setattr(ingress_faxes, "FaxJobRepository", _Repo)

    class _Storage:
        def upload(self, key, data, content_type, metadata):
            uploaded_keys.append(key)
            return key

        def delete(self, key):
            deleted_keys.append(key)
            return True

    class _Db:
        def __init__(self):
            self.commit_calls = 0
            self.rollback_calls = 0

        def commit(self):
            self.commit_calls += 1
            if self.commit_calls == 1:
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))

        def rollback(self):
            self.rollback_calls += 1

    class _UploadFile:
        filename = "sample.pdf"
        content_type = "application/pdf"
        file = io.BytesIO(b"%PDF-1.4\nfake pdf body\n")

    db = _Db()
    response = ingress_faxes.upload_fax(
        request=SimpleNamespace(),
        file=_UploadFile(),
        tenant_id="tenant-a",
        payer_hint=None,
        external_fax_id=None,
        user=SimpleNamespace(tenant_id="tenant-a", is_admin=False),
        db=db,
        storage=_Storage(),
    )

    assert response.status_code == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["fax_job_id"] == str(existing.fax_job_id)
    assert payload["message"] == "Duplicate fax detected. Returning existing job."
    assert db.rollback_calls == 1
    assert len(uploaded_keys) == 1
    assert deleted_keys == []


def test_upload_sample_uses_unique_storage_keys(monkeypatch):
    version_id = uuid4()

    class _VersionRepo:
        def __init__(self, _db):
            pass

        def get_by_id(self, _version_id):
            return SimpleNamespace(template_version_id=version_id)

    class _SampleRepo:
        def __init__(self, _db):
            pass

        def create(self, sample):
            if getattr(sample, "sample_id", None) is None:
                sample.sample_id = uuid4()
            if getattr(sample, "created_at", None) is None:
                sample.created_at = datetime.now(timezone.utc)

    class _Matcher:
        def compute_template_features(self, _image):
            return 1234, b"kp", b"desc"

    uploaded_keys: list[str] = []

    class _Storage:
        def upload(self, key, content, content_type):
            uploaded_keys.append(key)
            return key

    class _Db:
        def commit(self):
            return None

    monkeypatch.setattr(review_templates, "TemplateVersionRepository", _VersionRepo)
    monkeypatch.setattr(review_templates, "TemplateSampleRepository", _SampleRepo)
    monkeypatch.setattr(review_templates, "TemplateMatcher", _Matcher)
    monkeypatch.setattr(
        review_templates.cv2,
        "imdecode",
        lambda _nparr, _mode: review_templates.np.zeros((8, 8, 3), dtype=review_templates.np.uint8),
    )

    class _UploadFile:
        def __init__(self, filename: str):
            self.filename = filename
            self.content_type = "image/png"

        async def read(self):
            return b"fake-image-content"

    async def _run_uploads():
        first = await review_templates.upload_sample(
            version_id=version_id,
            file=_UploadFile("sample.png"),
            user=SimpleNamespace(is_admin=True),
            db=_Db(),
            storage=_Storage(),
        )
        second = await review_templates.upload_sample(
            version_id=version_id,
            file=_UploadFile("sample.png"),
            user=SimpleNamespace(is_admin=True),
            db=_Db(),
            storage=_Storage(),
        )
        return first, second

    first, second = asyncio.run(_run_uploads())

    assert len(uploaded_keys) == 2
    assert uploaded_keys[0] != uploaded_keys[1]
    assert first.sample_storage_key != second.sample_storage_key
    assert uploaded_keys[0].startswith(f"templates/{version_id}/")
    assert uploaded_keys[1].startswith(f"templates/{version_id}/")


def _mock_submit_review_context(monkeypatch):
    monkeypatch.setattr(
        review_routes,
        "get_settings",
        lambda: SimpleNamespace(environment="development"),
    )

    job = SimpleNamespace(
        fax_job_id=uuid4(),
        tenant_id="tenant-a",
        matched_template_version_id=None,
        status=FaxJobStatusEnum.NEEDS_REVIEW,
        needs_review=True,
        payer_hint=None,
        doc_type=None,
    )
    review = SimpleNamespace(
        review_id=uuid4(),
        is_submitted=False,
        is_expired=False,
        is_claimed=True,
        claimed_by="reviewer-1",
        review_packet={"extracted_fields": {"member_id": {"value": "OLD"}}},
    )
    extraction = SimpleNamespace(extraction_json={"member_id": {"value": "OLD"}})

    class _JobRepo:
        def __init__(self, _db):
            pass

        def get_by_id(self, _fax_job_id):
            return job

    class _ReviewRepo:
        def __init__(self, _db):
            pass

        def get_by_job(self, _fax_job_id):
            return review

        def submit_review(self, *_args, **_kwargs):
            raise AssertionError("submit_review should not be called for invalid corrections")

    class _ExtractionRepo:
        def __init__(self, _db):
            pass

        def get_by_job(self, _fax_job_id):
            return extraction

    monkeypatch.setattr(review_routes, "FaxJobRepository", _JobRepo)
    monkeypatch.setattr(review_routes, "ReviewRepository", _ReviewRepo)
    monkeypatch.setattr(review_routes, "ExtractionRepository", _ExtractionRepo)

    return job


def test_get_review_packet_empty_selected_pages_does_not_leak_all_pages(monkeypatch):
    monkeypatch.setattr(
        review_routes,
        "get_settings",
        lambda: SimpleNamespace(environment="development"),
    )

    job = SimpleNamespace(
        fax_job_id=uuid4(),
        tenant_id="tenant-a",
    )
    review = SimpleNamespace(
        review_id=uuid4(),
        review_packet={"pages": []},
        review_reasons=[],
        created_at=datetime.now(timezone.utc),
    )
    pages = [
        SimpleNamespace(
            page_number=1,
            fax_page_id=uuid4(),
            page_storage_key="p1",
            width_px=1000,
            height_px=1200,
        ),
        SimpleNamespace(
            page_number=2,
            fax_page_id=uuid4(),
            page_storage_key="p2",
            width_px=1000,
            height_px=1200,
        ),
    ]

    class _JobRepo:
        def __init__(self, _db):
            pass

        def get_by_id(self, _fax_job_id):
            return job

    class _ReviewRepo:
        def __init__(self, _db):
            pass

        def get_by_job(self, _fax_job_id):
            return review

    class _PageRepo:
        def __init__(self, _db):
            pass

        def get_by_job(self, _fax_job_id):
            return pages

    class _ExtractionRepo:
        def __init__(self, _db):
            pass

        def get_by_job(self, _fax_job_id):
            return None

    class _Audit:
        def log_read(self, *_args, **_kwargs):
            return None

        def log_download(self, *_args, **_kwargs):
            return None

    class _Db:
        def commit(self):
            return None

    monkeypatch.setattr(review_routes, "FaxJobRepository", _JobRepo)
    monkeypatch.setattr(review_routes, "ReviewRepository", _ReviewRepo)
    monkeypatch.setattr(review_routes, "FaxPageRepository", _PageRepo)
    monkeypatch.setattr(review_routes, "ExtractionRepository", _ExtractionRepo)
    monkeypatch.setattr(review_routes, "get_audit_logger", lambda _request, _db: _Audit())

    response = review_routes.get_review_packet(
        http_request=SimpleNamespace(),
        fax_job_id=job.fax_job_id,
        user=SimpleNamespace(user_id="u1", tenant_id="tenant-a", is_admin=False),
        db=_Db(),
        storage=SimpleNamespace(get_url=lambda _key, expires_in: "http://url"),
    )

    assert response.pages == []


def test_submit_review_rejects_duplicate_corrected_field_keys(monkeypatch):
    job = _mock_submit_review_context(monkeypatch)

    request = review_routes.SubmitRequest(
        reviewer_id="reviewer-1",
        corrected_fields=[
            review_routes.FieldCorrection(field_key="member_id", corrected_value="A123"),
            review_routes.FieldCorrection(field_key="MEMBER_ID", corrected_value="B456"),
        ],
    )

    with pytest.raises(HTTPException) as exc:
        review_routes.submit_review(
            http_request=SimpleNamespace(),
            fax_job_id=job.fax_job_id,
            request=request,
            user=SimpleNamespace(user_id="reviewer-1", tenant_id="tenant-a", is_admin=False),
            db=SimpleNamespace(),
        )

    assert exc.value.status_code == 400
    assert "Duplicate corrected field_key" in str(exc.value.detail)


def test_submit_review_rejects_unsupported_field_keys(monkeypatch):
    job = _mock_submit_review_context(monkeypatch)

    request = review_routes.SubmitRequest(
        reviewer_id="reviewer-1",
        corrected_fields=[
            review_routes.FieldCorrection(field_key="totally_unknown_field", corrected_value="x"),
        ],
    )

    with pytest.raises(HTTPException) as exc:
        review_routes.submit_review(
            http_request=SimpleNamespace(),
            fax_job_id=job.fax_job_id,
            request=request,
            user=SimpleNamespace(user_id="reviewer-1", tenant_id="tenant-a", is_admin=False),
            db=SimpleNamespace(),
        )

    assert exc.value.status_code == 400
    assert "Invalid or unsupported field_key" in str(exc.value.detail)
