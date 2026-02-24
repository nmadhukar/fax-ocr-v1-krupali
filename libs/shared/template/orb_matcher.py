"""
ORB feature matching for accurate template verification.

Uses ORB (Oriented FAST and Rotated BRIEF) features with
FLANN-based matching for robust template matching.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np

from libs.shared.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class OrbFeatures:
    """ORB features for a template image."""

    keypoints: list[cv2.KeyPoint]
    descriptors: np.ndarray | None
    image_width: int
    image_height: int

    def serialize(self) -> tuple[bytes, bytes | None]:
        """
        Serialize features for database storage.

        Returns:
            Tuple of (keypoints_bytes, descriptors_bytes).
        """
        # Serialize keypoints
        kp_data = [
            (kp.pt, kp.size, kp.angle, kp.response, kp.octave, kp.class_id)
            for kp in self.keypoints
        ]
        kp_bytes = json.dumps(kp_data).encode("utf-8")

        # Descriptors can be stored directly as bytes
        desc_bytes = self.descriptors.tobytes() if self.descriptors is not None else None

        return kp_bytes, desc_bytes

    @classmethod
    def deserialize(
        cls,
        kp_bytes: bytes,
        desc_bytes: bytes | None,
        image_width: int,
        image_height: int,
    ) -> "OrbFeatures":
        """
        Deserialize features from database storage.

        Args:
            kp_bytes: Serialized keypoints.
            desc_bytes: Serialized descriptors.
            image_width: Original image width.
            image_height: Original image height.

        Returns:
            OrbFeatures instance.
        """
        # Deserialize keypoints
        kp_data = json.loads(kp_bytes)
        keypoints = [
            cv2.KeyPoint(
                x=pt[0], y=pt[1],
                size=size, angle=angle,
                response=response, octave=octave, class_id=class_id
            )
            for pt, size, angle, response, octave, class_id in kp_data
        ]

        # Deserialize descriptors (.copy() to make writable — np.frombuffer is read-only)
        if desc_bytes is not None and len(desc_bytes) >= 32:
            descriptors = np.frombuffer(desc_bytes, dtype=np.uint8).copy()
            # ORB descriptors are 32 bytes each; discard trailing garbage
            usable = (len(descriptors) // 32) * 32
            if usable < len(descriptors):
                logger.warning(
                    "ORB descriptors truncated: %d bytes not divisible by 32 (dropped %d trailing bytes)",
                    len(descriptors), len(descriptors) - usable,
                )
            descriptors = descriptors[:usable].reshape(-1, 32)
        else:
            descriptors = None

        return cls(
            keypoints=keypoints,
            descriptors=descriptors,
            image_width=image_width,
            image_height=image_height,
        )


@dataclass
class MatchResult:
    """Result of ORB matching."""

    matched: bool
    score: float  # 0.0 - 1.0
    num_matches: int
    num_inliers: int = 0
    homography: np.ndarray | None = None

    @property
    def inlier_ratio(self) -> float:
        """Calculate ratio of inliers to total matches."""
        if self.num_matches == 0:
            return 0.0
        return self.num_inliers / self.num_matches


class OrbMatcher:
    """
    ORB feature matcher using FLANN.

    Provides robust template matching using feature detection
    and geometric verification.
    """

    def __init__(
        self,
        n_features: int | None = None,
        min_matches: int | None = None,
        lowe_ratio: float | None = None,
    ):
        """
        Initialize ORB matcher.

        Args:
            n_features: Number of ORB features to detect.
            min_matches: Minimum matches for positive match.
            lowe_ratio: Ratio for Lowe's ratio test.
        """
        settings = get_settings()

        self.n_features = n_features if n_features is not None else settings.template.orb_n_features
        self.min_matches = min_matches if min_matches is not None else settings.template.orb_min_matches
        self.lowe_ratio = lowe_ratio if lowe_ratio is not None else settings.template.lowe_ratio

        # Initialize ORB detector
        self.orb = cv2.ORB_create(nfeatures=self.n_features)

        # FLANN parameters for ORB (LSH index)
        FLANN_INDEX_LSH = 6
        self.index_params = dict(
            algorithm=FLANN_INDEX_LSH,
            table_number=settings.template.flann_table_number,
            key_size=settings.template.flann_key_size,
            multi_probe_level=settings.template.flann_multi_probe_level,
        )
        self.search_params = dict(checks=50)

    def compute_features(self, image: np.ndarray) -> OrbFeatures:
        """
        Compute ORB features for an image.

        Args:
            image: Input image (grayscale or BGR).

        Returns:
            OrbFeatures instance.
        """
        # Convert to grayscale if needed
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        # Detect and compute
        keypoints, descriptors = self.orb.detectAndCompute(gray, None)

        height, width = gray.shape[:2]

        return OrbFeatures(
            keypoints=list(keypoints),
            descriptors=descriptors,
            image_width=width,
            image_height=height,
        )

    def match(
        self,
        query_features: OrbFeatures,
        template_features: OrbFeatures,
        use_homography: bool = True,
    ) -> MatchResult:
        """
        Match query features against template features.

        Args:
            query_features: Features from query image.
            template_features: Features from template.
            use_homography: Whether to verify with homography.

        Returns:
            MatchResult with match details.
        """
        # Check for valid descriptors
        if (query_features.descriptors is None or
            template_features.descriptors is None or
            len(query_features.keypoints) < 4 or
            len(template_features.keypoints) < 4):
            return MatchResult(matched=False, score=0.0, num_matches=0)

        # Create FLANN matcher
        try:
            flann = cv2.FlannBasedMatcher(self.index_params, self.search_params)
            matches = flann.knnMatch(
                query_features.descriptors,
                template_features.descriptors,
                k=2,
            )
        except cv2.error:
            # FLANN can fail with certain descriptor configurations
            return MatchResult(matched=False, score=0.0, num_matches=0)

        # Apply Lowe's ratio test
        good_matches = []
        for match_pair in matches:
            if len(match_pair) == 2:
                m, n = match_pair
                if m.distance < self.lowe_ratio * n.distance:
                    good_matches.append(m)

        num_matches = len(good_matches)

        # Check minimum matches
        if num_matches < self.min_matches:
            return MatchResult(
                matched=False,
                score=num_matches / self.min_matches * 0.5,  # Partial score
                num_matches=num_matches,
            )

        # Optionally verify with homography
        num_inliers = num_matches
        homography = None

        if use_homography and num_matches >= 4:
            src_pts = np.float32([
                query_features.keypoints[m.queryIdx].pt
                for m in good_matches
            ]).reshape(-1, 1, 2)

            dst_pts = np.float32([
                template_features.keypoints[m.trainIdx].pt
                for m in good_matches
            ]).reshape(-1, 1, 2)

            # Find homography with RANSAC
            homography, mask = cv2.findHomography(
                src_pts, dst_pts, cv2.RANSAC, 5.0
            )

            if mask is not None:
                num_inliers = int(mask.sum())

        # Calculate score
        # Score is based on ratio of matches to maximum possible
        max_possible = min(
            len(query_features.keypoints),
            len(template_features.keypoints),
        )
        score = num_inliers / max_possible if max_possible > 0 else 0.0

        # Determine if matched
        matched = num_inliers >= self.min_matches

        return MatchResult(
            matched=matched,
            score=min(score, 1.0),  # Cap at 1.0
            num_matches=num_matches,
            num_inliers=num_inliers,
            homography=homography,
        )

    def match_image(
        self,
        query_image: np.ndarray,
        template_image: np.ndarray,
    ) -> MatchResult:
        """
        Match two images directly.

        Args:
            query_image: Query image.
            template_image: Template image.

        Returns:
            MatchResult with match details.
        """
        query_features = self.compute_features(query_image)
        template_features = self.compute_features(template_image)
        return self.match(query_features, template_features)

    def match_against_multiple(
        self,
        query_features: OrbFeatures,
        templates: list[tuple[Any, OrbFeatures]],
    ) -> list[tuple[Any, MatchResult]]:
        """
        Match query against multiple templates.

        Args:
            query_features: Query image features.
            templates: List of (template_id, features) tuples.

        Returns:
            List of (template_id, MatchResult) sorted by score.
        """
        results = []

        for template_id, template_features in templates:
            result = self.match(query_features, template_features)
            results.append((template_id, result))

        # Sort by score (highest first)
        results.sort(key=lambda x: x[1].score, reverse=True)
        return results
