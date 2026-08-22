from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.datastructures import UploadFile
from starlette.requests import Request

import backend.storage as storage_module
from backend.api.dependencies import auth as auth_dependencies
from backend.api.dependencies.auth import ApiKeyAuth
from backend.api.routers import files as files_router
from backend.api.routers import my_files as my_files_router
from backend.api.routers import uploads as uploads_router
from backend.api.routers import uploads_chunked as uploads_chunked_router
from backend.api.routers import uploads_receipts as uploads_receipts_module
from backend.api.routers.chat_files import _resolve_public_remote_target
from backend.api.routers.guest_agent import (
    GuestQueryRequest,
    _guest_workflow_name,
    query_guest,
    resolve_device_id,
)
from backend.application.event_stream.sse_encoder import SSEEncoder
from backend.domain.agents.events import AgentEvent
from backend.storage import ChunkedUploadManager


def _chunk_manager(tmp_path: Path) -> ChunkedUploadManager:
    manager = ChunkedUploadManager.__new__(ChunkedUploadManager)
    manager.temp_dir = tmp_path / "uploads"
    manager.chunk_size = 4
    manager.expire_hours = 1
    manager._ensure_directories()
    return manager


def test_completion_receipt_lookup_recovers_lost_upload_id(tmp_path, monkeypatch):
    receipt_dir = tmp_path / "completed"
    receipt_dir.mkdir()
    monkeypatch.setattr(uploads_receipts_module, "_completion_receipt_dir", receipt_dir)
    upload_id = "4033f6ae-d97e-45ed-b8d0-cc065b45b00d"
    response = uploads_router.CompleteUploadResponse(
        success=True,
        total=649,
        message="completed",
    )
    uploads_router._write_completion_receipt(
        upload_id,
        user_id="user-a",
        kb_id="kb-1",
        filename="archive.zip",
        total_size=123,
        total_chunks=12,
        response=response,
    )

    recovered = uploads_router._find_completion_receipt(
        user_id="user-a",
        kb_id="kb-1",
        filename="archive.zip",
        total_size=123,
    )
    assert recovered is not None
    assert recovered["upload_id"] == upload_id
    assert recovered["response"]["total"] == 649
    assert uploads_router._find_completion_receipt(
        user_id="user-b",
        kb_id="kb-1",
        filename="archive.zip",
        total_size=123,
    ) is None


def test_chunk_upload_is_owned_bounded_atomic_and_completes(tmp_path, monkeypatch):
    manager = _chunk_manager(tmp_path)
    monkeypatch.setattr(
        storage_module.storage_manager,
        "generate_storage_path",
        lambda filename, storage_type: ("file-1", str(tmp_path / "merged.bin")),
    )
    upload = manager.init_upload(
        "demo.bin",
        8,
        "application/octet-stream",
        user_id="user-a",
    )
    upload_id = upload["upload_id"]

    assert manager.get_upload_session(upload_id, user_id="user-b") is None
    assert not manager.save_chunk(upload_id, 0, b"1234", user_id="user-b")["success"]
    assert not manager.save_chunk(upload_id, -1, b"1", user_id="user-a")["success"]
    assert not manager.save_chunk(upload_id, 0, b"12345", user_id="user-a")["success"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: manager.save_chunk(upload_id, *args, user_id="user-a"),
                [(0, b"1234"), (1, b"5678")],
            )
        )
    assert all(result["success"] for result in results)
    assert manager.get_upload_progress(upload_id, user_id="user-a")["uploaded_chunks"] == 2

    session_before_merge = manager.get_upload_session(upload_id, user_id="user-a")
    assert session_before_merge["filename"] == "demo.bin"
    success, _, merged_path = manager.complete_upload(upload_id, user_id="user-a")
    assert success is True
    assert Path(merged_path).read_bytes() == b"12345678"
    assert manager.get_upload_session(upload_id, user_id="user-a") is None


def test_chunk_upload_rejects_non_uuid_path(tmp_path):
    manager = _chunk_manager(tmp_path)
    with pytest.raises(ValueError, match="上传会话 ID"):
        manager.get_upload_session("..", user_id="user-a")


