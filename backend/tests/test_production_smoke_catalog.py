from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace


def _load_production_smoke():
    path = Path(__file__).parents[2] / "deploy/production/production_smoke.py"
    spec = importlib.util.spec_from_file_location("production_smoke_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_wait_for_catalog_accepts_device_scoped_guest_thread(tmp_path: Path):
    module = _load_production_smoke()
    catalog_path = tmp_path / "conversations.db"
    marker_path = tmp_path / "conversation-catalog-migration.json"
    marker_path.write_text(
        json.dumps({"format_version": 1, "catalog": {"rows": 0}}),
        encoding="utf-8",
    )
    agent_thread = "production-switch-smoke-agent-1"
    guest_thread = "production-switch-smoke-guest-1"
    runtime_metadata = json.dumps({"agent_runtime": {"agent_code": "ne"}})
    with sqlite3.connect(catalog_path) as database:
        database.execute(
            """
            CREATE TABLE conversations (
                user_id TEXT NOT NULL,
                thread_id TEXT NOT NULL,
                is_deleted INTEGER NOT NULL,
                metadata TEXT NOT NULL
            )
            """
        )
        database.executemany(
            "INSERT INTO conversations VALUES (?, ?, 0, ?)",
            [
                ("user-1", agent_thread, runtime_metadata),
                (
                    "guest_device-scope",
                    f"g_device-scope__t_{guest_thread}",
                    runtime_metadata,
                ),
            ],
        )

    result = module.wait_for_catalog(
        SimpleNamespace(SmokeError=RuntimeError),
        catalog_path,
        marker_path,
        user_id="user-1",
        thread_ids=(agent_thread, guest_thread),
    )

    assert result["agent_and_guest_saved"] is True
    assert result["guest_device_scope_verified"] is True


def test_main_passes_model_scope_to_knowledge_catalog(
    tmp_path: Path, monkeypatch
):
    module = _load_production_smoke()
    runtime_root = tmp_path / "production"
    catalog_path = runtime_root / "data" / "conversations" / "conversations.db"
    captured: dict[str, object] = {}

    @contextmanager
    def read_only_mysql():
        yield object()

    def knowledge_proxy_result(
        base_url: str,
        *,
        selected_model: str,
        active_chat_models: set[str],
        active_non_chat_models: set[str],
    ) -> dict[str, object]:
        captured.update(
            {
                "base_url": base_url,
                "selected_model": selected_model,
                "active_chat_models": active_chat_models,
                "active_non_chat_models": active_non_chat_models,
            }
        )
        return {"name": "chat-model-scope-isolation"}

    smoke = SimpleNamespace(
        SmokeError=RuntimeError,
        read_only_mysql=read_only_mysql,
        select_smoke_identity=lambda _connection: {
            "api_key": "test-key",
            "api_key_id": 1,
            "user_id": "user-1",
            "model": "chat-default",
        },
        active_model_scopes=lambda _connection: (
            {"chat-default"},
            {"ingestion-only"},
        ),
        conversation_count=lambda _connection, _thread_ids: 0,
        health_result=lambda name, _url: {"name": name},
        request=lambda *_args, **_kwargs: (404, {}, b""),
        frontend_result=lambda name, _url: {"name": name},
        assert_vite_guard=lambda name, _url: {"name": f"{name}-guard"},
        profile_proxy_result=lambda name, _url, _key: {
            "name": f"{name}-profiles"
        },
        knowledge_proxy_result=knowledge_proxy_result,
        files_proxy_result=lambda _url, _suffix: {"name": "files"},
        run_stream=lambda name, *_args, **_kwargs: {"name": name},
        wait_for_checkpoint=lambda *_args, **_kwargs: {"name": "checkpoint"},
    )
    monkeypatch.setenv("DOC_AGENT_PRODUCTION_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("AGENT_CONVERSATION_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_CONVERSATION_SQLITE_PATH", str(catalog_path))
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    monkeypatch.setattr(module, "load_smoke_module", lambda _current: smoke)
    monkeypatch.setattr(
        module,
        "groundwater_dataset_result",
        lambda _smoke: {"name": "groundwater"},
    )
    monkeypatch.setattr(
        module,
        "wait_for_catalog",
        lambda *_args, **_kwargs: {"name": "catalog"},
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{'a' * 40}\n"
        ),
    )

    assert module.main() == 0
    assert captured == {
        "base_url": "http://127.0.0.1:5173",
        "selected_model": "chat-default",
        "active_chat_models": {"chat-default"},
        "active_non_chat_models": {"ingestion-only"},
    }
