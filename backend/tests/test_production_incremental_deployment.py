from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAPPER_PATH = REPO_ROOT / "deploy/production/changed_units.py"
VERIFIER_PATH = REPO_ROOT / "deploy/production/verify_unit_transition.py"
DEPLOY_SCRIPT = REPO_ROOT / "deploy/production/deploy-release.sh"
MANAGER_SCRIPT = REPO_ROOT / "deploy/production/manage.sh"
INSTALL_SCRIPT = REPO_ROOT / "deploy/production/install-user-units.sh"
PREFLIGHT_SCRIPT = REPO_ROOT / "deploy/production/preflight-release.sh"
PREPARE_SCRIPT = REPO_ROOT / "deploy/production/prepare-release.sh"
AUDIT_LAUNCHER_SCRIPT = REPO_ROOT / "deploy/production/run-frontend-5177.sh"
TIMER_PATH = (
    REPO_ROOT
    / "deploy/production/systemd/doc-agent-production-auto-deploy.timer"
)

SPEC = importlib.util.spec_from_file_location("production_changed_units", MAPPER_PATH)
assert SPEC is not None and SPEC.loader is not None
changed_units = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changed_units)


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "frontend/src/App.vue",
            ("doc-agent-prod-frontend-5173.service",),
        ),
        (
            "frontend_audit/src/App.vue",
            ("doc-agent-prod-frontend-5177.service",),
        ),
        (
            "frontend_csh_ai/src/App.vue",
            ("doc-agent-prod-frontend-5174.service",),
        ),
        (
            "frontend_fmca/src/App.vue",
            ("doc-agent-prod-frontend-5176.service",),
        ),
        ("guest/src/App.vue", ()),
        ("frontend/README.md", ()),
        ("backend/tests/test_auth.py", ()),
        ("backend/scripts/download_cdut_share.py", ()),
        ("backend/scripts/import_university_audit_kb.py", ()),
        ("deploy/production/manage.sh", ()),
        ("scripts/rebuild_self_entity_chunk_index.py", ()),
        (
            "deploy/production/systemd/doc-agent-prod-agent-api.service",
            ("doc-agent-prod-agent-api.service",),
        ),
        (
            "deploy/production/systemd/doc-agent-prod-frontend-5177.service",
            ("doc-agent-prod-frontend-5177.service",),
        ),
        (
            "deploy/production/systemd/doc-agent-prod-frontend-4173.service",
            (),
        ),
        (
            "deploy/production/systemd/doc-agent-production-healthcheck.service",
            (),
        ),
        ("AGENTS.md", ()),
    ],
)
def test_known_paths_map_to_only_formal_affected_units(
    path: str, expected: tuple[str, ...]
):
    assert changed_units.units_for_path(path) == expected


def test_shared_backend_change_restarts_apis_and_workers_only():
    assert changed_units.units_for_path("backend/api/http_server.py") == (
        "doc-agent-prod-knowledge-api.service",
        "doc-agent-prod-agent-api.service",
        "doc-agent-prod-guest-api.service",
        "doc-agent-prod-task-worker.service",
        "doc-agent-prod-ingest-worker.service",
    )


def test_unknown_runtime_path_falls_back_to_all_formal_units():
    assert changed_units.units_for_path("new-runtime/component.conf") \
        == changed_units.ALL_UNITS


def test_unknown_production_business_unit_falls_back_to_all_formal_units():
    assert changed_units.units_for_path(
        "deploy/production/systemd/doc-agent-prod-new-role.service"
    ) == changed_units.ALL_UNITS


def write_snapshot(
    path: Path,
    *,
    units: tuple[str, ...],
    release_sha: str,
    pid_offset: int = 0,
    overrides: dict[str, tuple[int, str]] | None = None,
) -> None:
    overrides = overrides or {}
    lines = []
    for index, unit in enumerate(units, start=1):
        pid, sha = overrides.get(unit, (1000 + pid_offset + index, release_sha))
        suffix = "frontend" if "frontend" in unit else ""
        cwd = path.parent / "runtime" / "releases" / sha / suffix
        lines.append(f"{unit}\t{pid}\t{cwd}\n")
    path.write_text("".join(lines), encoding="utf-8")


