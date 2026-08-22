"""Map a production Git diff to the formal systemd units that must restart.

The production runtime can safely contain processes from different immutable
release SHAs.  A shared backend change restarts all API and worker units, while
each deployed frontend restarts only its own unit(s).  Control-plane and test
files do not recycle business processes.  Unknown runtime paths deliberately
fall back to all units.
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

ALL_UNITS: Final[tuple[str, ...]] = (
    "doc-agent-prod-knowledge-api.service",
    "doc-agent-prod-agent-api.service",
    "doc-agent-prod-guest-api.service",
    "doc-agent-prod-frontend-5173.service",
    "doc-agent-prod-frontend-5177.service",
    "doc-agent-prod-frontend-5174.service",
    "doc-agent-prod-frontend-5176.service",
    "doc-agent-prod-task-worker.service",
    "doc-agent-prod-ingest-worker.service",
)
BACKEND_UNITS: Final[tuple[str, ...]] = (
    "doc-agent-prod-knowledge-api.service",
    "doc-agent-prod-agent-api.service",
    "doc-agent-prod-guest-api.service",
    "doc-agent-prod-task-worker.service",
    "doc-agent-prod-ingest-worker.service",
)
FRONTEND_PREFIXES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "frontend/",
        ("doc-agent-prod-frontend-5173.service",),
    ),
    ("frontend_audit/", ("doc-agent-prod-frontend-5177.service",)),
    ("frontend_csh_ai/", ("doc-agent-prod-frontend-5174.service",)),
    ("frontend_fmca/", ("doc-agent-prod-frontend-5176.service",)),
    # The guest UI currently has no formal production systemd unit.
    ("guest/", ()),
)
SYSTEMD_UNIT_IMPACTS: Final[dict[str, tuple[str, ...]]] = {
    f"deploy/production/systemd/{unit}": (unit,) for unit in ALL_UNITS
}
RETIRED_SYSTEMD_UNIT_PATHS: Final[frozenset[str]] = frozenset(
    {"deploy/production/systemd/doc-agent-prod-frontend-4173.service"}
)
BACKEND_ROOT_FILES: Final[frozenset[str]] = frozenset(
    {"pyproject.toml", "uv.lock", ".python-version"}
)
ONE_SHOT_BACKEND_FILES: Final[frozenset[str]] = frozenset(
    {
        "backend/scripts/download_cdut_share.py",
        "backend/scripts/import_university_audit_kb.py",
    }
)
NON_RUNTIME_PREFIXES: Final[tuple[str, ...]] = (
    ".agents/",
    ".codex/",
    ".cursor/",
    ".github/",
    ".gitlab/",
    ".trae/",
    # Tracked evaluator harness source only; datasets/results stay outside Git.
    "eval/",
    "backend/tests/",
    "deploy/",
    "docs/",
    # Root scripts are operator-run one-shot utilities, not long-lived
    # systemd service code. Service unit changes are mapped explicitly above.
    "scripts/",
    "tests/",
)
NON_RUNTIME_FILES: Final[frozenset[str]] = frozenset(
    {
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        "LICENSE",
        "Makefile",
        "README.md",
    }
)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def units_for_path(path: str) -> tuple[str, ...]:
    """Return ordered production units affected by one repository path."""

    normalized = path
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized:
        return ()
    if normalized.endswith((".md", ".mdx")):
        return ()
    if normalized in RETIRED_SYSTEMD_UNIT_PATHS:
        return ()
    if normalized in SYSTEMD_UNIT_IMPACTS:
        return SYSTEMD_UNIT_IMPACTS[normalized]
    if normalized.startswith(
        "deploy/production/systemd/doc-agent-prod-"
    ) and normalized.endswith(".service"):
        # A newly introduced production business unit has no safe narrow
        # mapping yet, so retain the normal unknown-runtime all-unit fallback.
        return ALL_UNITS
    for prefix, units in FRONTEND_PREFIXES:
        if normalized.startswith(prefix):
            return units
    if normalized in ONE_SHOT_BACKEND_FILES:
        return ()
    if normalized in BACKEND_ROOT_FILES or normalized.startswith("backend/"):
        if normalized.startswith("backend/tests/"):
            return ()
        return BACKEND_UNITS
    if normalized in NON_RUNTIME_FILES:
        return ()
    if normalized.startswith(NON_RUNTIME_PREFIXES):
        return ()
    return ALL_UNITS


def affected_units(paths: Iterable[str]) -> tuple[str, ...]:
    selected: set[str] = set()
    for path in paths:
        selected.update(units_for_path(path))
    return tuple(unit for unit in ALL_UNITS if unit in selected)


def changed_paths(git_dir: Path, old_sha: str, new_sha: str) -> tuple[str, ...]:
    for label, sha in (("old", old_sha), ("new", new_sha)):
        if not FULL_SHA.fullmatch(sha):
            raise ValueError(f"{label} SHA must be a full lowercase SHA")
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
        prefix=f".{path.name}.", dir=path.parent, text=True
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
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    paths = changed_paths(args.git_dir, args.old_sha, args.new_sha)
    units = affected_units(paths)
    atomic_write(args.plan, "".join(f"{unit}\n" for unit in units))
    atomic_write(
        args.report,
        json.dumps(
            {
                "format_version": 1,
                "old_sha": args.old_sha,
                "new_sha": args.new_sha,
                "changed_paths": list(paths),
                "units": list(units),
                "path_impacts": {path: list(units_for_path(path)) for path in paths},
            },
            ensure_ascii=True,
            indent=2,
        )
        + "\n",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
