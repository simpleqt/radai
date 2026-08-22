"""Render the isolated test runtime from the current production configuration.

All existing external service endpoints and credentials are copied verbatim from
the operator-owned source ``.env``.  Only application-local paths, ports and
safety switches are overridden.  The generated file is shell-quoted, atomic and
owner-readable only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REQUIRED_EXTERNAL_KEYS = (
    "MYSQL_HOST",
    "MYSQL_PORT",
    "MYSQL_USER",
    "MYSQL_PASSWORD",
    "MYSQL_DATABASE",
    "REDIS_HOST",
    "REDIS_PORT",
    "MINIO_ENDPOINT",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "KAFKA_BOOTSTRAP_SERVERS",
    "MILVUS_URI",
    "NEBULA_HOST",
    "NEBULA_GRAPH_PORT",
)


class ConfigError(RuntimeError):
    """The source environment is missing or unsafe."""


def _decode_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""
    if value.startswith("'"):
        if len(value) < 2 or "'" not in value[1:]:
            raise ConfigError("unterminated single-quoted environment value")
        return value[1 : value.rfind("'")]
    if value.startswith('"'):
        end = len(value) - 1
        while end > 0 and value[end].isspace():
            end -= 1
        if end <= 0 or value[end] != '"':
            raise ConfigError("unterminated double-quoted environment value")
        try:
            return json.loads(value[: end + 1])
        except json.JSONDecodeError:
            return bytes(value[1:end], "utf-8").decode("unicode_escape")
    return re.split(r"\s+#", value, maxsplit=1)[0].rstrip()


def read_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise ConfigError(f"source environment does not exist: {path}")
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(f"invalid environment line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not KEY_PATTERN.fullmatch(key):
            raise ConfigError(f"invalid environment key on line {line_number}")
        values[key] = _decode_value(raw_value)
    return values


def detect_public_host() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("1.1.1.1", 53))
            return str(probe.getsockname()[0])
    except OSError:
        return "127.0.0.1"


def minio_proxy_target(values: dict[str, str]) -> str:
    public_url = values.get("MINIO_PUBLIC_BASE_URL", "").strip()
    if public_url:
        parsed = urlsplit(public_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    endpoint = values.get("MINIO_ENDPOINT", "").strip()
    if endpoint.startswith(("http://", "https://")):
        parsed = urlsplit(endpoint)
        if parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"
    scheme = (
        "https"
        if values.get("MINIO_SECURE", "false").strip().lower()
        in {"1", "true", "yes", "on"}
        else "http"
    )
    return f"{scheme}://{endpoint}"


def render(args: argparse.Namespace) -> dict[str, object]:
    source = args.source.resolve()
    output = args.output.resolve()
    runtime_root = args.runtime_root.resolve()
    values = read_env(source)
    missing = [key for key in REQUIRED_EXTERNAL_KEYS if not values.get(key, "").strip()]
    if missing:
        raise ConfigError(
            "source environment is missing required external settings: "
            + ", ".join(missing)
        )

    public_host = (args.public_host or "").strip() or detect_public_host()
    data_dir = runtime_root / "data"
    log_dir = runtime_root / "logs" / "backend"
    overrides = {
        "APP_ENV": "test",
        "DOC_AGENT_TEST_CONFIG_MODE": "shared-existing-v1",
        "DOC_AGENT_TEST_CONFIG_SOURCE": str(source),
        "DOC_AGENT_TEST_CONFIG_SOURCE_SHA256": hashlib.sha256(
            source.read_bytes()
        ).hexdigest(),
        "DOC_AGENT_TEST_PUBLIC_HOST": public_host,
        "DOC_AGENT_TEST_RUNTIME_ROOT": str(runtime_root),
        "TEST_AGENT_PORT": "18082",
        "TEST_GUEST_API_PORT": "18083",
        "TEST_GUEST_FRONTEND_PORT": "18084",
        "TEST_KNOWLEDGE_PORT": "18086",
        "TEST_FRONTEND_PORT": "15173",
        "TEST_CSH_FRONTEND_PORT": "15174",
        "TEST_FMCA_FRONTEND_PORT": "15176",
        "TEST_LANDSLIDE_FRONTEND_PORT": "15175",
        "TEST_ENABLE_KNOWLEDGE_API": "true",
        "TEST_ENABLE_WORKERS": "false",
        "TEST_FILES_PROXY_TARGET": minio_proxy_target(values),
        "SERVER_HOST": "0.0.0.0",
        "SERVER_PORT": "18082",
        "SERVER_RELOAD": "false",
        "SERVER_WORKERS": "1",
        "GUEST_SERVER_HOST": "0.0.0.0",
        "GUEST_SERVER_PORT": "18083",
        "GUEST_SERVER_WORKERS": "1",
        "KNOWLEDGE_SERVICE_BASE_URL": "http://127.0.0.1:18086",
        "RAG_SERVICE_BASE_URL": "http://127.0.0.1:18086",
        "AGENT_CHECKPOINTER_BACKEND": "sqlite",
        "AGENT_CHECKPOINT_DATA_DIR": str(data_dir / "checkpoints"),
        "GUEST_CHECKPOINT_DATA_DIR": str(data_dir / "checkpoints"),
        "MAIN_CHECKPOINT_DB": "checkpoints.db",
        "GUEST_CHECKPOINT_DB": "guest_checkpoints.db",
        "AGENT_CONVERSATION_WRITES_ENABLED": "true",
        "AGENT_CONVERSATION_STORE_BACKEND": "sqlite",
        "AGENT_CONVERSATION_SQLITE_PATH": str(
            data_dir / "conversations" / "conversations.db"
        ),
        "AGENT_SHARED_OBJECT_STORAGE_READ_ONLY": "true",
        "AGENT_RESOURCE_LEASE_BACKEND": "memory",
        "AGENT_RUNTIME_STORE_BACKEND": "disabled",
        "AGENT_TRACE_BACKEND": "jsonl",
        "AGENT_TRACE_BLOB_BACKEND": "local",
        "AGENT_TRACE_ROOT": str(data_dir / "traces"),
        "AGENT_TRACE_INDEXED_ROOT": str(data_dir / "traces-indexed"),
        "MYSQL_SESSION_READ_ONLY": "true",
        "AUTH_REDIS_DB": "15",
        "HTTP_RATE_LIMIT_REDIS_ENABLED": "false",
        "STARTUP_SCHEMA_INITIALIZATION_ENABLED": "false",
        "STARTUP_RAG_INITIALIZATION_ENABLED": "true",
        "GUEST_RAG_STARTUP_ENABLED": "true",
        "HEALTH_ALLOW_EXPECTED_DEGRADED": "false",
        "KNOWLEDGE_API_STATELESS_REPLICA": "true",
        "STARTUP_TASK_PROCESSOR_ENABLED": "false",
        "STARTUP_RAG_PREWARM_ENABLED": "false",
        "STARTUP_EMBEDDING_PREWARM_ENABLED": "false",
        # HanLP 分词预热与生产对齐：自 3c423c2 起模型烘焙进后端镜像
        # （HANLP_HOME 只读缓存），启动预热仅 ~7s 本地加载，不再触发
        # 389MB 下载；关闭它会把代价转嫁到首个真实分词请求。
        "STARTUP_TOKENIZER_PREWARM_ENABLED": "true",
        "STARTUP_KEYWORD_INDEX_PREWARM_ENABLED": "false",
        "STARTUP_KEYWORD_GRAPH_PREWARM_ENABLED": "false",
        "TASK_DISPATCH_OUTBOX_ENABLED": "false",
        "FOLLOW_UP_SUGGESTIONS_ENABLED": "true",
        "LOG_KAFKA_ENABLED": "false",
        "SELF_INGESTION_ENABLED": "false",
        "STORAGE_BASE_PATH": str(data_dir / "storage"),
        "DOCUMENTS_PATH": str(data_dir / "storage" / "documents"),
        "RESULTS_PATH": str(data_dir / "storage" / "results"),
        "CACHE_PATH": str(data_dir / "storage" / "cache"),
        "UPLOAD_TEMP_DIR": str(data_dir / "storage" / "uploads"),
        "RAG_WORK_DIR": str(data_dir / "rag"),
        "LOG_DIR": str(log_dir),
        "LOG_FILE": str(log_dir / "app.log"),
        "LOG_KAFKA_TOPIC": "disabled-in-isolated-test",
    }
    values.update(overrides)

    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")
    lines = [
        "# Generated from the current external-service configuration.",
        "# Do not commit this file: it contains operator-managed credentials.",
    ]
    lines.extend(f"{key}={shlex.quote(value)}" for key, value in sorted(values.items()))
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(output)
    os.chmod(output, 0o600)
    return {
        "status": "rendered",
        "mode": overrides["DOC_AGENT_TEST_CONFIG_MODE"],
        "output": str(output),
        "public_host": public_host,
        "external_settings_reused": list(REQUIRED_EXTERNAL_KEYS),
        "safety": {
            "mysql_session_read_only": True,
            "conversation_metadata_backend": "isolated-sqlite",
            "auth_redis_db": 15,
            "shared_object_storage_read_only": True,
            "external_workers": False,
            "rag_startup_initialization": True,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--public-host")
    return parser


def main() -> int:
    try:
        result = render(build_parser().parse_args())
    except (ConfigError, OSError) as exc:
        print(f"runtime environment render failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
