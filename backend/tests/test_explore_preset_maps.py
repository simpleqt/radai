"""探索式学习预制脑图契约测试。"""

from __future__ import annotations

import asyncio

from backend.application.explore import service


def test_radiation_protection_map_uses_preset_without_llm(monkeypatch):
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("预制主题不应调用 LLM")

    monkeypatch.setattr(service, "_llm_json", fail_if_called)

    result = asyncio.run(
        service.generate_map(
            topic="辐射防护",
            kb_id="kb-test",
            max_children=6,
            subject="核电子学",
        )
    )

    assert result["source"] == "preset"
    assert result["root"]["title"] == "辐射防护"
    assert result["kb_id"] == "kb-test"
    assert len(result["children"]) == 6
    assert all(child["id"].startswith("n_") for child in result["children"])
