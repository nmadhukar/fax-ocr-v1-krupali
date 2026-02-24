"""
Document type classification for healthcare fax documents.

Classifies documents as:

  - Prior Auth Approval
  - Prior Auth Denial
  - Peer-to-Peer Review Denial
  - Fax Cover Sheet  (already detected by image preprocessing)
  - Clinical Notes
  - Other / Unknown

Uses regex pattern matching as the primary method.  Each document type
has strong indicators (high weight), weak indicators (confirmatory),
and negative indicators (disqualifying).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from libs.shared.db.models.enums import DocTypeEnum

logger = logging.getLogger(__name__)


# -------------------------------------------------------------------
# Pattern definitions
# -------------------------------------------------------------------

_DOC_TYPE_PATTERNS: dict[str, dict[str, list[str]]] = {
    "PRIOR_AUTH_APPROVAL": {
        "strong": [
            r"(?i)authorization\s+(?:has\s+been\s+)?approved",
            r"(?i)(?:prior\s+)?auth(?:orization)?\s+(?:is\s+)?approved",
            r"(?i)approved\s+auth(?:orization)?",
            r"(?i)approved\s+(?:for|through)",
            r"(?i)determination:\s*approved",
            r"(?i)decision:\s*approved",
            r"(?i)(?:your|the)\s+request\s+(?:has\s+been\s+)?approved",
            r"(?i)approval[\s.]*notification",
            r"(?i)notice\s+of\s+(?:action|authorization).*approved",
            r"(?i)auth\s+status[:\s]*approve",
            r"(?i)authorization\s+status[:\s]*approve",
            r"(?i)nurse\s+recommendation[:\s]*approve",
            r"(?i)authorization\s+notification",
            r"(?i)notice\s+of\s+days\s+approved",
            r"(?i)authorized/denied\s+days[:\s]*authorized",
            r"(?i)auto[\s.-]*approval",
            r"(?i)re\s*:\s*approval",
            r"(?i)\bapproval\s+(?:letter|notice)\b",
            r"(?i)(?:services?\s+(?:are|is)\s+)?approved\s+as\s+follows",
            r"(?i)no\s+(?:prior\s+)?(?:auth(?:orization)?|PA)\s+(?:is\s+)?(?:needed|required)",
        ],
        "weak": [
            r"(?i)effective\s+date",
            r"(?i)expiration\s+date",
            r"(?i)authorized\s+(?:units?|services?|visits?|days)",
            r"(?i)dates?\s+approved",
            r"(?i)authorized/denied\s+days",
            r"(?i)approved\s+(?:services?|procedures?)",
            r"(?i)next\s+review\s+date",
            r"(?i)admission[\s./]*(?:service\s+)?start\s+date",
            r"(?i)written\s+communication\s+of\s+approved",
        ],
        "negative": [
            # Use specific phrases to avoid matching field labels like
            # "Authorized/Denied Days" where "Denied" is just a label.
            r"(?i)(?:has|have)\s+been\s+denied",
            r"(?i)request\s+(?:was|is)\s+denied",
            r"(?i)will\s+not\s+approve",
            r"(?i)adverse\s+(?:determination|decision)\s+letter",
            r"(?i)notice\s+of\s+adverse\s+decision",
            r"(?i)not\s+(?:medically\s+)?necessary",
            r"(?i)denial\s+(?:was\s+)?upheld",
        ],
    },
    "PRIOR_AUTH_DENIAL": {
        "strong": [
            r"(?i)authorization\s+(?:has\s+been\s+)?denied",
            r"(?i)(?:prior\s+)?auth(?:orization)?\s+(?:is\s+)?denied",
            r"(?i)(?:your|the)\s+request\s+(?:has\s+been\s+)?denied",
            r"(?i)denial\s+(?:notification|notice|letter)",
            r"(?i)adverse\s+(?:determination|decision|benefit)",
            r"(?i)not\s+(?:medically\s+)?necessary",
            r"(?i)does\s+not\s+meet\s+(?:medical|clinical)\s+(?:necessity|criteria)",
            r"(?i)determination:\s*denied",
            r"(?i)notice\s+of\s+adverse\s+decision",
            r"(?i)the\s+request\s+(?:for\s+.*)?has\s+been\s+denied",
            r"(?i)auth\s+status:\s*deni",
            r"(?i)authorization\s+status:\s*deni",
            r"(?i)adverse\s+action\s+notice",
            r"(?i)we\s+will\s+not\s+approve\s+this",
        ],
        "weak": [
            r"(?i)appeal\s+rights?",
            r"(?i)(?:you\s+)?may\s+appeal",
            r"(?i)denial\s+reason",
            r"(?i)reason\s+for\s+(?:adverse|denial)",
            r"(?i)external\s+medical\s+review",
            r"(?i)denied\s+units",
            r"(?i)reason\s+not\s+covered",
        ],
        "negative": [
            # Only block PRIOR_AUTH_DENIAL if a P2P review was actually
            # completed (not just mentioned as an appeal option).
            # Use [\s.]+ to handle OCR errors (periods instead of spaces).
            r"(?i)peer[\s.\-]+to[\s.\-]+peer[\s.]+review[\s.]*was[\s.]+completed",
            r"(?i)(?:denial|decision)[\s.]*was[\s.]+upheld",
        ],
    },
    "PEER_TO_PEER_DENIAL": {
        "strong": [
            # Patterns for COMPLETED P2P reviews, not just P2P mentioned
            # as an option in appeal rights sections.
            # Use [\s.]* between words to handle OCR missing spaces.
            r"(?i)peer[\s.\-]+to[\s.\-]+peer[\s.]+review[\s.]*was[\s.]+completed",
            r"(?i)a[\s.]+peer[\s.]+to[\s.]+peer[\s.]+review[\s.]*was[\s.]+completed",
            r"(?i)p2p[\s.]+(?:review[\s.]+)?was[\s.]+completed",
            r"(?i)(?:denial|decision)[\s.]*was[\s.]+upheld",
            r"(?i)peer[\s.\-]+to[\s.\-]+peer.*denial[\s.]*was[\s.]+upheld",
            r"(?i)physician[\s.\-]+to[\s.\-]+physician[\s.]+review[\s.]*was[\s.]+completed",
        ],
        "weak": [
            r"(?i)clinical\s+(?:peer\s+)?review",
            r"(?i)medical\s+director",
            r"(?i)upheld",
        ],
        "negative": [],
    },
    "CLINICAL_NOTES": {
        "strong": [
            r"(?i)(?:clinical|progress|physician|nursing)\s+notes?",
            r"(?i)(?:history\s+(?:and|&)\s+physical|H\s*&\s*P)\b",
            r"(?i)(?:discharge|admission)\s+summary",
            r"(?i)consultation\s+(?:report|note)",
            r"(?i)(?:operative|procedure)\s+(?:report|note)",
            r"(?i)(?:radiology|pathology|lab(?:oratory)?)\s+report",
            r"(?i)(?:assessment\s+(?:and|&)\s+plan|A\s*&\s*P)\b",
            r"(?i)(?:chief\s+complaint|subjective|objective|impression)",
        ],
        "weak": [
            r"(?i)\bvital\s+signs\b",
            r"(?i)\ballergies\b.*\b(?:NKDA|none)\b",
            r"(?i)\bmedication\s+list\b",
            r"(?i)\bfollow[\s-]+up\b",
            r"(?i)\bdiagnos(?:is|es)\b",
        ],
        "negative": [
            r"(?i)authorization\s+(?:has\s+been\s+)?(?:approved|denied)",
            r"(?i)(?:prior\s+)?auth(?:orization)?\s+(?:is\s+)?(?:approved|denied)",
            r"(?i)(?:denial|approval)\s+(?:notification|notice|letter)",
        ],
    },
}

# Map internal keys → DocTypeEnum values
_KEY_TO_ENUM: dict[str, DocTypeEnum] = {
    "PRIOR_AUTH_APPROVAL": DocTypeEnum.PRIOR_AUTH_APPROVAL,
    "PRIOR_AUTH_DENIAL": DocTypeEnum.PRIOR_AUTH_DENIAL,
    "PEER_TO_PEER_DENIAL": DocTypeEnum.PEER_TO_PEER_DENIAL,
    "FAX_COVER_SHEET": DocTypeEnum.FAX_COVER_SHEET,
    "CLINICAL_NOTES": DocTypeEnum.CLINICAL_NOTES,
    "OTHER": DocTypeEnum.OTHER,
}


# -------------------------------------------------------------------
# Result dataclass
# -------------------------------------------------------------------

@dataclass
class DocClassificationResult:
    """Result of document type classification."""

    doc_type: DocTypeEnum
    confidence: float
    method: str  # "keyword" or "heuristic"
    evidence: list[str] = field(default_factory=list)
    all_scores: dict[str, float] = field(default_factory=dict)
    decision_value: str | None = None  # "APPROVED" / "DENIED" etc.

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_type": self.doc_type.value,
            "confidence": round(self.confidence, 4),
            "method": self.method,
            "evidence": self.evidence[:5],
            "all_scores": {k: round(v, 4) for k, v in self.all_scores.items()},
            "decision_value": self.decision_value,
        }


# -------------------------------------------------------------------
# Classifier
# -------------------------------------------------------------------

class DocClassifier:
    """
    Classifies healthcare documents by type.

    Uses regex pattern matching as the primary method.
    """

    def classify(
        self,
        ocr_text: str,
        is_cover_page: bool = False,
    ) -> DocClassificationResult:
        """Classify a document.

        Args:
            ocr_text: Full OCR text from all non-cover pages.
            is_cover_page: True if preprocessing already tagged as cover.
        """
        if is_cover_page:
            return DocClassificationResult(
                doc_type=DocTypeEnum.FAX_COVER_SHEET,
                confidence=0.95,
                method="heuristic",
                evidence=["Detected as cover page by preprocessing"],
                all_scores={"FAX_COVER_SHEET": 0.95},
            )

        if not ocr_text or not ocr_text.strip():
            return DocClassificationResult(
                doc_type=DocTypeEnum.UNKNOWN,
                confidence=0.0,
                method="keyword",
                evidence=["Empty OCR text"],
            )

        return self._keyword_classify(ocr_text)

    # ------------------------------------------------------------------
    # Tier 1 – regex / keyword classification
    # ------------------------------------------------------------------

    def _keyword_classify(self, ocr_text: str) -> DocClassificationResult:
        scores: dict[str, float] = {}
        evidence_map: dict[str, list[str]] = {}

        for type_key, patterns in _DOC_TYPE_PATTERNS.items():
            score = 0.0
            evidence: list[str] = []

            # Negative indicators → penalize heavily instead of hard-blocking.
            # Hard-blocking (continue) causes misclassification when a
            # document contains both positive and negative phrases.
            neg_hits = sum(1 for p in patterns.get("negative", []) if re.search(p, ocr_text))
            neg_penalty = neg_hits * 0.40

            # Strong indicators
            strong_hits = 0
            for pattern in patterns["strong"]:
                m = re.search(pattern, ocr_text)
                if m:
                    strong_hits += 1
                    evidence.append(m.group(0)[:120])
            if strong_hits:
                score = 0.50 + min(strong_hits * 0.12, 0.35)

            # Weak indicators
            weak_hits = sum(
                1 for p in patterns.get("weak", []) if re.search(p, ocr_text)
            )
            score += min(weak_hits * 0.05, 0.15)

            # Apply negative penalty (can drive score to zero)
            score = max(score - neg_penalty, 0.0)

            scores[type_key] = score
            evidence_map[type_key] = evidence

        # Minimum score threshold: scores below 0.15 are too weak to classify
        _MIN_CLASSIFY_SCORE = 0.15
        if not scores or max(scores.values()) < _MIN_CLASSIFY_SCORE:
            return DocClassificationResult(
                doc_type=DocTypeEnum.UNKNOWN,
                confidence=max(scores.values()) if scores else 0.0,
                method="keyword",
                evidence=[],
                all_scores=scores,
            )

        best_key = max(scores, key=lambda k: scores[k])
        best_score = scores[best_key]
        doc_enum = _KEY_TO_ENUM.get(best_key, DocTypeEnum.OTHER)

        # Derive decision value from the classification
        decision = None
        if best_key == "PRIOR_AUTH_APPROVAL":
            decision = "APPROVED"
        elif best_key in ("PRIOR_AUTH_DENIAL", "PEER_TO_PEER_DENIAL"):
            decision = "DENIED"

        return DocClassificationResult(
            doc_type=doc_enum,
            confidence=min(best_score, 1.0),
            method="keyword",
            evidence=evidence_map.get(best_key, []),
            all_scores=scores,
            decision_value=decision,
        )

