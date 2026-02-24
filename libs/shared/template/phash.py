"""
Perceptual hashing for fast template prefiltering.

Uses the difference hash (dHash) algorithm for perceptual similarity.
"""

from typing import Any

import numpy as np
from PIL import Image


class PerceptualHasher:
    """
    Perceptual hash computation using dHash algorithm.

    dHash is fast, effective, and resistant to minor variations
    like scaling, minor rotations, and compression artifacts.
    """

    def __init__(self, hash_size: int = 8):
        """
        Initialize the hasher.

        Args:
            hash_size: Size of the hash (default 8 = 64 bits, fits BigInteger).
        """
        self.hash_size = hash_size

    def compute_hash(self, image: np.ndarray | Image.Image) -> int:
        """
        Compute perceptual hash of an image.

        Args:
            image: Input image (numpy array or PIL Image).

        Returns:
            Integer hash value.
        """
        try:
            import imagehash
        except ImportError:
            raise ImportError(
                "imagehash is not installed. "
                "Please install it with: pip install imagehash"
            )

        # Convert to PIL Image if needed
        if isinstance(image, np.ndarray):
            # OpenCV uses BGR; PIL expects RGB
            import cv2
            if len(image.shape) == 3 and image.shape[2] == 3:
                rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            else:
                rgb = image
            pil_image = Image.fromarray(rgb)
        else:
            pil_image = image

        # Compute dHash
        hash_obj = imagehash.dhash(pil_image, hash_size=self.hash_size)

        # Convert to integer
        return int(str(hash_obj), 16)

    def compute_hash_from_file(self, file_path: str) -> int:
        """
        Compute perceptual hash from a file.

        Args:
            file_path: Path to image file.

        Returns:
            Integer hash value.
        """
        with Image.open(file_path) as pil_image:
            return self.compute_hash(pil_image)

    def compute_hash_from_bytes(self, data: bytes) -> int:
        """
        Compute perceptual hash from image bytes.

        Args:
            data: Image data as bytes.

        Returns:
            Integer hash value.
        """
        import io
        pil_image = Image.open(io.BytesIO(data))
        return self.compute_hash(pil_image)

    @staticmethod
    def to_signed(val: int) -> int:
        """Convert unsigned 64-bit hash to signed for PostgreSQL BigInteger storage."""
        if val > 2**63 - 1:
            return val - 2**64
        return val

    @staticmethod
    def to_unsigned(val: int) -> int:
        """Convert signed BigInteger back to unsigned for comparison."""
        if val < 0:
            return val + 2**64
        return val

    @staticmethod
    def hamming_distance(hash1: int, hash2: int) -> int:
        """
        Calculate Hamming distance between two hashes.

        Handles both signed (from DB) and unsigned hash values.

        Args:
            hash1: First hash value (signed or unsigned).
            hash2: Second hash value (signed or unsigned).

        Returns:
            Number of differing bits.
        """
        # Normalize to unsigned for correct XOR comparison
        h1 = PerceptualHasher.to_unsigned(hash1)
        h2 = PerceptualHasher.to_unsigned(hash2)
        xor = h1 ^ h2
        return bin(xor).count("1")

    def is_similar(
        self,
        hash1: int,
        hash2: int,
        threshold: int = 10,
    ) -> bool:
        """
        Check if two hashes are similar.

        Args:
            hash1: First hash value.
            hash2: Second hash value.
            threshold: Maximum Hamming distance for similarity.

        Returns:
            True if similar (distance <= threshold).
        """
        return self.hamming_distance(hash1, hash2) <= threshold

    def find_similar(
        self,
        query_hash: int,
        candidate_hashes: list[tuple[Any, int]],
        threshold: int = 10,
    ) -> list[tuple[Any, int, int]]:
        """
        Find similar hashes from a list of candidates.

        Args:
            query_hash: Hash to search for.
            candidate_hashes: List of (id, hash) tuples.
            threshold: Maximum Hamming distance.

        Returns:
            List of (id, hash, distance) tuples for matches,
            sorted by distance.
        """
        matches = []

        for candidate_id, candidate_hash in candidate_hashes:
            distance = self.hamming_distance(query_hash, candidate_hash)
            if distance <= threshold:
                matches.append((candidate_id, candidate_hash, distance))

        # Sort by distance (closest first)
        matches.sort(key=lambda x: x[2])
        return matches


# Convenience function
def compute_phash(image: np.ndarray | Image.Image, hash_size: int = 8) -> int:
    """
    Compute perceptual hash of an image.

    Args:
        image: Input image.
        hash_size: Hash size (default 8 = 64 bits).

    Returns:
        Integer hash value.
    """
    hasher = PerceptualHasher(hash_size=hash_size)
    return hasher.compute_hash(image)
