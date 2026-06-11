"""LLM 模型配置 ORM 定义"""

import enum
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer,
    JSON, SmallInteger, String, UniqueConstraint, ForeignKey,
)
from sqlalchemy.orm import relationship

from backend.db.models import Base


class ModelProvider(enum.Enum):
    """模型服务商"""
    DEEPSEEK = "deepseek"
    QWEN = "qwen"
    MINIMAX = "minimax"
    ZHIPU = "zhipu"
    OPENAI = "openai"
    CUSTOM = "custom"


class LlmModel(Base):
    """LLM 模型配置表（本期仅读写 usage_scope=chat；其他 scope 预留给知识库等）"""
    __tablename__ = "llm_models"
    __table_args__ = (
        UniqueConstraint("name", "usage_scope", name="uk_name_scope"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    usage_scope = Column(
        String(32),
        nullable=False,
        default="chat",
        comment="用途域：chat=对话；预留 kb_chat/embedding/rerank",
    )

    # 基础信息
    name = Column(String(100), nullable=False, comment="模型唯一标识（与 usage_scope 联合唯一）")
    display_name = Column(String(200), nullable=False, comment="前端显示名称")
    description = Column(String(500), nullable=True, comment="模型描述")
    provider = Column(String(50), nullable=False, default="custom", comment="服务商标识")
    icon_url = Column(String(500), nullable=True, comment="模型图标 URL")

    # LangChain 类映射
    use = Column(String(200), nullable=False, default="langchain_openai:ChatOpenAI",
                 comment="LangChain 类路径")
    model = Column(String(100), nullable=False, comment="实际模型名称")

    # API 连接配置
    api_key = Column(String(500), nullable=False, comment="API Key（加密或环境变量引用）")
    base_url = Column(String(500), nullable=False, comment="API Base URL")
    api_key_env_var = Column(String(100), nullable=True, comment="API Key 环境变量名")
    base_url_env_var = Column(String(100), nullable=True, comment="Base URL 环境变量名")

    # 模型参数
    request_timeout = Column(Float, nullable=False, default=600.0, comment="请求超时（秒）")
    max_retries = Column(SmallInteger, nullable=False, default=2, comment="最大重试次数")
    max_tokens = Column(Integer, nullable=False, default=16384, comment="最大输出 token")
    max_input_tokens = Column(
        Integer,
        nullable=False,
        default=131072,
        comment="最大输入 token（上下文窗口，默认 128K）",
    )
    temperature = Column(Float, nullable=False, default=0.1, comment="生成温度")
    top_p = Column(Float, nullable=True, comment="Top-P 采样")
    frequency_penalty = Column(Float, nullable=True, comment="频率惩罚")
    presence_penalty = Column(Float, nullable=True, comment="存在惩罚")

    # 扩展参数
    extra_body = Column(JSON, nullable=True, comment="额外请求体参数")
    when_thinking_enabled = Column(JSON, nullable=True, comment="思考模式启用时额外参数")
    thinking_config = Column(JSON, nullable=True, comment="思考模式配置")

    # 模型能力
    supports_vision = Column(Boolean, nullable=False, default=False, comment="支持图片输入")
    supports_thinking = Column(Boolean, nullable=False, default=False, comment="支持思考模式")
    supports_reasoning_effort = Column(Boolean, nullable=False, default=False,
                                       comment="支持推理努力调节")
    supports_streaming = Column(Boolean, nullable=False, default=True, comment="支持流式输出")

    # OpenAI Responses API
    use_responses_api = Column(Boolean, nullable=True, comment="走 /v1/responses API")
    output_version = Column(String(50), nullable=True, comment="结构化输出版本")

    # 状态与排序
    is_active = Column(Boolean, nullable=False, default=True, comment="是否启用")
    is_default = Column(Boolean, nullable=False, default=False, comment="是否默认模型")
    sort_order = Column(SmallInteger, nullable=False, default=0, comment="排序权重")

    # 审计
    created_at = Column(DateTime, nullable=False, default=datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)
    created_by = Column(String(36), nullable=True, comment="创建人")
    updated_by = Column(String(36), nullable=True, comment="最后修改人")

    # 关系
    access_rules = relationship("LlmModelAccess", back_populates="model",
                                cascade="all, delete-orphan")

    def resolve_api_key(self) -> str:
        """解析 API Key：``api_key`` 列中 ``$ENV`` → 环境变量；否则用列内明文。
        若仍为空，则回退 ``api_key_env_var`` 指向的环境变量。"""
        import os

        def _from_env(var_name: str) -> str | None:
            name = (var_name or "").strip()
            if not name:
                return None
            val = os.environ.get(name)
            return val if val else None

        raw = (self.api_key or "").strip()
        if raw.startswith("$"):
            env_name = raw[1:].strip()
            got = _from_env(env_name)
            if got:
                return got
            raw = ""

        if raw:
            return raw

        if self.api_key_env_var:
            got = _from_env(self.api_key_env_var)
            if got:
                return got

        return ""

    def resolve_base_url(self) -> str:
        """解析 Base URL：``$ENV`` → 环境变量；否则用列内值；空则回退 ``base_url_env_var``。"""
        import os

        def _from_env(var_name: str) -> str | None:
            name = (var_name or "").strip()
            if not name:
                return None
            val = os.environ.get(name)
            return val if val else None

        raw = (self.base_url or "").strip()
        if raw.startswith("$"):
            env_name = raw[1:].strip()
            got = _from_env(env_name)
            if got:
                return got
            raw = ""

        if raw:
            return raw

        if self.base_url_env_var:
            got = _from_env(self.base_url_env_var)
            if got:
                return got

        return ""

    def to_model_config_dict(self) -> dict:
        """转换为 factory.create_chat_model 所需的 ModelConfig 字典格式"""
        config = {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "use": self.use,
            "model": self.model,
            "api_key": self.resolve_api_key(),
            "base_url": self.resolve_base_url(),
            "request_timeout": self.request_timeout,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "max_input_tokens": self.max_input_tokens,
            "temperature": self.temperature,
            "supports_vision": self.supports_vision,
            "supports_thinking": self.supports_thinking,
            "supports_reasoning_effort": self.supports_reasoning_effort,
        }
        # 可选字段
        if self.top_p is not None:
            config["top_p"] = self.top_p
        if self.frequency_penalty is not None:
            config["frequency_penalty"] = self.frequency_penalty
        if self.presence_penalty is not None:
            config["presence_penalty"] = self.presence_penalty
        if self.extra_body:
            config["extra_body"] = self.extra_body
        if self.when_thinking_enabled:
            config["when_thinking_enabled"] = self.when_thinking_enabled
        if self.thinking_config:
            config["thinking"] = self.thinking_config
        if self.use_responses_api is not None:
            config["use_responses_api"] = self.use_responses_api
        if self.output_version:
            config["output_version"] = self.output_version
        return config

    def __repr__(self):
        return f"<LlmModel(id={self.id}, name={self.name}, provider={self.provider})>"


class LlmModelAccess(Base):
    """模型访问权限表"""
    __tablename__ = "llm_model_access"

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_id = Column(Integer, ForeignKey("llm_models.id", ondelete="CASCADE"), nullable=False, comment="关联 llm_models.id")
    user_id = Column(String(36), nullable=True, comment="用户 ID")
    role = Column(String(50), nullable=True, comment="角色名")
    is_allowed = Column(Boolean, nullable=False, default=True, comment="是否允许")
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    model = relationship("LlmModel", back_populates="access_rules")

    def __repr__(self):
        return f"<LlmModelAccess(model_id={self.model_id}, user_id={self.user_id})>"