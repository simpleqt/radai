"""双通道读数与融合（M-03 / T2-03）。

通道 A：OCR 单元格裁剪图 → 正则「数字(可带 1 位小数)」
通道 B：image_analysis 模型（ChatImageAnalysisTool 同款，usage_scope="image_analysis"）
融合：fuse 是**纯函数**（§6.3.6 四个分支），可穷举测试：
    A == B                   → 0.95, agree
    A 空 / B 空（另一方有值） → 0.70, disagree
    A != B                   → 0.30, disagree，两值都进 issue.candidates
    双方 UNSURE/空            → 0.00, issue 待人工
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from backend.config.loggers import get_logger

logger = get_logger(__name__)

_RE_SCORE = re.compile(r"^\d{1,3}(\.\d)?$")

# 置信度阈值：低于 0.85 不得静默入库为 confirmed（§6.3.6 开发指标）
CONFIRMED_THRESHOLD = Decimal("0.85")

# 通道枚举
CHANNEL_OCR_TABLE = "ocr_table"
CHANNEL_OCR_BOX = "ocr_box"
CHANNEL_VLM = "vlm"


@dataclass(frozen=True)
class ChannelResult:
    """单通道读数结果。value 为 None 表示未读出（EMPTY/UNSURE）。"""

    channel: str
    value: Decimal | None
    raw: str = ""


@dataclass(frozen=True)
class FusedScore:
    """融合结果。confidence 0~1；status：confirmed / pending_review。"""

    value: Decimal | None
    confidence: Decimal
    channel_agree: bool
    status: str  # confirmed | pending_review
    candidates: list[str] = field(default_factory=list)


def parse_score_text(text: str) -> Decimal | None:
    """OCR 原始文本 → Decimal；非数字(可带 1 位小数)格式返回 None。"""
    stripped = (text or "").strip().replace(" ", "")
    if not _RE_SCORE.fullmatch(stripped):
        return None
    return Decimal(stripped)


def fuse(
    a: ChannelResult,
    b: ChannelResult,
    c: ChannelResult | None = None,
) -> FusedScore:
    """融合逻辑（纯函数，四个分支穷举测试）。

    c 为第三通道（大题旁独立得分框），与汇总表读同一个数：
    仅当 c 存在时启用「多数一致」——三方或两方一致取多数值，置信度升 0.97；
    无 c 时按 A/B 双通道四分支处理（§6.3.6）。
    """
    if c is not None and c.value is not None:
        values = {
            x.channel: x.value
            for x in (a, b, c)
            if x is not None and x.value is not None
        }
        if len(values) >= 2:
            from collections import Counter

            counter = Counter(values.values())
            top_value, top_count = counter.most_common(1)[0]
            if top_count >= 2:
                return FusedScore(
                    value=top_value,
                    confidence=Decimal("0.97"),
                    channel_agree=True,
                    status="confirmed",
                    candidates=[str(v) for v in values.values()],
                )

    # 双通道分支（A/B）
    if a.value is not None and b.value is not None:
        if a.value == b.value:
            return FusedScore(
                value=a.value,
                confidence=Decimal("0.95"),
                channel_agree=True,
                status="confirmed",
                candidates=[str(a.value), str(b.value)],
            )
        # A != B：两个候选值都进 issue.candidates，置信度 0.30
        return FusedScore(
            value=None,
            confidence=Decimal("0.30"),
            channel_agree=False,
            status="pending_review",
            candidates=[str(a.value), str(b.value)],
        )

    # 单侧空
    only = a.value if a.value is not None else b.value
    if only is not None:
        return FusedScore(
            value=only,
            confidence=Decimal("0.70"),
            channel_agree=False,
            status="pending_review",
            candidates=[str(only)],
        )

    # 双方 UNSURE/空
    return FusedScore(
        value=None,
        confidence=Decimal("0.00"),
        channel_agree=False,
        status="pending_review",
        candidates=[],
    )


def should_confirm(score: FusedScore) -> bool:
    """低置信度（<0.85）项 100% 进入待确认清单，绝不静默入库为 confirmed。"""
    return score.status == "confirmed" and score.confidence >= CONFIRMED_THRESHOLD
