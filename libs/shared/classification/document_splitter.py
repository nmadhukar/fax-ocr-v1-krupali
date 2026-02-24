"""
Composite document boundary detection and splitting.

Detects when a multi-page fax contains multiple separate documents
(e.g., authorization letters from different payers stacked together)
and identifies split points.

Detection strategies:
1. Cover page boundaries — A cover/transmittal page signals a new document
2. Payer change boundaries — When payer branding changes between pages
3. Template header boundaries — When a recognized template header reappears
4. Text density gaps — Near-blank separator pages between documents

Each "segment" represents a logically independent document that should
be processed with its own payer detection, template matching, and extraction.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

from libs.shared.config.payer_signatures import PAYER_SIGNATURES

logger = logging.getLogger(__name__)


# Keywords that indicate a cover/separator page
COVER_KEYWORDS = (
    "cover sheet",
    "fax cover",
    "transmittal",
    "facsimile transmittal",
    "confidential notice",
    "this fax contains",
    "pages including cover",
    "attention:",
)


@dataclass
class DocumentSegment:
    """A logically independent document within a composite fax."""

    segment_index: int
    page_numbers: list[int]
    detected_payer: str | None = None
    payer_confidence: float = 0.0
    has_cover_page: bool = False
    split_reason: str = ""

    @property
    def content_pages(self) -> list[int]:
        """Non-cover pages in this segment."""
        if self.has_cover_page and len(self.page_numbers) > 1:
            return self.page_numbers[1:]
        return self.page_numbers

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_index": self.segment_index,
            "page_numbers": self.page_numbers,
            "content_pages": self.content_pages,
            "detected_payer": self.detected_payer,
            "payer_confidence": round(self.payer_confidence, 4),
            "has_cover_page": self.has_cover_page,
            "split_reason": self.split_reason,
        }


@dataclass
class SplitResult:
    """Result of composite document splitting."""

    is_composite: bool
    segments: list[DocumentSegment]
    total_pages: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_composite": self.is_composite,
            "segment_count": len(self.segments),
            "total_pages": self.total_pages,
            "segments": [s.to_dict() for s in self.segments],
        }


class DocumentSplitter:
    """
    Detects document boundaries within composite faxes.

    Uses page-level payer detection + cover page detection to identify
    where one document ends and another begins.

    Args:
        min_segment_pages: Minimum pages for a valid segment (avoids
            splitting single orphan pages into their own segment).
        payer_change_threshold: Minimum payer confidence to consider
            a payer change as a boundary.
    """

    def __init__(
        self,
        min_segment_pages: int = 1,
        payer_change_threshold: float = 0.50,
    ):
        self.min_segment_pages = min_segment_pages
        self.payer_change_threshold = payer_change_threshold

    def detect_segments(
        self,
        page_texts: dict[int, str],
        cover_pages: set[int] | None = None,
    ) -> SplitResult:
        """
        Detect document segments in a multi-page fax.

        Args:
            page_texts: Dict of {page_number: ocr_text} for each page.
            cover_pages: Set of page numbers already identified as cover pages.

        Returns:
            SplitResult with detected segments.
        """
        if not page_texts:
            return SplitResult(is_composite=False, segments=[], total_pages=0)

        sorted_pages = sorted(page_texts.keys())
        total_pages = len(sorted_pages)

        if cover_pages is None:
            cover_pages = set()

        # Step 1: Detect per-page payer + cover status
        page_info = self._analyze_pages(page_texts, cover_pages)

        # Step 2: Find boundary points
        boundaries = self._find_boundaries(page_info, sorted_pages)

        # Step 3: Build segments from boundaries
        segments = self._build_segments(boundaries, page_info, sorted_pages)

        # If only one segment, it's not composite
        is_composite = len(segments) > 1

        if is_composite:
            logger.info(
                "Composite document detected: %d segments across %d pages",
                len(segments),
                total_pages,
            )
            for seg in segments:
                logger.info(
                    "  Segment %d: pages %s, payer=%s (%.2f), reason=%s",
                    seg.segment_index,
                    seg.page_numbers,
                    seg.detected_payer,
                    seg.payer_confidence,
                    seg.split_reason,
                )

        return SplitResult(
            is_composite=is_composite,
            segments=segments,
            total_pages=total_pages,
        )

    def _analyze_pages(
        self,
        page_texts: dict[int, str],
        cover_pages: set[int],
    ) -> dict[int, dict[str, Any]]:
        """Analyze each page for payer branding and cover page status."""
        page_info: dict[int, dict[str, Any]] = {}

        for page_num, text in page_texts.items():
            text_lower = text.lower()

            # Detect payer from this page's text alone
            payer, payer_conf = self._detect_page_payer(text_lower)

            # Detect cover page from keywords
            is_cover = page_num in cover_pages or self._is_cover_page(text_lower)

            # Detect near-blank page (very little text)
            word_count = len(text.split())
            is_sparse = word_count < 20

            page_info[page_num] = {
                "payer": payer,
                "payer_confidence": payer_conf,
                "is_cover": is_cover,
                "is_sparse": is_sparse,
                "word_count": word_count,
            }

        return page_info

    def _detect_page_payer(self, text_lower: str) -> tuple[str | None, float]:
        """Detect payer from a single page's text (lightweight keyword scan)."""
        best_payer = None
        best_score = 0.0

        for payer_key, sig in PAYER_SIGNATURES.items():
            # Check negative keywords
            if any(nk.lower() in text_lower for nk in sig.negative_keywords):
                continue

            score = 0.0

            # Primary keywords
            for kw in sig.primary_keywords:
                if kw.lower() in text_lower:
                    score = 0.65
                    break

            # Secondary keywords (additive, capped)
            sec_hits = sum(1 for kw in sig.secondary_keywords if kw.lower() in text_lower)
            score += min(sec_hits * 0.10, 0.30)

            if score > best_score:
                best_score = score
                best_payer = payer_key

        if best_score < 0.30:
            return None, 0.0

        return best_payer, best_score

    def _is_cover_page(self, text_lower: str) -> bool:
        """Check if page text matches cover page patterns."""
        matches = sum(1 for kw in COVER_KEYWORDS if kw in text_lower)
        return matches >= 2

    def _find_boundaries(
        self,
        page_info: dict[int, dict[str, Any]],
        sorted_pages: list[int],
    ) -> list[tuple[int, str]]:
        """
        Find page numbers where document boundaries occur.

        Returns list of (page_number, reason) tuples where a new
        document starts.
        """
        boundaries: list[tuple[int, str]] = []

        # First page is always a boundary (start of first document)
        boundaries.append((sorted_pages[0], "start"))

        prev_payer = page_info[sorted_pages[0]].get("payer")

        for i in range(1, len(sorted_pages)):
            page_num = sorted_pages[i]
            info = page_info[page_num]
            prev_info = page_info[sorted_pages[i - 1]]

            # Boundary type 1: Cover page = new document starts
            if info["is_cover"]:
                boundaries.append((page_num, "cover_page"))
                prev_payer = None  # Reset payer tracking
                continue

            # Boundary type 2: Payer change on content page
            current_payer = info.get("payer")
            if (
                current_payer
                and prev_payer
                and current_payer != prev_payer
                and info["payer_confidence"] >= self.payer_change_threshold
            ):
                boundaries.append((page_num, f"payer_change:{prev_payer}->{current_payer}"))
                prev_payer = current_payer
                continue

            # Boundary type 3: Sparse separator followed by content
            # (a near-blank page between content pages)
            if (
                prev_info["is_sparse"]
                and not info["is_sparse"]
                and not prev_info["is_cover"]
                and info.get("payer")
                and info["payer_confidence"] >= self.payer_change_threshold
            ):
                # Check if the payer on this page differs from what came before the sparse page
                if i >= 2:
                    pre_sparse_payer = page_info[sorted_pages[i - 2]].get("payer")
                    if pre_sparse_payer and info["payer"] != pre_sparse_payer:
                        boundaries.append((page_num, "sparse_separator"))

            # Update tracking payer
            if current_payer and info["payer_confidence"] >= self.payer_change_threshold:
                prev_payer = current_payer

        return boundaries

    def _build_segments(
        self,
        boundaries: list[tuple[int, str]],
        page_info: dict[int, dict[str, Any]],
        sorted_pages: list[int],
    ) -> list[DocumentSegment]:
        """Build document segments from boundary points."""
        if not boundaries:
            return [
                DocumentSegment(
                    segment_index=0,
                    page_numbers=sorted_pages,
                )
            ]

        segments: list[DocumentSegment] = []
        boundary_pages = [b[0] for b in boundaries]
        boundary_reasons = [b[1] for b in boundaries]

        for idx in range(len(boundary_pages)):
            start_page = boundary_pages[idx]
            end_page = (
                boundary_pages[idx + 1]
                if idx + 1 < len(boundary_pages)
                else sorted_pages[-1] + 1
            )

            # Collect pages in this segment
            segment_pages = [p for p in sorted_pages if start_page <= p < end_page]
            if not segment_pages:
                continue

            # Determine segment's dominant payer
            payer_votes: dict[str, float] = {}
            for p in segment_pages:
                info = page_info[p]
                payer = info.get("payer")
                if payer and not info["is_cover"]:
                    payer_votes[payer] = payer_votes.get(payer, 0) + info["payer_confidence"]

            detected_payer = None
            payer_confidence = 0.0
            if payer_votes:
                detected_payer = max(payer_votes, key=lambda k: payer_votes[k])
                payer_confidence = payer_votes[detected_payer] / len(segment_pages)

            # Check if first page is cover
            has_cover = page_info[segment_pages[0]].get("is_cover", False)

            segments.append(DocumentSegment(
                segment_index=idx,
                page_numbers=segment_pages,
                detected_payer=detected_payer,
                payer_confidence=payer_confidence,
                has_cover_page=has_cover,
                split_reason=boundary_reasons[idx],
            ))

        # Merge tiny segments (less than min_segment_pages) into previous
        if self.min_segment_pages > 1:
            segments = self._merge_tiny_segments(segments)

        return segments

    def _merge_tiny_segments(
        self,
        segments: list[DocumentSegment],
    ) -> list[DocumentSegment]:
        """Merge segments with too few pages into adjacent segments."""
        if len(segments) <= 1:
            return segments

        merged: list[DocumentSegment] = [segments[0]]

        for seg in segments[1:]:
            if len(seg.page_numbers) < self.min_segment_pages:
                # Merge into previous segment
                merged[-1].page_numbers.extend(seg.page_numbers)
            else:
                merged.append(seg)

        # Re-index
        for i, seg in enumerate(merged):
            seg.segment_index = i

        return merged
