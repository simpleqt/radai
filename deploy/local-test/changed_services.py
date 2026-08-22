"""Map a Git diff to isolated-test services that need a restart.

The mapper is deliberately service-oriented. Deployment tooling, tests, and
agent metadata can change without recycling business processes. Unknown
runtime paths fall back to all services so an unmapped production component is
never silently left on old code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Final

ALL_SERVICES: Final[tuple[str, ...]] = (
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
BACKEND_SERVICES: Final[tuple[str, ...]] = (
    "agent-api",
    "knowledge-api",
    "guest-api",
)
FRONTEND_SERVICES: Final[tuple[str, ...]] = (
    "frontend",
    "audit-frontend",
    "csh-frontend",
    "fmca-frontend",
    "landslide-frontend",
    "guest-frontend",
)
STANDARD_FRONTEND_SERVICES: Final[tuple[str, ...]] = (
    "frontend",
    "csh-frontend",
    "fmca-frontend",
    "landslide-frontend",
    "guest-frontend",
)
FRONTEND_PREFIXES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("frontend/", ("frontend",)),
    ("frontend_audit/", ("audit-frontend",)),
    ("frontend_csh_ai/", ("csh-frontend",)),
    ("frontend_fmca/", ("fmca-frontend",)),
    ("frontend_landslide/", ("landslide-frontend",)),
    ("guest/", ("guest-frontend",)),
)
BACKEND_ROOT_FILES: Final[frozenset[str]] = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        "deploy/local-test/render_runtime_env.py",
    }
)
ONE_SHOT_BACKEND_FILES: Final[frozenset[str]] = frozenset(
    {
        "backend/scripts/download_cdut_share.py",
        "backend/scripts/import_university_audit_kb.py",
    }
)
DOCKER_TEST_FILE_IMPACTS: Final[dict[str, tuple[str, ...]]] = {
    "deploy/docker-test/backend.Dockerfile": BACKEND_SERVICES,
    "deploy/docker-test/backend.dockerignore": BACKEND_SERVICES,
    "deploy/docker-test/audit-frontend.Dockerfile": ("audit-frontend",),
    "deploy/docker-test/compose.audit.yml": ("audit-frontend",),
    "deploy/docker-test/compose.shadow.yml": ALL_SERVICES,
    "deploy/docker-test/frontend.Dockerfile": STANDARD_FRONTEND_SERVICES,
    "deploy/docker-test/nginx/audit-entrypoint.sh": ("audit-frontend",),
    "deploy/docker-test/nginx/audit.conf.template": ("audit-frontend",),
}
DOCKER_TEST_CONTROL_FILES: Final[frozenset[str]] = frozenset(
    {
        "deploy/docker-test/auto-update.sh",
        "deploy/docker-test/browser_smoke.js",
        "deploy/docker-test/cutover-host-test.sh",
        "deploy/docker-test/install-user-units.sh",
        "deploy/docker-test/manage.sh",
        "deploy/docker-test/quality.Dockerfile",
        "deploy/docker-test/run_quality_gate.py",
        "deploy/docker-test/seed_state.py",
        "deploy/docker-test/state_snapshot.py",
    }
)
NON_RUNTIME_PREFIXES: Final[tuple[str, ...]] = (
    ".agents/",
    ".codex/",
    ".cursor/",
    ".github/",
    ".gitlab/",
    ".trae/",
    "backend/tests/",
    # Evaluator harnesses and datasets: operator-run one-shots that import
    # backend, never imported by container service code. Mirrors the
    # production changed_units.py classification; without this an eval-only
    # edit falls through to ALL_SERVICES and needlessly rebuilds every
    # frontend image and container.
    "eval/",
    "deploy/local-test/",
    "deploy/production/",
    "deploy/shared-data/",
    "docs/",
    # Root scripts are operator-run one-shot utilities, not container service
    # code. Docker/service-definition changes are mapped explicitly above.
    "scripts/",
    "tests/",
)
NON_RUNTIME_FILES: Final[frozenset[str]] = frozenset(
    {
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "deploy/quality-gate.json",
        "LICENSE",
        "Makefile",
        "README.md",
    }
)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def services_for_path(path: str) -> tuple[str, ...]:
    """Return the ordered services affected by one repository path."""

    normalized = path
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return ()
    if normalized in DOCKER_TEST_FILE_IMPACTS:
        return DOCKER_TEST_FILE_IMPACTS[normalized]
    if normalized.startswith("deploy/docker-test/nginx/"):
        return FRONTEND_SERVICES
    if normalized in DOCKER_TEST_CONTROL_FILES or normalized.startswith(
        "deploy/docker-test/systemd/"
    ):
        return ()
    for prefix, services in FRONTEND_PREFIXES:
        if normalized.startswith(prefix):
            return services
    if normalized in ONE_SHOT_BACKEND_FILES:
        return ()
    if normalized in BACKEND_ROOT_FILES or normalized.startswith("backend/"):
        if normalized.startswith("backend/tests/"):
            return ()
        return BACKEND_SERVICES
    if normalized in NON_RUNTIME_FILES or normalized.endswith((".md", ".mdx")):
        return ()
    if normalized.startswith(NON_RUNTIME_PREFIXES):
        return ()
    return ALL_SERVICES


def affected_services(paths: Iterable[str]) -> tuple[str, ...]:
    """Deduplicate path impacts while preserving service start order."""

    selected: set[str] = set()
    for path in paths:
        selected.update(services_for_path(path))
    return tuple(name for name in ALL_SERVICES if name in selected)


def changed_paths(git_dir: Path, old_sha: str, new_sha: str) -> tuple[str, ...]:
    for label, sha in (("old", old_sha), ("new", new_sha)):
        if not FULL_SHA.fullmatch(sha):
            raise ValueError(f"{label} SHA must be a full 40-character lowercase SHA")
    result = subprocess.run(
        [
            "git",
            f"--git-dir={git_dir}",
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            old_sha,
            new_sha,
            "--",
        ],
        check=True,
        capture_output=True,
    )
    return tuple(
        item.decode("utf-8", errors="surrogateescape")
        for item in result.stdout.split(b"\0")
        if item
    )


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
        os.chmod(temporary_name, 0o600)
        Path(temporary_name).replace(path)
        os.chmod(path, 0o600)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("old_sha")
    parser.add_argument("new_sha")
    parser.add_argument("--git-dir", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--report", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    paths = changed_paths(args.git_dir, args.old_sha, args.new_sha)
    services = affected_services(paths)
    plan_content = "".join(f"{name}\n" for name in services)
    if args.plan:
        atomic_write(args.plan, plan_content)
    else:
        print(plan_content, end="")
    if args.report:
        report = {
            "format_version": 1,
            "old_sha": args.old_sha,
            "new_sha": args.new_sha,
            "changed_paths": list(paths),
            "services": list(services),
            "path_impacts": {path: list(services_for_path(path)) for path in paths},
        }
        atomic_write(
            args.report,
            json.dumps(report, ensure_ascii=True, indent=2) + "\n",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
