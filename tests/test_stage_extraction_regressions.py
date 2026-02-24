"""
Regression tests for pipeline stage behavior.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

pytest.importorskip("minio")
from workers.fax_processing_worker.tasks.stages import extraction as extraction_stage
from workers.fax_processing_worker.tasks.stages import ingestion as ingestion_stage


def test_layoutlm_extraction_respects_active_page_numbers(monkeypatch):
    monkeypatch.setattr(
        extraction_stage,
        "_try_acquire_layoutlm_slot",
        lambda _timeout_seconds, _fax_job_id: None,
    )

    class _PageRepo:
        def get_page_with_tokens(self, _job_uuid, page_num):
            token = SimpleNamespace(
                token_text=f"P{page_num}",
                bbox_x0=0.1,
                bbox_y0=0.1,
                bbox_x1=0.2,
                bbox_y1=0.2,
                confidence=0.9,
            )
            return SimpleNamespace(is_cover_page=False, ocr_tokens=[token])

    ctx = SimpleNamespace(
        settings=SimpleNamespace(vlm=SimpleNamespace(layoutlm_enabled=True)),
        pages=["p1", "p2", "p3"],
        page_repo=_PageRepo(),
        job_uuid=uuid4(),
        active_page_numbers={2},
        raw_ocr_tokens=["stale"],
        candidates_by_field={},
        payer_str=None,
        fax_job_id="job-1",
    )

    extraction_stage.layoutlm_extraction(ctx)

    assert len(ctx.raw_ocr_tokens) == 1
    assert ctx.raw_ocr_tokens[0][0]["text"] == "P2"


def test_layoutlm_slot_reclaims_stale_owner(monkeypatch):
    monkeypatch.setattr(extraction_stage, "_LAYOUTLM_INFLIGHT_TOKEN", "stale-token")
    monkeypatch.setattr(extraction_stage, "_LAYOUTLM_INFLIGHT_STARTED_MONO", 10.0)
    monkeypatch.setattr(extraction_stage.time, "monotonic", lambda: 400.0)

    token = extraction_stage._try_acquire_layoutlm_slot(90, "job-2")

    assert token is not None
    assert extraction_stage._LAYOUTLM_INFLIGHT_TOKEN == token

    # A stale owner cannot release the newly-claimed slot.
    extraction_stage._release_layoutlm_slot("stale-token")
    assert extraction_stage._LAYOUTLM_INFLIGHT_TOKEN == token

    extraction_stage._release_layoutlm_slot(token)
    assert extraction_stage._LAYOUTLM_INFLIGHT_TOKEN is None


def test_rebuild_ocr_text_scopes_to_active_pages():
    class _PageRepo:
        def get_page_with_tokens(self, _job_uuid, page_num):
            return SimpleNamespace(
                is_cover_page=False,
                get_full_text=lambda: f"TEXT_PAGE_{page_num}",
            )

    ctx = SimpleNamespace(
        pages=["p1", "p2", "p3"],
        page_repo=_PageRepo(),
        job_uuid=uuid4(),
        active_page_numbers={2},
        all_page_text="",
        all_ocr_text="",
        first_content_page=None,
        first_content_page_text="",
        full_doc_ocr_text="",
    )

    ingestion_stage._rebuild_ocr_text(ctx)

    assert "TEXT_PAGE_1" not in ctx.all_page_text
    assert "TEXT_PAGE_3" not in ctx.all_page_text
    assert ctx.all_page_text == "TEXT_PAGE_2"
    assert ctx.all_ocr_text == "TEXT_PAGE_2"
    assert ctx.full_doc_ocr_text == "TEXT_PAGE_2"
