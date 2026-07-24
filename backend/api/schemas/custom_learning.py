"""Request schemas for the custom-learning API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TemplateObjective(BaseModel):
    title: str
    description: str = ""
    rationale: str = ""
    checkpoints: list[str] = Field(default_factory=list)


class TemplateResource(BaseModel):
    type: str = "教材"
    title: str
    description: str = ""
    minutes: int = 0


class TemplateProject(BaseModel):
    title: str
    description: str = ""
    output: str = ""


class TemplateNode(BaseModel):
    id: str | None = None
    title: str
    subtitle: str = ""
    day: str = ""
    label: str = ""
    state: str | None = None
    status: str | None = None
    resource: str = ""
    task: str = ""
    check: str = ""


class CreatePlanRequest(BaseModel):
    template_id: str | None = None
    title: str
    subtitle: str = ""
    summary: str = ""
    domain: str = "ne"
    goal: str | None = None
    baseline: str | None = None
    duration: str = ""
    duration_days: int | None = Field(default=None, ge=1, le=365)
    daily_minutes: int = Field(default=90, ge=5, le=1440)
    depth: str = ""
    course_type: str = ""
    accent: str | None = None
    surface: str | None = None
    soft: str | None = None
    kb_ids: list[str] = Field(default_factory=list)
    objectives: list[TemplateObjective] = Field(default_factory=list)
    resources: list[TemplateResource] = Field(default_factory=list)
    projects: list[TemplateProject] = Field(default_factory=list)
    nodes: list[TemplateNode] = Field(default_factory=list)
    weekly_plan: list[str] = Field(default_factory=list)


class CompleteNodeRequest(BaseModel):
    score: int | None = Field(default=None, ge=0, le=100)
    payload: dict[str, Any] = Field(default_factory=dict)


class UpdatePlanRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=255)
    goal: str | None = None
    baseline: str | None = None
    depth: str | None = None
    duration_days: int | None = Field(default=None, ge=1, le=365)
    daily_minutes: int | None = Field(default=None, ge=5, le=1440)
    kb_ids: list[str] | None = None


class GeneratePlanRequest(BaseModel):
    goal: str
    domain: str = "ne"
    baseline: str = "unknown"
    depth: str = "analysis"
    duration_days: int = Field(default=7, ge=1, le=365)
    daily_minutes: int = Field(default=90, ge=5, le=1440)
    kb_ids: list[str] = Field(default_factory=list)
    use_learning_style: bool = False
    explore_snapshot: dict[str, Any] | None = None


class GradeQuizRequest(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)
    duration_seconds: int | None = Field(default=None, ge=0)


class QuizRequest(BaseModel):
    force: bool = False


class NextActionRequest(BaseModel):
    node_id: str | None = None
    answers: dict[str, Any] | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    prefer_materials: bool = True


class ExploreNodeRequest(BaseModel):
    max_children: int = Field(default=6, ge=3, le=8)
    top_k: int = Field(default=6, ge=3, le=15)
    model: str | None = None


class EnrichNodeRequest(BaseModel):
    force: bool = False


class AdjustPlanRequest(BaseModel):
    trigger_type: str = "user_change"
    reason: str = "用户触发计划调整"
    accept: bool = False
