"""
PaddleOCR client implementation.

Uses PP-OCRv5 for text detection and recognition.
"""

from functools import lru_cache
from typing import Any

import numpy as np

from libs.shared.config import get_settings
from libs.shared.ocr.base import BoundingBox, OcrClient, OcrResult, OcrToken


class PaddleOcrClient(OcrClient):
    """
    PaddleOCR client using PP-OCRv5.

    Provides high-accuracy OCR for fax documents.
    """

    def __init__(
        self,
        use_gpu: bool | None = None,
        lang: str | None = None,
        use_angle_cls: bool | None = None,
        det_db_thresh: float | None = None,
        det_db_box_thresh: float | None = None,
        rec_batch_num: int | None = None,
    ):
        """
        Initialize PaddleOCR client.

        Args:
            use_gpu: Whether to use GPU acceleration.
            lang: Language for recognition.
            use_angle_cls: Whether to use angle classification.
            det_db_thresh: Detection DB threshold.
            det_db_box_thresh: Detection box threshold.
            rec_batch_num: Recognition batch size.
        """
        settings = get_settings()

        self.use_gpu = use_gpu if use_gpu is not None else settings.ocr.enable_gpu
        self.lang = lang or settings.ocr.lang
        self.use_angle_cls = (
            use_angle_cls if use_angle_cls is not None else settings.ocr.use_angle_cls
        )
        self.det_db_thresh = det_db_thresh or settings.ocr.det_db_thresh
        self.det_db_box_thresh = det_db_box_thresh or settings.ocr.det_db_box_thresh
        self.rec_batch_num = rec_batch_num or settings.ocr.rec_batch_num

        self._ocr: Any = None

    @property
    def ocr(self) -> Any:
        """Lazy-load PaddleOCR instance."""
        if self._ocr is None:
            try:
                from paddleocr import PaddleOCR

                self._ocr = PaddleOCR(
                    use_angle_cls=self.use_angle_cls,
                    lang=self.lang,
                    use_gpu=self.use_gpu,
                    show_log=False,
                    det_db_thresh=self.det_db_thresh,
                    det_db_box_thresh=self.det_db_box_thresh,
                    rec_batch_num=self.rec_batch_num,
                )
            except ImportError:
                raise ImportError(
                    "PaddleOCR is not installed. "
                    "Please install it with: pip install paddleocr paddlepaddle"
                )
        return self._ocr

    def extract(
        self,
        image: np.ndarray,
        detect_tables: bool = False,
    ) -> OcrResult:
        """
        Extract text from an image using PaddleOCR.

        Args:
            image: Input image as numpy array.
            detect_tables: Whether to detect tables (not yet implemented).

        Returns:
            OcrResult with all extracted tokens.
        """
        # Get image dimensions
        if len(image.shape) == 2:
            height, width = image.shape
        else:
            height, width = image.shape[:2]

        # Run OCR
        result = self.ocr.ocr(image, cls=self.use_angle_cls)

        if result is None or len(result) == 0 or result[0] is None:
            return OcrResult(
                tokens=[],
                image_width=width,
                image_height=height,
            )

        # Parse results into tokens
        tokens = self._parse_results(result[0], width, height)

        return OcrResult(
            tokens=tokens,
            image_width=width,
            image_height=height,
        )

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
        # Process each image
        # Note: PaddleOCR doesn't have efficient batch processing
        # for different-sized images, so we process sequentially
        return [self.extract(img, detect_tables) for img in images]

    def _parse_results(
        self,
        ocr_results: list[Any],
        image_width: int,
        image_height: int,
    ) -> list[OcrToken]:
        """
        Parse PaddleOCR results into tokens.

        Args:
            ocr_results: Raw PaddleOCR output.
            image_width: Image width for normalization.
            image_height: Image height for normalization.

        Returns:
            List of OcrToken instances.
        """
        tokens: list[OcrToken] = []

        # Group detections into lines based on Y position
        detections_with_y: list[tuple[float, Any]] = []
        for detection in ocr_results:
            points = detection[0]
            y_center = (points[0][1] + points[2][1]) / 2
            detections_with_y.append((y_center, detection))

        # Sort by Y position
        detections_with_y.sort(key=lambda x: x[0])

        # Group into lines (detections with similar Y are on same line)
        line_threshold = image_height * 0.02  # 2% of image height
        lines: list[list[Any]] = []
        current_line: list[Any] = []
        last_y = -float("inf")

        for y_center, detection in detections_with_y:
            if y_center - last_y > line_threshold and current_line:
                lines.append(current_line)
                current_line = []
            current_line.append(detection)
            last_y = y_center

        if current_line:
            lines.append(current_line)

        # Process each line
        for line_num, line_detections in enumerate(lines, start=1):
            # Sort detections within line by X position (left to right)
            line_detections.sort(key=lambda d: d[0][0][0])

            for word_num, detection in enumerate(line_detections, start=1):
                points = detection[0]
                text, confidence = detection[1]

                # Create normalized bounding box
                bbox = BoundingBox.from_points(points, image_width, image_height)

                # Estimate font size from bounding box height
                bbox_height_px = (bbox.y1 - bbox.y0) * image_height
                # Approximate font size (assuming ~96 DPI display)
                font_size = int(bbox_height_px * 0.75)

                token = OcrToken(
                    text=text.strip(),
                    bbox=bbox,
                    confidence=confidence,
                    line_number=line_num,
                    word_number=word_num,
                    font_size_estimate=font_size,
                )
                tokens.append(token)

        return tokens


@lru_cache
def get_paddle_ocr_client() -> PaddleOcrClient:
    """Get a cached PaddleOCR client instance."""
    return PaddleOcrClient()
