#!/usr/bin/env python3
"""Atomically render non-secret production service overrides.

External service endpoints and credentials remain in the operator-owned
``backend.env``.  This file only owns application-local paths and safety
switches loaded by the production systemd units.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import stat
import sys
from pathlib import Path

KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_GROUNDWATER_BUILTIN_ROOT = Path(
    "/home/inspur/myenv/runtime/doc-agent-shared/groundwater/liutang"
)


class ServiceEnvError(RuntimeError):
    """A production service environment safety condition failed."""


def decode_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise ServiceEnvError("unterminated single-quoted environment value")
        return value[1:-1]
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            raise ServiceEnvError("unterminated double-quoted environment value")
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ServiceEnvError("invalid double-quoted environment value") from exc
        return str(decoded)
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def read_env(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file():
        raise ServiceEnvError("service environment must be a regular file")
    if path.stat().st_uid != os.getuid():
        raise ServiceEnvError("service environment owner is not the current user")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ServiceEnvError(f"invalid environment line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise ServiceEnvError(f"invalid environment key on line {line_number}")
        values[key] = decode_value(raw_value)
    return values


def confined(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.expanduser().resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ServiceEnvError(
            f"{label} must stay inside the production runtime"
        ) from exc
    return resolved


def render(args: argparse.Namespace) -> dict[str, object]:
    runtime_root = args.runtime_root.expanduser().resolve(strict=True)
    source = confined(args.source, runtime_root, label="source")
    output = confined(args.output, runtime_root, label="output")
    expected_config_root = runtime_root / "config"
    for label, path in (("source", source), ("output", output)):
        try:
            path.relative_to(expected_config_root)
        except ValueError as exc:
            raise ServiceEnvError(f"{label} must stay inside runtime config") from exc

    values = read_env(source)
    data_dir = runtime_root / "data"
    groundwater_root = args.groundwater_builtin_root.expanduser()
    if not groundwater_root.is_absolute():
        raise ServiceEnvError("groundwater builtin root must be absolute")
    if not groundwater_root.is_dir():
        raise ServiceEnvError("groundwater builtin root does not exist")
    groundwater_manifest = groundwater_root / "manifest.json"
    if groundwater_manifest.is_symlink() or not groundwater_manifest.is_file():
        raise ServiceEnvError("groundwater builtin manifest must be a regular file")
    overrides = {
        "APP_ENV": "production",
        "DOC_AGENT_RUNTIME_ROOT": str(runtime_root),
        "DOC_AGENT_PRODUCTION_RUNTIME_ROOT": str(runtime_root),
        "AGENT_CHECKPOINT_DATA_DIR": str(data_dir),
        "GUEST_CHECKPOINT_DATA_DIR": str(data_dir),
        "AGENT_TASK_DB_PATH": str(data_dir / "agent_tasks.db"),
        "AGENT_TRACE_INDEXED_ROOT": str(data_dir / "traces-indexed"),
        "AGENT_CONVERSATION_WRITES_ENABLED": "true",
        "AGENT_CONVERSATION_STORE_BACKEND": "sqlite",
        "AGENT_CONVERSATION_SQLITE_PATH": str(
            data_dir / "conversations" / "conversations.db"
        ),
        # Other production business tables remain writable.  Only the
        # conversation catalog is redirected away from MySQL.
        "MYSQL_SESSION_READ_ONLY": "false",
        "STARTUP_SCHEMA_INITIALIZATION_ENABLED": "false",
        "TASK_DISPATCH_OUTBOX_ENABLED": (
            "true" if args.enable_task_outbox else "false"
        ),
        "API_DOCS_ENABLED": "false",
        "RAG_WORK_DIR": str(data_dir / "rag"),
        "STORAGE_BASE_PATH": str(data_dir / "storage"),
        "DOCUMENTS_PATH": str(data_dir / "storage" / "documents"),
        "RESULTS_PATH": str(data_dir / "storage" / "results"),
        "CACHE_PATH": str(data_dir / "storage" / "cache"),
        "UPLOAD_TEMP_DIR": str(data_dir / "storage" / "uploads"),
        "GROUNDWATER_BUILTIN_ROOT": str(groundwater_root),
        "LOG_DIR": str(runtime_root / "logs" / "backend"),
        "LOG_FILE": str(runtime_root / "logs" / "backend" / "app.log"),
    }
    values.update(overrides)

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output.parent, 0o700)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    if temporary.exists() or temporary.is_symlink():
        raise ServiceEnvError("temporary service environment path already exists")
    lines = [
        "# Generated production service overrides; contains no external credentials.",
        "# External MySQL/Redis/Milvus/Nebula/MinIO/LLM settings stay in backend.env.",
        *(f"{key}={shlex.quote(value)}" for key, value in sorted(values.items())),
    ]
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
    temporary.replace(output)
    os.chmod(output, stat.S_IRUSR | stat.S_IWUSR)
    return {
        "status": "rendered",
        "output": str(output),
        "conversation_metadata_backend": "production-runtime-sqlite",
        "mysql_global_read_only": False,
        "task_dispatch_outbox_enabled": bool(args.enable_task_outbox),
        "api_docs_enabled": False,
        "external_service_values_changed": False,
        "groundwater_builtin_root": str(groundwater_root),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument(
        "--groundwater-builtin-root",
        type=Path,
        default=DEFAULT_GROUNDWATER_BUILTIN_ROOT,
    )
    parser.add_argument("--enable-task-outbox", action="store_true")
    return parser


def main() -> int:
    try:
        result = render(build_parser().parse_args())
    except (OSError, ServiceEnvError, UnicodeError) as exc:
        print(f"production service environment render failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
