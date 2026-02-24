"""
Payer auto-detection from OCR text.

Uses keyword matching to scan OCR text for known payer name patterns.
Handles 90%+ of real-world documents because payer names appear
prominently in headers, logos, and footer text.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from libs.shared.config.payer_signatures import PAYER_SIGNATURES
from libs.shared.db.models.enums import PayerNameEnum

logger = logging.getLogger(__name__)


@dataclass
class PayerDetectionResult:
    """Result of payer detection."""

    payer: PayerNameEnum
    confidence: float
    method: str  # "keyword"
    evidence: str
    all_scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "payer": self.payer.value,
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "evidence": self.evidence,
            "all_scores": {k: round(v, 4) for k, v in self.all_scores.items()},
        }


class PayerDetector:
    """
    Detects which of the 10 supported payers a document belongs to.

    Args:
        keyword_primary_base: Base score awarded for a primary keyword hit.
        keyword_secondary_increment: Score added per secondary keyword hit.
        keyword_secondary_cap: Maximum total from secondary hits.
    """

    def __init__(
        self,
        keyword_primary_base: float = 0.65,
        keyword_secondary_increment: float = 0.10,
        keyword_secondary_cap: float = 0.30,
    ):
        self._primary_base = keyword_primary_base
        self._secondary_inc = keyword_secondary_increment
        self._secondary_cap = keyword_secondary_cap

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(
        self,
        ocr_text: str,
    ) -> PayerDetectionResult:
        """Detect payer from OCR text.

        Args:
            ocr_text: Full OCR text (typically first non-cover page).

        Returns:
            ``PayerDetectionResult`` with the best-matching payer.
        """
        if not ocr_text or not ocr_text.strip():
            return self._unknown("empty OCR text")

        return self._keyword_detect(ocr_text)

    # ------------------------------------------------------------------
    # Tier 1 – keyword matching
    # ------------------------------------------------------------------

    def _keyword_detect(self, ocr_text: str) -> PayerDetectionResult:
        text_lower = ocr_text.lower()
        scores: dict[str, float] = {}
        evidence_map: dict[str, str] = {}

        for payer_key, sig in PAYER_SIGNATURES.items():
            # Negative keywords immediately disqualify
            if any(nk.lower() in text_lower for nk in sig.negative_keywords):
                scores[payer_key] = 0.0
                evidence_map[payer_key] = ""
                continue

            score = 0.0
            best_evidence = ""

            # Primary keywords – first hit wins
            for kw in sig.primary_keywords:
                if kw.lower() in text_lower:
                    score = self._primary_base
                    best_evidence = kw
                    break

            # Secondary keywords – additive
            sec_hits = 0
            for kw in sig.secondary_keywords:
                if kw.lower() in text_lower:
                    sec_hits += 1
                    if not best_evidence:
                        best_evidence = kw

            score += min(sec_hits * self._secondary_inc, self._secondary_cap)

            # Only secondary hits without a primary → cap at 0.45
            if score > 0 and score < self._primary_base:
                score = min(score, 0.45)

            scores[payer_key] = score
            evidence_map[payer_key] = best_evidence

        if not scores or max(scores.values()) == 0:
            return self._unknown("no keyword matches")

        best_key = max(scores, key=lambda k: scores[k])
        best_score = scores[best_key]

        payer_enum = self._resolve_enum(best_key)

        return PayerDetectionResult(
            payer=payer_enum,
            confidence=min(best_score, 1.0),
            method="keyword",
            evidence=evidence_map.get(best_key, ""),
            all_scores=scores,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_enum(key: str) -> PayerNameEnum:
        try:
            return PayerNameEnum(key)
        except ValueError:
            return PayerNameEnum.UNKNOWN

    @staticmethod
    def _unknown(reason: str) -> PayerDetectionResult:
        return PayerDetectionResult(
            payer=PayerNameEnum.UNKNOWN,
            confidence=0.0,
            method="keyword",
            evidence=reason,
            all_scores={},
        )
