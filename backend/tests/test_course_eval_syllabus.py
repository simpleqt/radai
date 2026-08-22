"""Tests for LLM-based syllabus parsing (T3-01).

mock LLM 覆盖三种情况：输出合法 / 输出缺字段 / 输出非 JSON。
"""

from __future__ import annotations

import json
from decimal import Decimal

from backend.application.course_eval.extraction.syllabus_parser import (
    EvalMethodExtract,
    ImplPlanExtract,
    SyllabusExtract,
    extract_with_retry,
    validate_extract,
)

VALID_SYLLABUS = {
    "course": {
        "name": "材料力学",
        "code": "2021L1609",
        "hours": 64,
        "credit": 4,
        "nature": "专业基础课程",
        "majors": ["工程力学", "土木工程"],
        "exam_mode": "闭卷",
    },
    "score_formula": {
        "total": "过程性考评×0.40 + 期末考试×0.60",
        "regular": "课程作业×0.80 + 课堂表现×0.20",
        "regular_weight": 0.40,
        "exam_weight": 0.60,
    },
    "goals": [
        {
            "code": "目标1",
            "description": "掌握材料力学四大基本受力构件",
            "weight_homework": 15.0,
            "weight_class": 5.0,
            "weight_exam": 30.0,
        },
        {
            "code": "目标2",
            "description": "具备工程问题分析能力",
            "weight_homework": 15.0,
            "weight_class": 5.0,
            "weight_exam": 30.0,
        },
    ],
    "chapters": [
        {"no": 1, "title": "第1章绪论", "hours": 2, "goals": ["目标1", "目标2"]}
    ],
}


def test_validate_extract_valid():
    parsed, issues = validate_extract(VALID_SYLLABUS, SyllabusExtract)
    assert parsed is not None
    assert issues == []
    assert len(parsed.goals) == 2
    # Σ weight_exam == exam_weight × 100（§6.4 校验）
    assert sum(g.weight_exam for g in parsed.goals) == Decimal(60)


def test_validate_extract_missing_goals():
    data = dict(VALID_SYLLABUS, goals=[])
    parsed, issues = validate_extract(data, SyllabusExtract)
    assert parsed is not None  # schema 合法（goals 可空）
    assert any(i.rule_id == "GOALS_EMPTY" for i in issues)


def test_validate_extract_weight_mismatch():
    data = json.loads(json.dumps(VALID_SYLLABUS))
    data["goals"][0]["weight_exam"] = 20.0  # Σ=50 != 60
    parsed, issues = validate_extract(data, SyllabusExtract)
    assert any(i.rule_id == "EXAM_WEIGHT_MISMATCH" for i in issues)


def test_validate_extract_schema_invalid():
    data = dict(VALID_SYLLABUS, course={"name": 123})  # name 非 str
    parsed, issues = validate_extract(data, SyllabusExtract)
    assert parsed is None
    assert issues and issues[0].rule_id == "SCHEMA_INVALID"


async def test_extract_with_retry_valid_first_try():
    async def llm_call(_text):
        return VALID_SYLLABUS

    parsed, issues = await extract_with_retry("text", SyllabusExtract, llm_call)
    assert parsed is not None and issues == []


async def test_extract_with_retry_non_json():
    async def llm_call(_text):
        raise json.JSONDecodeError("bad json", "", 0)

    parsed, issues = await extract_with_retry("text", SyllabusExtract, llm_call)
    assert parsed is None
    assert issues and issues[0].rule_id == "LLM_OUTPUT_INVALID"


async def test_extract_with_retry_missing_fields_retries_once():
    """输出缺字段 → 校验失败 → 不允许未校验数据入库，返回 None + issue 转人工（§6.4 硬规则）。"""
    calls = 0

    async def llm_call(_text):
        nonlocal calls
        calls += 1
        return {
            "course": {"name": "材料力学"}
        }  # 缺 score_formula/goals → SCHEMA_INVALID

    parsed, issues = await extract_with_retry("text", SyllabusExtract, llm_call)
    assert parsed is None  # 未通过校验的数据不得入库
    assert calls == 2  # 「失败重试一次」（§6.4）：初次 + 重试
    assert any(i.rule_id == "SCHEMA_INVALID" for i in issues)


async def test_impl_plan_and_eval_method_schemas():
    async def llm1(_t):
        return {
            "method_options": ["方法一", "方法二"],
            "pass_line": 0.6,
            "required_appendices": ["附表1", "附表2"],
            "formula_texts": [],
        }

    parsed, issues = await extract_with_retry("t", ImplPlanExtract, llm1)
    assert parsed is not None and issues == []
    assert len(parsed.method_options) == 2

    async def llm2(_t):
        return {"method": "course_average", "weights": {"regular": 0.4, "exam": 0.6}}

    parsed2, issues2 = await extract_with_retry("t", EvalMethodExtract, llm2)
    assert parsed2 is not None and issues2 == []
