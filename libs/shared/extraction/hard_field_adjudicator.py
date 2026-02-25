"""
Adjudication layer for hard/conflicted fields.

This module is intentionally additive. It does not replace existing
template/OCR/LayoutLM extraction; it adds one extra candidate when multiple
sources disagree or confidence is low.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from libs.shared.db.models.enums import ExtractionMethodEnum
from libs.shared.extraction.field_builder import ExtractionCandidate
from libs.shared.extraction.validators import FieldValidator


_METHOD_RELIABILITY = {
    ExtractionMethodEnum.TEMPLATE_OCR: 1.05,
    ExtractionMethodEnum.OCR_LABEL: 0.95,
    ExtractionMethodEnum.LAYOUTLM: 1.00,
    ExtractionMethodEnum.VLM: 0.95,
    ExtractionMethodEnum.LLM: 1.00,
    ExtractionMethodEnum.HYBRID: 1.00,
    ExtractionMethodEnum.DONUT: 0.85,
    ExtractionMethodEnum.HUMAN_REVIEW: 1.10,
    ExtractionMethodEnum.HUMAN: 1.10,
}


@dataclass
class AdjudicationResult:
    """Output of hard-field adjudication."""

    value: str | None
    confidence: float
    reason: str
    metadata: dict[str, Any]

    def to_candidate(self) -> ExtractionCandidate | None:
        if not self.value:
            return None
        # H4-FIX: Use HYBRID (not LLM) — adjudicator uses validation scoring,
        # not an LLM.  LLM tag corrupts method-based metrics and ranker bias.
        return ExtractionCandidate(
            value=self.value,
            method=ExtractionMethodEnum.HYBRID,
            confidence=self.confidence,
            evidence_text=self.reason,
            evidence_bbox=None,
        )


class HardFieldAdjudicator:
    """Resolve conflicting low-confidence candidates for one field."""

    def __init__(
        self,
        threshold: float = 0.72,
        conflict_gap: float = 0.12,
    ):
        self.threshold = threshold
        self.conflict_gap = conflict_gap
        self.validator = FieldValidator()

    @staticmethod
    def _normalize_value(value: str | None) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", " ", value).strip().lower()

    def should_adjudicate(
        self,
        candidates: list[ExtractionCandidate],
    ) -> bool:
        if len(candidates) < 2:
            return False

        ranked = sorted(
            (c for c in candidates if (c.value or "").strip()),
            key=lambda c: c.confidence,
            reverse=True,
        )
        if len(ranked) < 2:
            return False

        top = ranked[0].confidence
        second = ranked[1].confidence
        if top < self.threshold:
            return True

        top_value = self._normalize_value(ranked[0].value)
        second_value = self._normalize_value(ranked[1].value)
        if top_value != second_value and (top - second) <= self.conflict_gap:
            return True
        return False

    def adjudicate(
        self,
        *,
        field_key: str,
        candidates: list[ExtractionCandidate],
        page_text_window: str,
        payer_name: str | None,
        context: dict[str, Any] | None = None,
    ) -> AdjudicationResult:
        """
        Select the best value across candidates with validation-aware scoring.
        """
        context = context or {}
        scored: list[tuple[ExtractionCandidate, float, str]] = []
        norm_context = self._normalize_value(page_text_window)

        for candidate in candidates:
            value = (candidate.value or "").strip()
            if not value:
                continue

            score = float(candidate.confidence)
            score *= _METHOD_RELIABILITY.get(candidate.method, 1.0)

            value_norm = self._normalize_value(value)
            if value_norm and value_norm in norm_context:
                score += 0.06

            val_result = self.validator.validate(
                field_key=field_key,
                value=value,
                payer_name=payer_name,
                context=context,
            )
            if val_result.is_valid:
                score += 0.08
                reason = "validated"
            else:
                score -= 0.15
                reason = "validation_penalty"

            scored.append((candidate, score, reason))

        if not scored:
            return AdjudicationResult(
                value=None,
                confidence=0.0,
                reason="no_nonempty_candidates",
                metadata={"field_key": field_key},
            )

        best, best_score, reason = max(scored, key=lambda row: row[1])
        final_conf = max(0.0, min(1.0, best_score))
        return AdjudicationResult(
            value=best.value,
            confidence=final_conf,
            reason=f"hard_field_adjudication:{reason}",
            metadata={
                "field_key": field_key,
                "input_candidates": len(candidates),
                "selected_method": best.method.value,
            },
        )
