"""
Guardrails 模块 - 提供输入输出安全检查和结构化输出功能
"""

from .content_filters import ContentFilter, ContentSafetyLevel, FilterResult
from .input_validators import InputValidationResult, InputValidator
from .middleware import (
    GuardrailsMiddleware,
    add_guardrails_to_agent,
    add_guardrails_to_ocr_chain,
    create_guardrails_runnable,
    create_input_filter,
    create_ocr_filter,
    create_output_filter,
)
from .ocr_validators import OCRValidationResult, OCRValidator
from .output_validators import OutputValidationResult, OutputValidator
from .schemas import (
    AnalysisResultSchema,
    BoundingBox,
    DocumentLayout,
    DocumentProcessingConfig,
    LayoutBlock,
    LLMRequestSchema,
    LLMResponseSchema,
    OCRInputConfig,
    OCRResultValidation,
    PageLayout,
)

__all__ = [
    # Filters
    "ContentFilter",
    "ContentSafetyLevel",
    "FilterResult",
    # Validators
    "InputValidator",
    "InputValidationResult",
    "OutputValidator",
    "OutputValidationResult",
    "OCRValidator",
    "OCRValidationResult",
    # Schemas
    "OCRInputConfig",
    "BoundingBox",
    "LayoutBlock",
    "PageLayout",
    "DocumentLayout",
    "OCRResultValidation",
    "LLMRequestSchema",
    "LLMResponseSchema",
    "DocumentProcessingConfig",
    "AnalysisResultSchema",
    # Middleware
    "GuardrailsMiddleware",
    "create_guardrails_runnable",
    "create_input_filter",
    "create_output_filter",
    "create_ocr_filter",
    "add_guardrails_to_agent",
    "add_guardrails_to_ocr_chain",
]
