"""
File upload security: magic-byte validation and filename sanitization.

Prevents:
  - Extension spoofing (file claims to be PDF but is actually an EXE)
  - Path traversal (filename contains ``../``)
  - Null bytes and dangerous characters in filenames
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

MAX_FILENAME_LENGTH = 255

# Magic bytes for supported file types
# Maps content_type → list of (offset, magic_bytes) tuples
MAGIC_SIGNATURES: dict[str, list[tuple[int, bytes]]] = {
    "application/pdf": [(0, b"%PDF")],
    "image/tiff": [(0, b"II\x2a\x00"), (0, b"MM\x00\x2a")],  # Little-endian, Big-endian
    "image/tif": [(0, b"II\x2a\x00"), (0, b"MM\x00\x2a")],
    "image/png": [(0, b"\x89PNG\r\n\x1a\n")],
    "image/jpeg": [(0, b"\xff\xd8\xff")],
    "image/jpg": [(0, b"\xff\xd8\xff")],
}

# Dangerous filename patterns
_DANGEROUS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PATH_TRAVERSAL = re.compile(r'\.\.')
_WHITESPACE_COLLAPSE = re.compile(r'\s+')


def validate_file_magic(
    content: bytes,
    claimed_content_type: str,
) -> None:
    """
    Validate that file content matches its claimed MIME type via magic bytes.

    Args:
        content: Raw file bytes (at least first 16 bytes needed).
        claimed_content_type: The MIME type the client claims the file is.

    Raises:
        HTTPException 400 if magic bytes don't match the claimed type.
    """
    signatures = MAGIC_SIGNATURES.get(claimed_content_type)
    if not signatures:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported file type: {claimed_content_type}",
        )

    if len(content) < 16:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File too small to validate",
        )

    for offset, magic in signatures:
        if content[offset:offset + len(magic)] == magic:
            return  # Match found

    logger.warning(
        "File magic mismatch: claimed=%s, got first 8 bytes=%s",
        claimed_content_type,
        content[:8].hex(),
    )
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "File content does not match its claimed type. "
            "Ensure the file is a valid PDF, TIFF, PNG, or JPEG."
        ),
    )


def sanitize_filename(filename: str | None) -> str:
    """
    Sanitize an uploaded filename to prevent path traversal and injection.

    Rules:
      1. Strip Unicode control characters and normalize to NFKC
      2. Remove path components (keep only the basename)
      3. Replace dangerous characters with underscores
      4. Collapse whitespace
      5. Truncate to MAX_FILENAME_LENGTH
      6. Ensure a non-empty result

    Args:
        filename: The original filename from the upload.

    Returns:
        A safe filename string.
    """
    if not filename:
        return "unnamed_fax"

    # Normalize Unicode
    name = unicodedata.normalize("NFKC", filename)

    # Remove null bytes
    name = name.replace("\x00", "")

    # Keep only the basename (no directory components)
    name = os.path.basename(name)

    # Remove path traversal attempts
    name = _PATH_TRAVERSAL.sub("", name)

    # Replace dangerous characters
    name = _DANGEROUS_CHARS.sub("_", name)

    # Collapse whitespace
    name = _WHITESPACE_COLLAPSE.sub("_", name).strip("_. ")

    # Truncate
    if len(name) > MAX_FILENAME_LENGTH:
        # Preserve extension
        base, ext = os.path.splitext(name)
        max_base = MAX_FILENAME_LENGTH - len(ext)
        name = base[:max_base] + ext

    # Ensure non-empty
    if not name or name == ".":
        return "unnamed_fax"

    return name