def test_generated_local_file_must_be_inside_users_artifact_root(tmp_path, monkeypatch):
    monkeypatch.setattr(
        my_files_router,
        "get_paths",
        lambda: SimpleNamespace(base_dir=tmp_path),
    )
    allowed = tmp_path / "users" / "user-a" / "threads" / "t1" / "files" / "artifacts" / "ok.txt"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("ok", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")

    assert my_files_router._validated_generated_local_path(str(allowed), "user-a") == allowed
    with pytest.raises(HTTPException) as exc_info:
        my_files_router._validated_generated_local_path(str(outside), "user-a")
    assert exc_info.value.status_code == 403


def test_download_path_guard_rejects_other_user_and_outside_path(tmp_path, monkeypatch):
    monkeypatch.setattr(
        files_router,
        "get_paths",
        lambda: SimpleNamespace(base_dir=tmp_path),
    )
    allowed = tmp_path / "users" / "user-a" / "threads" / "t1" / "files" / "artifacts" / "ok.txt"
    allowed.parent.mkdir(parents=True)
    allowed.write_text("ok", encoding="utf-8")

    assert files_router._is_allowed_local_user_file_path(str(allowed), "user-a")
    assert not files_router._is_allowed_local_user_file_path(str(allowed), "user-b")
    assert not files_router._is_allowed_local_user_file_path("/etc/passwd", "user-a")


@pytest.mark.asyncio
async def test_private_remote_image_targets_are_rejected():
    # 私网/回环/非 http(s) 目标应被拒绝(返回 None,而非可连接的 pinned 元组)。
    assert await _resolve_public_remote_target("http://127.0.0.1/private.png") is None
    assert await _resolve_public_remote_target("http://[::1]/private.png") is None
    assert await _resolve_public_remote_target("file:///etc/passwd") is None


@pytest.mark.asyncio
async def test_guest_policy_error_preserves_http_status():
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 1),
            "scheme": "http",
        }
    )
    with pytest.raises(HTTPException) as exc_info:
        await query_guest(GuestQueryRequest(question="x", mode="deep-research"), request)
    assert exc_info.value.status_code == 400


def test_guest_request_rejects_unbounded_or_invalid_identifiers():
    with pytest.raises(ValidationError):
        GuestQueryRequest(question="x", thread_id="bad:thread")
    with pytest.raises(ValidationError):
        GuestQueryRequest(question="x", kb_ids=["kb"] * 65)
    with pytest.raises(ValidationError):
        GuestQueryRequest(question="x", kb_ids=["x" * 129])


def test_csh_and_ne_guests_follow_full_profile_default() -> None:
    assert _guest_workflow_name("csh") == "full"
    assert _guest_workflow_name("ne") == "full"


def test_guest_device_scope_rejects_weak_identifiers_and_is_stable(monkeypatch):
    monkeypatch.setattr(
        "backend.api.routers.guest_agent.app_settings.minio_secret_key",
        "test-signing-secret",
    )

    def request_with_device(value: str) -> Request:
        return Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/",
                "headers": [(b"x-device-id", value.encode("ascii"))],
                "query_string": b"",
                "server": ("test", 80),
                "client": ("test", 1),
                "scheme": "http",
            }
        )

    with pytest.raises(HTTPException) as exc_info:
        resolve_device_id(request_with_device("shared-device"))
    assert exc_info.value.status_code == 400

    raw = "gdev_123e4567-e89b-42d3-a456-426614174000"
    first = resolve_device_id(request_with_device(raw))
    second = resolve_device_id(request_with_device(raw))
    assert first == second
    assert raw not in first


