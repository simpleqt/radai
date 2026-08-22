from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
AUTO_UPDATE = REPO_ROOT / "deploy/docker-test/auto-update.sh"
INSTALL_USER_UNITS = REPO_ROOT / "deploy/docker-test/install-user-units.sh"
MANAGER = REPO_ROOT / "deploy/docker-test/manage.sh"
USER_SYSTEMD_DIRS = (
    REPO_ROOT / "deploy/docker-test/systemd",
    REPO_ROOT / "deploy/production/systemd",
)


def test_user_service_units_do_not_require_capability_management():
    """User managers on the target host cannot apply capability directives."""
    forbidden = (
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "ProtectKernelModules=true",
    )

    for systemd_dir in USER_SYSTEMD_DIRS:
        for service_path in sorted(systemd_dir.glob("*.service")):
            content = service_path.read_text(encoding="utf-8")
            for directive in forbidden:
                assert directive not in content, (
                    f"{service_path.relative_to(REPO_ROOT)} contains unsupported "
                    f"user-systemd directive {directive}"
                )


def test_control_installer_rejects_sha_other_than_checked_out_commit(tmp_path: Path):
    result = subprocess.run(
        [str(INSTALL_USER_UNITS)],
        check=False,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "DOC_AGENT_DOCKER_TEST_ROOT": str(tmp_path / "runtime"),
            "DOC_AGENT_DOCKER_CONTROL_SHA": "0" * 40,
        },
    )

    assert result.returncode != 0
    assert "控制面 SHA 与当前检出不一致" in result.stderr
    assert not (tmp_path / "runtime").exists()


def test_manager_has_verified_no_runtime_impact_transition():
    content = MANAGER.read_text(encoding="utf-8")

    assert "advance_unaffected_target" in content
    assert "assert_container_ids_unchanged" in content
    assert "未构建、未重启" in content


def test_manager_does_not_recreate_unaffected_compose_dependencies():
    content = MANAGER.read_text(encoding="utf-8")

    assert (
        'compose_with_env "${candidate}" up -d --no-deps --wait '
        '--wait-timeout 900 "${services[@]}"'
    ) in content
    assert (
        'compose_with_env "${previous}" up -d --no-deps --wait '
        '--wait-timeout 900 "${restore_services[@]}"'
    ) in content
    assert 'compose_with_env "${candidate}" rm -s -f audit-frontend' in content


def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_source(tmp_path: Path, *, include_manager: bool) -> tuple[Path, str]:
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git("init", "--bare", str(origin))
    git("init", "-b", "dev", str(seed))
    git("config", "user.name", "Docker Update Test", cwd=seed)
    git("config", "user.email", "docker-update@example.invalid", cwd=seed)

    docker_deploy = seed / "deploy/docker-test"
    local_deploy = seed / "deploy/local-test"
    shared_data_deploy = seed / "deploy/shared-data"
    docker_deploy.mkdir(parents=True)
    local_deploy.mkdir(parents=True)
    shared_data_deploy.mkdir(parents=True)
    shutil.copy2(AUTO_UPDATE, docker_deploy / "auto-update.sh")
    (local_deploy / "marker.txt").write_text("local-test\n", encoding="utf-8")
    (shared_data_deploy / "install_groundwater.py").write_text(
        "#!/usr/bin/env python3\n", encoding="utf-8"
    )
    (seed / "deploy/quality-gate.json").write_text("{}\n", encoding="utf-8")
    if include_manager:
        manager = docker_deploy / "manage.sh"
        manager.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        manager.chmod(0o755)

    git("add", "deploy", cwd=seed)
    git("commit", "-m", "test control release", cwd=seed)
    sha = git("rev-parse", "HEAD", cwd=seed)
    git("remote", "add", "origin", str(origin), cwd=seed)
    git("push", "-u", "origin", "dev", cwd=seed)
    git("--git-dir", str(origin), "symbolic-ref", "HEAD", "refs/heads/dev")

    source = tmp_path / "source.git"
    git("clone", "--bare", str(origin), str(source))
    return source, sha


def build_runtime(tmp_path: Path, sha: str) -> tuple[Path, Path]:
    runtime = tmp_path / "runtime"
    state = runtime / "state"
    credential = tmp_path / "git-auth/credentials"
    state.mkdir(parents=True)
    credential.parent.mkdir(parents=True)
    credential.write_text("", encoding="utf-8")
    credential.chmod(0o600)
    (state / "deployment.env").write_text(
        f"ACTIVE_SOURCE_SHA={sha}\nDEPLOYMENT_MODE=primary\n",
        encoding="utf-8",
    )
    return runtime, credential


def update_environment(runtime: Path, source: Path, credential: Path) -> dict[str, str]:
    return {
        **os.environ,
        "DOC_AGENT_DOCKER_TEST_ROOT": str(runtime),
        "DOC_AGENT_DOCKER_SOURCE_GIT": str(source),
        "DOC_AGENT_DOCKER_GIT_CREDENTIAL_FILE": str(credential),
        "DOC_AGENT_DOCKER_TEST_BRANCH": "dev",
    }


def test_no_change_materializes_immutable_control_without_redeploy(tmp_path: Path):
    source, sha = build_source(tmp_path, include_manager=True)
    runtime, credential = build_runtime(tmp_path, sha)

    result = subprocess.run(
        [str(AUTO_UPDATE)],
        check=True,
        capture_output=True,
        text=True,
        env=update_environment(runtime, source, credential),
    )

    control_release = runtime / "control-releases" / sha
    assert (control_release / ".source-sha").read_text(encoding="utf-8").strip() == sha
    assert (control_release / "deploy/quality-gate.json").read_text(
        encoding="utf-8"
    ) == "{}\n"
    assert (runtime / "control").resolve() == control_release
    assert (runtime / "bin/auto-update.sh").read_bytes() == (
        control_release / "deploy/docker-test/auto-update.sh"
    ).read_bytes()
    assert (runtime / "state/auto-update.success-sha").read_text(
        encoding="utf-8"
    ).strip() == sha
    assert "无需重启" in result.stdout


def test_invalid_control_release_is_removed(tmp_path: Path):
    source, sha = build_source(tmp_path, include_manager=False)
    runtime, credential = build_runtime(tmp_path, sha)

    result = subprocess.run(
        [str(AUTO_UPDATE)],
        check=False,
        capture_output=True,
        text=True,
        env=update_environment(runtime, source, credential),
    )

    assert result.returncode != 0
    assert not (runtime / "control-releases" / sha).exists()
    assert not list((runtime / "control-releases").glob(f".{sha}.*"))
    assert "缺少 Docker 测试控制器" in result.stderr
