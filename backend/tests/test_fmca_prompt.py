"""Knowledge-base prompt contracts for the fundamental mechanics agent."""

from __future__ import annotations

import pytest

from backend.application.agents.prompt_templates.fmca import build_system_prompt


@pytest.mark.parametrize("lightweight_mode", [False, True])
def test_fmca_bound_prompt_never_claims_knowledge_base_is_unavailable(
    lightweight_mode: bool,
):
    prompt = build_system_prompt(
        use_search=False,
        lightweight_mode=lightweight_mode,
        lightweight_has_kb=True,
    )

    assert "已选择并绑定课程知识库" in prompt
    assert "知识库未绑定" not in prompt
    assert "检索功能未启用" not in prompt
    assert "![图片说明]([IMAGE:xxx])" in prompt
    assert "图片白名单" in prompt
    assert "历史对话中已经出现" in prompt
    assert "图片标识" in prompt and "表格" in prompt


def test_fmca_full_bound_prompt_requires_search_for_explicit_kb_request():
    prompt = build_system_prompt(
        use_search=False,
        lightweight_mode=False,
        lightweight_has_kb=True,
    )

    assert "必须先调用 `multi_kb_search`" in prompt
    assert "每轮最多调用 2 次且必须串行" in prompt
    assert "未检索到匹配证据" in prompt


@pytest.mark.parametrize("lightweight_mode", [False, True])
def test_fmca_unbound_prompt_prohibits_fabricated_kb_evidence(
    lightweight_mode: bool,
):
    prompt = build_system_prompt(
        use_search=False,
        lightweight_mode=lightweight_mode,
        lightweight_has_kb=False,
    )

    assert "未绑定课程知识库" in prompt
    assert "不得声称" in prompt


def test_fmca_lightweight_bound_prompt_uses_injected_evidence():
    prompt = build_system_prompt(
        use_search=True,
        lightweight_mode=True,
        lightweight_has_kb=True,
    )

    assert "服务端预检索" in prompt
    assert "检索由服务端预先执行并注入上下文" in prompt
    assert "必须先调用 `multi_kb_search`" not in prompt
