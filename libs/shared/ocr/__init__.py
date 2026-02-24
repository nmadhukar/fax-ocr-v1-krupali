"""OCR client module."""

from libs.shared.ocr.base import OcrClient, OcrResult, OcrToken
from libs.shared.ocr.paddle_client import PaddleOcrClient

__all__ = ["OcrClient", "OcrResult", "OcrToken", "PaddleOcrClient"]
