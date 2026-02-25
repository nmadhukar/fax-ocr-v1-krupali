"""
Template-based field extraction with label-anchored intelligence.

Two extraction strategies:
1. Label-anchored (primary): Find the field's label text in OCR tokens,
   then extract the value relative to the label. The template ROI serves as
   a validation hint, not the sole extraction driver. This handles layout
   variations (different margins, column widths, slight repositioning).

2. ROI-based (fallback): When label search fails, extract tokens inside
   the stored bounding box. This is the legacy approach retained as a
   safety net.

The template's role shifts from "where to extract" to "what to look for":
- field_key: canonical field name
- label_aliases: text patterns to locate the label
- expected_type: value type for filtering (date, text, number)
- ROI: validation hint + fallback extraction region
"""

import logging
import re
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from libs.shared.db.models.fax_template import FaxTemplateField
from libs.shared.db.repositories.ocr_token_repo import OcrTokenRepository
from libs.shared.db.repositories.template_repo import TemplateFieldRepository
from libs.shared.extraction.ocr_label_extractor import (
    LABEL_ALIASES,
    _extract_inline_value,
    _filter_tokens_by_field_type,
    _normalize_tokens,
    _ocr_normalize,
    OcrTokenSimple,
)
from libs.shared.utils.bbox_utils import compute_union_bbox

logger = logging.getLogger(__name__)

# Regexes for field-type validation
_DATE_PART = r"(?:\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2})"
_DATE_RE = re.compile(
    rf"^{_DATE_PART}"                               # Base date (MM/DD/YYYY or YYYY-MM-DD)
    rf"(?:[- ]*(?:to|through|-)\s*{_DATE_PART})?$"   # Optional range
)


@dataclass
class ExtractionResult:
    """Result of field extraction."""

    field_key: str
    value: str | None
    confidence: float
    evidence_bbox: dict[str, float] | None
    evidence_text: str | None
    token_ids: list[int]
    extraction_strategy: str = "roi"  # "label_anchored" or "roi"

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "field_key": self.field_key,
            "value": self.value,
            "confidence": self.confidence,
            "evidence_bbox": self.evidence_bbox,
            "evidence_text": self.evidence_text,
            "token_ids": self.token_ids,
            "extraction_strategy": self.extraction_strategy,
        }


