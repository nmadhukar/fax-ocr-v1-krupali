"""Tests for adaptive extraction enhancements."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from libs.shared.classification.page_section_router import (
    KEY_FIELDS_SECTION_POLICY,
    PageSectionRouter,
)
from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.extraction.field_builder import ExtractionCandidate, FieldBuilder

pytest.importorskip("minio")
from workers.fax_processing_worker.tasks.stages import classification as classification_stage
from workers.fax_processing_worker.tasks.stages import extraction as extraction_stage


def test_page_section_router_builds_auth_allowlist():
    router = PageSectionRouter()
    routed = router.route_pages(
        {
            1: "Authorization Status Approved Member ID Effective Date Expiration Date",
            2: "You can ask for a Service Authorization Appeal and submit within 60 days",
        }
    )

    allow = router.build_field_allowlist(
        routed_pages=routed,
        candidate_fields={"decision", "provider_name"},
    )
    assert allow["decision"] == {1}
    assert allow["provider_name"] == {1}


def test_field_builder_uses_ranker_bias(tmp_path):
    model_path = tmp_path / "ranker.json"
    model_path.write_text(
        """
        {
          "version": "test",
          "global": {"method_bias": {"LAYOUTLM": 0.12, "TEMPLATE_OCR": -0.05}},
          "fields": {}
        }
        """.strip(),
        encoding="utf-8",
    )

    fb = FieldBuilder(
        ranker_enabled=True,
        ranker_model_path=str(model_path),
    )
    candidates = [
        ExtractionCandidate(
            value="Template Value",
            method=ExtractionMethodEnum.TEMPLATE_OCR,
            confidence=0.80,
        ),
        ExtractionCandidate(
            value="Layout Value",
            method=ExtractionMethodEnum.LAYOUTLM,
            confidence=0.78,
        ),
    ]
    result = fb.build_field("provider_name", candidates)
    assert result.value == "Layout Value"


def test_hard_field_adjudication_adds_candidate():
    class _FieldRepo:
        def __init__(self):
            self.upserts = 0

        def upsert_field(self, **_kwargs):
            self.upserts += 1

    class _PageRepo:
        def get_page_with_tokens(self, _job_uuid, _page_num):
            return SimpleNamespace(
                is_cover_page=False,
                get_full_text=lambda: "Requesting Provider Name: Acme Health Group",
            )

    ctx = SimpleNamespace(
        settings=SimpleNamespace(
            adaptive=SimpleNamespace(
                hard_field_adjudication_enabled=True,
                hard_field_threshold=0.75,
                hard_field_conflict_gap=0.20,
            )
        ),
        candidates_by_field={
            "provider_name": [
                ExtractionCandidate(
                    value="Acme Health Group",
                    method=ExtractionMethodEnum.OCR_LABEL,
                    confidence=0.58,
                ),
                ExtractionCandidate(
                    value="Acrne Health Group",
                    method=ExtractionMethodEnum.LAYOUTLM,
                    confidence=0.57,
                ),
            ]
        },
        raw_candidates_by_field={"provider_name": []},
        payer_str=None,
        page_repo=_PageRepo(),
        page_id_map={1: uuid4()},
        pages=["page1"],
        active_page_numbers={1},
        job_uuid=uuid4(),
        field_repo=_FieldRepo(),
        field_allowed_pages={"provider_name": {1}},
    )

    extraction_stage.hard_field_adjudication(ctx)

    methods = [c.method for c in ctx.candidates_by_field["provider_name"]]
    assert ExtractionMethodEnum.LLM in methods
    assert ctx.field_repo.upserts == 1


def test_route_page_sections_populates_context():
    class _PageRepo:
        def get_page_with_tokens(self, _job_uuid, page_num):
            if page_num == 1:
                text = "Authorization Status: Approved Member ID Effective Date"
            else:
                text = "If you disagree you may file an appeal within 60 days"
            return SimpleNamespace(
                is_cover_page=False,
                get_full_text=lambda: text,
            )

    ctx = SimpleNamespace(
        settings=SimpleNamespace(adaptive=SimpleNamespace(section_routing_enabled=True)),
        pages=["p1", "p2"],
        active_page_numbers={1, 2},
        page_repo=_PageRepo(),
        job_uuid=uuid4(),
        page_sections={},
        page_section_scores={},
        field_allowed_pages={},
    )

    classification_stage.route_page_sections(ctx)
    assert ctx.page_sections[1] == "authorization_summary"
    assert ctx.page_sections[2] in {"appeal_text", "instructions"}
    assert "decision" in ctx.field_allowed_pages
    assert set(ctx.field_allowed_pages["decision"]).issubset({1, 2})
    assert set(KEY_FIELDS_SECTION_POLICY.keys()).intersection(ctx.field_allowed_pages.keys())
