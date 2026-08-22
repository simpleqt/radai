"""Read-only connectivity checks for the existing shared infrastructure.

The checks deliberately avoid creating schemas, topics, buckets, collections,
spaces, keys or consumer groups.  Output contains only endpoint-independent
counts and status names; credentials are never printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from contextlib import suppress
from typing import Any, Callable
from urllib.parse import urlsplit

PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


class ProbeError(RuntimeError):
    """A required external service failed its read-only probe."""


def required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ProbeError(f"required setting is missing: {name}")
    return value


def integer(name: str, default: int | None = None) -> int:
    raw = os.getenv(name, "")
    if not raw and default is not None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ProbeError(f"{name} must be an integer") from exc


def stable_names_digest(names: list[str]) -> str:
    payload = "\n".join(sorted(names)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def disable_proxy_environment() -> None:
    """Match the application's direct-connect policy for internal services."""

    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)


def probe_mysql() -> dict[str, Any]:
    import pymysql

    connection = pymysql.connect(
        host=required("MYSQL_HOST"),
        port=integer("MYSQL_PORT"),
        user=required("MYSQL_USER"),
        password=required("MYSQL_PASSWORD"),
        database=required("MYSQL_DATABASE"),
        charset="utf8mb4",
        connect_timeout=8,
        read_timeout=15,
        write_timeout=8,
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("SELECT 1")
            cursor.fetchone()
            counts: dict[str, int] = {}
            for label, query in (
                ("conversations", "SELECT COUNT(*) FROM conversations"),
                ("knowledge_bases", "SELECT COUNT(*) FROM knowledge_bases"),
                (
                    "active_chat_models",
                    "SELECT COUNT(*) FROM llm_models "
                    "WHERE usage_scope='chat' AND is_active=1",
                ),
                (
                    "usable_full_api_keys",
                    "SELECT COUNT(*) FROM api_keys k "
                    "JOIN users u ON u.user_id=k.user_id "
                    "WHERE k.is_active=1 AND k.access_scope='full' "
                    "AND (k.expires_at IS NULL OR k.expires_at > NOW())",
                ),
            ):
                cursor.execute(query)
                row = cursor.fetchone()
                counts[label] = int(row[0] if row else 0)
        if counts["active_chat_models"] < 1:
            raise ProbeError("MySQL has no active chat model")
        if counts["usable_full_api_keys"] < 1:
            raise ProbeError("MySQL has no usable full API key")
        return {"status": "ready", "read_only": True, "counts": counts}
    finally:
        connection.close()


def probe_redis() -> dict[str, Any]:
    import redis

    client = redis.Redis(
        host=required("REDIS_HOST"),
        port=integer("REDIS_PORT", 6379),
        password=os.getenv("REDIS_PASSWORD") or None,
        db=integer("REDIS_DB", 0),
        socket_connect_timeout=5,
        socket_timeout=8,
        decode_responses=False,
    )
    try:
        if not client.ping():
            raise ProbeError("Redis PING returned false")
        return {
            "status": "ready",
            "read_only": True,
            "database_keys": int(client.dbsize()),
        }
    finally:
        client.close()


def _minio_endpoint() -> tuple[str, bool]:
    endpoint = required("MINIO_ENDPOINT")
    secure = os.getenv("MINIO_SECURE", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    if endpoint.startswith(("http://", "https://")):
        parsed = urlsplit(endpoint)
        endpoint = parsed.netloc
        secure = parsed.scheme == "https"
    return endpoint.rstrip("/"), secure


def probe_minio() -> dict[str, Any]:
    from minio import Minio

    endpoint, secure = _minio_endpoint()
    client = Minio(
        endpoint,
        access_key=required("MINIO_ACCESS_KEY"),
        secret_key=required("MINIO_SECRET_KEY"),
        secure=secure,
    )
    configured = [
        name
        for name in (
            os.getenv("MINIO_BUCKET_NAME", "").strip(),
            os.getenv("MINIO_USER_BUCKET_NAME", "").strip(),
        )
        if name
    ]
    presence = {name: bool(client.bucket_exists(name)) for name in configured}
    if configured and not all(presence.values()):
        raise ProbeError("one or more configured MinIO buckets do not exist")
    return {
        "status": "ready",
        "read_only": True,
        "configured_bucket_count": len(configured),
        "available_configured_bucket_count": sum(presence.values()),
    }


def probe_kafka() -> dict[str, Any]:
    from kafka import KafkaConsumer

    consumer = KafkaConsumer(
        bootstrap_servers=required("KAFKA_BOOTSTRAP_SERVERS").split(","),
        group_id=None,
        enable_auto_commit=False,
        request_timeout_ms=10000,
        consumer_timeout_ms=3000,
    )
    try:
        topics = sorted(consumer.topics())
        return {
            "status": "ready",
            "read_only": True,
            "topic_count": len(topics),
            "topic_names_digest": stable_names_digest(topics),
            "consumer_group_created": False,
        }
    finally:
        consumer.close(autocommit=False)


def _milvus_uri() -> str:
    uri = required("MILVUS_URI")
    if uri.startswith("grpc://"):
        return "tcp://" + uri[len("grpc://") :]
    if uri.startswith("grpcs://"):
        return "tcp://" + uri[len("grpcs://") :]
    if "://" not in uri:
        return "tcp://" + uri
    return uri


def probe_milvus() -> dict[str, Any]:
    from pymilvus import connections, utility

    alias = f"isolated_read_probe_{uuid.uuid4().hex}"
    kwargs: dict[str, Any] = {"alias": alias, "uri": _milvus_uri()}
    if os.getenv("MILVUS_USER", "").strip():
        kwargs["user"] = os.environ["MILVUS_USER"].strip()
    if os.getenv("MILVUS_PASSWORD", "").strip():
        kwargs["password"] = os.environ["MILVUS_PASSWORD"].strip()
    db_name = os.getenv("MILVUS_DB_NAME", "").strip()
    if db_name and db_name.lower() != "default":
        kwargs["db_name"] = db_name
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            connections.connect(**kwargs, timeout=8)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            connections.disconnect(alias)
            if attempt < 2:
                time.sleep(attempt + 1)
    if last_error is not None:
        raise last_error
    try:
        collections = sorted(utility.list_collections(using=alias, timeout=10))
        return {
            "status": "ready",
            "read_only": True,
            "collection_count": len(collections),
            "collection_names_digest": stable_names_digest(collections),
        }
    finally:
        connections.disconnect(alias)


def probe_nebula() -> dict[str, Any]:
    from nebula3.Config import Config
    from nebula3.gclient.net import ConnectionPool

    config = Config()
    config.max_connection_pool_size = 1
    pool = ConnectionPool()
    if not pool.init(
        [(required("NEBULA_HOST"), integer("NEBULA_GRAPH_PORT", 9669))],
        config,
    ):
        raise ProbeError("Nebula connection pool initialization failed")
    session = None
    try:
        session = pool.get_session(
            required("NEBULA_USER"),
            os.getenv("NEBULA_PASSWORD", ""),
        )
        result = session.execute("SHOW SPACES")
        if not result.is_succeeded():
            raise ProbeError("Nebula SHOW SPACES failed")
        names: list[str] = []
        for row in result.rows():
            with suppress(Exception):
                names.append(row.values[0].as_string().decode("utf-8"))
        return {
            "status": "ready",
            "read_only": True,
            "space_count": len(names),
            "space_names_digest": stable_names_digest(names),
        }
    finally:
        if session is not None:
            session.release()
        pool.close()


PROBES: tuple[tuple[str, Callable[[], dict[str, Any]]], ...] = (
    ("mysql", probe_mysql),
    ("redis", probe_redis),
    ("minio", probe_minio),
    ("kafka", probe_kafka),
    ("milvus", probe_milvus),
    ("nebula", probe_nebula),
)


def main() -> int:
    disable_proxy_environment()
    started = time.monotonic()
    results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for name, probe in PROBES:
        probe_started = time.monotonic()
        try:
            item = probe()
        except Exception as exc:
            item = {
                "status": "error",
                "error_type": type(exc).__name__,
            }
            failures.append(name)
        item["elapsed_ms"] = round((time.monotonic() - probe_started) * 1000)
        results[name] = item
    report = {
        "status": "passed" if not failures else "failed",
        "mode": "read-only-existing-services",
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "services": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        print(
            "external service probes failed: " + ", ".join(failures),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
