"""Safe end-to-end smoke test for the isolated local deployment.

The application flow writes only to the isolated SQLite checkpoint files and
local trace directories. Existing MySQL metadata and knowledge infrastructure
are queried read-only; this test never logs in, creates models, ingests files,
or mutates a shared vector store.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DEVICE_ID = "gdev_123e4567-e89b-42d3-a456-426614174000"
ALLOWED_HEALTH_STATUSES = {"healthy"}


class SmokeError(RuntimeError):
    """A safe business-flow assertion failed."""


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise SmokeError(f"required setting is missing: {name}")
    return value


def request(
    method: str,
    url: str,
    *,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> tuple[int, dict[str, str], bytes]:
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "doc-agent-isolated-smoke/1",
        **(headers or {}),
    }
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        url,
        data=payload,
        headers=request_headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            response_headers = {
                key.lower(): value for key, value in response.headers.items()
            }
            return response.status, response_headers, response.read()
    except urllib.error.HTTPError as exc:
        response_headers = {key.lower(): value for key, value in exc.headers.items()}
        return exc.code, response_headers, exc.read()


def json_request(
    method: str,
    url: str,
    *,
    expected: int = 200,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> Any:
    status, _response_headers, payload = request(
        method,
        url,
        body=body,
        headers=headers,
        timeout=timeout,
    )
    if status != expected:
        preview = payload.decode("utf-8", errors="replace")[:1000]
        raise SmokeError(f"{method} {url} returned HTTP {status}: {preview}")
    try:
        return json.loads(payload or b"{}")
    except json.JSONDecodeError as exc:
        raise SmokeError(f"{method} {url} did not return JSON") from exc


@contextmanager
def read_only_mysql() -> Iterator[Any]:
    import pymysql

    connection = pymysql.connect(
        host=required_env("MYSQL_HOST"),
        port=int(required_env("MYSQL_PORT")),
        user=required_env("MYSQL_USER"),
        password=required_env("MYSQL_PASSWORD"),
        database=required_env("MYSQL_DATABASE"),
        charset="utf8mb4",
        connect_timeout=8,
        read_timeout=30,
        write_timeout=8,
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        yield connection
    finally:
        connection.close()


def select_smoke_identity(connection: Any) -> dict[str, Any]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT k.id, k.api_key, k.user_id, COALESCE(u.role, 'user'),
                   u.username, COALESCE(u.display_name, u.username)
            FROM api_keys AS k
            JOIN users AS u ON u.user_id = k.user_id
            WHERE k.is_active = 1
              AND u.is_active = 1
              AND COALESCE(k.access_scope, 'full') = 'full'
              AND (k.expires_at IS NULL OR k.expires_at > NOW())
            ORDER BY (LOWER(COALESCE(u.role, '')) = 'admin') DESC, k.id
            LIMIT 1
            """
        )
        key_row = cursor.fetchone()
        if not key_row or not str(key_row[1] or "").strip():
            raise SmokeError("existing MySQL has no usable full-scope API key")

        cursor.execute(
            """
            SELECT name
            FROM llm_models
            WHERE usage_scope = 'chat' AND is_active = 1
            ORDER BY is_default DESC, sort_order, id
            LIMIT 1
            """
        )
        model_row = cursor.fetchone()
        if not model_row or not str(model_row[0] or "").strip():
            raise SmokeError("existing MySQL has no active chat model")

    return {
        "api_key_id": int(key_row[0]),
        "api_key": str(key_row[1]),
        "user_id": str(key_row[2]),
        "role": str(key_row[3] or "user"),
        "username": str(key_row[4] or ""),
        "display_name": str(key_row[5] or key_row[4] or ""),
        "model": str(model_row[0]),
    }


