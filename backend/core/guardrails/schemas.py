"""
数据模型定义 - 使用 Pydantic 定义各种输出格式
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# ============= OCR 相关 Schema =============


class OCRInputConfig(BaseModel):
    """OCR输入配置验证"""

    file_path: str = Field(description="文件路径", min_length=1)
    lang: str = Field(default="ch", description="语言（ch/en/korean/japan等）")
    method: str = Field(default="auto", description="解析方法（auto/txt/ocr）")
    page_range: tuple[int, int] | None = Field(
        default=None, description="页码范围（起始页，结束页）"
    )
    bbox: dict[str, Any] | None = Field(
        default=None, description="边界框坐标（用于区域识别）"
    )
    formula_enable: bool = Field(default=True, description="是否启用公式识别")
    table_enable: bool = Field(default=True, description="是否启用表格识别")

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, v):
        """验证文件路径不为空"""
        if not v or not v.strip():
            raise ValueError("文件路径不能为空")
        return v

    @field_validator("page_range")
    @classmethod
    def validate_page_range(cls, v):
        """验证页码范围"""
        if v is not None:
            if len(v) != 2:
                raise ValueError("页码范围必须包含两个整数")
            if v[0] < 0 or v[1] < 0:
                raise ValueError("页码不能为负数")
            if v[0] > v[1]:
                raise ValueError("起始页不能大于结束页")
        return v

    @field_validator("bbox")
    @classmethod
    def validate_bbox(cls, v):
        """验证边界框"""
        if v is not None:
            required_keys = ["x0", "y0", "x1", "y1", "page"]
            for key in required_keys:
                if key not in v:
                    raise ValueError(f"边界框缺少必需字段: {key}")
            if v["x0"] >= v["x1"] or v["y0"] >= v["y1"]:
                raise ValueError("边界框坐标无效")
        return v

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "file_path": "/path/to/document.pdf",
                    "lang": "ch",
                    "method": "auto",
                    "page_range": [0, 5],
                    "formula_enable": True,
                    "table_enable": True,
                }
            ]
        }
    }


class BoundingBox(BaseModel):
    """边界框"""

    x0: float = Field(description="左上角X坐标")
    y0: float = Field(description="左上角Y坐标")
    x1: float = Field(description="右下角X坐标")
    y1: float = Field(description="右下角Y坐标")
    page: int = Field(description="页码", ge=0)

    @field_validator("x1", "y1")
    @classmethod
    def validate_coordinates(cls, v, info):
        """验证坐标有效性"""
        if info.field_name == "x1":
            x0 = info.data.get("x0", 0)
            if v <= x0:
                raise ValueError("x1必须大于x0")
        if info.field_name == "y1":
            y0 = info.data.get("y0", 0)
            if v <= y0:
                raise ValueError("y1必须大于y0")
        return v

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "x0": self.x0,
            "y0": self.y0,
            "x1": self.x1,
            "y1": self.y1,
            "page": self.page,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BoundingBox":
        """从字典创建"""
        return cls(
            x0=data.get("x0", 0.0),
            y0=data.get("y0", 0.0),
            x1=data.get("x1", 0.0),
            y1=data.get("y1", 0.0),
            page=data.get("page", 0),
        )


class LayoutBlock(BaseModel):
    """布局块（符合 MinerU layout 规范）"""

    type: str = Field(description="块类型（text/table/figure/equation等）")
    bbox: BoundingBox = Field(description="边界框")
    text: str = Field(description="文本内容")
    confidence: float = Field(description="置信度", ge=0.0, le=1.0)
    page: int = Field(description="页码", ge=0)
    id: str | None = Field(default=None, description="块ID")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "type": self.type,
            "bbox": self.bbox.to_dict(),
            "text": self.text,
            "confidence": self.confidence,
            "page": self.page,
            "id": self.id,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LayoutBlock":
        """从字典创建"""
        return cls(
            type=data.get("type", "text"),
            bbox=BoundingBox.from_dict(data.get("bbox", {})),
            text=data.get("text", ""),
            confidence=data.get("confidence", 0.0),
            page=data.get("page", 0),
            id=data.get("id"),
            metadata=data.get("metadata", {}),
        )


class PageLayout(BaseModel):
    """页面布局"""

    page_num: int = Field(description="页码", ge=0)
    width: float = Field(description="页面宽度", gt=0)
    height: float = Field(description="页面高度", gt=0)
    blocks: list[LayoutBlock] = Field(default_factory=list, description="布局块列表")

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "page_num": self.page_num,
            "width": self.width,
            "height": self.height,
            "blocks": [block.to_dict() for block in self.blocks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PageLayout":
        """从字典创建"""
        return cls(
            page_num=data.get("page_num", 0),
            width=data.get("width", 0.0),
            height=data.get("height", 0.0),
            blocks=[LayoutBlock.from_dict(b) for b in data.get("blocks", [])],
        )


class DocumentLayout(BaseModel):
    """文档布局（符合 MinerU layout 规范）"""

    pages: list[PageLayout] = Field(default_factory=list, description="页面布局列表")
    total_pages: int = Field(description="总页数", ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")

    def to_dict(self) -> dict[str, Any]:
        """转换为字典"""
        return {
            "pages": [page.to_dict() for page in self.pages],
            "total_pages": self.total_pages,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DocumentLayout":
        """从字典创建"""
        return cls(
            pages=[PageLayout.from_dict(p) for p in data.get("pages", [])],
            total_pages=data.get("total_pages", 0),
            metadata=data.get("metadata", {}),
        )


class OCRResultValidation(BaseModel):
    """OCR结果验证模型"""

    task_id: str = Field(description="任务ID")
    total_pages: int = Field(description="总页数", ge=0)
    total_blocks: int = Field(description="总块数", ge=0)
    avg_confidence: float = Field(description="平均置信度", ge=0.0, le=1.0)
    min_confidence: float = Field(description="最低置信度", ge=0.0, le=1.0)
    max_confidence: float = Field(description="最高置信度", ge=0.0, le=1.0)
    layout_valid: bool = Field(description="布局是否有效")
    completeness_score: float = Field(description="完整性评分", ge=0.0, le=1.0)
    quality_score: float = Field(description="质量评分", ge=0.0, le=1.0)
    issues: list[str] = Field(default_factory=list, description="问题列表")
    warnings: list[str] = Field(default_factory=list, description="警告列表")

    @field_validator("avg_confidence", "completeness_score", "quality_score")
    @classmethod
    def validate_scores(cls, v):
        """验证评分范围"""
        if not 0.0 <= v <= 1.0:
            raise ValueError("评分必须在0到1之间")
        return v


# ============= LLM 交互相关 Schema =============


class LLMRequestSchema(BaseModel):
    """LLM请求验证"""

    prompt: str = Field(description="提示词", min_length=1)
    max_tokens: int | None = Field(default=None, description="最大token数", gt=0)
    temperature: float = Field(default=0.7, description="温度参数", ge=0.0, le=2.0)
    top_p: float = Field(default=1.0, description="top_p参数", ge=0.0, le=1.0)
    context: dict[str, Any] | None = Field(default=None, description="上下文信息")

    @field_validator("prompt")
    @classmethod
    def validate_prompt(cls, v):
        """验证提示词"""
        if not v or not v.strip():
            raise ValueError("提示词不能为空")
        if len(v) > 100000:
            raise ValueError("提示词过长（超过100000字符）")
        return v

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "prompt": "请分析这个文档的主要内容",
                    "max_tokens": 1000,
                    "temperature": 0.7,
                    "top_p": 1.0,
                }
            ]
        }
    }


class LLMResponseSchema(BaseModel):
    """LLM响应验证"""

    content: str = Field(description="响应内容", min_length=1)
    finish_reason: str | None = Field(default=None, description="完成原因")
    usage: dict[str, int] | None = Field(default=None, description="token使用情况")
    model: str | None = Field(default=None, description="模型名称")

    @field_validator("content")
    @classmethod
    def validate_content(cls, v):
        """验证响应内容"""
        if not v or not v.strip():
            raise ValueError("响应内容不能为空")
        if len(v) > 50000:
            raise ValueError("响应内容过长（超过50000字符）")
        return v


# ============= 文档处理相关 Schema =============


class DocumentProcessingConfig(BaseModel):
    """文档处理配置"""

    file_path: str = Field(description="文件路径", min_length=1)
    processing_type: str = Field(description="处理类型（ocr/analysis/extraction等）")
    ocr_config: OCRInputConfig | None = Field(default=None, description="OCR配置")
    llm_config: LLMRequestSchema | None = Field(default=None, description="LLM配置")
    output_format: str = Field(
        default="json", description="输出格式（json/markdown等）"
    )

    @field_validator("processing_type")
    @classmethod
    def validate_processing_type(cls, v):
        """验证处理类型"""
        valid_types = ["ocr", "analysis", "extraction", "summarization"]
        if v not in valid_types:
            raise ValueError(f"处理类型必须是以下之一: {', '.join(valid_types)}")
        return v

    @field_validator("output_format")
    @classmethod
    def validate_output_format(cls, v):
        """验证输出格式"""
        valid_formats = ["json", "markdown", "text"]
        if v not in valid_formats:
            raise ValueError(f"输出格式必须是以下之一: {', '.join(valid_formats)}")
        return v


class AnalysisResultSchema(BaseModel):
    """分析结果验证"""

    task_id: str = Field(description="任务ID")
    file_name: str = Field(description="文件名")
    processing_type: str = Field(description="处理类型")
    status: str = Field(description="状态")
    result: dict[str, Any] | None = Field(default=None, description="分析结果")
    ocr_result: DocumentLayout | None = Field(default=None, description="OCR结果")
    llm_result: LLMResponseSchema | None = Field(default=None, description="LLM结果")
    metadata: dict[str, Any] = Field(default_factory=dict, description="元数据")
    created_at: datetime = Field(default_factory=datetime.now, description="创建时间")
    completed_at: datetime | None = Field(default=None, description="完成时间")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v):
        """验证状态"""
        valid_statuses = ["pending", "processing", "completed", "failed"]
        if v not in valid_statuses:
            raise ValueError(f"状态必须是以下之一: {', '.join(valid_statuses)}")
        return v


# ============= 导出所有 Schema =============

__all__ = [
    # OCR相关
    "OCRInputConfig",
    "BoundingBox",
    "LayoutBlock",
    "PageLayout",
    "DocumentLayout",
    "OCRResultValidation",
    # LLM相关
    "LLMRequestSchema",
    "LLMResponseSchema",
    # 文档处理相关
    "DocumentProcessingConfig",
    "AnalysisResultSchema",
]
