"""Public chat catalog must hide names that are active in non-chat scopes."""

from __future__ import annotations

from types import SimpleNamespace

from backend.core.agents import config_compat


def _chat_rows():
    return [
        SimpleNamespace(
            name="glm-5.2",
            display_name="GLM",
            supports_vision=False,
            supports_thinking=False,
        ),
        SimpleNamespace(
            name="qwen3.6-35b",
            display_name="Qwen",
            supports_vision=True,
            supports_thinking=False,
        ),
    ]


def test_public_catalog_excludes_dual_scope_names(monkeypatch):
    monkeypatch.setattr(
        config_compat,
        "_fetch_active_chat_llm_models",
        _chat_rows,
    )
    monkeypatch.setattr(
        config_compat,
        "_fetch_active_non_chat_model_names",
        lambda: frozenset({"qwen3.6-35b"}),
    )
    info = config_compat.get_available_model_info()
    assert [model["name"] for model in info] == ["glm-5.2"]


def test_public_catalog_keeps_pure_chat_names(monkeypatch):
    monkeypatch.setattr(
        config_compat,
        "_fetch_active_chat_llm_models",
        _chat_rows,
    )
    monkeypatch.setattr(
        config_compat,
        "_fetch_active_non_chat_model_names",
        lambda: frozenset(),
    )
    info = config_compat.get_available_model_info()
    assert {model["name"] for model in info} == {"glm-5.2", "qwen3.6-35b"}