class TemplateExtractor:
    """
    Extracts fields from OCR tokens using template definitions.

    Uses a two-pass approach:
    1. Label-anchored: find field label in OCR, extract value relative to it
    2. ROI fallback: extract tokens in the stored bounding box
    """

    def __init__(self, overlap_threshold: float = 0.5):
        self.overlap_threshold = overlap_threshold
        # Build set of known label words for detecting label boundaries
        self._label_words: set[str] = set()
        self._label_phrases: set[str] = set()
        for aliases in LABEL_ALIASES.values():
            for alias in aliases:
                phrase = alias.lower().rstrip(":").rstrip("#").strip()
                self._label_phrases.add(phrase)
                for w in phrase.split():
                    self._label_words.add(w.rstrip(":").rstrip("#"))

    def extract_all_fields(
        self,
        template_version_id: UUID,
        page_id_map: dict[int, UUID],
        db: Session,
        field_page_overrides: dict[str, set[int]] | None = None,
    ) -> list[ExtractionResult]:
        """
        Extract all fields using label-anchored strategy with ROI fallback.

        Args:
            template_version_id: Template version UUID.
            page_id_map: Mapping of page_number -> fax_page_id.
            db: Database session.

        Returns:
            List of ExtractionResult for each field.
        """
        field_repo = TemplateFieldRepository(db)
        token_repo = OcrTokenRepository(db)

        fields = field_repo.get_by_version(template_version_id)
        results: list[ExtractionResult] = []

        # Pre-load all tokens per page for label-anchored extraction
        all_tokens_by_page: dict[int, list[OcrTokenSimple]] = {}
        for page_num, page_id in page_id_map.items():
            db_tokens = token_repo.get_by_page(page_id)
            if db_tokens:
                all_tokens_by_page[page_num] = _normalize_tokens(db_tokens)

        for field in fields:
            override_pages = (
                set(field_page_overrides.get(field.field_key, []))
                if field_page_overrides
                else set()
            )
            candidate_pages = [field.target_page] + sorted(
                p for p in override_pages if p != field.target_page
            )

            # Keep only pages that exist in this job.
            candidate_pages = [p for p in candidate_pages if p in page_id_map]

            page_id = page_id_map.get(field.target_page)
            if page_id is None and not candidate_pages:
                results.append(
                    ExtractionResult(
                        field_key=field.field_key,
                        value=None,
                        confidence=0.0,
                        evidence_bbox=None,
                        evidence_text=None,
                        token_ids=[],
                    )
                )
                continue

            # Get label aliases from template or fall back to global aliases.
            label_aliases = self._get_label_aliases(field)
            result = None

            # Try anchor-relative then label-anchored extraction on preferred pages.
            for page_num in candidate_pages:
                page_tokens = all_tokens_by_page.get(page_num, [])
                if not page_tokens:
                    continue

                anchor_result = self._extract_anchor_relative(
                    field=field,
                    page_num=page_num,
                    tokens=page_tokens,
                )
                if anchor_result and anchor_result.value:
                    result = anchor_result
                    break

                if label_aliases:
                    label_result = self._extract_label_anchored(
                        field=field,
                        label_aliases=label_aliases,
                        tokens=page_tokens,
                        page_num=page_num,
                    )
                    if label_result and label_result.value:
                        result = label_result
                        break

            # Fall back to ROI extraction if anchor/label strategies failed.
            if (result is None or result.value is None) and page_id is not None:
                result = self.extract_field(field, page_id, token_repo)

            if result is None:
                result = ExtractionResult(
                    field_key=field.field_key,
                    value=None,
                    confidence=0.0,
                    evidence_bbox=None,
                    evidence_text=None,
                    token_ids=[],
                )
            results.append(result)

        return results

    def _get_label_aliases(self, field: FaxTemplateField) -> list[str]:
        """Get label aliases for a field from template + global sources.

        Template aliases come first (payer-specific, tested at seed time),
        then global aliases are merged for broader coverage (ensures new
        patterns like Date Span are available even for older templates).
        """
        post_proc = field.post_processing or {}
        template_aliases = post_proc.get("label_aliases", [])

        if template_aliases:
            aliases = list(template_aliases)
        elif field.field_label:
            aliases = [field.field_label]
        else:
            aliases = []

        # Always merge global LABEL_ALIASES for broader coverage
        global_aliases = LABEL_ALIASES.get(field.field_key, [])
        seen = {a.lower() for a in aliases}
        for ga in global_aliases:
            if ga.lower() not in seen:
                aliases.append(ga)
                seen.add(ga.lower())

        return aliases

    def _get_anchor_config(self, field: FaxTemplateField) -> dict[str, Any]:
        """Return normalized anchor config from field.post_processing."""
        post_proc = field.post_processing or {}
        anchor = post_proc.get("anchor", {})
        if not isinstance(anchor, dict):
            return {}

        aliases = anchor.get("aliases", [])
        if isinstance(aliases, str):
            aliases = [aliases]
        aliases = [a for a in aliases if isinstance(a, str) and a.strip()]

        direction = str(anchor.get("direction", "right")).lower().strip()
        if direction not in {"right", "below"}:
            direction = "right"

        max_tokens_raw = anchor.get("max_tokens", 4)
        try:
            max_tokens = max(1, min(12, int(max_tokens_raw)))
        except Exception:
            max_tokens = 4

        return {
            "aliases": aliases,
            "direction": direction,
            "max_tokens": max_tokens,
        }

    def _extract_anchor_relative(
        self,
        field: FaxTemplateField,
        page_num: int,
        tokens: list[OcrTokenSimple],
    ) -> ExtractionResult | None:
        """
        Extract value relative to a configured anchor.

        Anchor config lives in field.post_processing["anchor"]:
          {
            "aliases": ["Requesting Provider Name", ...],
            "direction": "right" | "below",
            "max_tokens": 6
          }
        """
        anchor_cfg = self._get_anchor_config(field)
        aliases = anchor_cfg.get("aliases", [])
        if not aliases:
            return None

        canon_labels = [a.lower().strip() for a in aliases if a.strip()]
        anchor_tokens, score = self._find_label_tokens(canon_labels, tokens)
        if not anchor_tokens or score < 0.5:
            return None

        anchor_x0 = min(t.x0 for t in anchor_tokens)
        anchor_x1 = max(t.x1 for t in anchor_tokens)
        anchor_y0 = min(t.y0 for t in anchor_tokens)
        anchor_y1 = max(t.y1 for t in anchor_tokens)
        row_height = max(anchor_y1 - anchor_y0, 0.01)

        direction = anchor_cfg.get("direction", "right")
        if direction == "below":
            value_tokens = self._collect_below_tokens(
                all_tokens=tokens,
                label_tokens=anchor_tokens,
                label_x0=anchor_x0,
                label_x1=anchor_x1,
                label_y1=anchor_y1,
                row_height=row_height,
            )
        else:
            roi_max_x = min(1.0, float(field.roi_x1) + 0.08)
            value_tokens = self._collect_right_tokens(
                all_tokens=tokens,
                label_tokens=anchor_tokens,
                label_x1=anchor_x1,
                label_y0=anchor_y0,
                row_height=row_height,
                max_x=roi_max_x,
            )

        max_tokens = int(anchor_cfg.get("max_tokens", 4))
        value_tokens = value_tokens[:max_tokens]
        value_tokens = _filter_tokens_by_field_type(field.field_key, value_tokens)
        if not value_tokens:
            return None

        value_text = " ".join(t.text for t in value_tokens).strip()
        if not value_text:
            return None

        value_text = self._trim_value_by_type(field.field_key, value_text)
        if not value_text:
            return None

        confidence = min(
            1.0,
            self._compute_confidence(value_tokens, field, is_label_anchored=True) + 0.04,
        )
        evidence_bbox = {
            "x0": min(t.x0 for t in value_tokens),
            "y0": min(t.y0 for t in value_tokens),
            "x1": max(t.x1 for t in value_tokens),
            "y1": max(t.y1 for t in value_tokens),
            "page": page_num,
        }

        return ExtractionResult(
            field_key=field.field_key,
            value=value_text,
            confidence=confidence,
            evidence_bbox=evidence_bbox,
            evidence_text=value_text,
            token_ids=[],
            extraction_strategy="anchor_relative",
        )

    def _extract_label_anchored(
        self,
        field: FaxTemplateField,
        label_aliases: list[str],
        tokens: list[OcrTokenSimple],
        page_num: int,
    ) -> ExtractionResult | None:
        """
        Find the field label in OCR tokens and extract the value relative to it.

        Strategy:
        1. Search for label text (multi-word aware, space-insensitive)
        2. Check for inline "Label: Value" in a single token
        3. Collect value tokens to the RIGHT on the same line
        4. If no right-tokens, collect tokens BELOW the label
        5. Filter by expected field type
        6. Validate against template ROI (bonus/penalty)
        """
        canon_labels = [l.lower().strip() for l in label_aliases]

        # --- Step 1: Find label tokens ---
        best_label_tokens, best_score = self._find_label_tokens(
            canon_labels, tokens
        )
        if not best_label_tokens:
            return None

        # --- Step 2: Check for inline "Label: Value" ---
        inline_value = _extract_inline_value(best_label_tokens, canon_labels)
        if inline_value:
            inline_tokens = [best_label_tokens[-1]]
            inline_tokens = _filter_tokens_by_field_type(field.field_key, [
                OcrTokenSimple(
                    text=inline_value,
                    x0=best_label_tokens[-1].x0,
                    y0=best_label_tokens[-1].y0,
                    x1=best_label_tokens[-1].x1,
                    y1=best_label_tokens[-1].y1,
                    confidence=best_label_tokens[-1].confidence,
                    line_number=best_label_tokens[-1].line_number,
                )
            ])
            if inline_tokens:
                # Apply field-type trimming (e.g., split date ranges)
                trimmed_value = self._trim_value_by_type(
                    field.field_key, inline_value
                )
                confidence = self._compute_confidence(
                    inline_tokens, field, is_label_anchored=True
                )
                evidence_bbox = {
                    "x0": best_label_tokens[-1].x0,
                    "y0": best_label_tokens[-1].y0,
                    "x1": best_label_tokens[-1].x1,
                    "y1": best_label_tokens[-1].y1,
                    "page": page_num,
                }
                return ExtractionResult(
                    field_key=field.field_key,
                    value=trimmed_value,
                    confidence=confidence,
                    evidence_bbox=evidence_bbox,
                    evidence_text=trimmed_value,
                    token_ids=[],
                    extraction_strategy="label_anchored",
                )

        # --- Step 3: Collect value tokens RIGHT of label ---
        label_x1 = max(t.x1 for t in best_label_tokens)
        label_y0 = min(t.y0 for t in best_label_tokens)
        label_y1 = max(t.y1 for t in best_label_tokens)
        row_height = max(label_y1 - label_y0, 0.01)

        # Use template ROI right edge as a guide (with tolerance)
        roi_x1 = float(field.roi_x1)
        roi_max_x = roi_x1 + 0.05  # Allow 5% overshoot beyond ROI

        value_tokens = self._collect_right_tokens(
            tokens, best_label_tokens, label_x1, label_y0, row_height,
            max_x=roi_max_x,
        )

        # Filter right-tokens by field type BEFORE deciding to try below
        value_tokens = _filter_tokens_by_field_type(field.field_key, value_tokens)

        # --- Step 4: If no valid right-tokens, try BELOW ---
        if not value_tokens:
            label_x0 = min(t.x0 for t in best_label_tokens)
            value_tokens = self._collect_below_tokens(
                tokens, best_label_tokens, label_x0, label_x1, label_y1,
                row_height,
            )
            value_tokens = _filter_tokens_by_field_type(
                field.field_key, value_tokens
            )

        if not value_tokens:
            return None

        value_text = " ".join(t.text for t in value_tokens).strip()
        if not value_text:
            return None

        # --- Step 5b: Field-type-specific value length limits ---
        value_text = self._trim_value_by_type(field.field_key, value_text)

        # --- Step 6: Compute confidence with ROI validation ---
        confidence = self._compute_confidence(
            value_tokens, field, is_label_anchored=True
        )

        evidence_bbox = {
            "x0": min(t.x0 for t in value_tokens),
            "y0": min(t.y0 for t in value_tokens),
            "x1": max(t.x1 for t in value_tokens),
            "y1": max(t.y1 for t in value_tokens),
            "page": page_num,
        }

        # Sanity check bbox
        roi_w = evidence_bbox["x1"] - evidence_bbox["x0"]
        roi_h = evidence_bbox["y1"] - evidence_bbox["y0"]
        if roi_w < 0.005 or roi_h < 0.003 or roi_w > 0.9 or roi_h > 0.3:
            return None

        return ExtractionResult(
            field_key=field.field_key,
            value=value_text,
            confidence=confidence,
            evidence_bbox=evidence_bbox,
            evidence_text=value_text,
            token_ids=[],
            extraction_strategy="label_anchored",
        )

    def _find_label_tokens(
        self,
        canon_labels: list[str],
        tokens: list[OcrTokenSimple],
    ) -> tuple[list[OcrTokenSimple], float]:
        """Find tokens matching field label text. Returns (tokens, score).

        When multiple labels tie at the same score, prefers the match with
        extractable inline value (e.g., "Date Span08/25/2025" > "Start Date").
        """
        best_tokens: list[OcrTokenSimple] = []
        best_score = 0.0
        best_has_inline = False

        for label in canon_labels:
            label_words = label.split()

            for i, token in enumerate(tokens):
                tok_text = token.text.lower().strip()

                if len(label_words) == 1:
                    word_clean = label_words[0].rstrip(":").rstrip("#")
                    tok_clean = tok_text.rstrip(":").rstrip("#")
                    tok_no_space = tok_clean.replace(" ", "")
                    word_no_space = word_clean.replace(" ", "")
                    tok_norm = _ocr_normalize(tok_no_space)
                    word_norm = _ocr_normalize(word_no_space)
                    if (
                        word_clean in tok_clean
                        or tok_clean in word_clean
                        or word_no_space in tok_no_space
                        or tok_no_space in word_no_space
                        or word_norm in tok_norm
                        or tok_norm in word_norm
                    ):
                        score = len(word_clean) / max(len(tok_clean), 1)
                        if score > best_score:
                            best_tokens = [token]
                            best_score = score
                            best_has_inline = bool(
                                _extract_inline_value([token], [label])
                            )
                        elif score == best_score and not best_has_inline:
                            if _extract_inline_value([token], [label]):
                                best_tokens = [token]
                                best_has_inline = True
                else:
                    # Multi-word: find tokens on same/adjacent lines
                    matched = []
                    words_found = 0
                    for j in range(i, min(i + len(label_words) + 2, len(tokens))):
                        t = tokens[j]
                        if matched and abs(t.line_number - matched[0].line_number) > 1:
                            break
                        t_text = t.text.lower().strip().rstrip(":").rstrip("#")
                        t_norm = _ocr_normalize(t_text)
                        matched_any = False
                        for w in label_words[words_found:]:
                            w_clean = w.rstrip(":").rstrip("#")
                            w_norm = _ocr_normalize(w_clean)
                            if (w_clean in t_text or t_text in w_clean
                                    or w_norm in t_norm or t_norm in w_norm):
                                if t not in matched:
                                    matched.append(t)
                                words_found += 1
                                matched_any = True
                            else:
                                break
                        if not matched_any:
                            if matched:
                                continue  # Skip non-matching after first match
                            break

                    if words_found >= len(label_words) * 0.6:
                        score = words_found / len(label_words)
                        if score > best_score:
                            best_tokens = matched
                            best_score = score
                            best_has_inline = bool(
                                _extract_inline_value(matched, [label])
                            )
                        elif score == best_score:
                            # Tiebreaker 1: fewer tokens
                            if len(matched) < len(best_tokens):
                                best_tokens = matched
                                best_has_inline = bool(
                                    _extract_inline_value(matched, [label])
                                )
                            # Tiebreaker 2: inline value wins
                            elif (
                                len(matched) == len(best_tokens)
                                and not best_has_inline
                            ):
                                if _extract_inline_value(matched, [label]):
                                    best_tokens = matched
                                    best_has_inline = True

        return best_tokens, best_score

    def _looks_like_label(self, text: str) -> bool:
        """Check if a token looks like a field label."""
        stripped = text.strip()
        if stripped.endswith(":"):
            return True
        clean = stripped.lower().rstrip(":").rstrip("#")
        # Check individual word match
        if clean in self._label_words and len(clean) > 2:
            return True
        # Check full phrase match (e.g., "Patient Name", "Date of birth")
        if clean in self._label_phrases:
            return True
        return False

    def _collect_right_tokens(
        self,
        all_tokens: list[OcrTokenSimple],
        label_tokens: list[OcrTokenSimple],
        label_x1: float,
        label_y0: float,
        row_height: float,
        max_x: float = 1.0,
    ) -> list[OcrTokenSimple]:
        """Collect value tokens to the right of the label on the same line.

        Args:
            max_x: Maximum x coordinate to collect tokens up to (from template
                   ROI right edge + tolerance). Prevents grabbing the next
                   field's value on the same line.
        """
        right_candidates = []
        for token in all_tokens:
            if token in label_tokens:
                continue
            same_line = abs(token.y0 - label_y0) < row_height * 0.7
            to_right = token.x0 >= label_x1 - 0.01
            within_bounds = token.x0 < max_x
            if same_line and to_right and within_bounds:
                right_candidates.append(token)

        right_candidates.sort(key=lambda t: t.x0)

        value_tokens = []
        for i, tok in enumerate(right_candidates[:4]):
            if self._looks_like_label(tok.text):
                break
            if i > 0 and tok.x0 - right_candidates[i - 1].x1 > 0.10:
                break
            value_tokens.append(tok)

        return value_tokens

    def _collect_below_tokens(
        self,
        all_tokens: list[OcrTokenSimple],
        label_tokens: list[OcrTokenSimple],
        label_x0: float,
        label_x1: float,
        label_y1: float,
        row_height: float,
    ) -> list[OcrTokenSimple]:
        """Collect value tokens below the label, skipping intervening label rows."""
        below_candidates = []
        below_max_y = label_y1 + max(row_height * 2.5, 0.06)
        # Column alignment: tight tolerance from label position
        col_x0 = label_x0 - 0.03
        col_x1 = label_x1 + 0.06
        for token in all_tokens:
            if token in label_tokens:
                continue
            below = label_y1 - 0.005 <= token.y0 <= below_max_y
            h_close = token.x0 < col_x1 and token.x1 > col_x0
            if below and h_close:
                below_candidates.append(token)

        below_candidates.sort(key=lambda t: (t.y0, t.x0))

        value_tokens = []
        if below_candidates:
            # Skip leading label tokens to find first value line
            first_value_line = None
            for tok in below_candidates:
                if not self._looks_like_label(tok.text):
                    first_value_line = tok.line_number
                    break
            if first_value_line is not None:
                for tok in below_candidates:
                    if tok.line_number < first_value_line:
                        continue
                    if tok.line_number > first_value_line + 1:
                        break  # Allow 2 value lines (handles codes on 2nd line)
                    if self._looks_like_label(tok.text):
                        continue  # Skip labels, don't break (tight col alignment prevents cross-column)
                    value_tokens.append(tok)
                    if len(value_tokens) >= 4:
                        break

        return value_tokens

    def _compute_confidence(
        self,
        value_tokens: list[OcrTokenSimple],
        field: FaxTemplateField,
        is_label_anchored: bool = False,
    ) -> float:
        """
        Compute extraction confidence.

        Factors:
        - Base: average OCR confidence of value tokens
        - Bonus: +0.05 for label-anchored (found the label, higher trust)
        - Bonus: +0.05 if extracted value center is near template ROI
        - Penalty: -0.10 if extracted value is far from template ROI
        """
        if not value_tokens:
            return 0.0

        avg_conf = sum(t.confidence for t in value_tokens) / len(value_tokens)

        if is_label_anchored:
            avg_conf += 0.05  # Label-anchored bonus

        # Check proximity to template ROI
        val_cx = (min(t.x0 for t in value_tokens) + max(t.x1 for t in value_tokens)) / 2
        val_cy = (min(t.y0 for t in value_tokens) + max(t.y1 for t in value_tokens)) / 2
        roi_cx = (float(field.roi_x0) + float(field.roi_x1)) / 2
        roi_cy = (float(field.roi_y0) + float(field.roi_y1)) / 2

        dist = ((val_cx - roi_cx) ** 2 + (val_cy - roi_cy) ** 2) ** 0.5

        if dist < 0.10:
            avg_conf += 0.05  # Near expected position
        elif dist > 0.30:
            avg_conf -= 0.10  # Far from expected position

        return max(0.30, min(1.0, avg_conf))

    # Label-prefix patterns to strip before type-specific processing.
    # Healthcare fax forms often include the label in the same OCR token
    # group as the value, e.g. "Member ID: 82353822-01" or
    # "Member Date of Birth: 09/10/1985".
    _LABEL_STRIP_RE: dict[str, "re.Pattern[str]"] = {}

    @staticmethod
    def _strip_label_prefix(field_key: str, value: str) -> str:
        """Strip common label prefixes from extracted values."""
        patterns: dict[str, str] = {
            "member_id": (
                r"^(?:member\s*(?:id|#|number|plan\s*id)|medicaid\s*id|"
                r"subscriber\s*id|plan\s*id|id\s*#?|enrollee\s*id)\s*[:=\s]+"
            ),
            "patient_dob": (
                r"^(?:member\s*date\s*of\s*birth|date\s*of\s*birth|"
                r"patient\s*(?:date\s*of\s*birth|dob)|dob|birth\s*date)\s*[:=\s]+"
            ),
            "patient_name": (
                r"^(?:member\s*name|patient\s*name|subscriber\s*name|"
                r"patient|member|name)\s*[:=\s]+"
            ),
            "provider_name": (
                r"^(?:servicing\s*provider\s*(?:name)?|requesting\s*provider\s*(?:name)?|"
                r"provider\s*(?:name)?|facility\s*(?:name)?|treating\s*provider)\s*[:=.,\s]+"
            ),
            "prior_auth_number": (
                r"^(?:authorization\s*(?:number|#|no\.?)|auth\s*(?:number|#|no\.?)|"
                r"reference\s*(?:number|#)|prior\s*auth\s*(?:number|#)?)\s*[:=\s]+"
            ),
        }
        pat_str = patterns.get(field_key.lower())
        if not pat_str:
            return value
        stripped = re.sub(pat_str, "", value, count=1, flags=re.IGNORECASE).strip()
        # Return stripped (may be "") — do NOT fall back to original when pattern consumed everything
        # (empty means the entire value was a label prefix with no actual value)
        return stripped

    @staticmethod
    def _trim_value_by_type(field_key: str, value: str) -> str:
        """Trim extracted value based on field type expectations.

        Prevents collecting too many tokens for fields with known formats.
        """
        key = field_key.lower()

        # Strip label prefixes first (e.g. "Member ID: 82353822-01" → "82353822-01")
        value = TemplateExtractor._strip_label_prefix(key, value)

        if key in ("prior_auth_number", "reference_number"):
            # Auth/reference numbers: typically 1 token, alphanumeric with digits
            # Examples: UM84429804, 0806WD89S, 1119W6G11, OP0085373636
            # Strip non-ID prefix before # (e.g., "is#213403025" → "213403025")
            if "#" in value:
                after_hash = value.split("#", 1)[1].strip()
                if after_hash and any(c.isdigit() for c in after_hash):
                    value = after_hash
            # Reject if value looks like pure narrative prose
            if len(value.split()) > 6:
                return ""  # Too many words — definitely not an auth number
            parts = value.split()
            if len(parts) > 1:
                # Keep only tokens that look like IDs (must contain at least
                # one digit — excludes plain English words like "Talbot")
                id_parts = []
                for part in parts:
                    clean = part.strip("()[]{}.,;:")
                    has_digit = any(c.isdigit() for c in clean)
                    is_alnum = re.match(r"^[A-Za-z0-9\-#]+$", clean)
                    if has_digit and is_alnum and len(clean) >= 3:
                        id_parts.append(part)
                    else:
                        break
                if id_parts:
                    return " ".join(id_parts)
            return value

        if "date" in key or key in ("patient_dob",):
            # Reject "Date Span: start -- end" for next_review_date —
            # that is the auth period, not the review date.
            if key == "next_review_date" and re.search(
                r"date\s*span", value, re.IGNORECASE
            ):
                return ""  # Force this candidate to be skipped

            # Dates: extract date-like patterns (MM/DD/YYYY or YYYY-MM-DD)
            _dp = r"(?:\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|\d{4}[/\-]\d{1,2}[/\-]\d{1,2})"
            all_dates = re.findall(_dp, value)
            if all_dates:
                # For expiration/end dates, take the LAST date from a range
                if "expir" in key:
                    return all_dates[-1]
                # For all others (effective, DOB), take the FIRST date
                return all_dates[0]
            return value

        if key == "member_id":
            # Strip any remaining label-like prefix tokens
            # e.g. "Member ID: H67649627" → "H67649627" (after label strip above)
            parts = value.split()
            if parts:
                # Take the first token that contains digits
                for part in parts:
                    clean = part.strip("#:=()[]{}.,;")
                    if clean and any(c.isdigit() for c in clean):
                        return clean
                return parts[0]
            return value

        if key in ("service_code", "cpt_code", "hcpc_code"):
            # Service codes: short alphanumeric codes with digits
            # Examples: H2034, H2036, 99213, 3.5RTC
            # OCR fix: "11XXXX" may be misread "H" → "11" (H looks like 11 at low DPI)
            parts = value.split()
            code_parts = []
            for part in parts:
                clean = part.strip("()[]{}.,;:")
                # OCR correction: "112036" → "H2036" (H misread as 11)
                if re.match(r"^11[0-9]{4}$", clean):
                    clean = "H" + clean[2:]
                    part = clean
                has_digit = any(c.isdigit() for c in clean)
                is_code_like = re.match(r"^[A-Za-z0-9.\-]+$", clean) and len(clean) <= 10
                if has_digit and is_code_like:
                    code_parts.append(part)
                else:
                    continue  # Skip non-code tokens (e.g. "UNITS") between codes
            if code_parts:
                return " ".join(code_parts)
            return value

        if key in ("provider_npi", "npi"):
            # NPI: exactly 10 digits
            npi_match = re.search(r"\d{10}", value)
            if npi_match:
                return npi_match.group(0)
            return value

        if key == "patient_name":
            # Patient names: strip leading/trailing dates and non-name text
            # e.g., "06/29/1990 MILLERRIKKI" → "MILLERRIKKI"
            # e.g., "Brock Baker 09/10/1985" → "Brock Baker"
            cleaned = re.sub(r"^\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}\s+", "", value)
            cleaned = re.sub(r"\s+\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}.*$", "", cleaned)
            # Strip leading pure-digit strings (auth numbers that bleed in)
            cleaned = re.sub(r"^\d{6,}\s+", "", cleaned)
            # Strip leading "[" artifact (e.g., "[NICHOLAS LIMONGI")
            cleaned = re.sub(r"^\[+", "", cleaned)
            cleaned = cleaned.strip()
            # Reject values that are label text, not names
            # (e.g., "Dateofbirth" — OCR runs of the "Date of Birth" column header)
            _label_junk = re.match(
                r"^(?:date\s*of\s*birth|dateofbirth|dob|member\s*(?:id|dob|name)|"
                r"auth(?:orization)?|prior\s*auth|health\s*plan|provider|"
                r"service\s*code|diagnosis|hcpc|npi)\b",
                cleaned, re.IGNORECASE,
            )
            if _label_junk:
                return ""
            return cleaned if cleaned else value

        if key == "provider_name":
            # Strip trailing punctuation artifacts
            value = value.rstrip(":.").strip()
            # If value is just label text (no real content after), return empty
            if re.match(
                r"^(?:servicing\s*provider\s*name|provider\s*name|"
                r"requesting\s*provider|facility\s*name)[.:,\s]*$",
                value, re.IGNORECASE,
            ):
                return ""
            return value

        return value

    def verify_template_match(
        self,
        template_version_id: UUID,
        detected_payer: str | None,
        page_id_map: dict[int, UUID],
        db: Session,
        template_match_score: float = 0.0,
    ) -> dict[str, Any]:
        """
        Verify that a matched template actually applies to this document.

        Checks:
        1. High match score bypass (>= 0.85): image-level match is strong evidence
        2. Payer consistency: detected payer matches template's payer
        3. Anchor score: how many template labels are found near expected positions

        Returns:
            {
                "verified": bool,
                "payer_match": bool,
                "anchor_score": float (0.0-1.0),
                "anchors_found": int,
                "anchors_total": int,
                "reason": str,
            }
        """
        field_repo = TemplateFieldRepository(db)
        token_repo = OcrTokenRepository(db)
        fields = field_repo.get_by_version(template_version_id)

        if not fields:
            return {
                "verified": False,
                "payer_match": True,
                "anchor_score": 0.0,
                "anchors_found": 0,
                "anchors_total": 0,
                "reason": "no_fields_defined",
            }

        # Load template to check payer
        from libs.shared.db.models.fax_template import FaxTemplateVersion
        template_version = db.get(FaxTemplateVersion, template_version_id)
        payer_match = True
        if template_version and detected_payer:
            template_payer = template_version.template.payer_name.value
            payer_match = template_payer == detected_payer

        # High match score bypass: if image-level match is very strong (>= 0.85),
        # trust it without strict anchor validation. Pages may be rotated or have
        # minor layout shifts that cause anchor misses, but the visual match is solid.
        if template_match_score >= 0.85 and payer_match:
            return {
                "verified": True,
                "payer_match": payer_match,
                "anchor_score": 1.0,
                "anchors_found": len(fields),
                "anchors_total": len(fields),
                "reason": "high_match_score_bypass",
            }

        # Check anchor score: for each field, is the label near the expected position?
        all_tokens_by_page: dict[int, list[OcrTokenSimple]] = {}
        for page_num, page_id in page_id_map.items():
            db_tokens = token_repo.get_by_page(page_id)
            if db_tokens:
                all_tokens_by_page[page_num] = _normalize_tokens(db_tokens)

        anchors_found = 0
        anchors_total = 0

        for field in fields:
            label_aliases = self._get_label_aliases(field)
            if not label_aliases:
                continue

            anchors_total += 1
            page_tokens = all_tokens_by_page.get(field.target_page, [])
            if not page_tokens:
                continue

            canon_labels = [l.lower().strip() for l in label_aliases]
            label_tokens, score = self._find_label_tokens(canon_labels, page_tokens)

            if not label_tokens or score < 0.5:
                continue

            # Check if found label is near the expected ROI position
            label_cx = (min(t.x0 for t in label_tokens) + max(t.x1 for t in label_tokens)) / 2
            label_cy = (min(t.y0 for t in label_tokens) + max(t.y1 for t in label_tokens)) / 2
            roi_cx = (float(field.roi_x0) + float(field.roi_x1)) / 2
            roi_cy = (float(field.roi_y0) + float(field.roi_y1)) / 2

            dist = ((label_cx - roi_cx) ** 2 + (label_cy - roi_cy) ** 2) ** 0.5

            if dist < 0.20:
                anchors_found += 1

        anchor_score = anchors_found / anchors_total if anchors_total > 0 else 0.0

        # Template is verified if payer matches AND anchor score >= 0.25
        # (relaxed from 0.40 — rotated pages and layout variation cause anchor misses)
        anchor_threshold = 0.25
        verified = payer_match and anchor_score >= anchor_threshold

        reason = "ok"
        if not payer_match:
            reason = "payer_mismatch"
        elif anchor_score < anchor_threshold:
            reason = f"low_anchor_score_{anchor_score:.2f}"

        return {
            "verified": verified,
            "payer_match": payer_match,
            "anchor_score": anchor_score,
            "anchors_found": anchors_found,
            "anchors_total": anchors_total,
            "reason": reason,
        }

    # --- Legacy ROI-based extraction (retained as fallback) ---

    def extract_field(
        self,
        field: FaxTemplateField,
        page_id: UUID,
        token_repo: OcrTokenRepository,
    ) -> ExtractionResult:
        """Extract a single field using ROI-based approach (fallback)."""
        tokens = token_repo.get_tokens_in_roi(
            fax_page_id=page_id,
            roi_x0=float(field.roi_x0),
            roi_y0=float(field.roi_y0),
            roi_x1=float(field.roi_x1),
            roi_y1=float(field.roi_y1),
            overlap_threshold=self.overlap_threshold,
        )

        if not tokens:
            return ExtractionResult(
                field_key=field.field_key,
                value=None,
                confidence=0.0,
                evidence_bbox=None,
                evidence_text=None,
                token_ids=[],
            )

        sorted_tokens = sorted(
            tokens,
            key=lambda t: (float(t.bbox_y0), float(t.bbox_x0)),
        )

        value = " ".join(t.token_text for t in sorted_tokens)
        avg_confidence = sum(float(t.confidence) for t in sorted_tokens) / len(sorted_tokens)

        token_bboxes = [
            {
                "x0": float(t.bbox_x0),
                "y0": float(t.bbox_y0),
                "x1": float(t.bbox_x1),
                "y1": float(t.bbox_y1),
            }
            for t in sorted_tokens
        ]
        evidence_bbox = compute_union_bbox(token_bboxes)
        evidence_bbox["page"] = field.target_page

        token_ids = [t.ocr_token_id for t in sorted_tokens]

        return ExtractionResult(
            field_key=field.field_key,
            value=value,
            confidence=avg_confidence,
            evidence_bbox=evidence_bbox,
            evidence_text=value,
            token_ids=token_ids,
            extraction_strategy="roi",
        )

    def extract_from_roi(
        self,
        page_id: UUID,
        roi: dict[str, float],
        token_repo: OcrTokenRepository,
    ) -> ExtractionResult:
        """Extract text from a specific ROI."""
        tokens = token_repo.get_tokens_in_roi(
            fax_page_id=page_id,
            roi_x0=roi["x0"],
            roi_y0=roi["y0"],
            roi_x1=roi["x1"],
            roi_y1=roi["y1"],
            overlap_threshold=self.overlap_threshold,
        )

        if not tokens:
            return ExtractionResult(
                field_key="",
                value=None,
                confidence=0.0,
                evidence_bbox=None,
                evidence_text=None,
                token_ids=[],
            )

        sorted_tokens = sorted(
            tokens,
            key=lambda t: (float(t.bbox_y0), float(t.bbox_x0)),
        )

        value = " ".join(t.token_text for t in sorted_tokens)
        avg_confidence = sum(float(t.confidence) for t in sorted_tokens) / len(sorted_tokens)

        token_bboxes = [
            {
                "x0": float(t.bbox_x0),
                "y0": float(t.bbox_y0),
                "x1": float(t.bbox_x1),
                "y1": float(t.bbox_y1),
            }
            for t in sorted_tokens
        ]

        return ExtractionResult(
            field_key="",
            value=value,
            confidence=avg_confidence,
            evidence_bbox=compute_union_bbox(token_bboxes),
            evidence_text=value,
            token_ids=[t.ocr_token_id for t in sorted_tokens],
        )

    def suggest_roi(
        self,
        page_id: UUID,
        target_value: str,
        token_repo: OcrTokenRepository,
        fuzzy_threshold: int = 2,
    ) -> dict[str, float] | None:
        """Suggest ROI coordinates for a field value."""
        try:
            import Levenshtein
        except ImportError:
            fuzzy_threshold = 0

        all_tokens = token_repo.get_by_page(page_id)
        if not all_tokens:
            return None

        target_clean = target_value.lower().strip()
        matching_tokens = []

        for token in all_tokens:
            token_clean = token.token_text.lower().strip()
            if target_clean in token_clean or token_clean in target_clean:
                matching_tokens.append(token)

        if not matching_tokens and fuzzy_threshold > 0:
            for token in all_tokens:
                token_clean = token.token_text.lower().strip()
                distance = Levenshtein.distance(target_clean, token_clean)
                if distance <= fuzzy_threshold:
                    matching_tokens.append(token)

        if not matching_tokens:
            return None

        token_bboxes = [
            {
                "x0": float(t.bbox_x0),
                "y0": float(t.bbox_y0),
                "x1": float(t.bbox_x1),
                "y1": float(t.bbox_y1),
            }
            for t in matching_tokens
        ]

        roi = compute_union_bbox(token_bboxes)

        padding = 0.01
        roi["x0"] = max(0.0, roi["x0"] - padding)
        roi["y0"] = max(0.0, roi["y0"] - padding)
        roi["x1"] = min(1.0, roi["x1"] + padding)
        roi["y1"] = min(1.0, roi["y1"] + padding)

        return roi
