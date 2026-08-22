from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.db.models.llm_model import LlmModel
from deploy.production.model_usage_transition import (
    ModelUsageTransitionError,
    activate_frontend_isolation,
    prepare_scoped_models,
    rollback_frontend_isolation,
    verify_scoped_models,
)


def _session_factory():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    LlmModel.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        session.add_all(
            [
                LlmModel(
                    usage_scope="chat",
                    name="qwen3.5-35b",
                    display_name="Qwen 3.5",
                    model="upstream-qwen35",
                    api_key="secret-not-for-output",
                    base_url="https://models.invalid/v1",
                    supports_vision=True,
                    is_active=True,
                    is_default=False,
                ),
                LlmModel(
                    usage_scope="chat",
                    name="qwen3.6-35b",
                    display_name="Qwen 3.6",
                    model="upstream-qwen36",
                    api_key="secret-not-for-output",
                    base_url="https://models.invalid/v1",
                    supports_vision=True,
                    is_active=True,
                    is_default=False,
                ),
            ]
        )
    return factory


def _rows(factory):
    with factory() as session:
        return {
            (row.name, row.usage_scope): row
            for row in session.scalars(select(LlmModel)).all()
        }


def test_prepare_is_idempotent_and_keeps_chat_rows_active():
    factory = _session_factory()

    first = prepare_scoped_models(factory)
    second = prepare_scoped_models(factory)
    verified = verify_scoped_models(factory)
    rows = _rows(factory)

    assert len(first["created"]) == 2
    assert second["created"] == []
    assert verified["status"] == "verified"
    assert rows[("qwen3.5-35b", "chat")].is_active is True
    assert rows[("qwen3.6-35b", "chat")].is_active is True
    assert rows[("qwen3.5-35b", "ingestion")].is_default is True
    assert rows[("qwen3.6-35b", "image_analysis")].supports_vision is True


def test_activate_journal_has_no_secrets_and_rollback_restores_flags(tmp_path):
    factory = _session_factory()
    prepare_scoped_models(factory)
    journal = tmp_path / "model-usage-activation.json"

    activated = activate_frontend_isolation(journal, factory)
    rows = _rows(factory)
    journal_text = journal.read_text(encoding="utf-8")

    assert activated["status"] == "activated"
    assert journal.stat().st_mode & 0o077 == 0
    assert "secret-not-for-output" not in journal_text
    assert rows[("qwen3.5-35b", "chat")].is_active is False
    assert rows[("qwen3.6-35b", "chat")].is_active is False

    rolled_back = rollback_frontend_isolation(journal, factory)
    rows = _rows(factory)
    assert rolled_back["status"] == "rolled_back"
    assert rows[("qwen3.5-35b", "chat")].is_active is True
    assert rows[("qwen3.6-35b", "chat")].is_active is True
    assert json.loads(journal_text)["status"] == "activated"
    assert json.loads(journal.read_text(encoding="utf-8"))["status"] == "rolled_back"


def test_activate_fails_closed_before_internal_scopes_exist(tmp_path):
    factory = _session_factory()

    with pytest.raises(ModelUsageTransitionError, match="缺少"):
        activate_frontend_isolation(tmp_path / "journal.json", factory)


def test_runtime_verify_allows_admin_to_replace_internal_defaults():
    factory = _session_factory()
    prepare_scoped_models(factory)
    with factory.begin() as session:
        rows = list(session.scalars(select(LlmModel)).all())
        for row in rows:
            if row.usage_scope in {"ingestion", "image_analysis"}:
                row.is_default = False
        session.add_all(
            [
                LlmModel(
                    usage_scope="ingestion",
                    name="replacement-ingestion",
                    display_name="Replacement ingestion",
                    model="replacement-ingestion-upstream",
                    api_key="replacement-secret",
                    base_url="https://models.invalid/v1",
                    is_active=True,
                    is_default=True,
                ),
                LlmModel(
                    usage_scope="image_analysis",
                    name="replacement-image",
                    display_name="Replacement image",
                    model="replacement-image-upstream",
                    api_key="replacement-secret",
                    base_url="https://models.invalid/v1",
                    supports_vision=True,
                    is_active=True,
                    is_default=True,
                ),
            ]
        )

    verified = verify_scoped_models(factory)

    assert [item["name"] for item in verified["internal_models"]] == [
        "replacement-ingestion",
        "replacement-image",
    ]
