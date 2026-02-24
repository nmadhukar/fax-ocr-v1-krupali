"""
VLM client interface and data models.

Defines the abstract interface for Vision Language Model clients.
The implementation is LayoutLM Document QA (impira/layoutlm-document-qa).
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np


@dataclass(frozen=True)
class VlmConfig:
    """Configuration for a VLM client instance."""

    model_name: str = "impira/layoutlm-document-qa"
    max_pages: int = 5
    max_image_size_px: int = 2048
    use_gpu: bool = False
    max_new_tokens: int = 512


@dataclass
class VlmResponse:
    """Response from a VLM extraction request."""

    fields: dict[str, Any]
    raw_sequence: str
    model: str
    latency_ms: float = 0.0

    @property
    def field_count(self) -> int:
        return len(self.fields)


class VlmClient(ABC):
    """
    Abstract base class for VLM clients.

    Concrete implementations provide the ``extract`` method for
    extracting structured fields from document images.
    """

    def __init__(self, config: VlmConfig):
        self.config = config

    @abstractmethod
    def extract(
        self,
        image: np.ndarray,
        field_keys: list[str] | None = None,
    ) -> VlmResponse:
        """
        Extract structured fields from a single page image.

        Args:
            image: Page image as a numpy array (BGR or grayscale).
            field_keys: Optional list of field keys to extract.
                        If None, extracts all known fields.

        Returns:
            VlmResponse with extracted field values.
        """

    @abstractmethod
    def is_available(self) -> bool:
        """Return True when the VLM model is loaded and ready."""

    @property
    def model_name(self) -> str:
        """Model identifier for logging."""
        return self.config.model_name

