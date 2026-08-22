"""Verify that a production deployment restarted exactly the planned units."""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FULL_SHA: Final = re.compile(r"^[0-9a-f]{40}$")


@dataclass(frozen=True)
class UnitState:
    pid: int
    cwd: Path


def read_snapshot(path: Path) -> dict[str, UnitState]:
    states: dict[str, UnitState] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        fields = raw_line.split("\t")
        if len(fields) != 3:
            raise ValueError(f"invalid snapshot line {line_number}: {path}")
        unit, raw_pid, raw_cwd = fields
        if unit in states or not raw_pid.isdigit() or int(raw_pid) <= 0:
            raise ValueError(f"invalid unit state on line {line_number}: {path}")
        states[unit] = UnitState(pid=int(raw_pid), cwd=Path(raw_cwd))
    return states


def release_sha(cwd: Path, releases_dir: Path) -> str:
    try:
        relative = cwd.relative_to(releases_dir)
    except ValueError as exc:
        raise ValueError(f"unit cwd is outside immutable releases: {cwd}") from exc
    if not relative.parts or not FULL_SHA.fullmatch(relative.parts[0]):
        raise ValueError(f"unit cwd has no immutable release SHA: {cwd}")
    return relative.parts[0]


def verify(
    before: dict[str, UnitState],
    after: dict[str, UnitState],
    planned: set[str],
    target_sha: str,
    releases_dir: Path,
) -> None:
    if before.keys() != after.keys():
        raise ValueError("before/after snapshots contain different unit sets")
    unknown = planned.difference(before)
    if unknown:
        raise ValueError(f"restart plan contains unknown units: {sorted(unknown)}")
    for unit in before:
        old = before[unit]
        new = after[unit]
        if unit in planned:
            if new.pid == old.pid:
                raise ValueError(f"planned unit retained its PID: {unit}")
            if release_sha(new.cwd, releases_dir) != target_sha:
                raise ValueError(f"planned unit does not run target SHA: {unit}")
        elif new != old:
            raise ValueError(f"unplanned unit changed PID or working directory: {unit}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--target-sha", required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    args = parser.parse_args()
    if not FULL_SHA.fullmatch(args.target_sha):
        parser.error("--target-sha must be a full lowercase SHA")
    before = read_snapshot(args.before)
    after = read_snapshot(args.after)
    planned_lines = args.plan.read_text(encoding="utf-8").splitlines()
    if len(planned_lines) != len(set(planned_lines)):
        raise ValueError("restart plan contains duplicate units")
    verify(
        before,
        after,
        set(planned_lines),
        args.target_sha,
        args.runtime_root / "releases",
    )
    print("production unit transition verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
