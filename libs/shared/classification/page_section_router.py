"""
Page section routing for extraction safety.

Classifies each page into coarse sections and builds per-field page allowlists.
This is additive to existing extraction logic: if no confident section exists,
the router falls back to allowing all content pages.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


SECTION_AUTH_SUMMARY = "authorization_summary"
SECTION_APPEAL = "appeal_text"
SECTION_INSTRUCTIONS = "instructions"
SECTION_CLINICAL = "clinical_attachment"
SECTION_COVER = "cover"
SECTION_OTHER = "other"


_SECTION_PATTERNS: dict[str, dict[str, list[str]]] = {
    SECTION_AUTH_SUMMARY: {
        "strong": [
            r"(?i)\bauthorization\s+status\b",
            r"(?i)\bdate\s*span\b",
            r"(?i)\bapproved\b",
            r"(?i)\bdenied\b",
            r"(?i)\bauthorization\s+number\b",
            r"(?i)\bmember\s+id\b",
        ],
        "weak": [
            r"(?i)\beffective\s+date\b",
            r"(?i)\bexpiration\s+date\b",
            r"(?i)\brequesting\s+provider\b",
            r"(?i)\bservice\s+code\b",
            r"(?i)\bunits?\b",
        ],
        "negative": [
            r"(?i)\bappeal\s+rights?\b",
            r"(?i)\bhow\s+to\s+appeal\b",
            r"(?i)\binstructions?\b",
        ],
    },
    SECTION_APPEAL: {
        "strong": [
            r"(?i)\bappeal\s+rights?\b",
            r"(?i)\bservice\s+authorization\s+appeal\b",
            r"(?i)\bclinical\s+peer\b",
            r"(?i)\bpeer[\s\-]+to[\s\-]+peer\b",
            r"(?i)\bif\s+you\s+disagree\b",
            r"(?i)\bcan\s+be\s+submitted\s+within\b",
        ],
        "weak": [
            r"(?i)\bappeal\b",
            r"(?i)\breconsideration\b",
            r"(?i)\badverse\s+determination\b",
            r"(?i)\bgrievance\b",
        ],
        "negative": [],
    },
    SECTION_INSTRUCTIONS: {
        "strong": [
            r"(?i)\binstructions?\b",
            r"(?i)\bhow\s+to\b",
            r"(?i)\bplease\s+submit\b",
            r"(?i)\bfax\s+to\b",
            r"(?i)\bmail\s+to\b",
        ],
        "weak": [
            r"(?i)\bcontact\b",
            r"(?i)\bprovider\s+portal\b",
            r"(?i)\bphone\b",
            r"(?i)\bfax\b",
        ],
        "negative": [],
    },
    SECTION_CLINICAL: {
        "strong": [
            r"(?i)\bclinical\s+notes?\b",
            r"(?i)\bprogress\s+notes?\b",
            r"(?i)\bhistory\s+and\s+physical\b",
            r"(?i)\blab(?:oratory)?\s+results?\b",
            r"(?i)\bdischarge\s+summary\b",
        ],
        "weak": [
            r"(?i)\bdiagnosis\b",
            r"(?i)\bassessment\b",
            r"(?i)\bimpression\b",
            r"(?i)\bplan\b",
        ],
        "negative": [],
    },
}


KEY_FIELDS_SECTION_POLICY: dict[str, set[str]] = {
    "decision": {SECTION_AUTH_SUMMARY},
    "prior_auth_number": {SECTION_AUTH_SUMMARY},
    "member_id": {SECTION_AUTH_SUMMARY},
    "patient_name": {SECTION_AUTH_SUMMARY},
    "patient_dob": {SECTION_AUTH_SUMMARY},
    "auth_effective_date": {SECTION_AUTH_SUMMARY},
    "auth_expiration_date": {SECTION_AUTH_SUMMARY},
    "next_review_date": {SECTION_AUTH_SUMMARY},
    "provider_name": {SECTION_AUTH_SUMMARY},
    "provider_npi": {SECTION_AUTH_SUMMARY, SECTION_INSTRUCTIONS},
    "service_code": {SECTION_AUTH_SUMMARY, SECTION_CLINICAL},
    "diagnosis_code": {SECTION_AUTH_SUMMARY, SECTION_CLINICAL},
    "diagnosis_codes": {SECTION_AUTH_SUMMARY, SECTION_CLINICAL},
    "units_requested": {SECTION_AUTH_SUMMARY},
}

# M2-FIX: Pre-compile all regex patterns at module load time.
_COMPILED_SECTION_PATTERNS: dict[str, dict[str, list[re.Pattern]]] = {
    section: {
        group: [re.compile(pat) for pat in patterns]
        for group, patterns in pattern_sets.items()
    }
    for section, pattern_sets in _SECTION_PATTERNS.items()
}


@dataclass
class PageSectionResult:
    """Section classification result for one page."""

    page_number: int
    section: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "page_number": self.page_number,
            "section": self.section,
            "confidence": round(self.confidence, 4),
            "evidence": self.evidence[:5],
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
        }


class PageSectionRouter:
    """Route pages into semantic sections for extraction control."""

    def classify_page(
        self,
        text: str,
        *,
        is_cover_page: bool = False,
        page_number: int = 0,
    ) -> PageSectionResult:
        if is_cover_page:
            return PageSectionResult(
                page_number=page_number,
                section=SECTION_COVER,
                confidence=1.0,
                evidence=["cover_page_flag"],
                scores={SECTION_COVER: 1.0},
            )

        if not text.strip():
            return PageSectionResult(
                page_number=page_number,
                section=SECTION_OTHER,
                confidence=0.0,
                evidence=["empty_text"],
                scores={SECTION_OTHER: 0.0},
            )

        scores: dict[str, float] = {}
        evidence_map: dict[str, list[str]] = {}

        for section, pattern_sets in _COMPILED_SECTION_PATTERNS.items():
            strong_hits = []
            for pat in pattern_sets["strong"]:
                m = pat.search(text)
                if m:
                    strong_hits.append(m.group(0)[:120])

            weak_hits = sum(
                1 for pat in pattern_sets["weak"] if pat.search(text)
            )
            neg_hits = sum(
                1 for pat in pattern_sets["negative"] if pat.search(text)
            )

            score = 0.0
            if strong_hits:
                score = 0.55 + min(0.30, len(strong_hits) * 0.10)
            score += min(0.15, weak_hits * 0.04)
            score = max(0.0, score - min(0.25, neg_hits * 0.10))

            scores[section] = min(score, 1.0)
            evidence_map[section] = strong_hits

        best_section = max(scores, key=lambda k: scores[k]) if scores else SECTION_OTHER
        best_score = scores.get(best_section, 0.0)
        if best_score < 0.20:
            best_section = SECTION_OTHER

        return PageSectionResult(
            page_number=page_number,
            section=best_section,
            confidence=best_score,
            evidence=evidence_map.get(best_section, []),
            scores=scores,
        )

    def route_pages(
        self,
        page_texts: dict[int, str],
        *,
        cover_pages: set[int] | None = None,
    ) -> dict[int, PageSectionResult]:
        cover_pages = cover_pages or set()
        routed: dict[int, PageSectionResult] = {}

        for page_num, text in page_texts.items():
            result = self.classify_page(
                text, is_cover_page=page_num in cover_pages, page_number=page_num
            )
            routed[page_num] = result

        return routed

    def build_field_allowlist(
        self,
        routed_pages: dict[int, PageSectionResult],
        *,
        candidate_fields: set[str] | None = None,
    ) -> dict[str, set[int]]:
        """
        Build per-field page allowlists from routed page sections.

        Fallback behavior:
        - If a field has no routed pages in its preferred sections, allow all
          non-cover pages so existing extraction behavior still works.
        """
        non_cover_pages = {
            pnum for pnum, r in routed_pages.items() if r.section != SECTION_COVER
        }
        if not non_cover_pages:
            non_cover_pages = set(routed_pages.keys())

        if candidate_fields is None:
            fields = set(KEY_FIELDS_SECTION_POLICY.keys())
        else:
            fields = set(candidate_fields)

        allowlist: dict[str, set[int]] = {}
        for field_key in fields:
            policy_sections = KEY_FIELDS_SECTION_POLICY.get(field_key)
            if not policy_sections:
                allowlist[field_key] = set(non_cover_pages)
                continue

            pages = {
                pnum
                for pnum, result in routed_pages.items()
                if result.section in policy_sections
            }
            allowlist[field_key] = pages or set(non_cover_pages)

        return allowlist
