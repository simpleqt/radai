"""管理员后台 API 的请求/响应 schema(拆自 api/routers/admin.py)。"""

from __future__ import annotations

from backend.application.auth_service import API_KEY_SCOPE_FULL
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from typing import Any, Literal


class UserAdminItem(BaseModel):
    user_id: str
    username: str
    email: str | None = None
    role: str
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_login: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UserCreateResponse(UserAdminItem):
    generated_password: str | None = None
    raw_api_key: str | None = None


class UserListResponse(BaseModel):
    items: list[UserAdminItem]
    total: int
    limit: int
    offset: int


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)
    password: str | None = Field(default=None, min_length=6, max_length=256)
    email: str | None = Field(default=None, max_length=100)
    role: str = "user"
    is_active: bool = True
    create_api_key: bool = True


class UserUpdateRequest(BaseModel):
    email: str | None = Field(default=None, max_length=100)
    role: str | None = Field(default=None, max_length=20)
    is_active: bool | None = None


class ResetPasswordRequest(BaseModel):
    new_password: str | None = Field(default=None, min_length=6, max_length=256)


class ResetPasswordResponse(BaseModel):
    success: bool
    generated_password: str | None = None


class ApiKeyItem(BaseModel):
    id: int
    api_key: str
    user_id: str | None = None
    username: str | None = None
    key_name: str | None = None
    access_scope: Literal["full", "mcp_only"]
    mcp_allowed_tools: list[str] | None = None
    is_active: bool
    rate_limit: int
    created_at: datetime | None = None
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    usage_count: int


class ApiKeyListResponse(BaseModel):
    items: list[ApiKeyItem]
    total: int
    limit: int
    offset: int


class ApiKeyCreateRequest(BaseModel):
    key_name: str | None = Field(default=None, max_length=50)
    access_scope: Literal["full", "mcp_only"] = API_KEY_SCOPE_FULL
    mcp_allowed_tools: list[str] | None = None
    rate_limit: int = Field(default=100, ge=1, le=1000000)
    expires_at: datetime | None = None


class ApiKeyCreateResponse(ApiKeyItem):
    raw_api_key: str


class ApiKeyUpdateRequest(BaseModel):
    key_name: str | None = Field(default=None, max_length=50)
    access_scope: Literal["full", "mcp_only"] | None = None
    mcp_allowed_tools: list[str] | None = None
    is_active: bool | None = None
    rate_limit: int | None = Field(default=None, ge=1, le=1000000)
    expires_at: datetime | None = None


class AuditLogItem(BaseModel):
    audit_id: int
    actor_user_id: str | None = None
    actor_username: str | None = None
    action: str
    target_type: str
    target_id: str | None = None
    status: str
    method: str | None = None
    path: str | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    detail_json: dict[str, Any] | None = None
    created_at: datetime | None = None


class AuditLogListResponse(BaseModel):
    items: list[AuditLogItem]
    total: int
    limit: int
    offset: int


class TaskAdminItem(BaseModel):
    task_id: str
    doc_id: str
    filename: str | None = None
    user_id: str | None = None
    username: str | None = None
    batch_id: str | None = None
    status: str
    progress: int
    priority: int
    retry_count: int
    max_retries: int
    error: str | None = None
    error_detail: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class TaskListResponse(BaseModel):
    items: list[TaskAdminItem]
    total: int
    limit: int
    offset: int


class SystemComponent(BaseModel):
    status: str
    detail: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
