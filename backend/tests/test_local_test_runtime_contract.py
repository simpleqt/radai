from __future__ import annotations

import argparse
import importlib.util
import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "deploy/local-test/render_runtime_env.py"
MANAGE_SCRIPT = REPO_ROOT / "deploy/local-test/manage.sh"
RELEASE_SCRIPT = REPO_ROOT / "deploy/local-test/release.sh"
AUTO_UPDATE_SCRIPT = REPO_ROOT / "deploy/local-test/auto-update.sh"
RUNTIME_UNIT = REPO_ROOT / "deploy/local-test/systemd/doc-agent-test-runtime.service"
AUTO_UPDATE_UNIT = (
    REPO_ROOT / "deploy/local-test/systemd/doc-agent-test-auto-update.service"
)
SPEC = importlib.util.spec_from_file_location("render_runtime_env", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
render_runtime_env = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(render_runtime_env)


def test_render_forces_mysql_read_only_and_functional_parity_flags(tmp_path: Path):
    source = tmp_path / "source.env"
    output = tmp_path / "runtime/config/runtime.env"
    runtime_root = tmp_path / "runtime"
    external_values = {
        "MYSQL_HOST": "mysql.internal",
        "MYSQL_PORT": "3306",
        "MYSQL_USER": "application",
        "MYSQL_PASSWORD": "sentinel-password",
        "MYSQL_DATABASE": "application",
        "REDIS_HOST": "redis.internal",
        "REDIS_PORT": "6379",
        "MINIO_ENDPOINT": "minio.internal:9000",
        "MINIO_ACCESS_KEY": "sentinel-access",
        "MINIO_SECRET_KEY": "sentinel-secret",
        "KAFKA_BOOTSTRAP_SERVERS": "kafka.internal:9092",
        "MILVUS_URI": "http://milvus.internal:19530",
        "NEBULA_HOST": "nebula.internal",
        "NEBULA_GRAPH_PORT": "9669",
        "MYSQL_SESSION_READ_ONLY": "false",
        "AGENT_CONVERSATION_STORE_BACKEND": "mysql",
        "TEST_ENABLE_WORKERS": "true",
        "STARTUP_RAG_INITIALIZATION_ENABLED": "false",
        "GUEST_RAG_STARTUP_ENABLED": "false",
        "HEALTH_ALLOW_EXPECTED_DEGRADED": "true",
    }
    source.write_text(
        "".join(f"{key}={value}\n" for key, value in external_values.items()),
        encoding="utf-8",
    )

    result = render_runtime_env.render(
        argparse.Namespace(
            source=source,
            output=output,
            runtime_root=runtime_root,
            public_host="test.example.invalid",
        )
    )
    rendered = render_runtime_env.read_env(output)

    assert rendered["MYSQL_HOST"] == external_values["MYSQL_HOST"]
    assert rendered["MYSQL_PASSWORD"] == external_values["MYSQL_PASSWORD"]
    assert rendered["MYSQL_SESSION_READ_ONLY"] == "true"
    assert rendered["AGENT_CONVERSATION_WRITES_ENABLED"] == "true"
    assert rendered["AGENT_CONVERSATION_STORE_BACKEND"] == "sqlite"
    assert rendered["AGENT_CONVERSATION_SQLITE_PATH"] == str(
        runtime_root / "data/conversations/conversations.db"
    )
    assert rendered["TEST_ENABLE_WORKERS"] == "false"
    assert rendered["KNOWLEDGE_API_STATELESS_REPLICA"] == "true"
    assert rendered["STARTUP_RAG_INITIALIZATION_ENABLED"] == "true"
    assert rendered["GUEST_RAG_STARTUP_ENABLED"] == "true"
    assert rendered["HEALTH_ALLOW_EXPECTED_DEGRADED"] == "false"
    assert result["safety"] == {
        "mysql_session_read_only": True,
        "conversation_metadata_backend": "isolated-sqlite",
        "auth_redis_db": 15,
        "shared_object_storage_read_only": True,
        "external_workers": False,
        "rag_startup_initialization": True,
    }
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def run_reload_plan(tmp_path: Path, content: str, mode: int = 0o600):
    plan = tmp_path / "restart-plan"
    plan.write_text(content, encoding="utf-8")
    plan.chmod(mode)
    return subprocess.run(
        ["bash", str(MANAGE_SCRIPT), "reload-selected", str(plan)],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "DOC_AGENT_TEST_RUNTIME_ROOT": str(tmp_path / "runtime"),
        },
        check=False,
        capture_output=True,
        text=True,
    )


