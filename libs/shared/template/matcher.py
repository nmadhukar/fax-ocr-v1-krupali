"""
Two-stage template matcher.

Stage 1: pHash prefilter for fast candidate selection
Stage 2: ORB/FLANN for accurate verification
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy.orm import Session

from libs.shared.config import get_settings
from libs.shared.db.models.fax_template import FaxTemplateSample, FaxTemplateVersion
from libs.shared.db.repositories.template_repo import (
    TemplateSampleRepository,
    TemplateVersionRepository,
)
from libs.shared.template.orb_matcher import MatchResult as OrbMatchResult
from libs.shared.template.orb_matcher import OrbFeatures, OrbMatcher
from libs.shared.template.phash import PerceptualHasher


@dataclass
class MatchResult:
    """Result of template matching."""

    matched: bool
    template_version_id: UUID | None
    template_name: str | None
    payer_name: str | None
    score: float
    phash_distance: int | None = None
    orb_matches: int = 0
    orb_inliers: int = 0
    matched_page_number: int | None = None
    candidate_count: int = 0
    match_mode: str = "strict"
    adaptive_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "matched": self.matched,
            "template_version_id": str(self.template_version_id) if self.template_version_id else None,
            "template_name": self.template_name,
            "payer_name": self.payer_name,
            "score": self.score,
            "phash_distance": self.phash_distance,
            "orb_matches": self.orb_matches,
            "orb_inliers": self.orb_inliers,
            "matched_page_number": self.matched_page_number,
            "candidate_count": self.candidate_count,
            "match_mode": self.match_mode,
            "adaptive_used": self.adaptive_used,
        }


class TemplateMatcher:
    """
    Two-stage template matcher for fax documents.

    Stage 1: pHash prefilter (fast, low accuracy)
    Stage 2: ORB/FLANN verification (slower, high accuracy)
    """

    def __init__(
        self,
        phash_threshold: int | None = None,
        orb_min_matches: int | None = None,
        min_score: float | None = None,
    ):
        """
        Initialize template matcher.

        Args:
            phash_threshold: Maximum Hamming distance for pHash.
            orb_min_matches: Minimum ORB matches for positive.
            min_score: Minimum score for match.
        """
        settings = get_settings()

        self.phash_threshold = phash_threshold if phash_threshold is not None else settings.template.phash_threshold
        self.orb_min_matches = orb_min_matches if orb_min_matches is not None else settings.template.orb_min_matches
        self.min_score = min_score if min_score is not None else settings.template.match_min_score

        self.hasher = PerceptualHasher()
        self.orb_matcher = OrbMatcher(min_matches=self.orb_min_matches)

    def match(
        self,
        image: np.ndarray,
        db: Session,
        payer_hint: str | None = None,
    ) -> MatchResult:
        """
        Match an image against all active templates.

        Args:
            image: Query image (first page of fax).
            db: Database session.
            payer_hint: Optional payer hint to narrow search.

        Returns:
            MatchResult with best matching template.
        """
        # Compute query features
        query_phash = self.hasher.compute_hash(image)
        query_orb = self.orb_matcher.compute_features(image)

        # Stage 1: pHash prefilter
        sample_repo = TemplateSampleRepository(db)
        version_repo = TemplateVersionRepository(db)

        # Get all active versions with their samples
        active_versions = version_repo.get_all_active_versions()

        candidates: list[tuple[FaxTemplateVersion, FaxTemplateSample, int]] = []

        for version in active_versions:
            # Filter by payer hint if provided
            if payer_hint and version.template.payer_name.value != payer_hint:
                continue

            # Check each sample
            for sample in version.samples:
                distance = self.hasher.hamming_distance(query_phash, sample.phash_value)
                threshold = version.match_phash_threshold or self.phash_threshold

                if distance <= threshold:
                    candidates.append((version, sample, distance))

        if not candidates:
            return MatchResult(
                matched=False,
                template_version_id=None,
                template_name=None,
                payer_name=None,
                score=0.0,
                candidate_count=0,
                match_mode="single_page",
            )

        # Sort candidates by pHash distance
        candidates.sort(key=lambda x: x[2])

        # Stage 2: ORB verification on top candidates
        best_result: MatchResult | None = None
        best_score = 0.0

        for version, sample, phash_dist in candidates[:5]:  # Top 5 candidates
            # Deserialize ORB features
            if sample.orb_keypoints and sample.orb_descriptors:
                template_orb = OrbFeatures.deserialize(
                    sample.orb_keypoints,
                    sample.orb_descriptors,
                    sample.width_px,
                    sample.height_px,
                )

                # Skip corrupted/empty features
                if not template_orb.keypoints or template_orb.descriptors is None:
                    continue

                # Match ORB features
                orb_result = self.orb_matcher.match(query_orb, template_orb)

                # Calculate combined score
                # Weight: 70% ORB, 30% pHash
                phash_denom = self.phash_threshold * 2 if self.phash_threshold > 0 else 1
                phash_score = 1.0 - (phash_dist / phash_denom)
                combined_score = 0.7 * orb_result.score + 0.3 * max(phash_score, 0)

                # Check minimum thresholds
                min_score = version.match_min_score or self.min_score
                min_matches = version.match_orb_min_matches or self.orb_min_matches

                if orb_result.num_inliers >= min_matches and combined_score >= min_score:
                    if combined_score > best_score:
                        best_score = combined_score
                        best_result = MatchResult(
                            matched=True,
                            template_version_id=version.template_version_id,
                            template_name=version.template.template_name,
                            payer_name=version.template.payer_name.value,
                            score=combined_score,
                            phash_distance=phash_dist,
                            orb_matches=orb_result.num_matches,
                            orb_inliers=orb_result.num_inliers,
                            candidate_count=len(candidates),
                            match_mode="single_page",
                        )

        if best_result:
            return best_result

        # No match found
        return MatchResult(
            matched=False,
            template_version_id=None,
            template_name=None,
            payer_name=None,
            score=best_score,
            candidate_count=len(candidates),
            match_mode="single_page",
        )

    # Adaptive matching: relaxed pHash for near-miss form versions
    ADAPTIVE_PHASH_MULTIPLIER = 2.5  # e.g., threshold 5 → 12
    ADAPTIVE_ORB_MIN_MULTIPLIER = 1.5  # require 50% more ORB inliers
    ADAPTIVE_MIN_SCORE_BOOST = 0.05  # raise min_score slightly

    def match_best_page(
        self,
        page_images: dict[int, np.ndarray],
        db: Session,
        payer_hint: str | None = None,
    ) -> MatchResult:
        """
        Match multiple page images against templates, return the best match.

        Two-pass matching strategy:
        - Pass 1 (strict): Standard pHash threshold, normal ORB min
        - Pass 2 (adaptive): Relaxed pHash (2.5x threshold) with stricter
          ORB requirements (1.5x min inliers). Only runs if Pass 1 fails
          and a payer hint is available.

        Args:
            page_images: Dict of {page_number: image} for non-cover pages.
            db: Database session.
            payer_hint: Optional payer hint to narrow search.

        Returns:
            MatchResult with best match and matched_page_number set.
        """
        import logging

        logger = logging.getLogger(__name__)

        if not page_images:
            return MatchResult(
                matched=False,
                template_version_id=None,
                template_name=None,
                payer_name=None,
                score=0.0,
                match_mode="none",
            )

        # Get all active template versions + samples
        version_repo = TemplateVersionRepository(db)
        all_active_versions = version_repo.get_all_active_versions()

        if not all_active_versions:
            return MatchResult(
                matched=False,
                template_version_id=None,
                template_name=None,
                payer_name=None,
                score=0.0,
                match_mode="none",
            )

        # Filter by payer hint
        payer_versions: list[FaxTemplateVersion] = []
        if payer_hint:
            payer_versions = [
                v for v in all_active_versions
                if v.template.payer_name.value == payer_hint
            ]
            active_versions = payer_versions if payer_versions else all_active_versions
        else:
            active_versions = all_active_versions

        # Compute pHash for all pages once (reused across passes)
        page_phashes: dict[int, int] = {}
        for page_num, page_image in page_images.items():
            page_phashes[page_num] = self.hasher.compute_hash(page_image)

        # --- Pass 1: Strict matching ---
        result = self._match_pages_with_threshold(
            page_images=page_images,
            page_phashes=page_phashes,
            active_versions=active_versions,
            phash_multiplier=1.0,
            orb_multiplier=1.0,
            score_boost=0.0,
            logger=logger,
        )

        if result.matched:
            logger.info(
                "Strict match: page %d (score=%.3f, phash=%d, orb=%d)",
                result.matched_page_number,
                result.score,
                result.phash_distance,
                result.orb_inliers,
            )
            result.match_mode = "strict"
            return result

        # --- Pass 2: Adaptive matching (relaxed pHash, stricter ORB) ---
        # Only attempt if we have a payer hint (limits search space)
        if payer_hint and payer_versions:
            adaptive_result = self._match_pages_with_threshold(
                page_images=page_images,
                page_phashes=page_phashes,
                active_versions=payer_versions,
                phash_multiplier=self.ADAPTIVE_PHASH_MULTIPLIER,
                orb_multiplier=self.ADAPTIVE_ORB_MIN_MULTIPLIER,
                score_boost=self.ADAPTIVE_MIN_SCORE_BOOST,
                logger=logger,
            )

            if adaptive_result.matched:
                logger.info(
                    "Adaptive match: page %d (score=%.3f, phash=%d, orb=%d) "
                    "— relaxed pHash with stricter ORB for payer %s",
                    adaptive_result.matched_page_number,
                    adaptive_result.score,
                    adaptive_result.phash_distance,
                    adaptive_result.orb_inliers,
                    payer_hint,
                )
                adaptive_result.match_mode = "adaptive"
                adaptive_result.adaptive_used = True
                return adaptive_result

        return MatchResult(
            matched=False,
            template_version_id=None,
            template_name=None,
            payer_name=None,
            score=0.0,
            match_mode="none",
        )

    def _match_pages_with_threshold(
        self,
        page_images: dict[int, np.ndarray],
        page_phashes: dict[int, int],
        active_versions: list[FaxTemplateVersion],
        phash_multiplier: float,
        orb_multiplier: float,
        score_boost: float,
        logger: Any,
    ) -> MatchResult:
        """
        Internal matching pass with configurable thresholds.

        Args:
            page_images: Page images.
            page_phashes: Pre-computed pHash per page.
            active_versions: Template versions to match against.
            phash_multiplier: Multiplier for pHash threshold (1.0 = strict, 2.5 = adaptive).
            orb_multiplier: Multiplier for ORB min matches (1.0 = normal, 1.5 = stricter).
            score_boost: Additional min_score requirement (0.0 = normal, 0.05 = stricter).
            logger: Logger instance.

        Returns:
            MatchResult (matched=True if found, False otherwise).
        """
        # Phase 1: pHash prefilter across ALL pages
        phash_candidates: list[tuple[int, FaxTemplateVersion, FaxTemplateSample, int]] = []

        for page_num, page_phash in page_phashes.items():
            for version in active_versions:
                for sample in version.samples:
                    distance = self.hasher.hamming_distance(page_phash, sample.phash_value)
                    base_threshold = version.match_phash_threshold or self.phash_threshold
                    threshold = int(base_threshold * phash_multiplier)

                    if distance <= threshold:
                        phash_candidates.append((page_num, version, sample, distance))

        if not phash_candidates:
            return MatchResult(
                matched=False,
                template_version_id=None,
                template_name=None,
                payer_name=None,
                score=0.0,
                candidate_count=0,
            )

        # Sort by pHash distance (best first)
        phash_candidates.sort(key=lambda x: x[3])

        # Phase 2: ORB verification on top candidates (limit to best 8)
        best_result: MatchResult | None = None
        best_score = 0.0
        orb_cache: dict[int, OrbFeatures] = {}

        for page_num, version, sample, phash_dist in phash_candidates[:8]:
            if sample.orb_keypoints and sample.orb_descriptors:
                # Compute ORB for this page (cached)
                if page_num not in orb_cache:
                    orb_cache[page_num] = self.orb_matcher.compute_features(
                        page_images[page_num]
                    )
                query_orb = orb_cache[page_num]

                template_orb = OrbFeatures.deserialize(
                    sample.orb_keypoints,
                    sample.orb_descriptors,
                    sample.width_px,
                    sample.height_px,
                )

                orb_result = self.orb_matcher.match(query_orb, template_orb)

                base_threshold = version.match_phash_threshold or self.phash_threshold
                phash_denom = base_threshold * 2 if base_threshold > 0 else 1
                phash_score = 1.0 - (phash_dist / phash_denom)
                combined_score = 0.7 * orb_result.score + 0.3 * max(phash_score, 0)

                min_score = float(version.match_min_score or self.min_score) + score_boost
                min_matches = int(
                    (version.match_orb_min_matches or self.orb_min_matches) * orb_multiplier
                )

                if orb_result.num_inliers >= min_matches and combined_score >= min_score:
                    if combined_score > best_score:
                        best_score = combined_score
                        best_result = MatchResult(
                            matched=True,
                            template_version_id=version.template_version_id,
                            template_name=version.template.template_name,
                            payer_name=version.template.payer_name.value,
                            score=combined_score,
                            phash_distance=phash_dist,
                            orb_matches=orb_result.num_matches,
                            orb_inliers=orb_result.num_inliers,
                            matched_page_number=page_num,
                            candidate_count=len(phash_candidates),
                        )

        if best_result:
            return best_result

        return MatchResult(
            matched=False,
            template_version_id=None,
            template_name=None,
            payer_name=None,
            score=best_score,
            candidate_count=len(phash_candidates),
        )

    def compute_template_features(
        self,
        image: np.ndarray,
    ) -> tuple[int, bytes, bytes]:
        """
        Compute features for a new template sample.

        Args:
            image: Template sample image.

        Returns:
            Tuple of (phash_value, orb_keypoints_bytes, orb_descriptors_bytes).
        """
        # Compute pHash
        phash = self.hasher.compute_hash(image)

        # Compute ORB features
        orb_features = self.orb_matcher.compute_features(image)
        kp_bytes, desc_bytes = orb_features.serialize()

        return phash, kp_bytes, desc_bytes or b""

    def test_match(
        self,
        image: np.ndarray,
        template_image: np.ndarray,
    ) -> MatchResult:
        """
        Test match between two images directly.

        Useful for testing without database.

        Args:
            image: Query image.
            template_image: Template image.

        Returns:
            MatchResult.
        """
        # Compute features
        query_phash = self.hasher.compute_hash(image)
        template_phash = self.hasher.compute_hash(template_image)

        phash_dist = self.hasher.hamming_distance(query_phash, template_phash)

        # ORB match
        orb_result = self.orb_matcher.match_image(image, template_image)

        # Combined score
        phash_score = 1.0 - (phash_dist / (self.phash_threshold * 2))
        combined_score = 0.7 * orb_result.score + 0.3 * max(phash_score, 0)

        matched = (
            phash_dist <= self.phash_threshold and
            orb_result.num_inliers >= self.orb_min_matches and
            combined_score >= self.min_score
        )

        return MatchResult(
            matched=matched,
            template_version_id=None,
            template_name="test",
            payer_name=None,
            score=combined_score,
            phash_distance=phash_dist,
            orb_matches=orb_result.num_matches,
            orb_inliers=orb_result.num_inliers,
        )
