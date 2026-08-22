"""轻量智能体按域选择提示词（NE / CSH）契约测试。"""

from __future__ import annotations

from backend.application.agent_executors.lightweight_qa_agent import (
    _build_lightweight_system_header_and_template,
    _clean_question,
    _get_lightweight_search_prefix,
    _is_table_analysis_query,
    _summarize_kb_for_process_ui,
)


def test_csh_lightweight_system_header_uses_hydrogeology_role():
    header, template = _build_lightweight_system_header_and_template(
        prompt_domain="csh",
        use_search=True,
        lightweight_has_kb=True,
    )
    assert "场地污染" in header or "水文地质" in header
    assert "核电子学智能体（轻量问答）" not in header
    assert "核辐射与放射性勘查" not in template
    assert "地下水" in template or "渗透" in template or "GB/T" in template


def test_ne_lightweight_system_header_uses_nuclear_role():
    header, template = _build_lightweight_system_header_and_template(
        prompt_domain="ne",
        use_search=False,
        lightweight_has_kb=False,
    )
    assert "核电子学" in header
    assert "<lightweight_answer_template>" in template
    assert "系统原理图" in template
    assert "场地污染" not in header


def test_search_prefix_differs_by_domain():
    ne_prefix = _get_lightweight_search_prefix("ne")
    csh_prefix = _get_lightweight_search_prefix("csh")
    assert "核电子学" in ne_prefix
    assert "水文地质" in csh_prefix or "地下水" in csh_prefix
    assert "等离子体" not in csh_prefix or "勿使用" in csh_prefix


def test_kb_process_summary_lists_fragment_titles_not_raw_rules():
    raw = """## 检索到的上下文 (来自 1 个活跃知识库)

### 片段 1（来自：地下水污染运移模拟教材）

地下水污染运移是指...

## 本轮可用图片白名单（仅允许使用以下标识符）

- [IMAGE:abc] | 图注：示意图

> **强制规则**：
> 1. 禁止伪造图片
"""
    summary = _summarize_kb_for_process_ui(raw)
    assert "地下水污染运移模拟教材" in summary
    assert "强制规则" not in summary
    assert "[IMAGE:abc]" not in summary


def test_lightweight_table_query_detects_uploaded_table_intent():
    table_files = [{"file_id": "uf_1", "file_name": "sales.xlsx"}]

    assert _is_table_analysis_query("按月份统计销售额", table_files)
    assert _is_table_analysis_query("总结一下", table_files)
    assert not _is_table_analysis_query("你好", table_files)
    assert not _is_table_analysis_query("按月份统计销售额", [])


def test_clean_question_removes_runtime_table_file_hint():
    question = """帮我分析一下

[用户已上传可分析表格文件]
- file_id="uf_1", 文件名="sales.xlsx"
如需分析表格，请优先使用工具。

<runtime_current_time>2026-06-04</runtime_current_time>
"""

    cleaned = _clean_question(question)
    assert "file_id" not in cleaned
    assert "可分析表格文件" not in cleaned
    assert "帮我分析一下" in cleaned
