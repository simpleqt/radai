"""OCR 服务适配器 - 多 Provider 支持

提供统一的 OCR 服务接口，支持多个 OCR 提供者（MinerU、Tesseract 等）。
支持文档解析、状态查询、结果获取等功能。
"""

from .base_ocr import BaseOCR, OCRConfig, OCRResult, OCRStatus
from .mineru_ocr import MinerUOCR
from .ocr_provider import OCRProvider, get_ocr_provider

__all__ = [
    "BaseOCR",
    "OCRResult",
    "OCRStatus",
    "OCRConfig",
    "MinerUOCR",
    "OCRProvider",
    "get_ocr_provider",
]
