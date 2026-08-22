from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "deploy/local-test/changed_services.py"
SPEC = importlib.util.spec_from_file_location("changed_services", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
changed_services = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(changed_services)


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("frontend/src/App.vue", ("frontend",)),
        ("frontend_audit/src/App.vue", ("audit-frontend",)),
        ("frontend_csh_ai/package-lock.json", ("csh-frontend",)),
        ("frontend_fmca/src/main.ts", ("fmca-frontend",)),
        ("guest/src/App.vue", ("guest-frontend",)),
        (
            "backend/api/http_server.py",
            ("agent-api", "knowledge-api", "guest-api"),
        ),
        ("backend/scripts/download_cdut_share.py", ()),
        ("backend/scripts/import_university_audit_kb.py", ()),
        ("scripts/rebuild_self_entity_chunk_index.py", ()),
        (
            "deploy/local-test/render_runtime_env.py",
            ("agent-api", "knowledge-api", "guest-api"),
        ),
        ("backend/tests/test_http_server_readiness.py", ()),
        ("deploy/local-test/release.sh", ()),
        ("deploy/docker-test/manage.sh", ()),
        ("deploy/docker-test/browser_smoke.js", ()),
        ("deploy/docker-test/quality.Dockerfile", ()),
        ("deploy/docker-test/run_quality_gate.py", ()),
        ("deploy/quality-gate.json", ()),
        ("deploy/docker-test/systemd/auto-update.service", ()),
        (
            "deploy/docker-test/backend.Dockerfile",
            ("agent-api", "knowledge-api", "guest-api"),
        ),
        (
            "deploy/docker-test/frontend.Dockerfile",
            ("frontend", "csh-frontend", "fmca-frontend", "landslide-frontend", "guest-frontend"),
        ),
        (
            "deploy/docker-test/audit-frontend.Dockerfile",
            ("audit-frontend",),
        ),
        ("deploy/docker-test/compose.audit.yml", ("audit-frontend",)),
        (
            "deploy/docker-test/nginx/audit.conf.template",
            ("audit-frontend",),
        ),
        (
            "deploy/docker-test/nginx/default.conf.template",
            (
                "frontend",
                "audit-frontend",
                "csh-frontend",
                "fmca-frontend",
                "landslide-frontend",
                "guest-frontend",
            ),
        ),
        (
            "deploy/docker-test/compose.shadow.yml",
            (
                "agent-api",
                "knowledge-api",
                "guest-api",
                "frontend",
                "audit-frontend",
                "csh-frontend",
                "fmca-frontend",
                "landslide-frontend",
                "guest-frontend",
            ),
        ),
        (".agents/skills/radai-project-context/SKILL.md", ()),
        ("docs/design.md", ()),
        # 评测器是操作员一次性脚本（import backend，而非被 backend import），
        # 与生产 changed_units.py 同口径：eval 变更不得触发业务容器重建。
        ("eval/datasets/self_kb/eval_ragas_ablation.py", ()),
        ("eval/course_eval/run_eval.py", ()),
        ("eval/graphrag_bench/run_official_graphrag_eval.py", ()),
        (
            " frontend/src/App.vue",
            (
                "agent-api",
                "knowledge-api",
                "guest-api",
                "frontend",
                "audit-frontend",
                "csh-frontend",
                "fmca-frontend",
                "landslide-frontend",
                "guest-frontend",
            ),
        ),
    ],
)
def test_services_for_known_path(path: str, expected: tuple[str, ...]):
    assert changed_services.services_for_path(path) == expected


def test_combined_impacts_are_deduplicated_in_start_order():
    assert changed_services.affected_services(
        [
            "guest/src/App.vue",
            "backend/core/auth/service.py",
            "frontend/src/App.vue",
            "backend/core/auth/service.py",
        ]
    ) == (
        "agent-api",
        "knowledge-api",
        "guest-api",
        "frontend",
        "guest-frontend",
    )


def test_unknown_runtime_path_falls_back_to_all_services():
    assert changed_services.services_for_path("runtime-new/component.conf") == (
        "agent-api",
        "knowledge-api",
        "guest-api",
        "frontend",
        "audit-frontend",
        "csh-frontend",
        "fmca-frontend",
        "landslide-frontend",
        "guest-frontend",
    )


def test_cli_writes_owner_only_frontend_plan_and_audit_report(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    subprocess.run(["git", "init", "-q", str(repository)], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "test@example.invalid",
        ],
        check=True,
    )
    frontend_file = repository / "frontend/src/App.vue"
    frontend_file.parent.mkdir(parents=True)
    frontend_file.write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-qm", "old"],
        check=True,
    )
    old_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    frontend_file.write_text("new\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "commit", "-qam", "new"], check=True)
    new_sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    plan = tmp_path / "state/restart-plan"
    report = tmp_path / "state/service-impact.json"

    subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--git-dir",
            str(repository / ".git"),
            "--plan",
            str(plan),
            "--report",
            str(report),
            old_sha,
            new_sha,
        ],
        check=True,
    )

    assert plan.read_text(encoding="utf-8") == "frontend\n"
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload == {
        "format_version": 1,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "changed_paths": ["frontend/src/App.vue"],
        "services": ["frontend"],
        "path_impacts": {
            "frontend/src/App.vue": ["frontend"]
        },
    }
    assert stat.S_IMODE(plan.stat().st_mode) & 0o077 == 0
    assert stat.S_IMODE(report.stat().st_mode) & 0o077 == 0
