"""Tests for narrative drafting (T3-05).

assert_no_digits 拦截 LLM 输出中的数字，违反则重试；
重试耗尽仍含数字则拒绝草稿（不允许带数字的文字进入报告）。
"""

from __future__ import annotations

import pytest

from backend.application.course_eval.report.narrative_drafter import (
    assert_no_digits,
    draft_with_retry,
)


def test_assert_no_digits_accepts_clean_text():
    assert assert_no_digits("部分学生基础知识掌握不牢，需加强练习") is True


def test_assert_no_digits_rejects_digits():
    assert assert_no_digits("得分率为四成") is True  # 中文数字不算
    assert assert_no_digits("得分率 42%") is False
    assert assert_no_digits("平均分 52.88") is False


async def test_draft_with_retry_clean_first():
    async def llm_call(_key, _ctx):
        return "试卷整体难度适中，需关注作图题的正确率"

    draft = await draft_with_retry("problems", "context", llm_call)
    assert "试卷整体难度适中" in draft


async def test_draft_with_retry_digits_retried_then_ok():
    calls = 0

    async def llm_call(_key, _ctx):
        nonlocal calls
        calls += 1
        if calls == 1:
            return "得分率 42%，需改进"  # 含数字，触发重试
        return "部分大题得分率偏低，建议加强针对性训练"

    draft = await draft_with_retry("problems", "context", llm_call)
    assert calls == 2
    assert "得分率偏低" in draft


async def test_draft_with_retry_fails_after_retries():
    async def llm_call(_key, _ctx):
        return "得分率 42%"  # 始终含数字

    with pytest.raises(ValueError, match="连续"):
        await draft_with_retry("problems", "context", llm_call)