def test_empty_reload_plan_is_a_true_noop(tmp_path: Path):
    result = run_reload_plan(tmp_path, "")

    assert result.returncode == 0, result.stderr
    assert "不重启业务服务" in result.stdout
    assert not (tmp_path / "runtime/config/runtime.env").exists()


def test_reload_plan_rejects_duplicate_services_before_runtime_changes(tmp_path: Path):
    result = run_reload_plan(tmp_path, "frontend\nfrontend\n")

    assert result.returncode != 0
    assert "重复服务" in result.stderr
    assert not (tmp_path / "runtime/config/runtime.env").exists()


def test_reload_plan_rejects_group_readable_file(tmp_path: Path):
    result = run_reload_plan(tmp_path, "frontend\n", mode=0o640)

    assert result.returncode != 0
    assert "不能授予组/其他用户权限" in result.stderr
    assert not (tmp_path / "runtime/config/runtime.env").exists()


def test_runtime_unit_uses_reload_without_a_full_restart():
    unit = RUNTIME_UNIT.read_text(encoding="utf-8")
    release = RELEASE_SCRIPT.read_text(encoding="utf-8")

    stable_manager = "/home/inspur/myenv/runtime/doc-agent-test/bin/manage.sh"
    assert f"ExecStart={stable_manager} start-apps" in unit
    assert f"ExecReload={stable_manager} reload-selected" in unit
    assert f"ExecStop={stable_manager} stop-apps" in unit
    assert "/current/deploy/local-test/manage.sh" not in unit
    assert 'systemctl --user reload "${RUNTIME_UNIT}"' in release
    assert 'systemctl --user restart "${RUNTIME_UNIT}"' not in release


def test_installed_updater_records_success_without_automatic_production_push():
    updater = AUTO_UPDATE_SCRIPT.read_text(encoding="utf-8")
    unit = AUTO_UPDATE_UNIT.read_text(encoding="utf-8")

    assert "Environment=DOC_AGENT_TEST_AUTO_PROMOTE_PRODUCTION=false" in unit
    assert (
        'AUTO_PROMOTE_PRODUCTION="${DOC_AGENT_TEST_AUTO_PROMOTE_PRODUCTION:-false}"'
        in updater
    )
    assert '[[ "${AUTO_PROMOTE_PRODUCTION}" != "true" ]]' in updater
    assert "等待授权人员提升" in updater
    assert 'record_sha "${SUCCESS_SHA_FILE}" "${remote_sha}"' in updater
    assert 'record_sha "${SUCCESS_SHA_FILE}" "${deployed_sha}"' in updater


def test_pid_snapshot_is_safe_under_nounset(tmp_path: Path):
    runtime_root = tmp_path / "runtime"
    pid_dir = runtime_root / "state/pids"
    pid_dir.mkdir(parents=True)
    (pid_dir / "agent-api.pid").write_text(f"{os.getpid()}\n", encoding="utf-8")
    snapshot = runtime_root / "state/test-pids.before"

    result = subprocess.run(
        [
            "bash",
            "-c",
            'set -Eeuo pipefail; source "$1"; capture_test_pids "$2"',
            "bash",
            str(RELEASE_SCRIPT),
            str(snapshot),
        ],
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "DOC_AGENT_TEST_RUNTIME_ROOT": str(runtime_root),
        },
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert snapshot.read_text(encoding="utf-8") == f"agent-api {os.getpid()}\n"
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
