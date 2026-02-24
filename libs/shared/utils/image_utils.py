"""
Image preprocessing utilities for fax documents.

Provides fax-optimized preprocessing pipeline including:
- Deskewing using Hough Line Transform
- Denoising using Non-Local Means
- Adaptive thresholding
- Quality metric computation
"""

from dataclasses import dataclass
from typing import Any

import cv2
import numpy as np


@dataclass
class QualityMetrics:
    """Image quality metrics."""

    blur_score: float  # Laplacian variance (higher = sharper)
    skew_angle_deg: float  # Detected skew angle
    text_density: float  # Ratio of text pixels (0.0-1.0)
    is_low_quality: bool  # True if quality below threshold
    noise_level: float = 0.0  # Estimated noise level

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary (JSON-safe native types)."""
        return {
            "blur_score": float(self.blur_score),
            "skew_angle_deg": float(self.skew_angle_deg),
            "text_density": float(self.text_density),
            "is_low_quality": bool(self.is_low_quality),
            "noise_level": float(self.noise_level),
        }


class ImagePreprocessor:
    """
    Fax-optimized image preprocessing pipeline.

    Applies a sequence of image processing steps to improve
    OCR accuracy on fax documents.
    """

    # Quality thresholds
    BLUR_THRESHOLD = 100.0  # Below this is considered blurry
    MIN_TEXT_DENSITY = 0.01  # Minimum text density for valid page
    MAX_TEXT_DENSITY = 0.5  # Maximum (above is likely noise/image)

    def __init__(
        self,
        target_dpi: int = 300,
        enable_deskew: bool = True,
        enable_denoise: bool = True,
        enable_threshold: bool = True,
    ):
        """
        Initialize preprocessor.

        Args:
            target_dpi: Target DPI for normalization.
            enable_deskew: Whether to apply deskewing.
            enable_denoise: Whether to apply denoising.
            enable_threshold: Whether to apply thresholding.
        """
        self.target_dpi = target_dpi
        self.enable_deskew = enable_deskew
        self.enable_denoise = enable_denoise
        self.enable_threshold = enable_threshold

    def preprocess(
        self,
        image: np.ndarray,
        compute_metrics: bool = True,
    ) -> tuple[np.ndarray, QualityMetrics | None]:
        """
        Apply full preprocessing pipeline.

        Args:
            image: Input image (BGR or grayscale).
            compute_metrics: Whether to compute quality metrics.

        Returns:
            Tuple of (processed_image, metrics).
        """
        # Convert to grayscale if needed
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image.copy()

        # Track applied operations
        applied_ops: list[str] = []

        # 1. Compute initial metrics
        initial_blur = self._compute_blur_score(gray)
        skew_angle = 0.0

        # 2. Deskew
        if self.enable_deskew:
            gray, skew_angle = self._deskew(gray)
            if abs(skew_angle) > 0.5:
                applied_ops.append("deskew")

        # 3. Denoise
        if self.enable_denoise:
            gray = self._denoise(gray)
            applied_ops.append("denoise")

        # 4. Normalize contrast (CLAHE)
        gray = self._normalize_contrast(gray)
        applied_ops.append("clahe")

        # 5. Adaptive threshold (optional, for binarization)
        if self.enable_threshold:
            binary = self._adaptive_threshold(gray)
            applied_ops.append("threshold")
        else:
            binary = gray

        # Compute final metrics
        metrics = None
        if compute_metrics:
            metrics = self._compute_metrics(gray, binary, skew_angle)

        return binary, metrics

    def _deskew(self, image: np.ndarray) -> tuple[np.ndarray, float]:
        """
        Deskew image using Hough Line Transform.

        Args:
            image: Grayscale image.

        Returns:
            Tuple of (deskewed_image, angle_degrees).
        """
        # Edge detection
        edges = cv2.Canny(image, 50, 150, apertureSize=3)

        # Hough Line Transform
        lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=100)

        if lines is None or len(lines) == 0:
            return image, 0.0

        # Calculate angles
        angles = []
        for line in lines:
            rho, theta = line[0]
            angle = np.degrees(theta) - 90  # Convert to deviation from horizontal
            # Only consider small angles (likely text lines)
            if abs(angle) < 45:
                angles.append(angle)

        if not angles:
            return image, 0.0

        # Use median angle to be robust to outliers
        median_angle = np.median(angles)

        # Only deskew if angle is significant
        if abs(median_angle) < 0.5:
            return image, median_angle

        # Rotate image
        height, width = image.shape[:2]
        center = (width // 2, height // 2)
        rotation_matrix = cv2.getRotationMatrix2D(center, median_angle, 1.0)
        rotated = cv2.warpAffine(
            image,
            rotation_matrix,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_REPLICATE,
        )

        return rotated, median_angle

    def _denoise(self, image: np.ndarray) -> np.ndarray:
        """
        Apply Non-Local Means denoising.

        Args:
            image: Grayscale image.

        Returns:
            Denoised image.
        """
        # Parameters tuned for fax documents
        return cv2.fastNlMeansDenoising(
            image,
            h=7,  # Filter strength
            templateWindowSize=7,
            searchWindowSize=21,
        )

    def _normalize_contrast(self, image: np.ndarray) -> np.ndarray:
        """
        Normalize contrast using CLAHE.

        Args:
            image: Grayscale image.

        Returns:
            Contrast-normalized image.
        """
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        return clahe.apply(image)

    def _adaptive_threshold(self, image: np.ndarray) -> np.ndarray:
        """
        Apply adaptive thresholding for binarization.

        Args:
            image: Grayscale image.

        Returns:
            Binary image.
        """
        return cv2.adaptiveThreshold(
            image,
            255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY,
            blockSize=11,
            C=2,
        )

    def _compute_blur_score(self, image: np.ndarray) -> float:
        """
        Compute blur score using Laplacian variance.

        Higher values indicate sharper images.

        Args:
            image: Grayscale image.

        Returns:
            Blur score (Laplacian variance).
        """
        laplacian = cv2.Laplacian(image, cv2.CV_64F)
        return float(laplacian.var())

    def _compute_text_density(self, binary_image: np.ndarray) -> float:
        """
        Compute text density (ratio of dark pixels).

        Args:
            binary_image: Binary image (white background, black text).

        Returns:
            Text density (0.0-1.0).
        """
        # Count dark pixels (text)
        dark_pixels = np.sum(binary_image == 0)
        total_pixels = binary_image.size
        return dark_pixels / total_pixels

    def _compute_metrics(
        self,
        gray_image: np.ndarray,
        binary_image: np.ndarray,
        skew_angle: float,
    ) -> QualityMetrics:
        """
        Compute all quality metrics.

        Args:
            gray_image: Grayscale image.
            binary_image: Binary image.
            skew_angle: Detected skew angle.

        Returns:
            QualityMetrics instance.
        """
        blur_score = self._compute_blur_score(gray_image)
        text_density = self._compute_text_density(binary_image)

        # Estimate noise level from high-frequency components
        noise_level = self._estimate_noise(gray_image)

        # Determine if low quality
        is_low_quality = (
            blur_score < self.BLUR_THRESHOLD or
            text_density < self.MIN_TEXT_DENSITY or
            text_density > self.MAX_TEXT_DENSITY
        )

        return QualityMetrics(
            blur_score=blur_score,
            skew_angle_deg=skew_angle,
            text_density=text_density,
            is_low_quality=is_low_quality,
            noise_level=noise_level,
        )

    def _estimate_noise(self, image: np.ndarray) -> float:
        """
        Estimate noise level using median absolute deviation.

        Args:
            image: Grayscale image.

        Returns:
            Estimated noise level.
        """
        # Apply Laplacian to get high-frequency components
        laplacian = cv2.Laplacian(image, cv2.CV_64F)

        # Robust noise estimation using MAD
        sigma = np.median(np.abs(laplacian)) / 0.6745
        return float(sigma)

    def detect_cover_page(
        self,
        image: np.ndarray,
        keywords: list[str] | None = None,
    ) -> bool:
        """
        Detect if page is a cover/transmittal page.

        Cover pages typically have low text density and
        contain specific keywords.

        Args:
            image: Grayscale or binary image.
            keywords: Optional list of cover page keywords.

        Returns:
            True if likely a cover page.
        """
        if keywords is None:
            keywords = [
                "cover sheet",
                "fax cover",
                "fax cover sheet",
                "cover page",
                "transmittal",
                "confidential notice",
                "confidentiality notice",
                "facsimile",
            ]

        # Check text density
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        binary = self._adaptive_threshold(gray)
        text_density = self._compute_text_density(binary)

        # Cover pages typically have very low text density.
        # Use a conservative threshold to avoid false positives —
        # only flag pages with almost no text at all.
        if text_density < 0.01:
            return True

        # Could also use OCR to check for keywords, but that's
        # done in the pipeline after OCR is performed
        return False

    def resize_to_dpi(
        self,
        image: np.ndarray,
        current_dpi: int,
    ) -> np.ndarray:
        """
        Resize image to target DPI.

        Args:
            image: Input image.
            current_dpi: Current DPI of the image.

        Returns:
            Resized image.
        """
        if current_dpi == self.target_dpi:
            return image

        scale = self.target_dpi / current_dpi
        height, width = image.shape[:2]
        new_width = int(width * scale)
        new_height = int(height * scale)

        return cv2.resize(
            image,
            (new_width, new_height),
            interpolation=cv2.INTER_CUBIC if scale > 1 else cv2.INTER_AREA,
        )

    def crop_borders(
        self,
        image: np.ndarray,
        margin_percent: float = 0.02,
    ) -> np.ndarray:
        """
        Crop dark borders from image.

        Args:
            image: Input image.
            margin_percent: Percentage of image to potentially crop.

        Returns:
            Cropped image.
        """
        # Convert to grayscale if needed
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image

        # Find content boundaries
        binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

        # Find bounding rect of content
        coords = cv2.findNonZero(255 - binary)
        if coords is None:
            return image

        x, y, w, h = cv2.boundingRect(coords)

        # Add small margin
        height, width = gray.shape[:2]
        margin_x = int(width * margin_percent)
        margin_y = int(height * margin_percent)

        x = max(0, x - margin_x)
        y = max(0, y - margin_y)
        w = min(width - x, w + 2 * margin_x)
        h = min(height - y, h + 2 * margin_y)

        return image[y:y+h, x:x+w]
