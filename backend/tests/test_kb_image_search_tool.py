from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.application.agent_executors.image_url_registry import (
    get_registry,
    reset_registry,
)
from backend.application.tools.providers.kb_image_search_tool import (
    KB_IMAGE_SEARCH_TIMEOUT_SECONDS,
    _collect_image_entries,
    _effective_kb_ids,
    _render_image_whitelist,
    _search_one_kb_with_timeout,
)


def _image_chunk() -> SimpleNamespace:
    content = (
        "Image Content Analysis:\n"
        "Image Path: /files/beam-fbd.png\n"
        "Captions: 图3-1 简支梁受力图\n"
        "Media Ref: 图3-1\n"
        "Visual Analysis: 两端支座与集中力 P\n"
        "OCR Text: 简支梁\n"
    )
    return SimpleNamespace(
        file_name="基础力学-梁的内力.pdf",
        chunk_index=12,
        content=content,
    )


def test_collect_image_entries_registers_whitelist_token() -> None:
    reset_registry()
    registry = get_registry()

    entries = _collect_image_entries([_image_chunk()], registry)

    assert len(entries) == 1
    entry = entries[0]
    assert entry["token"].startswith("[IMAGE:")
    assert entry["token"].endswith("]")
    assert entry["caption"] == "图3-1 简支梁受力图"
    assert entry["media_ref"] == "图3-1"
    assert entry["source"] == "基础力学-梁的内力.pdf#12"
    assert registry.resolve(entry["token"]) == entry["url"]


def test_collect_image_entries_includes_kb_name_in_source() -> None:
    reset_registry()
    registry = get_registry()

    entries = _collect_image_entries(
        [_image_chunk()],
        registry,
        kb_name="基础力学课程",
    )

    assert entries[0]["source"] == "基础力学课程｜基础力学-梁的内力.pdf#12"


def test_effective_kb_ids_prefers_explicit_ids() -> None:
    assert _effective_kb_ids(
        ["kb-a"],
        {"configurable": {"kb_ids": ["kb-b", "kb-c"]}},
    ) == ["kb-a"]


def test_effective_kb_ids_reads_runtime_config() -> None:
    assert _effective_kb_ids(
        None,
        {"configurable": {"kb_ids": ["kb-b", "kb-c"]}},
    ) == ["kb-b", "kb-c"]


def test_effective_kb_ids_returns_none_when_absent() -> None:
    assert _effective_kb_ids(None, {"configurable": {}}) is None
    assert _effective_kb_ids(None, None) is None


def test_collect_image_entries_deduplicates_same_url() -> None:
    reset_registry()
    registry = get_registry()
    chunk = _image_chunk()

    entries = _collect_image_entries([chunk, chunk], registry)

    assert len(entries) == 1


def test_render_image_whitelist_includes_rules_and_source() -> None:
    text = _render_image_whitelist(
        [
            {
                "token": "[IMAGE:001]",
                "url": "https://example.com/a.png",
                "caption": "图1 受力图",
                "media_ref": "图1",
                "visual": "支座反力",
                "source": "教材.pdf#5",
            }
        ]
    )

    assert "[IMAGE:001]" in text
    assert "图1 受力图" in text
    assert "教材.pdf#5" in text
    assert "只能引用上方列出的" in text


def test_render_image_whitelist_empty() -> None:
    assert _render_image_whitelist([]) == ""


def test_image_search_timeout_and_concurrency_defaults() -> None:
    assert KB_IMAGE_SEARCH_TIMEOUT_SECONDS >= 5


@pytest.mark.asyncio
async def test_search_one_kb_with_timeout_returns_empty(monkeypatch) -> None:
    async def slow_search(*_args, **_kwargs):
        await asyncio.sleep(1)
        return []

    monkeypatch.setattr(
        "backend.application.tools.providers.kb_image_search_tool._search_one_kb",
        slow_search,
    )
    monkeypatch.setattr(
        "backend.application.tools.providers.kb_image_search_tool.KB_IMAGE_SEARCH_TIMEOUT_SECONDS",
        0.05,
    )

    result = await _search_one_kb_with_timeout(
        factory=None,
        entry=SimpleNamespace(kb_id="kb-a", kb_code="code-a"),
        query="受力图",
        top_k=8,
        query_tokens=[],
        query_embedding=None,
        registry=get_registry(),
        kb_name="基础力学",
    )

    assert result == []
