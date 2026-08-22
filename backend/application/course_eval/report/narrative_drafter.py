"""定性文字起草（M-07 / T3-05）。

LLM 只写段落文字，**不得输出任何数字**（数字由渲染器从快照填入，§6.7 Prompt 硬约束）。
产出后跑 assert_no_digits 校验，违反则重试；QC-04 重复度由 quality_service 检测。
"""

from __future__ import annotations

import re

_RE_DIGIT = re.compile(r"\d")


def assert_no_digits(text: str) -> bool:
    """LLM 输出不得含数字（§1.5 第 4 条）。返回 True 表示通过。"""
    return not _RE_DIGIT.search(text)


async def draft_with_retry(
    section_key: str,
    prompt_context: str,
    llm_call,
    *,
    max_retries: int = 2,
) -> str:
    """起草单段文字：输出必须过 assert_no_digits，违反则重试。"""
    for attempt in range(max_retries + 1):
        draft = await llm_call(section_key, prompt_context)
        if assert_no_digits(draft):
            return draft
    # 重试耗尽仍含数字：拒绝该草稿（不允许带数字的文字进入报告）
    raise ValueError(f"LLM 起草「{section_key}」连续 {max_retries + 1} 次输出含数字")
