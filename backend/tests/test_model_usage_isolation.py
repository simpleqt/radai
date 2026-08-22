from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError


def _model_payload(**overrides):
    payload = {
        "name": "model-a",
        "display_name": "Model A",
        "model": "upstream-model-a",
        "api_key": "test-key",
        "base_url": "https://models.invalid/v1",
    }
    payload.update(overrides)
    return payload


def test_admin_model_request_requires_vision_for_image_analysis_scope():
    from backend.api.routers.models import ModelCreateRequest

    with pytest.raises(ValidationError, match="图片分析用途模型必须启用视觉能力"):
        ModelCreateRequest(**_model_payload(usage_scope="image_analysis"))

    request = ModelCreateRequest(
        **_model_payload(usage_scope="image_analysis", supports_vision=True)
    )
    assert request.usage_scope == "image_analysis"


def test_db_catalog_does_not_fall_back_to_yaml_when_empty(monkeypatch):
    from backend.core.agents import config_compat

    monkeypatch.setenv("MODEL_CONFIG_SOURCE", "db")
    monkeypatch.setattr(config_compat, "_fetch_active_chat_llm_models", lambda: [])
    monkeypatch.setattr(
        "backend.core.config.get_app_config",
        lambda: pytest.fail("DB catalog must not fall back to YAML"),
    )

    assert config_compat.get_available_model_info() == []
    assert config_compat.get_default_model_name() is None


def test_database_model_factory_uses_requested_scope(monkeypatch):
    import backend.core.models.factory as factory

    captured = {}
    sentinel = object()

    def load(name, usage_scope):
        captured["selection"] = (name, usage_scope)
        return {"name": name}

    monkeypatch.setenv("MODEL_CONFIG_SOURCE", "db")
    monkeypatch.setattr(factory, "_load_from_db", load)
    monkeypatch.setattr(
        factory,
        "_create_from_db_config",
        lambda config, thinking_enabled, **kwargs: sentinel,
    )

    result = factory.create_chat_model(
        name="ingestion-only",
        usage_scope="ingestion",
    )

    assert result is sentinel
    assert captured["selection"] == ("ingestion-only", "ingestion")


def test_database_model_factory_fails_closed_on_scope_miss(monkeypatch):
    import backend.core.models.factory as factory

    monkeypatch.setenv("MODEL_CONFIG_SOURCE", "db")
    monkeypatch.setattr(factory, "_load_from_db", lambda name, usage_scope: None)
    monkeypatch.setattr(
        factory,
        "_create_from_config",
        lambda *args, **kwargs: pytest.fail("YAML fallback must not be used"),
    )

    with pytest.raises(ValueError, match="usage scope chat"):
        factory.create_chat_model(name="internal-only")


async def test_chat_model_catalog_rejects_non_chat_selection(monkeypatch):
    import backend.services.chat_model_catalog as catalog

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, statement):
            assert "usage_scope" in str(statement)
            return None

    monkeypatch.setenv("MODEL_CONFIG_SOURCE", "db")
    monkeypatch.setattr(catalog, "get_async_session_factory", lambda: FakeSession)

    with pytest.raises(catalog.ChatModelNotAvailable):
        await catalog.resolve_active_chat_model("ingestion-only")


async def test_chat_model_catalog_returns_only_public_capabilities(monkeypatch):
    import backend.services.chat_model_catalog as catalog

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def scalar(self, _statement):
            return SimpleNamespace(name="public-chat", supports_vision=True)

    monkeypatch.setenv("MODEL_CONFIG_SOURCE", "db")
    monkeypatch.setattr(catalog, "get_async_session_factory", lambda: FakeSession)

    selected = await catalog.resolve_active_chat_model("public-chat")
    assert selected.name == "public-chat"
    assert selected.supports_vision is True
