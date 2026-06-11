"""
OCR专用验证器 - OCR结果质量评估和验证
"""

from dataclasses import dataclass
from typing import Any

from .schemas import DocumentLayout, OCRResultValidation, PageLayout


@dataclass
class OCRValidationResult:
    """OCR验证结果"""

    is_valid: bool
    quality_score: float
    completeness_score: float
    layout_valid: bool
    errors: list[str]
    warnings: list[str]
    metadata: dict[str, Any]


class OCRValidator:
    """OCR专用验证器"""

    def __init__(
        self,
        min_confidence: float = 0.5,
        min_blocks_per_page: int = 5,
        max_empty_pages: int = 0,
        validate_layout: bool = True,
        check_bbox_validity: bool = True,
        check_text_quality: bool = True,
    ):
        """
        初始化OCR验证器

        Args:
            min_confidence: 最低置信度阈值
            min_blocks_per_page: 每页最少块数
            max_empty_pages: 最大空白页数
            validate_layout: 是否验证布局
            check_bbox_validity: 是否检查边界框有效性
            check_text_quality: 是否检查文本质量
        """
        self.min_confidence = min_confidence
        self.min_blocks_per_page = min_blocks_per_page
        self.max_empty_pages = max_empty_pages
        self.validate_layout = validate_layout
        self.check_bbox_validity = check_bbox_validity
        self.check_text_quality = check_text_quality

    def validate_ocr_result(
        self,
        ocr_result: dict[str, Any],
    ) -> OCRValidationResult:
        """
        验证OCR结果

        Args:
            ocr_result: OCR结果字典

        Returns:
            OCRValidationResult: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        # 1. 检查必需字段
        required_fields = ["task_id", "status", "file_name"]
        for field in required_fields:
            if field not in ocr_result:
                errors.append(f"OCR结果缺少必需字段: {field}")

        if errors:
            return OCRValidationResult(
                is_valid=False,
                quality_score=0.0,
                completeness_score=0.0,
                layout_valid=False,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        # 2. 检查状态
        if ocr_result["status"] != "completed":
            errors.append(f"OCR任务未完成，状态: {ocr_result['status']}")
            return OCRValidationResult(
                is_valid=False,
                quality_score=0.0,
                completeness_score=0.0,
                layout_valid=False,
                errors=errors,
                warnings=warnings,
                metadata=metadata,
            )

        # 3. 验证文档布局
        layout_valid = True
        if "layout_pdf" in ocr_result:
            layout_result = self.validate_document_layout(ocr_result["layout_pdf"])
            layout_valid = layout_result["is_valid"]
            errors.extend(layout_result["errors"])
            warnings.extend(layout_result["warnings"])
            metadata.update(layout_result["metadata"])

        # 4. 计算质量评分
        quality_score = self._calculate_quality_score(ocr_result, metadata)
        completeness_score = self._calculate_completeness_score(ocr_result, metadata)

        # 5. 返回结果
        is_valid = len(errors) == 0 and quality_score >= 0.5

        return OCRValidationResult(
            is_valid=is_valid,
            quality_score=quality_score,
            completeness_score=completeness_score,
            layout_valid=layout_valid,
            errors=errors,
            warnings=warnings,
            metadata=metadata,
        )

    def validate_document_layout(
        self,
        document_layout: dict[str, Any],
    ) -> dict[str, Any]:
        """
        验证文档布局

        Args:
            document_layout: 文档布局字典

        Returns:
            Dict: 验证结果
        """
        errors: list[str] = []
        warnings: list[str] = []
        metadata: dict[str, Any] = {}

        try:
            # 使用Pydantic验证文档布局
            layout = DocumentLayout(**document_layout)

            # 1. 检查页面数量
            if len(layout.pages) != layout.total_pages:
                errors.append(
                    f"页面数量({len(layout.pages)})与总页数({layout.total_pages})不一致"
                )

            # 2. 检查每个页面
            empty_pages = 0
            total_blocks = 0
            total_confidence = 0.0
            block_count = 0

            for i, page in enumerate(layout.pages):
                # 检查页面尺寸
                if page.width <= 0 or page.height <= 0:
                    errors.append(f"页面{i}的尺寸无效: {page.width}x{page.height}")

                # 检查块数量
                if len(page.blocks) < self.min_blocks_per_page:
                    warnings.append(f"页面{i}的块数过少: {len(page.blocks)}")

                if len(page.blocks) == 0:
                    empty_pages += 1

                total_blocks += len(page.blocks)

                # 检查每个块
                for j, block in enumerate(page.blocks):
                    block_count += 1
                    total_confidence += block.confidence

                    # 检查置信度
                    if block.confidence < self.min_confidence:
                        warnings.append(
                            f"页面{i}块{j}的置信度过低: {block.confidence:.2f}"
                        )

                    # 检查文本
                    if self.check_text_quality:
                        text_result = self._check_text_quality(block.text)
                        if not text_result["is_valid"]:
                            warnings.append(f"页面{i}块{j}: {text_result['warning']}")

                    # 检查边界框
                    if self.check_bbox_validity:
                        bbox_result = self._check_bbox_validity(block.bbox, page)
                        if not bbox_result["is_valid"]:
                            errors.append(f"页面{i}块{j}: {bbox_result['error']}")

            # 3. 检查空白页
            if empty_pages > self.max_empty_pages:
                errors.append(f"空白页过多: {empty_pages}/{layout.total_pages}")

            # 4. 计算统计信息
            metadata["total_blocks"] = total_blocks
            metadata["empty_pages"] = empty_pages
            metadata["avg_blocks_per_page"] = (
                total_blocks / layout.total_pages if layout.total_pages > 0 else 0
            )
            metadata["avg_confidence"] = (
                total_confidence / block_count if block_count > 0 else 0.0
            )

            # 5. 检查块类型分布
            block_types = {}
            for page in layout.pages:
                for block in page.blocks:
                    block_type = block.type
                    block_types[block_type] = block_types.get(block_type, 0) + 1

            metadata["block_types"] = block_types

            # 6. 检查布局一致性
            if self.validate_layout:
                layout_consistency = self._check_layout_consistency(layout)
                if not layout_consistency["is_valid"]:
                    warnings.extend(layout_consistency["warnings"])
                metadata["layout_consistency"] = layout_consistency

            is_valid = len(errors) == 0

            return {
                "is_valid": is_valid,
                "errors": errors,
                "warnings": warnings,
                "metadata": metadata,
            }

        except Exception as e:
            errors.append(f"文档布局验证失败: {str(e)}")
            return {
                "is_valid": False,
                "errors": errors,
                "warnings": warnings,
                "metadata": metadata,
            }

    def _calculate_quality_score(
        self,
        ocr_result: dict[str, Any],
        metadata: dict[str, Any],
    ) -> float:
        """
        计算质量评分

        Args:
            ocr_result: OCR结果
            metadata: 元数据

        Returns:
            float: 质量评分（0-1）
        """
        score = 1.0

        # 1. 基于平均置信度
        avg_confidence = metadata.get("avg_confidence", 0.0)
        score *= avg_confidence

        # 2. 基于空白页比例
        empty_pages = metadata.get("empty_pages", 0)
        total_pages = ocr_result.get("total_pages", 1)
        empty_ratio = empty_pages / total_pages if total_pages > 0 else 0
        score *= 1.0 - empty_ratio * 0.5

        # 3. 基于块数量
        avg_blocks = metadata.get("avg_blocks_per_page", 0)
        if avg_blocks < self.min_blocks_per_page:
            score *= 0.5

        # 4. 基于布局一致性
        layout_consistency = metadata.get("layout_consistency", {})
        consistency_score = layout_consistency.get("score", 1.0)
        score *= consistency_score

        return max(0.0, min(1.0, score))

    def _calculate_completeness_score(
        self,
        ocr_result: dict[str, Any],
        metadata: dict[str, Any],
    ) -> float:
        """
        计算完整性评分

        Args:
            ocr_result: OCR结果
            metadata: 元数据

        Returns:
            float: 完整性评分（0-1）
        """
        score = 1.0

        # 1. 检查是否所有页面都有数据
        total_pages = ocr_result.get("total_pages", 0)
        pages = ocr_result.get("pages", [])
        if len(pages) != total_pages:
            score *= 0.5

        # 2. 检查是否有必需的块类型
        block_types = metadata.get("block_types", {})
        if "text" not in block_types:
            score *= 0.8

        # 3. 检查总块数
        total_blocks = metadata.get("total_blocks", 0)
        if total_blocks == 0:
            score = 0.0

        return max(0.0, min(1.0, score))

    def _check_text_quality(self, text: str) -> dict[str, Any]:
        """
        检查文本质量

        Args:
            text: 文本内容

        Returns:
            Dict: 检查结果
        """
        if not text or not text.strip():
            return {"is_valid": False, "warning": "文本为空"}

        # 检查文本长度
        if len(text.strip()) < 2:
            return {"is_valid": False, "warning": "文本过短"}

        # 检查特殊字符比例
        special_chars = sum(1 for c in text if not c.isalnum() and not c.isspace())
        special_ratio = special_chars / len(text) if len(text) > 0 else 0

        if special_ratio > 0.5:
            return {
                "is_valid": False,
                "warning": f"特殊字符比例过高: {special_ratio:.2%}",
            }

        return {"is_valid": True, "warning": None}

    def _check_bbox_validity(
        self,
        bbox: Any,
        page: PageLayout,
    ) -> dict[str, Any]:
        """
        检查边界框有效性

        Args:
            bbox: 边界框
            page: 页面布局

        Returns:
            Dict: 检查结果
        """
        # 检查坐标有效性
        if bbox.x0 >= bbox.x1:
            return {"is_valid": False, "error": f"x0({bbox.x0})必须小于x1({bbox.x1})"}

        if bbox.y0 >= bbox.y1:
            return {"is_valid": False, "error": f"y0({bbox.y0})必须小于y1({bbox.y1})"}

        # 检查坐标是否为负数
        if bbox.x0 < 0 or bbox.y0 < 0:
            return {
                "is_valid": False,
                "error": f"坐标不能为负数: ({bbox.x0}, {bbox.y0})",
            }

        # 检查是否超出页面范围
        if bbox.x1 > page.width or bbox.y1 > page.height:
            return {
                "is_valid": False,
                "error": f"边界框超出页面范围: 页面{page.width}x{page.height}, 边界框({bbox.x1}, {bbox.y1})",
            }

        return {"is_valid": True, "error": None}

    def _check_layout_consistency(
        self,
        layout: DocumentLayout,
    ) -> dict[str, Any]:
        """
        检查布局一致性

        Args:
            layout: 文档布局

        Returns:
            Dict: 检查结果
        """
        warnings = []
        score = 1.0

        if len(layout.pages) == 0:
            return {"is_valid": True, "warnings": [], "score": 1.0}

        # 1. 检查页面尺寸一致性
        widths = [page.width for page in layout.pages]
        heights = [page.height for page in layout.pages]

        width_std = sum(widths) / len(widths)
        height_std = sum(heights) / len(heights)

        width_var = sum((w - width_std) ** 2 for w in widths) / len(widths)
        height_var = sum((h - height_std) ** 2 for h in heights) / len(heights)

        if width_var > width_std**2 * 0.1:
            warnings.append("页面宽度差异较大")
            score *= 0.9

        if height_var > height_std**2 * 0.1:
            warnings.append("页面高度差异较大")
            score *= 0.9

        # 2. 检查块类型分布一致性
        block_types_per_page = []
        for page in layout.pages:
            types = set(block.type for block in page.blocks)
            block_types_per_page.append(types)

        # 检查是否有页面缺少常见块类型
        all_types = set()
        for types in block_types_per_page:
            all_types.update(types)

        for i, types in enumerate(block_types_per_page):
            missing_types = all_types - types
            if missing_types and len(types) > 0:
                warnings.append(f"页面{i}缺少块类型: {', '.join(missing_types)}")
                score *= 0.95

        return {
            "is_valid": True,
            "warnings": warnings,
            "score": max(0.0, min(1.0, score)),
        }

    def create_validation_report(
        self,
        ocr_result: dict[str, Any],
    ) -> OCRResultValidation:
        """
        创建验证报告

        Args:
            ocr_result: OCR结果

        Returns:
            OCRResultValidation: 验证报告
        """
        validation_result = self.validate_ocr_result(ocr_result)

        # 计算置信度统计
        metadata = validation_result.metadata
        avg_confidence = metadata.get("avg_confidence", 0.0)
        total_blocks = metadata.get("total_blocks", 0)

        # 估算最小和最大置信度
        min_confidence = 1.0
        max_confidence = 0.0

        if "layout_pdf" in ocr_result:
            layout = ocr_result["layout_pdf"]
            if "pages" in layout:
                for page in layout["pages"]:
                    if "blocks" in page:
                        for block in page["blocks"]:
                            conf = block.get("confidence", 0.0)
                            min_confidence = min(min_confidence, conf)
                            max_confidence = max(max_confidence, conf)

        return OCRResultValidation(
            task_id=ocr_result.get("task_id", ""),
            total_pages=ocr_result.get("total_pages", 0),
            total_blocks=total_blocks,
            avg_confidence=avg_confidence,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            layout_valid=validation_result.layout_valid,
            completeness_score=validation_result.completeness_score,
            quality_score=validation_result.quality_score,
            issues=validation_result.errors,
            warnings=validation_result.warnings,
        )


# 创建默认验证器实例
default_validator = OCRValidator()
strict_validator = OCRValidator(min_confidence=0.7, min_blocks_per_page=10)
quality_validator = OCRValidator(
    min_confidence=0.5, validate_layout=True, check_bbox_validity=True
)
