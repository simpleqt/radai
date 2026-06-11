"""
输入验证器 - 验证用户输入的合法性和安全性
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .content_filters import ContentFilter, ContentSafetyLevel
from .schemas import DocumentProcessingConfig, LLMRequestSchema, OCRInputConfig


@dataclass
class InputValidationResult:
    """输入验证结果"""

    is_valid: bool
    filtered_input: Any
    errors: list[str]
    warnings: list[str]
    metadata: dict[str, Any]


class InputValidator:
    """输入验证器"""

    def __init__(
        self,
        content_filter: ContentFilter | None = None,
        min_length: int = 1,
        max_length: int = 100000,
        allow_empty: bool = False,
        strict_mode: bool = False,
        enable_file_validation: bool = True,
        max_file_size: int = 100 * 1024 * 1024,  # 100MB
    ):
        """
        初始化输入验证器

        Args:
            content_filter: 内容过滤器实例
            min_length: 最小长度
            max_length: 最大长度
            allow_empty: 是否允许空输入
            strict_mode: 严格模式（任何警告都视为错误）
            enable_file_validation: 是否启用文件验证
            max_file_size: 最大文件大小（字节）
        """
        self.content_filter = content_filter or ContentFilter()
        self.min_length = min_length
        self.max_length = max_length
        self.allow_empty = allow_empty
        self.strict_mode = strict_mode
        self.enable_file_validation = enable_file_validation
        self.max_file_size = max_file_size

    def validate(self, user_input: str) -> InputValidationResult:
        """
        验证用户输入

        Args:
            user_input: 用户输入文本

        Returns:
            InputValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        # 1. 检查空输入
        if not user_input or not user_input.strip():
            if not self.allow_empty:
                errors.append("输入不能为空")
                return InputValidationResult(
                    is_valid=False,
                    filtered_input="",
                    errors=errors,
                    warnings=warnings,
                    metadata=metadata,
                )
            else:
                return InputValidationResult(
                    is_valid=True,
                    filtered_input="",
                    errors=[],
                    warnings=["输入为空"],
                    metadata={},
                )

        # 2. 检查长度
        input_length = len(user_input)
        metadata["input_length"] = input_length

        if input_length < self.min_length:
            errors.append(f"输入长度不足（最少 {self.min_length} 字符）")

        if input_length > self.max_length:
            errors.append(f"输入长度超限（最多 {self.max_length} 字符）")

        # 3. 内容安全检查
        filter_result = self.content_filter.filter_input(user_input)
        metadata["safety_level"] = filter_result.safety_level.value
        metadata["filter_details"] = filter_result.details

        if not filter_result.is_safe:
            errors.extend(filter_result.issues)
        elif filter_result.safety_level == ContentSafetyLevel.WARNING:
            if self.strict_mode:
                errors.extend(filter_result.issues)
            else:
                warnings.extend(filter_result.issues)

        # 4. 返回结果
        is_valid = len(errors) == 0

        return InputValidationResult(
            is_valid=is_valid,
            filtered_input=filter_result.filtered_content if is_valid else user_input,
            errors=errors,
            warnings=warnings,
            metadata=metadata,
        )

    def validate_or_raise(self, user_input: str) -> str:
        """
        验证输入，如果失败则抛出异常

        Args:
            user_input: 用户输入

        Returns:
            str: 过滤后的输入

        Raises:
            ValueError: 验证失败时抛出
        """
        result = self.validate(user_input)

        if not result.is_valid:
            error_msg = "输入验证失败:\n" + "\n".join(
                f"- {err}" for err in result.errors
            )
            raise ValueError(error_msg)

        return result.filtered_input

    def validate_file_path(self, file_path: str) -> InputValidationResult:
        """
        验证文件路径

        Args:
            file_path: 文件路径

        Returns:
            InputValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        # 1. 检查路径不为空
        if not file_path or not file_path.strip():
            errors.append("文件路径不能为空")
            return InputValidationResult(
                is_valid=False,
                filtered_input="",
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        # 2. 检查路径遍历攻击
        if ".." in file_path or file_path.startswith("/"):
            warnings.append("文件路径可能存在安全风险")

        # 3. 检查文件是否存在
        path = Path(file_path)
        if not path.exists():
            errors.append(f"文件不存在: {file_path}")
            return InputValidationResult(
                is_valid=False,
                filtered_input="",
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        # 4. 检查是否为文件
        if not path.is_file():
            errors.append(f"路径不是文件: {file_path}")
            return InputValidationResult(
                is_valid=False,
                filtered_input="",
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        # 5. 检查文件大小
        if self.enable_file_validation:
            file_size = path.stat().st_size
            metadata["file_size"] = file_size
            metadata["file_size_mb"] = file_size / (1024 * 1024)

            if file_size > self.max_file_size:
                errors.append(
                    f"文件大小超限（最多 {self.max_file_size / (1024 * 1024):.0f}MB）"
                )

        # 6. 检查文件扩展名
        allowed_extensions = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp"}
        file_extension = path.suffix.lower()
        if file_extension not in allowed_extensions:
            warnings.append(f"文件扩展名 {file_extension} 可能不被支持")

        is_valid = len(errors) == 0

        return InputValidationResult(
            is_valid=is_valid,
            filtered_input=file_path,
            errors=errors,
            warnings=warnings,
            metadata=metadata,
        )

    def validate_ocr_config(self, config: dict[str, Any]) -> InputValidationResult:
        """
        验证OCR配置

        Args:
            config: OCR配置字典

        Returns:
            InputValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        try:
            # 使用Pydantic验证配置
            ocr_config = OCRInputConfig(**config)
            metadata["ocr_config"] = ocr_config.model_dump()

            # 验证文件路径
            file_result = self.validate_file_path(ocr_config.file_path)
            if not file_result.is_valid:
                errors.extend(file_result.errors)
            warnings.extend(file_result.warnings)

            is_valid = len(errors) == 0

            return InputValidationResult(
                is_valid=is_valid,
                filtered_input=ocr_config,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        except Exception as e:
            errors.append(f"OCR配置验证失败: {str(e)}")
            return InputValidationResult(
                is_valid=False,
                filtered_input=None,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

    def validate_llm_request(self, request: dict[str, Any]) -> InputValidationResult:
        """
        验证LLM请求

        Args:
            request: LLM请求字典

        Returns:
            InputValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        try:
            # 使用Pydantic验证请求
            llm_request = LLMRequestSchema(**request)
            metadata["llm_request"] = llm_request.model_dump()

            # 验证提示词
            prompt_result = self.validate(llm_request.prompt)
            if not prompt_result.is_valid:
                errors.extend(prompt_result.errors)
            warnings.extend(prompt_result.warnings)

            is_valid = len(errors) == 0

            return InputValidationResult(
                is_valid=is_valid,
                filtered_input=llm_request,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        except Exception as e:
            errors.append(f"LLM请求验证失败: {str(e)}")
            return InputValidationResult(
                is_valid=False,
                filtered_input=None,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

    def validate_document_processing_config(
        self, config: dict[str, Any]
    ) -> InputValidationResult:
        """
        验证文档处理配置

        Args:
            config: 文档处理配置字典

        Returns:
            InputValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        try:
            # 使用Pydantic验证配置
            processing_config = DocumentProcessingConfig(**config)
            metadata["processing_config"] = processing_config.model_dump()

            # 验证文件路径
            file_result = self.validate_file_path(processing_config.file_path)
            if not file_result.is_valid:
                errors.extend(file_result.errors)
            warnings.extend(file_result.warnings)

            # 验证OCR配置
            if processing_config.ocr_config:
                ocr_result = self.validate_ocr_config(
                    processing_config.ocr_config.model_dump()
                )
                if not ocr_result.is_valid:
                    errors.extend(ocr_result.errors)
                warnings.extend(ocr_result.warnings)

            # 验证LLM配置
            if processing_config.llm_config:
                llm_result = self.validate_llm_request(
                    processing_config.llm_config.model_dump()
                )
                if not llm_result.is_valid:
                    errors.extend(llm_result.errors)
                warnings.extend(llm_result.warnings)

            is_valid = len(errors) == 0

            return InputValidationResult(
                is_valid=is_valid,
                filtered_input=processing_config,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        except Exception as e:
            errors.append(f"文档处理配置验证失败: {str(e)}")
            return InputValidationResult(
                is_valid=False,
                filtered_input=None,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )


# 创建默认验证器实例
default_validator = InputValidator()
strict_validator = InputValidator(strict_mode=True)
ocr_validator = InputValidator(enable_file_validation=True)
llm_validator = InputValidator(max_length=100000, strict_mode=False)
