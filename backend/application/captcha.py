"""Shared CAPTCHA challenge generation."""

from __future__ import annotations

import secrets


def build_captcha_text() -> tuple[str, str]:
    """Return a small arithmetic challenge and its expected answer.

    操作数取两位数：个位数算术的答案空间只有 ~19 个值，5 次尝试内
    按概率降序猜测即可 ~35% 命中（无需识图）；两位数把空间扩大到
    ~280，配合 3 次尝试上限把单 captcha 的盲猜命中压到 ~1%。
    """
    left = secrets.randbelow(90) + 10
    right = secrets.randbelow(90) + 10
    if secrets.randbelow(2) == 0:
        return f"{left} + {right} = ?", str(left + right)
    bigger = max(left, right)
    smaller = min(left, right)
    return f"{bigger} - {smaller} = ?", str(bigger - smaller)


__all__ = ["build_captcha_text"]
