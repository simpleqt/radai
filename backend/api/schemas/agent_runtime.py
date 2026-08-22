"""Agent 运行时 API 的请求 schema(拆自 api/routers/agent_runtime.py)。"""

from __future__ import annotations

import base64
import binascii
import json
import re

from backend.core.thread_key import validate_external_thread_id
from backend.domain.agents.workflows import normalize_workflow_name
from pydantic import BaseModel, Field, model_validator
from typing import Any


class ImageAttachment(BaseModel):
    base64: str | None = Field(default=None, max_length=7_000_000)
    image_url: str | None = Field(default=None, max_length=4096)
    mime_type: str = Field(default="image/png", max_length=64)

    @model_validator(mode="after")
    def validate_image(self):
        if bool(self.base64) == bool(self.image_url):
            raise ValueError("附件必须且只能提供 base64 或 image_url")
        if not re.fullmatch(r"image/(png|jpeg|jpg|webp|gif)", self.mime_type.lower()):
            raise ValueError("附件 MIME 类型不受支持")
        if self.image_url and not self.image_url.startswith(("https://", "http://")):
            raise ValueError("image_url 仅支持 HTTP(S)；内嵌图片请使用 base64")
        if self.base64:
            try:
                decoded = base64.b64decode(self.base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("附件 base64 格式无效") from exc
            if len(decoded) > 5 * 1024 * 1024:
                raise ValueError("单个附件不能超过 5 MiB")
        return self


class AgentRuntimeQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=50_000)
    agent_code: str = Field(
        default="ne", min_length=1, max_length=64,
        description="Agent profile code, e.g. ne or csh",
    )
    thread_id: str | None = Field(default=None, min_length=1, max_length=100)
    workflow: str | None = Field(
        default=None,
        description=(
            "lightweight/full/deep_research/plan_solve/"
            "multi_agent_parallel/multi_agent_alternating"
        ),
    )
    kb_ids: list[str] | None = Field(default=None, max_length=64)
    selected_skills: list[str] | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=128)
    use_search: bool = True
    subagent_enabled: bool = False
    thinking: bool = True
    # Deep retrieval: KB searches in this request rerank the head boundary
    # band with the full-text LLM evidence judge (~+1s per search, measured
    # composite 0.674->0.706 on the cross-document hard set).
    deep_retrieval: bool = False
    attachments: list[ImageAttachment] = Field(default_factory=list, max_length=8)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def validate_payload_sizes(self):
        if self.thread_id is not None:
            validate_external_thread_id(self.thread_id)
        for label, values, max_item_length in (
            ("kb_ids", self.kb_ids or [], 128),
            ("selected_skills", self.selected_skills or [], 128),
        ):
            if any(len(str(item)) > max_item_length for item in values):
                raise ValueError(f"{label} 包含过长条目")
        if len(json.dumps(self.metadata, ensure_ascii=False, default=str)) > 65_536:
            raise ValueError("metadata 不能超过 64 KiB")
        attachment_payload = [item.model_dump(exclude_none=True) for item in self.attachments]
        if len(json.dumps(attachment_payload, ensure_ascii=False)) > 10_485_760:
            raise ValueError("attachments 不能超过 10 MiB")
        return self

    def resolve_workflow(self) -> str | None:
        explicit = (self.workflow or "").strip()
        return normalize_workflow_name(explicit) if explicit else None
