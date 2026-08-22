"""Tests for score extraction fusion logic (T2-03).

fuse 是纯函数，四个分支穷举测试：一致/单侧空/冲突/双空，
以及第三通道多数一致、低置信度不静默入库。
"""

from __future__ import annotations

from decimal import Decimal

from backend.application.course_eval.extraction.paper_score_extractor import (
    CHANNEL_OCR_TABLE,
    CHANNEL_VLM,
    ChannelResult,
    FusedScore,
    fuse,
    parse_score_text,
    should_confirm,
)


def _ocr(value: str | None) -> ChannelResult:
    return ChannelResult(
        channel=CHANNEL_OCR_TABLE,
        value=parse_score_text(value) if value else None,
        raw=value or "",
    )


def _vlm(value: str | None) -> ChannelResult:
    return ChannelResult(
        channel=CHANNEL_VLM,
        value=parse_score_text(value) if value else None,
        raw=value or "",
    )


def test_parse_score_text():
    assert parse_score_text("16") == Decimal("16")
    assert parse_score_text(" 16.5 ") == Decimal("16.5")
    assert parse_score_text("ab") is None
    assert parse_score_text("16.55") is None  # 超一位小数


def test_fuse_agree_both():
    """A == B → 0.95, agree=True, confirmed（§6.3.6 分支 1）。"""
    result = fuse(_ocr("16"), _vlm("16"))
    assert result.value == Decimal("16")
    assert result.confidence == Decimal("0.95")
    assert result.channel_agree is True
    assert result.status == "confirmed"


def test_fuse_single_side_empty():
    """A 空 / B 空（另一方有值）→ 0.70, disagree, pending_review（分支 2）。"""
    result = fuse(_ocr(None), _vlm("16"))
    assert result.value == Decimal("16")
    assert result.confidence == Decimal("0.70")
    assert result.channel_agree is False
    assert result.status == "pending_review"

    result2 = fuse(_ocr("16"), _vlm(None))
    assert result2.value == Decimal("16")
    assert result2.confidence == Decimal("0.70")


def test_fuse_conflict_keeps_candidates():
    """A != B → 0.30, disagree, 两个候选值都进 issue.candidates（分支 3）。"""
    result = fuse(_ocr("16"), _vlm("18"))
    assert result.value is None
    assert result.confidence == Decimal("0.30")
    assert result.channel_agree is False
    assert result.status == "pending_review"
    assert result.candidates == ["16", "18"]


def test_fuse_both_empty():
    """双方 UNSURE/空 → 0.00, issue 待人工录入（分支 4）。"""
    result = fuse(_ocr(None), _vlm(None))
    assert result.value is None
    assert result.confidence == Decimal("0.00")
    assert result.status == "pending_review"
    assert result.candidates == []


def test_fuse_third_channel_majority():
    """第三通道（得分框）与一方一致 → 多数一致，置信度升 0.97。"""
    box = ChannelResult(channel="ocr_box", value=Decimal("16"))
    result = fuse(_ocr("16"), _vlm("18"), box)
    assert result.value == Decimal("16")
    assert result.confidence == Decimal("0.97")
    assert result.channel_agree is True
    assert result.status == "confirmed"


def test_should_confirm_gates_low_confidence():
    """低置信度（<0.85）项 100% 进入待确认清单（§6.3.6 开发指标）。"""
    assert should_confirm(fuse(_ocr("16"), _vlm("16"))) is True  # 0.95
    assert should_confirm(fuse(_ocr("16"), _vlm("18"))) is False  # 0.30 冲突
    assert should_confirm(fuse(_ocr(None), _vlm("16"))) is False  # 0.70 单侧空
    assert should_confirm(fuse(_ocr(None), _vlm(None))) is False  # 0.00 双空


def test_fuse_is_pure():
    """fuse 必须是纯函数：同输入同输出，无状态。"""
    a = _ocr("16")
    b = _vlm("16")
    assert fuse(a, b) == fuse(a, b)
    assert isinstance(fuse(a, b), FusedScore)
