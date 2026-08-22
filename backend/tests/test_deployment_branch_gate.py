from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMOTE_SCRIPT = REPO_ROOT / "deploy/production/promote-tested.sh"
PREPARE_SCRIPT = REPO_ROOT / "deploy/production/prepare-release.sh"
QUALITY_POLICY = REPO_ROOT / "deploy/quality-gate.json"


def write_valid_gate_reports(reports: Path, candidate_sha: str) -> None:
    policy_raw = QUALITY_POLICY.read_bytes()
    policy = json.loads(policy_raw)
    checks = {
        name: {"status": "passed"} for name in policy["required_checks"]
    }
    (reports / "quality-1.json").write_text(
        json.dumps(
            {
                "format_version": policy["format_version"],
                "policy_id": policy["policy_id"],
                "policy_sha256": hashlib.sha256(policy_raw).hexdigest(),
                "status": "passed",
                "git_sha": candidate_sha,
                "completed_at_unix": 1,
                "checks": checks,
                "tests": {
                    "passed": policy["minimum_passed_tests"],
                    "skipped": policy["maximum_skipped_tests"],
                },
                "coverage": {"percent": policy["minimum_coverage_percent"]},
            }
        ),
        encoding="utf-8",
    )
    (reports / "browser-1.json").write_text(
        json.dumps(
            {
                "format_version": 1,
                "status": "passed",
                "git_sha": candidate_sha,
                "completed_at_unix": 1,
                "results": [
                    {"name": name, "status": "passed"}
                    for name in (
                        "frontend",
                        "audit-frontend",
                        "csh-frontend",
                        "fmca-frontend",
                        "guest-frontend",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_remote(tmp_path: Path) -> tuple[Path, str, str]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git("init", "--bare", str(origin))
    git("init", "-b", "main", str(seed))
    git("config", "user.name", "Branch Gate Test", cwd=seed)
    git("config", "user.email", "branch-gate@example.invalid", cwd=seed)
    (seed / "application.txt").write_text("formal\n", encoding="utf-8")
    git("add", "application.txt", cwd=seed)
    git("commit", "-m", "formal baseline", cwd=seed)
    formal_sha = git("rev-parse", "HEAD", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "-u", "origin", "main", cwd=seed)
    git("branch", "production", cwd=seed)
    git("push", "origin", "production", cwd=seed)

    git("switch", "-c", "dev", cwd=seed)
    (seed / "application.txt").write_text("tested\n", encoding="utf-8")
    git("commit", "-am", "tested candidate", cwd=seed)
    candidate_sha = git("rev-parse", "HEAD", cwd=seed)
    git("push", "-u", "origin", "dev", cwd=seed)
    git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/main")
    return origin, formal_sha, candidate_sha


def build_test_runtime(
    tmp_path: Path,
    *,
    origin: Path,
    candidate_sha: str,
    report_sha: str,
) -> tuple[Path, Path]:
    runtime = tmp_path / "test-runtime"
    source = runtime / "source.git"
    release = runtime / "releases" / candidate_sha
    state = runtime / "state"
    reports = runtime / "reports"
    credential = runtime / "config" / "git-auth" / "credentials"

    git("clone", "--bare", str(origin), str(source))
    git("clone", str(origin), str(release))
    git("checkout", "--detach", candidate_sha, cwd=release)
    (release / ".venv/bin").mkdir(parents=True)
    (release / ".venv/bin/python").symlink_to(Path(sys.executable).resolve())
    manage = release / "deploy/local-test/manage.sh"
    manage.parent.mkdir(parents=True)
    manage.write_text(
        '#!/usr/bin/env bash\nset -Eeuo pipefail\n[[ "${1:-}" == "assert-running" ]]\n',
        encoding="utf-8",
    )
    manage.chmod(0o755)
    state.mkdir(parents=True)
    reports.mkdir(parents=True)
    credential.parent.mkdir(parents=True)
    credential.write_text("", encoding="utf-8")
    credential.chmod(0o600)
    (state / "auto-update.success-sha").write_text(
        candidate_sha + "\n", encoding="utf-8"
    )
    (reports / "smoke-1.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "git_sha": report_sha,
                "completed_at_unix": 1,
            }
        ),
        encoding="utf-8",
    )
    write_valid_gate_reports(reports, candidate_sha)
    (runtime / "current").symlink_to(release)
    return runtime, credential


def promotion_environment(runtime: Path, credential: Path) -> dict[str, str]:
    return {
        **os.environ,
        "DOC_AGENT_TEST_RUNTIME_ROOT": str(runtime),
        "DOC_AGENT_TEST_GIT_CREDENTIAL_FILE": str(credential),
        "DOC_AGENT_TEST_BRANCH": "dev",
        "DOC_AGENT_PRODUCTION_BRANCH": "production",
        "DOC_AGENT_TEST_RUNTIME_UNIT": "",
    }


def build_docker_test_runtime(
    tmp_path: Path,
    *,
    origin: Path,
    candidate_sha: str,
) -> tuple[Path, Path, Path]:
    runtime = tmp_path / "docker-test-runtime"
    source = tmp_path / "test-source.git"
    state = runtime / "state"
    reports = runtime / "reports"
    credential = tmp_path / "git-auth" / "credentials"
    manager = runtime / "control/deploy/docker-test/manage.sh"

    git("clone", "--bare", str(origin), str(source))
    state.mkdir(parents=True)
    reports.mkdir(parents=True)
    credential.parent.mkdir(parents=True)
    manager.parent.mkdir(parents=True)
    credential.write_text("", encoding="utf-8")
    credential.chmod(0o600)
    manager.write_text(
        "#!/usr/bin/env bash\nset -Eeuo pipefail\n"
        "[[ \"${1:-}\" == status || \"${1:-}\" == smoke ]]\n",
        encoding="utf-8",
    )
    manager.chmod(0o755)
    (state / "deployment.env").write_text(
        f"ACTIVE_SOURCE_SHA={candidate_sha}\nDEPLOYMENT_MODE=primary\n",
        encoding="utf-8",
    )
    (state / "auto-update.success-sha").write_text(
        candidate_sha + "\n", encoding="utf-8"
    )
    (reports / "smoke-1.json").write_text(
        json.dumps(
            {
                "status": "passed",
                "git_sha": candidate_sha,
                "completed_at_unix": 1,
            }
        ),
        encoding="utf-8",
    )
    write_valid_gate_reports(reports, candidate_sha)
    return runtime, source, credential


def test_tested_sha_fast_forwards_production_without_moving_main(tmp_path: Path):
    origin, formal_sha, candidate_sha = build_remote(tmp_path)
    runtime, credential = build_test_runtime(
        tmp_path,
        origin=origin,
        candidate_sha=candidate_sha,
        report_sha=candidate_sha,
    )

    subprocess.run(
        [str(PROMOTE_SCRIPT), candidate_sha],
        check=True,
        capture_output=True,
        text=True,
        env=promotion_environment(runtime, credential),
    )

    assert (
        git("--git-dir", str(origin), "rev-parse", "refs/heads/production")
        == candidate_sha
    )
    assert git("--git-dir", str(origin), "rev-parse", "refs/heads/main") == formal_sha
    assert (runtime / "state/production-promotion.success-sha").read_text(
        encoding="utf-8"
    ).strip() == candidate_sha


def test_docker_primary_test_can_fast_forward_production(tmp_path: Path):
    origin, formal_sha, candidate_sha = build_remote(tmp_path)
    runtime, source, credential = build_docker_test_runtime(
        tmp_path,
        origin=origin,
        candidate_sha=candidate_sha,
    )
    environment = {
        **os.environ,
        "DOC_AGENT_TEST_RUNTIME_ROOT": str(runtime),
        "DOC_AGENT_TEST_SOURCE_GIT_DIR": str(source),
        "DOC_AGENT_TEST_GIT_CREDENTIAL_FILE": str(credential),
        "DOC_AGENT_TEST_BRANCH": "dev",
        "DOC_AGENT_PRODUCTION_BRANCH": "production",
        "DOC_AGENT_TEST_RUNTIME_TIMER": "",
    }

    subprocess.run(
        [str(PROMOTE_SCRIPT), candidate_sha],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert (
        git("--git-dir", str(origin), "rev-parse", "refs/heads/production")
        == candidate_sha
    )
    assert git("--git-dir", str(origin), "rev-parse", "refs/heads/main") == formal_sha
    assert (runtime / "state/production-promotion.success-sha").read_text(
        encoding="utf-8"
    ).strip() == candidate_sha


def test_stale_smoke_report_cannot_move_production(tmp_path: Path):
    origin, formal_sha, candidate_sha = build_remote(tmp_path)
    runtime, credential = build_test_runtime(
        tmp_path,
        origin=origin,
        candidate_sha=candidate_sha,
        report_sha=formal_sha,
    )

    result = subprocess.run(
        [str(PROMOTE_SCRIPT), candidate_sha],
        check=False,
        capture_output=True,
        text=True,
        env=promotion_environment(runtime, credential),
    )

    assert result.returncode != 0
    assert (
        git("--git-dir", str(origin), "rev-parse", "refs/heads/production")
        == formal_sha
    )


def test_missing_quality_report_cannot_move_production(tmp_path: Path):
    origin, formal_sha, candidate_sha = build_remote(tmp_path)
    runtime, credential = build_test_runtime(
        tmp_path,
        origin=origin,
        candidate_sha=candidate_sha,
        report_sha=candidate_sha,
    )
    (runtime / "reports/quality-1.json").unlink()

    result = subprocess.run(
        [str(PROMOTE_SCRIPT), candidate_sha],
        check=False,
        capture_output=True,
        text=True,
        env=promotion_environment(runtime, credential),
    )

    assert result.returncode != 0
    assert "离线全量质量门禁" in result.stderr
    assert (
        git("--git-dir", str(origin), "rev-parse", "refs/heads/production")
        == formal_sha
    )


def test_failed_quality_report_cannot_move_production(tmp_path: Path):
    origin, formal_sha, candidate_sha = build_remote(tmp_path)
    runtime, credential = build_test_runtime(
        tmp_path,
        origin=origin,
        candidate_sha=candidate_sha,
        report_sha=candidate_sha,
    )
    quality_path = runtime / "reports/quality-1.json"
    quality = json.loads(quality_path.read_text(encoding="utf-8"))
    quality["status"] = "failed"
    quality_path.write_text(json.dumps(quality), encoding="utf-8")

    result = subprocess.run(
        [str(PROMOTE_SCRIPT), candidate_sha],
        check=False,
        capture_output=True,
        text=True,
        env=promotion_environment(runtime, credential),
    )

    assert result.returncode != 0
    assert (
        git("--git-dir", str(origin), "rev-parse", "refs/heads/production")
        == formal_sha
    )


def test_stale_browser_report_cannot_move_production(tmp_path: Path):
    origin, formal_sha, candidate_sha = build_remote(tmp_path)
    runtime, credential = build_test_runtime(
        tmp_path,
        origin=origin,
        candidate_sha=candidate_sha,
        report_sha=candidate_sha,
    )
    browser_path = runtime / "reports/browser-1.json"
    browser = json.loads(browser_path.read_text(encoding="utf-8"))
    browser["git_sha"] = formal_sha
    browser_path.write_text(json.dumps(browser), encoding="utf-8")

    result = subprocess.run(
        [str(PROMOTE_SCRIPT), candidate_sha],
        check=False,
        capture_output=True,
        text=True,
        env=promotion_environment(runtime, credential),
    )

    assert result.returncode != 0
    assert "真实浏览器冒烟" in result.stderr
    assert (
        git("--git-dir", str(origin), "rev-parse", "refs/heads/production")
        == formal_sha
    )


def test_failed_runtime_health_cannot_move_production(tmp_path: Path):
    origin, formal_sha, candidate_sha = build_remote(tmp_path)
    runtime, credential = build_test_runtime(
        tmp_path,
        origin=origin,
        candidate_sha=candidate_sha,
        report_sha=candidate_sha,
    )
    manage = runtime / "current/deploy/local-test/manage.sh"
    manage.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    manage.chmod(0o755)

    result = subprocess.run(
        [str(PROMOTE_SCRIPT), candidate_sha],
        check=False,
        capture_output=True,
        text=True,
        env=promotion_environment(runtime, credential),
    )

    assert result.returncode != 0
    assert (
        git("--git-dir", str(origin), "rev-parse", "refs/heads/production")
        == formal_sha
    )


def build_production_runtime(tmp_path: Path, origin: Path) -> tuple[Path, Path]:
    runtime = tmp_path / "production-runtime"
    source = runtime / "source.git"
    credential = runtime / "config/git-auth/credentials"
    backend_env = runtime / "config/backend.env"
    git("clone", "--bare", str(origin), str(source))
    credential.parent.mkdir(parents=True)
    credential.write_text("", encoding="utf-8")
    credential.chmod(0o600)
    backend_env.write_text("", encoding="utf-8")
    return runtime, credential


def prepare_environment(runtime: Path, credential: Path) -> dict[str, str]:
    return {
        **os.environ,
        "DOC_AGENT_PRODUCTION_RUNTIME_ROOT": str(runtime),
        "DOC_AGENT_PRODUCTION_GIT_CREDENTIAL_FILE": str(credential),
        "DOC_AGENT_PRODUCTION_BRANCH": "production",
    }


def test_production_prepare_rejects_sha_outside_production(tmp_path: Path):
    origin, _formal_sha, candidate_sha = build_remote(tmp_path)
    runtime, credential = build_production_runtime(tmp_path, origin)

    result = subprocess.run(
        [str(PREPARE_SCRIPT), candidate_sha],
        check=False,
        capture_output=True,
        text=True,
        env=prepare_environment(runtime, credential),
    )

    assert result.returncode != 0
    assert "不是 origin/production 当前 tip" in result.stderr
    assert not (runtime / "releases" / candidate_sha).exists()


def test_production_prepare_rejects_stale_ancestor_of_branch_tip(tmp_path: Path):
    origin, formal_sha, candidate_sha = build_remote(tmp_path)
    git(
        "--git-dir",
        str(origin),
        "update-ref",
        "refs/heads/production",
        candidate_sha,
    )
    runtime, credential = build_production_runtime(tmp_path, origin)

    result = subprocess.run(
        [str(PREPARE_SCRIPT), formal_sha],
        check=False,
        capture_output=True,
        text=True,
        env=prepare_environment(runtime, credential),
    )

    assert result.returncode != 0
    assert "不是 origin/production 当前 tip" in result.stderr
    assert not (runtime / "releases" / formal_sha).exists()
