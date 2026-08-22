#!/usr/bin/env python3
"""Production business smoke with runtime-local conversation metadata.

The test creates unique Agent and Guest conversations in production checkpoint
and conversation SQLite files.  It opens MySQL through read-only sessions only
and proves that the matching conversation rows remain absent there.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_RUNTIME_ROOT = Path("/home/inspur/myenv/runtime/doc-agent-production")
DEFAULT_GROUNDWATER_BUILTIN_ROOT = Path(
    "/home/inspur/myenv/runtime/doc-agent-shared/groundwater/liutang"
)
DEVICE_ID = "gdev_123e4567-e89b-42d3-a456-426614174000"


def load_smoke_module(current: Path) -> Any:
    module_path = current / "deploy" / "local-test" / "smoke_test.py"
    spec = importlib.util.spec_from_file_location(
        "production_smoke_helpers", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load smoke helpers: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_report(report_dir: Path, report: dict[str, Any]) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(report_dir, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".production-smoke-", suffix=".tmp", dir=report_dir
    )
    report_path = report_dir / f"production-smoke-{int(time.time())}.json"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.chmod(temporary_name, 0o600)
        Path(temporary_name).replace(report_path)
        os.chmod(report_path, 0o600)
    finally:
        if Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return report_path


def documentation_disabled(smoke: Any, name: str, base_url: str) -> dict[str, Any]:
    statuses: dict[str, int] = {}
    for path in ("/docs", "/redoc", "/openapi.json"):
        status, _headers, _body = smoke.request("GET", f"{base_url}{path}")
        statuses[path] = status
        if status != 404:
            raise smoke.SmokeError(
                f"{name} exposed production API documentation at {path}: HTTP {status}"
            )
    return {"name": f"{name}-api-documentation-disabled", "statuses": statuses}


def groundwater_dataset_result(smoke: Any) -> dict[str, Any]:
    configured = Path(os.getenv("GROUNDWATER_BUILTIN_ROOT", "")).expanduser()
    if configured != DEFAULT_GROUNDWATER_BUILTIN_ROOT:
        raise smoke.SmokeError("production groundwater dataset path is not stable")
    if not configured.is_dir():
        raise smoke.SmokeError("production groundwater dataset is missing")
    manifest_path = configured / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise smoke.SmokeError("production groundwater manifest is not a regular file")
    manifest_mode = stat.S_IMODE(manifest_path.stat().st_mode)
    resolved_mode = stat.S_IMODE(configured.resolve(strict=True).stat().st_mode)
    if manifest_mode & 0o222 or resolved_mode & 0o222:
        raise smoke.SmokeError("production groundwater dataset is writable")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    required = {
        "schema_version",
        "project",
        "data_version",
        "datasets",
        "record_counts",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise smoke.SmokeError("production groundwater manifest contract is incomplete")
    return {
        "name": "production-groundwater-builtin-dataset",
        "path": str(configured),
        "read_only": True,
        "manifest_contract": "passed",
    }


def wait_for_catalog(
    smoke: Any,
    catalog_path: Path,
    marker_path: Path,
    *,
    user_id: str,
    thread_ids: tuple[str, str],
) -> dict[str, Any]:
    if not marker_path.is_file():
        raise smoke.SmokeError("production conversation migration marker is missing")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("format_version") != 1:
        raise smoke.SmokeError("unsupported production conversation migration marker")
    baseline = int((marker.get("catalog") or {}).get("rows") or 0)
    agent_thread, guest_thread = thread_ids
    guest_thread_pattern = f"%__t_{guest_thread}"
    deadline = time.monotonic() + 45
    rows: list[tuple[Any, ...]] = []
    total = 0
    while time.monotonic() < deadline:
        try:
            uri = f"file:{catalog_path.resolve()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=10) as database:
                integrity = database.execute("PRAGMA quick_check").fetchone()
                total_row = database.execute(
                    "SELECT COUNT(*) FROM conversations"
                ).fetchone()
                rows = database.execute(
                    """
                    SELECT user_id, thread_id, is_deleted, metadata
                    FROM conversations
                    WHERE thread_id = ? OR thread_id LIKE ?
                    ORDER BY thread_id
                    """,
                    (agent_thread, guest_thread_pattern),
                ).fetchall()
            total = int(total_row[0] if total_row else 0)
            if integrity and integrity[0] == "ok" and len(rows) == 2:
                break
        except sqlite3.Error:
            pass
        time.sleep(1)
    if len(rows) != 2:
        raise smoke.SmokeError("production smoke conversations were not saved locally")
    if any(int(row[2] or 0) != 0 for row in rows):
        raise smoke.SmokeError("production smoke conversation was unexpectedly deleted")
    agent_rows = [row for row in rows if row[1] == agent_thread]
    if not agent_rows or str(agent_rows[0][0]) != user_id:
        raise smoke.SmokeError("authenticated production conversation owner mismatch")
    guest_rows = [
        row
        for row in rows
        if str(row[1]).endswith(f"__t_{guest_thread}")
        and str(row[0]).startswith("guest_")
    ]
    if len(guest_rows) != 1:
        raise smoke.SmokeError("anonymous production conversation scope mismatch")
    metadata = json.loads(agent_rows[0][3] or "{}")
    runtime = metadata.get("agent_runtime") if isinstance(metadata, dict) else None
    if not isinstance(runtime, dict) or runtime.get("agent_code") != "ne":
        raise smoke.SmokeError("production conversation metadata is incomplete")
    if total < baseline + 2:
        raise smoke.SmokeError(
            "production conversation catalog fell below its baseline"
        )
    return {
        "name": "production-runtime-conversation-catalog",
        "quick_check": "ok",
        "migrated_baseline_rows": baseline,
        "current_rows": total,
        "agent_and_guest_saved": True,
        "guest_device_scope_verified": True,
    }


def main() -> int:
    runtime_root = Path(
        os.getenv("DOC_AGENT_PRODUCTION_RUNTIME_ROOT", str(DEFAULT_RUNTIME_ROOT))
    ).resolve()
    current = runtime_root / "current"
    backend_env = runtime_root / "config" / "backend.env"
    service_env = runtime_root / "config" / "service.env"
    report_dir = runtime_root / "reports"
    sys.path.insert(0, str(current))
    from dotenv import load_dotenv

    load_dotenv(backend_env, override=False)
    load_dotenv(service_env, override=True)
    smoke = load_smoke_module(current)
    if os.getenv("AGENT_CONVERSATION_STORE_BACKEND", "").lower() != "sqlite":
        raise smoke.SmokeError("production conversation backend is not SQLite")
    expected_catalog = runtime_root / "data" / "conversations" / "conversations.db"
    configured_catalog = Path(os.getenv("AGENT_CONVERSATION_SQLITE_PATH", "")).resolve(
        strict=False
    )
    if configured_catalog != expected_catalog:
        raise smoke.SmokeError("production conversation catalog path is not isolated")
    if os.getenv("API_DOCS_ENABLED", "true").lower() not in {"0", "false", "no", "off"}:
        raise smoke.SmokeError("production API documentation has not been disabled")

    suffix = f"{int(time.time())}-{os.getpid()}"
    agent_thread = f"production-switch-smoke-agent-{suffix}"
    guest_thread = f"production-switch-smoke-guest-{suffix}"
    thread_ids = (agent_thread, guest_thread)
    agent_api = "http://127.0.0.1:8082"
    guest_api = "http://127.0.0.1:8083"
    knowledge_api = "http://127.0.0.1:8086"
    frontends = {
        "frontend-5173": "http://127.0.0.1:5173",
        "university-audit-frontend-5177": "http://127.0.0.1:5177",
        "csh-frontend-5174": "http://127.0.0.1:5174",
        "fmca-frontend-5176": "http://127.0.0.1:5176",
    }

    with smoke.read_only_mysql() as connection:
        identity = smoke.select_smoke_identity(connection)
        active_chat_models, active_non_chat_models = smoke.active_model_scopes(
            connection
        )
        before_conversations = smoke.conversation_count(connection, list(thread_ids))
    if before_conversations != 0:
        raise smoke.SmokeError("production smoke thread IDs already exist in MySQL")

    api_key = identity["api_key"]
    results: list[dict[str, Any]] = [
        smoke.health_result("agent-health", f"{agent_api}/health"),
        smoke.health_result("guest-health", f"{guest_api}/health"),
        smoke.health_result("knowledge-health", f"{knowledge_api}/health"),
        documentation_disabled(smoke, "agent", agent_api),
        documentation_disabled(smoke, "guest", guest_api),
        documentation_disabled(smoke, "knowledge", knowledge_api),
        groundwater_dataset_result(smoke),
    ]
    for name, base_url in frontends.items():
        results.append(smoke.frontend_result(name, base_url))
        results.append(smoke.assert_vite_guard(name, base_url))
        results.append(smoke.profile_proxy_result(name, base_url, api_key))

    results.append(
        smoke.knowledge_proxy_result(
            frontends["frontend-5173"],
            selected_model=identity["model"],
            active_chat_models=active_chat_models,
            active_non_chat_models=active_non_chat_models,
        )
    )
    results.append(smoke.files_proxy_result(frontends["frontend-5173"], suffix))
    results.append(
        smoke.run_stream(
            "production-authenticated-agent-business-flow",
            f"{frontends['frontend-5173']}/api/v2/agent-runtime/query",
            headers={"X-API-Key": api_key},
            require_follow_up=True,
            body={
                "question": "请用一句简短中文回复：正式服务切换流程已连通。",
                "agent_code": "ne",
                "thread_id": agent_thread,
                "workflow": "lightweight",
                "model": identity["model"],
                "use_search": False,
                "kb_ids": [],
                "selected_skills": [],
                "subagent_enabled": False,
                "thinking": False,
                "metadata": {"source": "production-switch-smoke"},
            },
        )
    )
    results.append(
        smoke.wait_for_checkpoint(
            runtime_root / "data" / "checkpoints.db", agent_thread
        )
    )
    results.append(
        smoke.run_stream(
            "production-anonymous-guest-business-flow",
            f"{guest_api}/api/v2/guest/query",
            headers={"X-Device-ID": DEVICE_ID},
            guest=True,
            body={
                "question": "请用一句简短中文回复：正式访客服务已连通。",
                "thread_id": guest_thread,
                "agent_code": "ne",
                "model": identity["model"],
                "use_search": False,
                "kb_ids": [],
                "subagent_enabled": False,
            },
        )
    )
    results.append(
        smoke.wait_for_checkpoint(
            runtime_root / "data" / "guest_checkpoints.db", guest_thread
        )
    )
    results.append(
        wait_for_catalog(
            smoke,
            expected_catalog,
            runtime_root / "state" / "conversation-catalog-migration.json",
            user_id=str(identity["user_id"]),
            thread_ids=thread_ids,
        )
    )

    with smoke.read_only_mysql() as connection:
        after_conversations = smoke.conversation_count(connection, list(thread_ids))
    if after_conversations != before_conversations:
        raise smoke.SmokeError(
            "production conversation metadata unexpectedly changed MySQL"
        )
    results.append(
        {
            "name": "production-mysql-conversation-write-guard",
            "matching_rows_before": before_conversations,
            "matching_rows_after": after_conversations,
            "conversation_metadata_read_only": True,
            "other_business_tables_globally_read_only": False,
        }
    )

    git_sha = subprocess.run(
        ["git", "-C", str(current), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    report = {
        "status": "passed",
        "mode": "production-original-ports-existing-external-services",
        "git_sha": git_sha,
        "completed_at_unix": int(time.time()),
        "selected_existing_model": identity["model"],
        "selected_api_key_id": identity["api_key_id"],
        "safety": {
            "conversation_metadata_backend": "production-runtime-sqlite",
            "mysql_conversation_rows_unchanged": True,
            "external_services_reused": True,
            "api_documentation_disabled": True,
            "groundwater_builtin_dataset_read_only": True,
        },
        "results": results,
    }
    report_path = write_report(report_dir, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"production smoke test failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