def test_transition_verifier_accepts_only_planned_pid_change(tmp_path: Path):
    old_sha = "a" * 40
    target_sha = "b" * 40
    selected = changed_units.ALL_UNITS[1]
    before = tmp_path / "before.tsv"
    after = tmp_path / "after.tsv"
    plan = tmp_path / "plan"
    write_snapshot(before, units=changed_units.ALL_UNITS, release_sha=old_sha)
    write_snapshot(
        after,
        units=changed_units.ALL_UNITS,
        release_sha=old_sha,
        overrides={selected: (9001, target_sha)},
    )
    plan.write_text(selected + "\n", encoding="utf-8")

    subprocess.run(
        [
            sys.executable,
            str(VERIFIER_PATH),
            "--before",
            str(before),
            "--after",
            str(after),
            "--plan",
            str(plan),
            "--target-sha",
            target_sha,
            "--runtime-root",
            str(tmp_path / "runtime"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_transition_verifier_rejects_unplanned_restart(tmp_path: Path):
    old_sha = "a" * 40
    target_sha = "b" * 40
    selected = changed_units.ALL_UNITS[1]
    unplanned = changed_units.ALL_UNITS[2]
    before = tmp_path / "before.tsv"
    after = tmp_path / "after.tsv"
    plan = tmp_path / "plan"
    write_snapshot(before, units=changed_units.ALL_UNITS, release_sha=old_sha)
    write_snapshot(
        after,
        units=changed_units.ALL_UNITS,
        release_sha=old_sha,
        overrides={
            selected: (9001, target_sha),
            unplanned: (9002, target_sha),
        },
    )
    plan.write_text(selected + "\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(VERIFIER_PATH),
            "--before",
            str(before),
            "--after",
            str(after),
            "--plan",
            str(plan),
            "--target-sha",
            target_sha,
            "--runtime-root",
            str(tmp_path / "runtime"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unplanned unit changed" in result.stderr


def make_executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def test_installer_explicitly_retires_only_legacy_4173_frontend(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    unit_target = tmp_path / "user-units"
    fake_bin = tmp_path / "fake-bin"
    groundwater = tmp_path / "groundwater"
    credential_source = tmp_path / "credential-source"
    calls = tmp_path / "systemctl.calls"
    (runtime / "source.git").mkdir(parents=True)
    (runtime / "config").mkdir()
    unit_target.mkdir()
    fake_bin.mkdir()
    groundwater.mkdir()
    (runtime / "config/backend.env").write_text("SAFE=value\n", encoding="utf-8")
    (runtime / "config/service.env").write_text("SAFE=value\n", encoding="utf-8")
    (groundwater / "manifest.json").write_text("{}\n", encoding="utf-8")
    credential_source.write_text("credential-placeholder\n", encoding="utf-8")
    credential_source.chmod(0o600)
    legacy_unit = unit_target / "doc-agent-prod-frontend-4173.service"
    legacy_unit.write_text("[Unit]\nDescription=stale\n", encoding="utf-8")
    make_executable(
        fake_bin / "systemctl",
        f"""#!/usr/bin/env bash
printf '%s\n' "$*" >>"{calls}"
if [[ "$*" == "--user is-active --quiet doc-agent-prod-frontend-4173.service" ]]; then
  exit 3
fi
exit 0
""",
    )
    make_executable(fake_bin / "ss", "#!/usr/bin/env bash\nexit 0\n")

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DOC_AGENT_PRODUCTION_RUNTIME_ROOT": str(runtime),
            "DOC_AGENT_PRODUCTION_USER_UNIT_DIR": str(unit_target),
            "DOC_AGENT_PRODUCTION_GIT_CREDENTIAL_SOURCE": str(
                credential_source
            ),
            "DOC_AGENT_GROUNDWATER_BUILTIN_ROOT": str(groundwater),
            "DOC_AGENT_PRODUCTION_RETIRE_LEGACY_4173": "true",
        },
    )

    assert result.returncode == 0, result.stderr
    assert not legacy_unit.exists()
    assert (unit_target / "doc-agent-prod-frontend-5173.service").is_file()
    assert (unit_target / "doc-agent-prod-frontend-5177.service").is_file()
    systemctl_calls = calls.read_text(encoding="utf-8").splitlines()
    assert [line for line in systemctl_calls if "--user stop" in line] == [
        "--user stop doc-agent-prod-frontend-4173.service"
    ]
    assert "legacy 4173 单元已停止并移除，端口无监听" in result.stdout


def run_preflight_with_outbox_lines(
    tmp_path: Path, outbox_lines: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    target_sha = "a" * 40
    runtime = tmp_path / "runtime"
    release = runtime / "releases" / target_sha
    config = runtime / "config"
    bin_dir = runtime / "bin"
    fake_bin = tmp_path / "fake-bin"
    groundwater = tmp_path / "groundwater"
    (release / ".venv/bin").mkdir(parents=True)
    config.mkdir(parents=True)
    bin_dir.mkdir(parents=True)
    fake_bin.mkdir()
    groundwater.mkdir()
    (groundwater / "manifest.json").write_text("{}\n", encoding="utf-8")
    (config / "backend.env").write_text("SAFE=value\n", encoding="utf-8")
    settings = (
        f"DOC_AGENT_RUNTIME_ROOT={runtime}",
        "AGENT_CONVERSATION_STORE_BACKEND=sqlite",
        f"AGENT_CONVERSATION_SQLITE_PATH={runtime}/data/conversations/conversations.db",
        "MYSQL_SESSION_READ_ONLY=false",
        "STARTUP_SCHEMA_INITIALIZATION_ENABLED=false",
        *outbox_lines,
        "API_DOCS_ENABLED=false",
        f"RAG_WORK_DIR={runtime}/data/rag",
        f"GROUNDWATER_BUILTIN_ROOT={groundwater}",
    )
    (config / "service.env").write_text(
        "\n".join(settings) + "\n", encoding="utf-8"
    )
    make_executable(release / ".venv/bin/python", "#!/usr/bin/env bash\nexit 0\n")
    make_executable(bin_dir / "run_with_env.py", "#!/usr/bin/env bash\nexit 0\n")
    make_executable(
        fake_bin / "git",
        f"#!/usr/bin/env bash\nprintf '%s\\n' '{target_sha}'\n",
    )

    return subprocess.run(
        ["bash", str(PREFLIGHT_SCRIPT), target_sha],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DOC_AGENT_PRODUCTION_RUNTIME_ROOT": str(runtime),
            "DOC_AGENT_GROUNDWATER_BUILTIN_ROOT": str(groundwater),
        },
    )


@pytest.mark.parametrize("value", ["false", "true"])
def test_production_preflight_accepts_explicit_boolean_task_outbox(
    tmp_path: Path, value: str
) -> None:
    result = run_preflight_with_outbox_lines(
        tmp_path, (f"TASK_DISPATCH_OUTBOX_ENABLED={value}",)
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "outbox_lines",
    [
        (),
        ("TASK_DISPATCH_OUTBOX_ENABLED=enabled",),
        (
            "TASK_DISPATCH_OUTBOX_ENABLED=false",
            "TASK_DISPATCH_OUTBOX_ENABLED=true",
        ),
    ],
)
def test_production_preflight_rejects_unsafe_task_outbox_setting(
    tmp_path: Path, outbox_lines: tuple[str, ...]
) -> None:
    result = run_preflight_with_outbox_lines(tmp_path, outbox_lines)

    assert result.returncode != 0
    assert "TASK_DISPATCH_OUTBOX_ENABLED" in result.stderr


def test_same_set_rollback_runs_even_when_new_release_is_unhealthy(tmp_path: Path):
    source = tmp_path / "source"
    runtime = tmp_path / "runtime"
    fake_bin = tmp_path / "fake-bin"
    markers = tmp_path / "markers"
    selected = changed_units.ALL_UNITS[0]

    git("init", "-b", "production", str(source))
    git("config", "user.name", "Rollback Test", cwd=source)
    git("config", "user.email", "rollback@example.invalid", cwd=source)
    (source / "application.txt").write_text("old\n", encoding="utf-8")
    git("add", ".", cwd=source)
    git("commit", "-m", "old", cwd=source)
    old_sha = git("rev-parse", "HEAD", cwd=source)
    (source / "application.txt").write_text("new\n", encoding="utf-8")
    git("commit", "-am", "new", cwd=source)
    new_sha = git("rev-parse", "HEAD", cwd=source)

    releases = runtime / "releases"
    releases.mkdir(parents=True)
    for sha in (old_sha, new_sha):
        release = releases / sha
        git("worktree", "add", "--detach", str(release), sha, cwd=source)
        (release / "backend").mkdir(exist_ok=True)
        (release / ".venv/bin").mkdir(parents=True)
        make_executable(release / ".venv/bin/python", "#!/usr/bin/env bash\n")
        for frontend in ("frontend", "frontend_csh_ai", "frontend_fmca"):
            vite = release / frontend / "node_modules/.bin/vite"
            vite.parent.mkdir(parents=True)
            make_executable(vite, "#!/usr/bin/env bash\n")
    audit_package = releases / new_sha / "frontend_audit/package.json"
    audit_package.parent.mkdir(parents=True)
    audit_package.write_text("{}\n", encoding="utf-8")
    (releases / new_sha / "frontend_audit/node_modules/.bin").mkdir(parents=True)
    make_executable(
        releases / new_sha / "frontend_audit/node_modules/.bin/vite",
        "#!/usr/bin/env bash\n",
    )

    config = runtime / "config/backend.env"
    config.parent.mkdir(parents=True)
    config.write_text("", encoding="utf-8")
    for sha in (old_sha, new_sha):
        (releases / sha / "backend/.env").symlink_to(config)
    (runtime / "current").symlink_to(releases / new_sha)

    plan = runtime / "state/plan"
    plan.parent.mkdir()
    plan.write_text(selected + "\n", encoding="utf-8")
    plan.chmod(0o600)
    fake_bin.mkdir()
    markers.mkdir()
    make_executable(
        fake_bin / "systemctl",
        f"""#!/usr/bin/env bash
set -eu
[[ "${{1:-}}" != "--user" ]] || shift
action="${{1:-}}"
shift || true
unit="${{@: -1}}"
case "${{action}}" in
  restart)
    printf '%s\\n' "${{unit}}" >>"{markers / 'restarts'}"
    touch "{markers}/active-${{unit}}"
    ;;
  is-active)
    if [[ "${{unit}}" == "{selected}" && ! -f "{markers}/active-${{unit}}" ]]; then
      exit 1
    fi
    ;;
  *) exit 99 ;;
esac
""",
    )
    make_executable(fake_bin / "curl", "#!/usr/bin/env bash\nprintf '200'\n")

    subprocess.run(
        [str(MANAGER_SCRIPT), "rollback-selected", old_sha, str(plan)],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DOC_AGENT_PRODUCTION_RUNTIME_ROOT": str(runtime),
        },
    )

    assert (runtime / "current").resolve() == releases / old_sha
    assert (markers / "restarts").read_text(encoding="utf-8") == selected + "\n"


def test_no_remote_update_performs_health_check_without_deploying(tmp_path: Path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    runtime = tmp_path / "runtime"
    fake_bin = tmp_path / "fake-bin"
    markers = tmp_path / "markers"
    git("init", "--bare", str(origin))
    git("init", "-b", "production", str(seed))
    git("config", "user.name", "No Update Test", cwd=seed)
    git("config", "user.email", "no-update@example.invalid", cwd=seed)
    (seed / "application.txt").write_text("same\n", encoding="utf-8")
    git("add", ".", cwd=seed)
    git("commit", "-m", "formal", cwd=seed)
    sha = git("rev-parse", "HEAD", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "-u", "origin", "production", cwd=seed)
    git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/production")

    source = runtime / "source.git"
    release = runtime / "releases" / sha
    credential = runtime / "config/git-auth/credentials"
    bin_dir = runtime / "bin"
    git("clone", "--bare", str(origin), str(source))
    git("clone", str(origin), str(release))
    git("checkout", "--detach", sha, cwd=release)
    (runtime / "current").symlink_to(release)
    credential.parent.mkdir(parents=True)
    credential.write_text("", encoding="utf-8")
    credential.chmod(0o600)
    bin_dir.mkdir(parents=True)
    fake_bin.mkdir()
    markers.mkdir()

    manager = bin_dir / "manage.sh"
    make_executable(
        manager,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >>"{markers / "manager"}"\n',
    )
    for name in ("prepare-release.sh", "changed_units.py", "verify_unit_transition.py"):
        make_executable(
            bin_dir / name,
            f'#!/usr/bin/env bash\ntouch "{markers / name}"\nexit 99\n',
        )
    make_executable(fake_bin / "curl", "#!/usr/bin/env bash\nprintf '200'\n")

    result = subprocess.run(
        [str(DEPLOY_SCRIPT), "automatic"],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "DOC_AGENT_PRODUCTION_RUNTIME_ROOT": str(runtime),
            "DOC_AGENT_PRODUCTION_BRANCH": "production",
            "DOC_AGENT_PRODUCTION_GIT_CREDENTIAL_FILE": str(credential),
            "DOC_AGENT_PRODUCTION_PREPARE_SCRIPT": str(
                bin_dir / "prepare-release.sh"
            ),
            "DOC_AGENT_PRODUCTION_MANAGER": str(manager),
            "DOC_AGENT_PRODUCTION_IMPACT_MAPPER": str(bin_dir / "changed_units.py"),
            "DOC_AGENT_PRODUCTION_TRANSITION_VERIFIER": str(
                bin_dir / "verify_unit_transition.py"
            ),
            "DOC_AGENT_PRODUCTION_PUBLIC_URLS": "https://example.invalid/",
        },
    )

    assert (markers / "manager").read_text(encoding="utf-8") == "assert-running\n"
    assert not (markers / "prepare-release.sh").exists()
    assert not (markers / "changed_units.py").exists()
    assert not (markers / "verify_unit_transition.py").exists()
    assert "零服务重启" in result.stdout
    assert (runtime / "state/production-deploy.success-sha").read_text(
        encoding="utf-8"
    ).strip() == sha


def test_production_timer_and_scripts_enforce_deployment_contract():
    timer = TIMER_PATH.read_text(encoding="utf-8")
    deployer = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    manager = MANAGER_SCRIPT.read_text(encoding="utf-8")
    installer = INSTALL_SCRIPT.read_text(encoding="utf-8")
    preflight = PREFLIGHT_SCRIPT.read_text(encoding="utf-8")
    prepare = PREPARE_SCRIPT.read_text(encoding="utf-8")
    audit_launcher = AUDIT_LAUNCHER_SCRIPT.read_text(encoding="utf-8")
    production_smoke = (
        REPO_ROOT / "deploy/production/production_smoke.py"
    ).read_text(encoding="utf-8")
    router = (REPO_ROOT / "frontend_audit/src/router/index.ts").read_text(
        encoding="utf-8"
    )
    audit_unit = (
        REPO_ROOT
        / "deploy/production/systemd/doc-agent-prod-frontend-5177.service"
    ).read_text(encoding="utf-8")
    production_target = (
        REPO_ROOT / "deploy/production/systemd/doc-agent-production.target"
    ).read_text(encoding="utf-8")

    assert "OnCalendar=*-*-* 03:00:00 Asia/Shanghai" in timer
    assert "Persistent=false" in timer
    assert "02:00" not in timer
    assert 'BRANCH="${DOC_AGENT_PRODUCTION_BRANCH:-production}"' in deployer
    assert 'if [[ "${old_sha}" == "${target_sha}" ]]' in deployer
    assert 'SMOKE_RELATIVE_PATH="deploy/production/production_smoke.py"' \
        in deployer
    assert 'smoke_script="${target_release}/${SMOKE_RELATIVE_PATH}"' \
        in deployer
    assert '"${target_release}/.venv/bin/python" "${smoke_script}"' \
        in deployer
    assert '${RUNTIME_ROOT}/bin/production_smoke.py' not in deployer
    assert 'DOC_AGENT_PRODUCTION_SMOKE_SCRIPT' not in deployer
    assert '"${MANAGER}" switch-selected' in deployer
    assert '"${MANAGER}" rollback-selected' in deployer
    assert "restart_selected" in manager
    assert "rollback_selected()" in manager
    rollback_body = manager.split("rollback_selected()", 1)[1].split(
        "start_services()", 1
    )[0]
    assert "assert_running || fail" not in rollback_body
    assert "同集合自动回滚" in manager
    assert "doc-agent-production-auto-deploy.timer" in installer
    runtime_scripts = installer.split("RUNTIME_SCRIPTS=(", 1)[1].split(")", 1)[0]
    assert "production_smoke.py" not in runtime_scripts
    assert "run-frontend-5177.sh" in runtime_scripts
    assert 'ENABLE_TASK_OUTBOX="${DOC_AGENT_PRODUCTION_ENABLE_TASK_OUTBOX:-false}"' \
        in installer
    assert 'RETIRE_LEGACY_FRONTEND_4173="${DOC_AGENT_PRODUCTION_RETIRE_LEGACY_4173:-false}"' \
        in installer
    assert 'systemctl --user stop "${LEGACY_FRONTEND_UNIT}"' in installer
    assert 'unlink -- "${unit_file}"' in installer
    assert "4173 端口仍存在监听" in installer
    assert 'require_boolean_setting "TASK_DISPATCH_OUTBOX_ENABLED"' in preflight
    assert 'require_setting "TASK_DISPATCH_OUTBOX_ENABLED=true"' not in preflight
    assert "for frontend in frontend frontend_audit frontend_csh_ai" in prepare
    assert "VITE_AUDIT_ONLY" not in audit_unit
    assert "run-frontend-5177.sh --check" in audit_unit
    assert "run-frontend-5177.sh" in audit_unit
    assert 'AUDIT_FRONTEND="${CURRENT}/frontend_audit"' in audit_launcher
    assert 'LEGACY_FRONTEND="${CURRENT}/frontend"' in audit_launcher
    assert "export VITE_AUDIT_ONLY=true" in audit_launcher
    assert "--host 0.0.0.0 --port 5177 --strictPort" in audit_launcher
    assert "doc-agent-prod-frontend-5177.service" in production_target
    assert "path: '/audit/:threadId?'" in router
    assert "path: '/:pathMatch(.*)*'" in router
    assert "redirect: '/audit'" in router
    assert "wait_http 5177 /audit 120" in manager
    assert "for port in 5173 5174 5176 5177" in manager
    assert "4173" not in manager
    assert "4173" not in deployer
    assert "4173" not in production_smoke
    assert "4173" not in production_target
    assert '"university-audit-frontend-5177": "http://127.0.0.1:5177"' in production_smoke
    for unit in changed_units.ALL_UNITS:
        assert unit in installer
        assert (
            REPO_ROOT / "deploy" / "production" / "systemd" / unit
        ).is_file()
    assert not (
        REPO_ROOT
        / "deploy/production/systemd/doc-agent-prod-frontend-4173.service"
    ).exists()
    assert "deploy-now.sh" in installer


def _git(repo: Path, *args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd or repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_prepare_release_reuses_unchanged_frontend_builds(tmp_path: Path) -> None:
    """后端-only release 应硬链接复用未变前端的 node_modules+dist。

    复用契约：donor marker == donor 自身源树 == 新 release 源树；被复用
    前端不得触发 npm（桩会在日志留痕）；前端源树变更后必须回退真实构建
    并写入新 marker。
    """

    frontend_dirs = (
        "frontend",
        "frontend_audit",
        "frontend_csh_ai",
        "frontend_fmca",
        "guest",
    )
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "production")
    _git(origin, "config", "user.email", "test@localhost")
    _git(origin, "config", "user.name", "Test")
    for name in ("pyproject.toml", "uv.lock", "backend/a.txt"):
        path = origin / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stub\n", encoding="utf-8")
    for name in frontend_dirs:
        package = origin / name / "package.json"
        package.parent.mkdir(parents=True, exist_ok=True)
        package.write_text('{"name":"%s"}\n' % name, encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "base")
    base_sha = _git(origin, "rev-parse", "HEAD")
    base_tree = _git(origin, "rev-parse", "HEAD:frontend")

    (origin / "backend" / "a.txt").write_text("backend-only change\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "backend only")
    backend_sha = _git(origin, "rev-parse", "HEAD")

    runtime = tmp_path / "runtime"
    subprocess.run(
        ["git", "clone", "--bare", str(origin), str(runtime / "source.git")],
        check=True,
        capture_output=True,
    )
    releases = runtime / "releases"
    releases.mkdir(parents=True)
    config = runtime / "config" / "backend.env"
    config.parent.mkdir(parents=True)
    config.write_text("# stub\n", encoding="utf-8")
    credential = runtime / "config" / "git-auth" / "credentials"
    credential.parent.mkdir(parents=True)
    credential.write_text("", encoding="utf-8")
    credential.chmod(0o600)

    # donor：上一 release，frontend 已构建且 marker 与自身源树一致。
    donor = releases / base_sha
    _git(
        runtime / "source.git",
        "worktree",
        "add",
        "--detach",
        str(donor),
        base_sha,
    )
    vite = donor / "frontend" / "node_modules" / ".bin" / "vite"
    vite.parent.mkdir(parents=True)
    vite.write_text("#!/bin/sh\n", encoding="utf-8")
    vite.chmod(0o755)
    donor_dist = donor / "frontend" / "dist" / "index.html"
    donor_dist.parent.mkdir(parents=True)
    donor_dist.write_text("dist\n", encoding="utf-8")
    (donor / "frontend" / ".prepared-frontend-tree").write_text(
        base_tree + "\n", encoding="utf-8"
    )
    (runtime / "current").symlink_to(donor)

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    npm_log = tmp_path / "npm-call-dirs.log"
    make_executable(
        stubs / "uv",
        "#!/usr/bin/env bash\n"
        "mkdir -p .venv/bin\n"
        "printf '#!/bin/sh\\n' > .venv/bin/python\n"
        "chmod +x .venv/bin/python\n",
    )
    make_executable(
        stubs / "npm",
        "#!/usr/bin/env bash\n"
        'pwd >> "%s"\n'
        "mkdir -p node_modules/.bin dist\n"
        "printf '#!/bin/sh\\n' > node_modules/.bin/vite\n"
        "chmod +x node_modules/.bin/vite\n"
        "printf 'built\\n' > dist/index.html\n" % npm_log,
    )

    def run_prepare() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(PREPARE_SCRIPT)],
            check=False,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{stubs}:{os.environ['PATH']}",
                "DOC_AGENT_PRODUCTION_RUNTIME_ROOT": str(runtime),
            },
        )

    result = run_prepare()
    assert result.returncode == 0, result.stderr

    new_release = releases / backend_sha
    reused_dist = new_release / "frontend" / "dist" / "index.html"
    assert reused_dist.exists()
    assert reused_dist.stat().st_ino == donor_dist.stat().st_ino
    assert reused_dist.stat().st_dev == donor_dist.stat().st_dev
    marker = (
        new_release / "frontend" / ".prepared-frontend-tree"
    ).read_text(encoding="utf-8").strip()
    assert marker == _git(origin, "rev-parse", f"{backend_sha}:frontend")
    called_dirs = {
        Path(line.strip()).name
        for line in npm_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    assert "frontend" not in called_dirs
    assert called_dirs == {"frontend_audit", "frontend_csh_ai", "frontend_fmca", "guest"}
    assert "复用 frontend 构建产物" in result.stdout

    # 前端源树变更后必须回退真实构建并写入新 marker。
    (origin / "frontend" / "src" / "App.vue").parent.mkdir(parents=True, exist_ok=True)
    (origin / "frontend" / "src" / "App.vue").write_text(
        "<template/>\n", encoding="utf-8"
    )
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "frontend change")
    frontend_sha = _git(origin, "rev-parse", "HEAD")
    result = run_prepare()
    assert result.returncode == 0, result.stderr
    fresh_release = releases / frontend_sha
    fresh_dist = fresh_release / "frontend" / "dist" / "index.html"
    assert fresh_dist.read_text(encoding="utf-8") == "built\n"
    assert fresh_dist.stat().st_ino != donor_dist.stat().st_ino
    fresh_marker = (
        fresh_release / "frontend" / ".prepared-frontend-tree"
    ).read_text(encoding="utf-8").strip()
    assert fresh_marker == _git(origin, "rev-parse", f"{frontend_sha}:frontend")
    # 其余四个前端源树仍未变，链式复用自上一 release。
    assert (
        fresh_release / "frontend_audit" / "dist" / "index.html"
    ).stat().st_ino == (
        new_release / "frontend_audit" / "dist" / "index.html"
    ).stat().st_ino
