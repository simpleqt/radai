"""制度文件 LLM 结构化抽取（M-04 / T3-01）。

三类文档三个 schema，共用「抽取 → Pydantic 校验 → 失败重试一次 → 仍失败则报 issue 转人工」。
LLM 输出必须过校验才能入库，不允许未校验数据直接落库。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field, ValidationError

# ── Schema ──


class CourseInfo(BaseModel):
    name: str
    code: str | None = None
    hours: int | None = None
    credit: Decimal | None = None
    nature: str | None = None
    majors: list[str] = Field(default_factory=list)
    exam_mode: str | None = None


class GoalDraft(BaseModel):
    code: str
    description: str = ""
    graduation_req_no: str | None = None
    graduation_req_point: str | None = None
    weight_homework: Decimal | None = Field(default=None, ge=0, le=100)
    weight_class: Decimal | None = Field(default=None, ge=0, le=100)
    weight_exam: Decimal | None = Field(default=None, ge=0, le=100)


class ScoreFormula(BaseModel):
    total: str = ""
    regular: str = ""
    regular_weight: Decimal | None = None
    exam_weight: Decimal | None = None


class SyllabusExtract(BaseModel):
    """教学大纲抽取契约（§6.4）。"""

    course: CourseInfo
    score_formula: ScoreFormula
    goals: list[GoalDraft] = Field(default_factory=list)
    chapters: list[dict[str, Any]] = Field(default_factory=list)


class ImplPlanExtract(BaseModel):
    method_options: list[str] = Field(default_factory=list)
    pass_line: Decimal | None = None
    required_appendices: list[str] = Field(default_factory=list)
    formula_texts: list[str] = Field(default_factory=list)


class EvalMethodExtract(BaseModel):
    method: str = ""
    weights: dict[str, Decimal] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


# ── 抽取骨架 ──


@dataclass
class ExtractIssue:
    rule_id: str
    detail: str


def validate_extract(
    data: dict, schema: type[BaseModel]
) -> tuple[BaseModel | None, list[ExtractIssue]]:
    """Pydantic 校验 + 必填检查（§6.4：goals 非空、Σ weight_exam == exam_weight×100）。"""
    try:
        parsed = schema.model_validate(data)
    except ValidationError as exc:
        return None, [
            ExtractIssue(rule_id="SCHEMA_INVALID", detail=str(exc.errors()[:3]))
        ]

    issues: list[ExtractIssue] = []
    if isinstance(parsed, SyllabusExtract):
        if not parsed.goals:
            issues.append(ExtractIssue(rule_id="GOALS_EMPTY", detail="goals 非空"))
        exam_sum = sum((g.weight_exam or 0) for g in parsed.goals)
        expected = (parsed.score_formula.exam_weight or 0) * 100
        if parsed.score_formula.exam_weight is not None and abs(
            exam_sum - expected
        ) > Decimal("0.5"):
            issues.append(
                ExtractIssue(
                    rule_id="EXAM_WEIGHT_MISMATCH",
                    detail=f"Σ weight_exam={exam_sum} != exam_weight×100={expected}",
                )
            )
    return parsed, issues


async def extract_with_retry(
    text: str,
    schema: type[BaseModel],
    llm_call,
    *,
    retries: int = 1,
) -> tuple[BaseModel | None, list[ExtractIssue]]:
    """LLM 抽取 → 校验 → 失败重试一次 → 仍失败报 issue 转人工（§6.4 硬规则）。

    llm_call: async (text) -> dict（返回 JSON 解析后的 dict）
    """
    last_issues: list[ExtractIssue] = []
    for attempt in range(retries + 1):
        try:
            raw = await llm_call(text)
            parsed, issues = validate_extract(raw, schema)
            if parsed is not None and not issues:
                return parsed, []
            last_issues = issues
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            last_issues = [
                ExtractIssue(
                    rule_id="LLM_OUTPUT_INVALID", detail=f"attempt {attempt + 1}: {exc}"
                )
            ]
    return None, last_issues
