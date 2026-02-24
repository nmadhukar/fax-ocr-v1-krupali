"""
Embedding client using sentence-transformers (all-MiniLM-L6-v2).

Generates 384-dimensional embeddings for OCR text chunks.
Model is loaded lazily on first use.  Runs locally — no paid API.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384


class EmbeddingClient:
    """Local sentence-transformers embedding client."""

    def __init__(self, model_name: str = DEFAULT_MODEL) -> None:
        self.model_name = model_name
        self._model: Any = None
        self._available: bool | None = None  # cached after first check

    # ------------------------------------------------------------------
    # Lazy model loading
    # ------------------------------------------------------------------

    def _load_model(self) -> None:
        """Load the sentence-transformers model (lazy, first-use)."""
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self.model_name)
            logger.info("Loaded embedding model: %s", self.model_name)
        except (ImportError, RuntimeError):
            logger.warning(
                "sentence-transformers not available; embeddings disabled"
            )
            self._available = False
            raise

    def is_available(self) -> bool:
        """Check whether the embedding model can be loaded.

        Result is cached after the first check to avoid repeating the
        expensive import chain on every Tier-2 query call.
        """
        if self._available is not None:
            return self._available
        try:
            import sentence_transformers  # noqa: F401
            self._available = True
        except (ImportError, RuntimeError):
            # RuntimeError: peft/transformers version incompatibility
            self._available = False
        return self._available

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode a batch of texts into embedding vectors.

        Args:
            texts: List of text strings to encode.

        Returns:
            List of 384-dimensional float vectors.
        """
        if not texts:
            return []
        self._load_model()
        embeddings = self._model.encode(texts, show_progress_bar=False)
        return [vec.tolist() for vec in embeddings]

    def encode_single(self, text: str) -> list[float]:
        """Encode a single text into an embedding vector."""
        results = self.encode([text])
        return results[0] if results else []

    # ------------------------------------------------------------------
    # Text chunking
    # ------------------------------------------------------------------

    @staticmethod
    def chunk_text(
        text: str,
        max_tokens: int = 256,
        overlap: int = 32,
    ) -> list[str]:
        """Split text into overlapping chunks for embedding.

        Uses simple whitespace tokenization (approx 1 word ≈ 1 token).

        Args:
            text: Full OCR text to chunk.
            max_tokens: Max words per chunk.
            overlap: Overlap between consecutive chunks (words).

        Returns:
            List of text chunks.
        """
        if not text or not text.strip():
            return []

        words = text.split()
        if len(words) <= max_tokens:
            return [text.strip()]

        chunks: list[str] = []
        start = 0
        while start < len(words):
            end = min(start + max_tokens, len(words))
            chunk = " ".join(words[start:end])
            chunks.append(chunk)
            if end >= len(words):
                break
            start += max_tokens - overlap

        return chunks

    @staticmethod
    def clean_ocr_text(text: str) -> str:
        """Light cleaning for OCR text before embedding.

        Collapses whitespace, strips control chars.
        """
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()