@pytest.mark.asyncio
async def test_guest_health_reports_not_ready_components():
    from backend.api import guest_http_server

    guest_http_server.app.state.startup_components = {
        "rag": {"status": "error", "error": "ConnectionError"},
        "checkpointer": {"status": "ready", "error": ""},
    }
    response = await guest_http_server.health_check()
    assert response.status_code == 503

    guest_http_server.app.state.startup_components = {
        "rag": {"status": "ready", "error": ""},
        "checkpointer": {"status": "ready", "error": ""},
    }
    payload = await guest_http_server.health_check()
    assert payload["status"] == "healthy"

    guest_http_server.app.state.startup_components = {
        "rag": {"status": "degraded", "error": "disabled by configuration"},
        "checkpointer": {"status": "ready", "error": ""},
    }
    payload = await guest_http_server.health_check()
    assert payload["status"] == "degraded"


@pytest.mark.asyncio
async def test_optional_auth_rejects_supplied_invalid_credentials(monkeypatch):
    async def invalid(_value):
        return None

    monkeypatch.setattr(auth_dependencies, "_resolve_api_key_auth", invalid)
    with pytest.raises(HTTPException) as exc_info:
        await auth_dependencies.optional_verify_api_key(
            x_api_key="invalid",
            authorization=None,
        )
    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_legacy_kb_chunk_upload_enforces_owner_bounds_and_size(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads_router._legacy_chunk_manager, "temp_dir", tmp_path / "legacy")
    uploads_router._legacy_chunk_manager._ensure_directories()
    require_access = AsyncMock()
    monkeypatch.setattr(uploads_chunked_router, "require_kb_access", require_access)
    session = object()
    owner = ApiKeyAuth(api_key="a", user_id="user-a")
    other = ApiKeyAuth(api_key="b", user_id="user-b")
    initialized = await uploads_router.init_chunked_upload(
        uploads_router.UploadInitRequest(
            kb_id="kb-1",
            filename="demo.txt",
            file_size=3,
            content_type="text/plain",
        ),
        owner,
        session,
    )
    require_access.assert_awaited_once_with(session, "kb-1", owner, access="write")

    with pytest.raises(HTTPException) as exc_info:
        await uploads_router.upload_chunk(
            initialized.upload_id,
            0,
            UploadFile(file=BytesIO(b"abc"), filename="chunk"),
            other,
        )
    assert exc_info.value.status_code == 404

    with pytest.raises(HTTPException) as exc_info:
        await uploads_router.upload_chunk(
            initialized.upload_id,
            1,
            UploadFile(file=BytesIO(b"abc"), filename="chunk"),
            owner,
        )
    assert exc_info.value.status_code == 400

    with pytest.raises(HTTPException) as exc_info:
        await uploads_router.upload_chunk(
            initialized.upload_id,
            0,
            UploadFile(file=BytesIO(b"ab"), filename="chunk"),
            owner,
        )
    assert exc_info.value.status_code == 400

    result = await uploads_router.upload_chunk(
        initialized.upload_id,
        0,
        UploadFile(file=BytesIO(b"abc"), filename="chunk"),
        owner,
    )
    assert result.progress == 100

    status = await uploads_router.get_chunked_upload_status(
        initialized.upload_id,
        owner,
        session,
    )
    assert status.upload_id == initialized.upload_id
    assert status.file_id == initialized.file_id
    assert status.kb_id == "kb-1"
    assert status.filename == "demo.txt"
    assert status.uploaded_chunks == [0]
    assert status.progress == 100
    assert require_access.await_count == 2

    with pytest.raises(HTTPException) as exc_info:
        await uploads_router.get_chunked_upload_status(
            initialized.upload_id,
            other,
            session,
        )
    assert exc_info.value.status_code == 404

    assert uploads_router._legacy_chunk_manager.cancel_upload(
        initialized.upload_id,
        user_id=owner.user_id,
    )


@pytest.mark.asyncio
async def test_sse_queue_applies_backpressure():
    produced = 0

    async def events():
        nonlocal produced
        for sequence in range(100):
            produced += 1
            yield AgentEvent(
                type="message.delta",
                request_id="r",
                thread_id="t",
                sequence=sequence,
                payload={"content": "x"},
            )

    stream = SSEEncoder(queue_maxsize=2).encode_stream(events())
    first = await anext(stream)
    assert "message.delta" in first
    await asyncio.sleep(0.01)
    assert produced <= 4
    await stream.aclose()