def active_model_scopes(connection: Any) -> tuple[set[str], set[str]]:
    """Read only model identifiers; never expose credentials from the registry."""
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT name, COALESCE(usage_scope, '')
            FROM llm_models
            WHERE is_active = 1
            """
        )
        rows = cursor.fetchall()
    chat_models: set[str] = set()
    non_chat_models: set[str] = set()
    for name, usage_scope in rows:
        normalized_name = str(name or "").strip()
        if not normalized_name:
            continue
        target = (
            chat_models
            if str(usage_scope or "").strip() == "chat"
            else non_chat_models
        )
        target.add(normalized_name)
    return chat_models, non_chat_models


@contextmanager
def isolated_bearer(identity: dict[str, Any]) -> Iterator[str]:
    """Create one short-lived test session only in the isolated auth Redis DB."""
    from redis import Redis

    shared_db = int(os.getenv("REDIS_DB", "0") or 0)
    auth_db = int(required_env("AUTH_REDIS_DB"))
    if auth_db == shared_db:
        raise SmokeError("AUTH_REDIS_DB must differ from the shared Redis DB")
    common = {
        "host": required_env("REDIS_HOST"),
        "port": int(required_env("REDIS_PORT")),
        "password": os.getenv("REDIS_PASSWORD") or None,
        "decode_responses": True,
        "socket_connect_timeout": 5,
        "socket_timeout": 10,
    }
    auth_redis = Redis(db=auth_db, **common)
    shared_redis = Redis(db=shared_db, **common)
    token = secrets.token_urlsafe(32)
    token_key = f"auth:token:{token}"
    epoch_key = f"auth:session_epoch:{identity['user_id']}"
    try:
        raw_epoch = auth_redis.get(epoch_key)
        try:
            session_epoch = max(0, int(raw_epoch or 0))
        except (TypeError, ValueError) as exc:
            raise SmokeError("isolated auth session epoch is malformed") from exc
        auth_redis.setex(
            token_key,
            300,
            json.dumps(
                {
                    "user_id": identity["user_id"],
                    "username": identity["username"],
                    "display_name": identity["display_name"],
                    "role": identity["role"],
                    "session_epoch": session_epoch,
                },
                ensure_ascii=False,
            ),
        )
        if shared_redis.exists(token_key):
            raise SmokeError("isolated bearer token leaked into the shared Redis DB")
        yield token
    finally:
        auth_redis.delete(token_key)
        auth_redis.close()
        shared_redis.close()


def conversation_count(connection: Any, thread_ids: list[str]) -> int:
    placeholders = ", ".join(["%s"] * len(thread_ids))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT COUNT(*) FROM conversations WHERE thread_id IN ({placeholders})",
            tuple(thread_ids),
        )
        row = cursor.fetchone()
    return int(row[0] if row else 0)


def health_result(name: str, url: str) -> dict[str, Any]:
    payload = json_request("GET", url)
    status = str(payload.get("status") or "").lower()
    if status not in ALLOWED_HEALTH_STATUSES:
        raise SmokeError(f"{name} has unexpected health status: {status or 'missing'}")
    components = payload.get("components")
    component_states = {}
    if isinstance(components, dict):
        component_states = {
            str(key): str((value or {}).get("status") or "")
            for key, value in components.items()
            if isinstance(value, dict)
        }
    return {
        "name": name,
        "http_status": 200,
        "service_status": status,
        "components": component_states,
    }


def frontend_result(name: str, base_url: str) -> dict[str, Any]:
    status, _headers, payload = request(
        "GET",
        f"{base_url}/",
        headers={"Accept": "text/html"},
    )
    if status != 200 or not payload:
        raise SmokeError(f"{name} frontend returned HTTP {status} or an empty body")
    return {"name": name, "http_status": status, "bytes": len(payload)}


def assert_vite_guard(name: str, base_url: str) -> dict[str, Any]:
    status, _headers, _payload = request("GET", f"{base_url}/@fs/etc/passwd")
    if status != 403:
        raise SmokeError(f"{name} filesystem guard returned {status}, expected 403")
    return {"name": f"{name}-vite-fs-guard", "http_status": status}


def profile_proxy_result(
    name: str,
    base_url: str,
    api_key: str,
) -> dict[str, Any]:
    payload = json_request(
        "GET",
        f"{base_url}/api/v2/agent-runtime/profiles",
        headers={"X-API-Key": api_key},
    )
    profiles = payload.get("profiles") if isinstance(payload, dict) else None
    if not isinstance(profiles, list) or not any(
        str(item.get("code") or "") == "ne"
        for item in profiles
        if isinstance(item, dict)
    ):
        raise SmokeError(f"{name} API proxy did not expose the ne agent profile")
    return {"name": f"{name}-api-proxy", "profile_count": len(profiles)}


def validate_model_catalog(
    payload: Any,
    *,
    selected_model: str,
    active_chat_models: set[str],
    active_non_chat_models: set[str],
) -> dict[str, Any]:
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list) or not models:
        raise SmokeError("knowledge API proxy returned no existing chat models")
    exposed_models = {
        str(item.get("id") or item.get("name") or "").strip()
        for item in models
        if isinstance(item, dict)
    }
    exposed_models.discard("")
    if selected_model not in exposed_models:
        raise SmokeError("selected existing chat model is absent from the public catalog")
    if not exposed_models.issubset(active_chat_models):
        raise SmokeError("public model catalog exposed a model outside the chat scope")
    leaked_models = exposed_models.intersection(active_non_chat_models)
    if leaked_models:
        raise SmokeError("public model catalog leaked an active non-chat model")
    default_model = str(payload.get("default_model") or "").strip()
    if not default_model or default_model not in exposed_models:
        raise SmokeError("public model catalog has no valid chat default")
    return {
        "name": "chat-model-scope-isolation",
        "model_count": len(models),
        "selected_model_present": True,
        "default_model_present": True,
        "active_non_chat_models_excluded": len(active_non_chat_models),
    }


def knowledge_proxy_result(
    base_url: str,
    *,
    selected_model: str,
    active_chat_models: set[str],
    active_non_chat_models: set[str],
) -> dict[str, Any]:
    payload = json_request("GET", f"{base_url}/knowledge-api/api/v1/rag/models")
    return validate_model_catalog(
        payload,
        selected_model=selected_model,
        active_chat_models=active_chat_models,
        active_non_chat_models=active_non_chat_models,
    )


def files_proxy_result(base_url: str, unique_suffix: str) -> dict[str, Any]:
    status, _headers, _payload = request(
        "GET",
        f"{base_url}/files/isolated-read-probe-{unique_suffix}",
    )
    if status not in {403, 404}:
        raise SmokeError(
            f"read-only MinIO proxy probe returned unexpected HTTP {status}"
        )
    return {
        "name": "existing-minio-read-only-proxy",
        "http_status": status,
        "write_attempted": False,
    }


def parse_sse(
    payload: bytes,
    *,
    guest: bool,
    require_follow_up: bool = False,
) -> dict[str, Any]:
    event_types: list[str] = []
    visible_parts: list[str] = []
    errors: list[str] = []
    follow_up_suggestions: list[str] = []
    saw_done = False
    for raw_line in payload.decode("utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            saw_done = True
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("type") or "")
        event_types.append(event_type)
        body = event if guest else event.get("payload")
        if not isinstance(body, dict):
            body = {}
        if event_type in {"message.delta", "message.replace", "chunk"}:
            content = str(body.get("content") or "")
            if content:
                visible_parts.append(content)
        if event_type == "error":
            errors.append(str(body.get("message") or body.get("error") or "error"))
        if event_type in {"follow_up.created", "follow_up_suggestions"}:
            raw_suggestions = body.get("suggestions")
            if isinstance(raw_suggestions, list):
                follow_up_suggestions.extend(
                    str(item).strip() for item in raw_suggestions if str(item).strip()
                )
        if event_type == "done":
            saw_done = True

    if errors:
        raise SmokeError(f"agent stream emitted an error event: {errors[0]}")
    visible_text = "".join(visible_parts).strip()
    if not saw_done:
        raise SmokeError("agent stream did not emit a terminal done event")
    if not visible_text:
        raise SmokeError("agent stream completed without visible answer content")
    if require_follow_up and not follow_up_suggestions:
        raise SmokeError("agent stream did not emit usable follow-up suggestions")
    return {
        "bytes": len(payload),
        "event_count": len(event_types),
        "event_types": sorted(set(event_types)),
        "visible_chars": len(visible_text),
        "answer_sha256": hashlib.sha256(visible_text.encode("utf-8")).hexdigest()[:16],
        "follow_up_suggestion_count": len(follow_up_suggestions),
    }


def run_stream(
    name: str,
    url: str,
    *,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
    guest: bool = False,
    require_follow_up: bool = False,
) -> dict[str, Any]:
    status, response_headers, payload = request(
        "POST",
        url,
        body=body,
        headers=headers,
        timeout=360,
    )
    if status != 200:
        preview = payload.decode("utf-8", errors="replace")[:1000]
        raise SmokeError(f"{name} returned HTTP {status}: {preview}")
    content_type = response_headers.get("content-type", "").lower()
    if "text/event-stream" not in content_type:
        raise SmokeError(f"{name} returned unexpected content type: {content_type}")
    return {
        "name": name,
        **parse_sse(
            payload,
            guest=guest,
            require_follow_up=require_follow_up,
        ),
    }


def sqlite_checkpoint_count(path: Path) -> int:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as database:
        row = database.execute("SELECT COUNT(*) FROM checkpoints").fetchone()
    return int(row[0] if row else 0)


def wait_for_checkpoint(path: Path, external_thread_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 45
    pattern = f"%__t_{external_thread_id}"
    while time.monotonic() < deadline:
        uri = f"file:{path.resolve()}?mode=ro"
        try:
            with sqlite3.connect(uri, uri=True, timeout=10) as database:
                row = database.execute(
                    """
                    SELECT thread_id, COUNT(*)
                    FROM checkpoints
                    WHERE thread_id LIKE ?
                    GROUP BY thread_id
                    ORDER BY COUNT(*) DESC
                    LIMIT 1
                    """,
                    (pattern,),
                ).fetchone()
            if row:
                return {
                    "name": f"{path.name}-isolated-checkpoint",
                    "checkpoint_rows": int(row[1]),
                    "thread_suffix_verified": True,
                }
        except sqlite3.Error:
            pass
        time.sleep(1)
    raise SmokeError(
        f"isolated checkpoint was not persisted in {path.name} for the smoke thread"
    )


def verify_migrated_baseline(data_dir: Path, marker_path: Path) -> dict[str, Any]:
    if not marker_path.is_file():
        raise SmokeError(f"checkpoint migration marker is missing: {marker_path}")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("format_version") != 2:
        raise SmokeError("unsupported checkpoint migration marker format")
    databases = marker.get("databases")
    if not isinstance(databases, dict) or "checkpoints.db" not in databases:
        raise SmokeError("checkpoint migration marker has no main database")

    counts: dict[str, int] = {}
    for name, expected in databases.items():
        if name not in {"checkpoints.db", "guest_checkpoints.db"}:
            raise SmokeError(f"unexpected database in migration marker: {name}")
        current = sqlite_checkpoint_count(data_dir / name)
        baseline = int((expected or {}).get("checkpoint_rows") or 0)
        if current < baseline:
            raise SmokeError(f"{name} checkpoint count fell below migrated baseline")
        counts[name] = current
    return {
        "name": "conversation-checkpoint-migration",
        "format_version": 2,
        "database_counts": counts,
        "mysql_metadata_mode": marker.get("mysql_metadata_mode"),
    }


def verify_conversation_catalog(
    catalog_path: Path,
    marker_path: Path,
    *,
    user_id: str,
    thread_id: str,
) -> dict[str, Any]:
    if not marker_path.is_file():
        raise SmokeError(f"conversation catalog migration marker is missing: {marker_path}")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker.get("format_version") != 1:
        raise SmokeError("unsupported conversation catalog migration marker format")
    uri = f"file:{catalog_path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as database:
        integrity = database.execute("PRAGMA quick_check").fetchone()
        count_row = database.execute("SELECT COUNT(*) FROM conversations").fetchone()
        smoke_row = database.execute(
            """
            SELECT title, is_deleted, metadata
            FROM conversations
            WHERE user_id = ? AND thread_id = ?
            """,
            (user_id, thread_id),
        ).fetchone()
    if not integrity or integrity[0] != "ok":
        raise SmokeError("isolated conversation catalog quick_check failed")
    if not smoke_row or int(smoke_row[1] or 0) != 0:
        raise SmokeError("authenticated smoke conversation was not saved locally")
    metadata = json.loads(smoke_row[2] or "{}")
    runtime_metadata = metadata.get("agent_runtime") if isinstance(metadata, dict) else None
    if not isinstance(runtime_metadata, dict) or runtime_metadata.get("agent_code") != "ne":
        raise SmokeError("local smoke conversation has no valid agent metadata")
    current_rows = int(count_row[0] if count_row else 0)
    baseline_rows = int((marker.get("catalog") or {}).get("rows") or 0)
    if current_rows < baseline_rows:
        raise SmokeError("isolated conversation catalog fell below its migrated baseline")
    return {
        "name": "isolated-conversation-catalog",
        "format_version": 1,
        "rows": current_rows,
        "migrated_baseline_rows": baseline_rows,
        "smoke_thread_saved": True,
    }


def write_report(report: dict[str, Any]) -> Path:
    report_dir = Path(
        os.getenv(
            "DOC_AGENT_TEST_REPORT_DIR",
            "/home/inspur/myenv/runtime/doc-agent-test/reports",
        )
    )
    report_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(report_dir, 0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=".smoke-",
        suffix=".tmp",
        dir=report_dir,
    )
    report_path = report_dir / f"smoke-{int(time.time())}.json"
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(report, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.chmod(temporary_name, 0o600)
        Path(temporary_name).replace(report_path)
        os.chmod(report_path, 0o600)
    finally:
        if Path(temporary_name).exists():
            Path(temporary_name).unlink()
    return report_path


def main() -> int:
    runtime_root = Path(
        os.getenv(
            "DOC_AGENT_TEST_RUNTIME_ROOT",
            "/home/inspur/myenv/runtime/doc-agent-test",
        )
    )
    agent_api = f"http://127.0.0.1:{required_env('TEST_AGENT_PORT')}"
    guest_api = f"http://127.0.0.1:{required_env('TEST_GUEST_API_PORT')}"
    knowledge_api = f"http://127.0.0.1:{required_env('TEST_KNOWLEDGE_PORT')}"
    frontends = {
        "frontend": f"http://127.0.0.1:{required_env('TEST_FRONTEND_PORT')}",
        "audit-frontend": (
            f"http://127.0.0.1:{required_env('TEST_AUDIT_FRONTEND_PORT')}"
        ),
        "csh-frontend": (f"http://127.0.0.1:{required_env('TEST_CSH_FRONTEND_PORT')}"),
        "fmca-frontend": (
            f"http://127.0.0.1:{required_env('TEST_FMCA_FRONTEND_PORT')}"
        ),
        "landslide-frontend": (
            f"http://127.0.0.1:{required_env('TEST_LANDSLIDE_FRONTEND_PORT')}"
        ),
        "guest-frontend": (
            f"http://127.0.0.1:{required_env('TEST_GUEST_FRONTEND_PORT')}"
        ),
    }
    unique_suffix = f"{int(time.time())}-{os.getpid()}"
    agent_thread = f"isolated-test-agent-{unique_suffix}"
    guest_thread = f"isolated-test-guest-{unique_suffix}"

    results: list[dict[str, Any]] = []
    with read_only_mysql() as connection:
        identity = select_smoke_identity(connection)
        active_chat_models, active_non_chat_models = active_model_scopes(connection)
        before_conversations = conversation_count(
            connection,
            [agent_thread, guest_thread],
        )
    if before_conversations != 0:
        raise SmokeError("new smoke thread IDs unexpectedly already exist in MySQL")

    api_key = identity["api_key"]
    auth_headers = {"X-API-Key": api_key}

    results.extend(
        [
            health_result("agent-health", f"{agent_api}/health"),
            health_result("knowledge-health", f"{knowledge_api}/health"),
            health_result("guest-health", f"{guest_api}/health"),
        ]
    )
    for name, base_url in frontends.items():
        results.append(frontend_result(name, base_url))
        results.append(assert_vite_guard(name, base_url))
        results.append(profile_proxy_result(name, base_url, api_key))

    results.append(
        knowledge_proxy_result(
            frontends["frontend"],
            selected_model=identity["model"],
            active_chat_models=active_chat_models,
            active_non_chat_models=active_non_chat_models,
        )
    )
    results.append(files_proxy_result(frontends["frontend"], unique_suffix))

    with isolated_bearer(identity) as token:
        bearer_headers = {"Authorization": f"Bearer {token}"}
        verification = json_request(
            "GET",
            f"{frontends['frontend']}/api/v1/auth/verify",
            headers=bearer_headers,
        )
        if not verification.get("valid"):
            raise SmokeError("isolated bearer verification did not return valid=true")
        results.append(
            {
                "name": "isolated-auth-redis-session",
                "verified": True,
                "auth_redis_db": int(required_env("AUTH_REDIS_DB")),
                "shared_redis_db": int(os.getenv("REDIS_DB", "0") or 0),
            }
        )

        results.append(
            run_stream(
                "authenticated-agent-business-flow",
                f"{frontends['frontend']}/api/v2/agent-runtime/query",
                headers=auth_headers,
                require_follow_up=True,
                body={
                    "question": "请用一句简短中文回复：隔离测试流程已连通。",
                    "agent_code": "ne",
                    "thread_id": agent_thread,
                    "workflow": "lightweight",
                    "model": identity["model"],
                    "use_search": False,
                    "kb_ids": [],
                    "selected_skills": [],
                    "subagent_enabled": False,
                    "thinking": False,
                    "metadata": {"source": "isolated-safe-smoke"},
                },
            )
        )

        conversations = json_request(
            "GET",
            f"{frontends['frontend']}/api/v2/conversations?limit=100&offset=0",
            headers=bearer_headers,
        )
        if not isinstance(conversations, list) or not any(
            isinstance(item, dict) and item.get("thread_id") == agent_thread
            for item in conversations
        ):
            raise SmokeError("saved smoke conversation is absent from the user list API")
        results.append(
            {
                "name": "conversation-list-via-isolated-session",
                "smoke_thread_present": True,
            }
        )
    results.append(
        wait_for_checkpoint(
            runtime_root / "data/checkpoints/checkpoints.db",
            agent_thread,
        )
    )

    results.append(
        run_stream(
            "anonymous-guest-business-flow",
            f"{frontends['guest-frontend']}/api/v2/guest/query",
            headers={"X-Device-ID": DEVICE_ID},
            guest=True,
            require_follow_up=True,
            body={
                "question": "请用一句简短中文回复：访客隔离测试已连通。",
                "thread_id": guest_thread,
                "agent_code": "ne",
                "model": identity["model"],
                "use_search": False,
                "kb_ids": [],
                "subagent_enabled": False,
            },
        )
    )
    results.append(
        wait_for_checkpoint(
            runtime_root / "data/checkpoints/guest_checkpoints.db",
            guest_thread,
        )
    )

    results.append(
        verify_migrated_baseline(
            runtime_root / "data/checkpoints",
            runtime_root / "state/checkpoint-migration.json",
        )
    )
    results.append(
        verify_conversation_catalog(
            runtime_root / "data/conversations/conversations.db",
            runtime_root / "state/conversation-catalog-migration.json",
            user_id=identity["user_id"],
            thread_id=agent_thread,
        )
    )

    with read_only_mysql() as connection:
        after_conversations = conversation_count(
            connection,
            [agent_thread, guest_thread],
        )
    if after_conversations != before_conversations:
        raise SmokeError(
            "isolated test unexpectedly wrote conversation metadata to MySQL"
        )
    results.append(
        {
            "name": "shared-mysql-conversation-write-guard",
            "read_only": True,
            "matching_rows_before": before_conversations,
            "matching_rows_after": after_conversations,
        }
    )

    report = {
        "status": "passed",
        "git_sha": os.getenv("DOC_AGENT_TEST_RELEASE_SHA", "worktree"),
        "completed_at_unix": int(time.time()),
        "mode": "isolated-apps-shared-external-services",
        "safety": {
            "mysql_session_read_only": True,
            "shared_vector_write_attempted": False,
            "shared_object_write_attempted": False,
            "login_attempted": False,
            "isolated_bearer_seeded": True,
            "worker_started": False,
        },
        "selected_existing_model": identity["model"],
        "selected_api_key_id": identity["api_key_id"],
        "results": results,
    }
    report_path = write_report(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (SmokeError, OSError, sqlite3.Error, ValueError) as exc:
        print(f"isolated smoke test failed: {exc}", file=sys.stderr)
        sys.exit(1)
