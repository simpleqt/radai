#!/usr/bin/env python3
"""Execute one command with owner-only systemd-style environment files.

The deployment shell must never ``source`` a runtime environment file: values
such as passwords can contain shell metacharacters, and an environment file is
configuration rather than executable shell code.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

try:
    from .render_service_env import ServiceEnvError, read_env
except ImportError:  # Executed directly from the installed runtime bin directory.
    from render_service_env import ServiceEnvError, read_env


def build_environment(paths: list[Path]) -> dict[str, str]:
    environment = dict(os.environ)
    for path in paths:
        values = read_env(path)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ServiceEnvError("environment file permissions are not private")
        environment.update(values)
    return environment


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", action="append", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        print("run-with-env requires a command", file=sys.stderr)
        return 2
    try:
        environment = build_environment(args.env_file)
        os.execvpe(command[0], command, environment)
    except (OSError, ServiceEnvError, UnicodeError) as exc:
        print(f"run-with-env failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
