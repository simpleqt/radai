"""
输出验证器 - 验证模型输出的安全性和格式正确性
"""

from dataclasses import dataclass
from typing import Any

from .content_filters import ContentFilter, ContentSafetyLevel
from .schemas import DocumentLayout, LLMResponseSchema


@dataclass
class OutputValidationResult:
    """输出验证结果"""

    is_valid: bool
    filtered_output: Any
    errors: list[str]
    warnings: list[str]
    metadata: dict[str, Any]


class OutputValidator:
    """输出验证器"""

    def __init__(
        self,
        content_filter: ContentFilter | None = None,
        min_length: int = 1,
        max_length: int = 100000,
        check_factuality: bool = False,
        strict_mode: bool = False,
        require_sources: bool = False,
    ):
        """
        初始化输出验证器

        Args:
            content_filter: 内容过滤器
            min_length: 最小长度
            max_length: 最大长度
            check_factuality: 是否检查事实性（未实现）
            strict_mode: 严格模式
            require_sources: 是否要求引用来源（RAG场景）
        """
        self.content_filter = content_filter or ContentFilter()
        self.min_length = min_length
        self.max_length = max_length
        self.check_factuality = check_factuality
        self.strict_mode = strict_mode
        self.require_sources = require_sources

    def validate(
        self,
        output: str,
        sources: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> OutputValidationResult:
        """
        验证模型输出

        Args:
            output: 模型输出文本
            sources: 引用来源列表
            context: 额外上下文信息

        Returns:
            OutputValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}
        context = context or {}

        # 1. 检查空输出
        if not output or not output.strip():
            errors.append("输出不能为空")
            return OutputValidationResult(
                is_valid=False,
                filtered_output="",
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        # 2. 检查长度
        output_length = len(output)
        metadata["output_length"] = output_length

        if output_length < self.min_length:
            warnings.append(f"输出长度过短（少于 {self.min_length} 字符）")

        if output_length > self.max_length:
            errors.append(f"输出长度超限（超过 {self.max_length} 字符）")

        # 3. 内容安全检查
        filter_result = self.content_filter.filter_output(output)
        metadata["safety_level"] = filter_result.safety_level.value
        metadata["filter_details"] = filter_result.details

        if not filter_result.is_safe:
            errors.extend(filter_result.issues)
        elif filter_result.safety_level == ContentSafetyLevel.WARNING:
            if self.strict_mode:
                errors.extend(filter_result.issues)
            else:
                warnings.extend(filter_result.issues)

        # 4. 检查引用来源（RAG场景）
        if self.require_sources:
            if not sources or len(sources) == 0:
                errors.append("RAG回答必须包含引用来源")
            else:
                metadata["sources_count"] = len(sources)
                # 检查输出是否真的使用了来源
                if not self._check_source_usage(output, sources):
                    warnings.append("输出可能未充分使用提供的来源")

        # 5. 事实性检查（占位符，未实现）
        if self.check_factuality:
            warnings.append("事实性检查功能尚未实现")

        # 6. 返回结果
        is_valid = len(errors) == 0

        return OutputValidationResult(
            is_valid=is_valid,
            filtered_output=filter_result.filtered_content if is_valid else output,
            errors=errors,
            warnings=warnings,
            metadata=metadata,
        )

    def validate_or_raise(
        self,
        output: str,
        sources: list[str] | None = None,
        context: dict[str, Any] | None = None,
    ) -> str:
        """
        验证输出，如果失败则抛出异常

        Args:
            output: 模型输出
            sources: 引用来源
            context: 上下文

        Returns:
            str: 过滤后的输出

        Raises:
            ValueError: 验证失败时抛出
        """
        result = self.validate(output, sources, context)

        if not result.is_valid:
            error_msg = "输出验证失败:\n" + "\n".join(
                f"- {err}" for err in result.errors
            )
            raise ValueError(error_msg)

        return result.filtered_output

    def validate_ocr_result(
        self,
        ocr_result: dict[str, Any],
    ) -> OutputValidationResult:
        """
        验证OCR结果

        Args:
            ocr_result: OCR结果字典

        Returns:
            OutputValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        # 1. 检查必需字段
        required_fields = ["task_id", "status", "file_name"]
        for field in required_fields:
            if field not in ocr_result:
                errors.append(f"OCR结果缺少必需字段: {field}")

        # 2. 检查状态
        if "status" in ocr_result:
            valid_statuses = ["pending", "processing", "completed", "failed"]
            if ocr_result["status"] not in valid_statuses:
                errors.append(f"无效的OCR状态: {ocr_result['status']}")

        # 3. 检查页数
        if "total_pages" in ocr_result:
            total_pages = ocr_result["total_pages"]
            if total_pages < 0:
                errors.append("总页数不能为负数")
            metadata["total_pages"] = total_pages

        # 4. 检查页面数据
        if "pages" in ocr_result:
            pages = ocr_result["pages"]
            if not isinstance(pages, list):
                errors.append("pages字段必须是列表")
            else:
                metadata["page_count"] = len(pages)
                if len(pages) != ocr_result.get("total_pages", 0):
                    warnings.append("页面数量与总页数不一致")

        # 5. 检查layout_pdf
        if "layout_pdf" in ocr_result:
            layout_pdf = ocr_result["layout_pdf"]
            if not isinstance(layout_pdf, dict):
                errors.append("layout_pdf字段必须是字典")
            else:
                # 验证layout_pdf结构
                if "pages" in layout_pdf:
                    if not isinstance(layout_pdf["pages"], list):
                        errors.append("layout_pdf.pages必须是列表")

        # 6. 返回结果
        is_valid = len(errors) == 0

        return OutputValidationResult(
            is_valid=is_valid,
            filtered_output=ocr_result,
            errors=errors,
            warnings=warnings,
            metadata=metadata,
        )

    def validate_llm_response(
        self,
        llm_response: dict[str, Any],
    ) -> OutputValidationResult:
        """
        验证LLM响应

        Args:
            response: LLM响应字典

        Returns:
            OutputValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        try:
            # 使用Pydantic验证响应
            validated_response = LLMResponseSchema(**llm_response)
            metadata["llm_response"] = validated_response.model_dump()

            # 验证内容
            content_result = self.validate(validated_response.content)
            if not content_result.is_valid:
                errors.extend(content_result.errors)
            warnings.extend(content_result.warnings)

            # 检查finish_reason
            if validated_response.finish_reason:
                valid_reasons = ["stop", "length", "content_filter", "tool_calls"]
                if validated_response.finish_reason not in valid_reasons:
                    warnings.append(
                        f"未知的finish_reason: {validated_response.finish_reason}"
                    )

            is_valid = len(errors) == 0

            return OutputValidationResult(
                is_valid=is_valid,
                filtered_output=validated_response,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        except Exception as e:
            errors.append(f"LLM响应验证失败: {str(e)}")
            return OutputValidationResult(
                is_valid=False,
                filtered_output=None,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

    def validate_document_layout(
        self,
        document_layout: dict[str, Any],
    ) -> OutputValidationResult:
        """
        验证文档布局（符合MinerU layout规范）

        Args:
            document_layout: 文档布局字典

        Returns:
            OutputValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        try:
            # 使用Pydantic验证文档布局
            layout = DocumentLayout(**document_layout)
            metadata["document_layout"] = layout.model_dump()

            # 1. 检查页面数量
            if len(layout.pages) != layout.total_pages:
                errors.append(
                    f"页面数量({len(layout.pages)})与总页数({layout.total_pages})不一致"
                )

            # 2. 检查每个页面
            total_blocks = 0
            for i, page in enumerate(layout.pages):
                if page.page_num != i:
                    warnings.append(f"页面{i}的page_num字段不正确")

                # 检查页面尺寸
                if page.width <= 0 or page.height <= 0:
                    errors.append(f"页面{i}的尺寸无效")

                # 检查块
                total_blocks += len(page.blocks)

                # 检查每个块
                for j, block in enumerate(page.blocks):
                    if block.confidence < 0 or block.confidence > 1:
                        errors.append(f"页面{i}块{j}的置信度无效")

                    if not block.text or not block.text.strip():
                        warnings.append(f"页面{i}块{j}的文本为空")

                    # 检查边界框
                    bbox = block.bbox
                    if bbox.x0 >= bbox.x1 or bbox.y0 >= bbox.y1:
                        errors.append(f"页面{i}块{j}的边界框坐标无效")

                    if bbox.x0 < 0 or bbox.y0 < 0:
                        errors.append(f"页面{i}块{j}的边界框坐标为负数")

                    if bbox.x1 > page.width or bbox.y1 > page.height:
                        warnings.append(f"页面{i}块{j}的边界框超出页面范围")

            metadata["total_blocks"] = total_blocks
            metadata["avg_blocks_per_page"] = (
                total_blocks / layout.total_pages if layout.total_pages > 0 else 0
            )

            # 3. 检查块类型
            block_types = set()
            for page in layout.pages:
                for block in page.blocks:
                    block_types.add(block.type)

            metadata["block_types"] = list(block_types)

            valid_types = {"text", "table", "figure", "equation", "title", "list"}
            invalid_types = block_types - valid_types
            if invalid_types:
                warnings.append(f"未知的块类型: {', '.join(invalid_types)}")

            is_valid = len(errors) == 0

            return OutputValidationResult(
                is_valid=is_valid,
                filtered_output=layout,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        except Exception as e:
            errors.append(f"文档布局验证失败: {str(e)}")
            return OutputValidationResult(
                is_valid=False,
                filtered_output=None,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

    def _check_source_usage(self, output: str, sources: list[str]) -> bool:
        """
        检查输出是否使用了来源

        简单实现：检查来源中的关键词是否出现在输出中
        """
        if not sources:
            return False

        # 简单检查：至少有一个来源的部分内容出现在输出中
        for source in sources:
            # 提取来源中的关键词（简单实现）
            source_words = set(source.lower().split())
            output_words = set(output.lower().split())

            # 如果有超过 30% 的词重叠，认为使用了该来源
            if len(source_words) > 0:
                overlap = len(source_words & output_words)
                if overlap / len(source_words) > 0.3:
                    return True

        return False


# 创建默认验证器实例
default_validator = OutputValidator()
rag_validator = OutputValidator(require_sources=True)
strict_validator = OutputValidator(strict_mode=True)
ocr_validator = OutputValidator()
llm_validator = OutputValidator()
