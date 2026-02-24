"""
OCR client interface and data models.
"""

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass
class BoundingBox:
    """Normalized bounding box coordinates [0.0 - 1.0]."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        """Calculate width."""
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        """Calculate height."""
        return self.y1 - self.y0

    @property
    def center(self) -> tuple[float, float]:
        """Calculate center point."""
        return ((self.x0 + self.x1) / 2, (self.y0 + self.y1) / 2)

    @property
    def area(self) -> float:
        """Calculate area (non-negative, even for degenerate boxes)."""
        return max(0.0, self.width * self.height)

    def to_dict(self) -> dict[str, float]:
        """Convert to dictionary."""
        return {
            "x0": self.x0,
            "y0": self.y0,
            "x1": self.x1,
            "y1": self.y1,
        }

    @classmethod
    def from_points(
        cls,
        points: list[list[float]],
        image_width: int,
        image_height: int,
    ) -> "BoundingBox":
        """
        Create from polygon points and normalize to [0.0-1.0].

        Args:
            points: List of [x, y] coordinates (typically 4 corners).
            image_width: Image width in pixels.
            image_height: Image height in pixels.

        Returns:
            Normalized BoundingBox.
        """
        x_coords = [p[0] for p in points]
        y_coords = [p[1] for p in points]

        if image_width <= 0 or image_height <= 0:
            return cls(x0=0.0, y0=0.0, x1=0.0, y1=0.0)

        return cls(
            x0=min(x_coords) / image_width,
            y0=min(y_coords) / image_height,
            x1=max(x_coords) / image_width,
            y1=max(y_coords) / image_height,
        )


@dataclass
class OcrToken:
    """Single OCR token with position and confidence."""

    text: str
    bbox: BoundingBox
    confidence: float
    line_number: int
    word_number: int

    # Optional metadata
    is_numeric: bool = False
    is_date_like: bool = False
    is_bold: bool = False
    is_handwritten: bool = False
    font_size_estimate: int | None = None

    def __post_init__(self) -> None:
        """Compute derived properties."""
        self.is_numeric = self._check_numeric()
        self.is_date_like = self._check_date_like()

    def _check_numeric(self) -> bool:
        """Check if token is numeric."""
        # Remove common separators and check if mostly digits
        cleaned = self.text.replace(",", "").replace(".", "").replace("-", "")
        return cleaned.isdigit() and len(cleaned) > 0

    def _check_date_like(self) -> bool:
        """Check if token looks like part of a date."""
        # Common date patterns
        date_patterns = [
            r"^\d{1,2}/\d{1,2}/\d{2,4}$",  # MM/DD/YYYY
            r"^\d{1,2}-\d{1,2}-\d{2,4}$",  # MM-DD-YYYY
            r"^\d{4}-\d{2}-\d{2}$",  # YYYY-MM-DD
            r"^(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)",  # Month names
            r"^\d{1,2}$",  # Day numbers
        ]
        for pattern in date_patterns:
            if re.match(pattern, self.text, re.IGNORECASE):
                return True
        return False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for database storage."""
        return {
            "token_text": self.text,
            "bbox_x0": self.bbox.x0,
            "bbox_y0": self.bbox.y0,
            "bbox_x1": self.bbox.x1,
            "bbox_y1": self.bbox.y1,
            "confidence": self.confidence,
            "line_number": self.line_number,
            "word_number": self.word_number,
            "is_numeric": self.is_numeric,
            "is_date_like": self.is_date_like,
            "is_bold": self.is_bold,
            "is_handwritten": self.is_handwritten,
            "font_size_estimate": self.font_size_estimate,
        }


@dataclass
class OcrLine:
    """A line of OCR tokens."""

    tokens: list[OcrToken]
    line_number: int

    @property
    def text(self) -> str:
        """Get concatenated text."""
        return " ".join(t.text for t in self.tokens)

    @property
    def bbox(self) -> BoundingBox:
        """Get bounding box covering all tokens."""
        if not self.tokens:
            return BoundingBox(0, 0, 0, 0)

        return BoundingBox(
            x0=min(t.bbox.x0 for t in self.tokens),
            y0=min(t.bbox.y0 for t in self.tokens),
            x1=max(t.bbox.x1 for t in self.tokens),
            y1=max(t.bbox.y1 for t in self.tokens),
        )

    @property
    def average_confidence(self) -> float:
        """Get average confidence of all tokens."""
        if not self.tokens:
            return 0.0
        return sum(t.confidence for t in self.tokens) / len(self.tokens)


@dataclass
class OcrResult:
    """Complete OCR result for a page."""

    tokens: list[OcrToken]
    lines: list[OcrLine] = field(default_factory=list)
    image_width: int = 0
    image_height: int = 0

    # Quality metrics
    average_confidence: float = 0.0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        """Compute derived fields."""
        self.total_tokens = len(self.tokens)
        if self.tokens:
            self.average_confidence = sum(t.confidence for t in self.tokens) / len(self.tokens)

        # Group tokens into lines if not already done
        if not self.lines and self.tokens:
            self._build_lines()

    def _build_lines(self) -> None:
        """Group tokens into lines."""
        line_map: dict[int, list[OcrToken]] = {}
        for token in self.tokens:
            if token.line_number not in line_map:
                line_map[token.line_number] = []
            line_map[token.line_number].append(token)

        self.lines = [
            OcrLine(
                tokens=sorted(tokens, key=lambda t: t.word_number),
                line_number=line_num,
            )
            for line_num, tokens in sorted(line_map.items())
        ]

    @property
    def full_text(self) -> str:
        """Get full text with line breaks."""
        return "\n".join(line.text for line in self.lines)

    def get_tokens_in_region(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        overlap_threshold: float = 0.5,
    ) -> list[OcrToken]:
        """
        Get tokens overlapping with a region.

        Args:
            x0, y0, x1, y1: Region coordinates (normalized).
            overlap_threshold: Minimum overlap ratio.

        Returns:
            List of overlapping tokens.
        """
        result = []
        for token in self.tokens:
            # Calculate intersection
            ix0 = max(token.bbox.x0, x0)
            iy0 = max(token.bbox.y0, y0)
            ix1 = min(token.bbox.x1, x1)
            iy1 = min(token.bbox.y1, y1)

            if ix0 < ix1 and iy0 < iy1:
                intersection = (ix1 - ix0) * (iy1 - iy0)
                token_area = token.bbox.area

                if token_area > 0 and intersection / token_area >= overlap_threshold:
                    result.append(token)

        return sorted(result, key=lambda t: (t.bbox.y0, t.bbox.x0))


class OcrClient(ABC):
    """
    Abstract base class for OCR clients.

    Implementations must provide the extract method.
    """

    @abstractmethod
    def extract(
        self,
        image: np.ndarray,
        detect_tables: bool = False,
    ) -> OcrResult:
        """
        Extract text from an image.

        Args:
            image: Input image as numpy array (BGR or grayscale).
            detect_tables: Whether to detect and parse tables.

        Returns:
            OcrResult with all extracted tokens.
        """
        pass

    @abstractmethod
    def extract_batch(
        self,
        images: list[np.ndarray],
        detect_tables: bool = False,
    ) -> list[OcrResult]:
        """
        Extract text from multiple images.

        Args:
            images: List of input images.
            detect_tables: Whether to detect tables.

        Returns:
            List of OcrResult for each image.
        """
        pass
