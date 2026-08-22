from __future__ import annotations

import asyncio
import io
import threading
from types import SimpleNamespace

import pytest
from fastapi import UploadFile
from starlette.requests import Request

from backend import storage as storage_module
from backend.api.routers import chat_files, my_files
from backend.services import user_file_service
from backend.services.chat_file_parser import ExtractedImage, ParseResult


class _Session:
    def __init__(self, *, commit_error: Exception | None = None):
        self.commit_error = commit_error
        self.added = []
        self.rolled_back = False

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        if self.commit_error is not None:
            raise self.commit_error

    async def rollback(self):
        self.rolled_back = True


class _Parser:
    async def parse(self, *_args, **_kwargs):
        return ParseResult(
            category="short_doc",
            method="python",
            content="content",
            content_length=7,
            mime_type="text/plain",
            images=[
                ExtractedImage(
                    data=b"image",
                    name="figure.png",
                    content_type="image/png",
                )
            ],
        )


@pytest.mark.asyncio
async def test_minio_upload_waits_for_started_thread_before_cancellation(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    class MinioClient:
        def put_object(self, *_args, **_kwargs):
            started.set()
            release.wait(timeout=2)

    monkeypatch.setattr(storage_module.minio_ops, "_get_minio_client", lambda: MinioClient())
    upload = asyncio.create_task(
        storage_module.upload_bytes_to_minio(
            b"content",
            "users/user-1/current-object.txt",
            bucket_name="users",
        )
    )
    for _ in range(100):
        if started.is_set():
            break
        await asyncio.sleep(0.001)
    assert started.is_set()

    upload.cancel()
    await asyncio.sleep(0.01)
    assert not upload.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(upload, timeout=1)


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [],
            "query_string": b"",
            "client": ("203.0.113.20", 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _upload(filename: str = "notes.txt") -> UploadFile:
    return UploadFile(
        filename=filename,
        file=io.BytesIO(b"content"),
        headers={"content-type": "text/plain"},
    )


@pytest.mark.asyncio
async def test_cleanup_is_reverse_order_and_best_effort(monkeypatch):
    removal_attempts: list[str] = []

    class MinioClient:
        def remove_object(self, _bucket: str, object_key: str):
            removal_attempts.append(object_key)
            if object_key == "new/second":
                raise RuntimeError("temporary MinIO failure")

    monkeypatch.setattr(user_file_service, "_get_minio_client", lambda: MinioClient())

    await user_file_service.cleanup_uploaded_minio_objects(
        "users",
        ("new/first", "new/second", "new/second"),
    )

    assert removal_attempts == ["new/second", "new/first"]


@pytest.mark.asyncio
async def test_service_removes_only_current_upload_keys_when_later_upload_fails(
    monkeypatch,
):
    uploaded_keys: list[str] = []
    attempted_keys: list[str] = []
    removed_keys: list[tuple[str, str]] = []

    async def fake_upload(*, object_name: str, **_kwargs):
        attempted_keys.append(object_name)
        if not uploaded_keys:
            uploaded_keys.append(object_name)
            return object_name
        return None

    class MinioClient:
        def remove_object(self, bucket: str, object_key: str):
            removed_keys.append((bucket, object_key))

    monkeypatch.setattr(user_file_service, "upload_bytes_to_minio", fake_upload)
    monkeypatch.setattr(user_file_service, "get_user_minio_bucket_name", lambda: "users")
    monkeypatch.setattr(user_file_service, "_get_minio_client", lambda: MinioClient())

    with pytest.raises(user_file_service.UserFileServiceError):
        await user_file_service.prepare_uploaded_user_file(
            session=_Session(),
            owner_user_id="user-1",
            data=b"content",
            filename="notes.txt",
            mime_type="text/plain",
            storage_scope="personal",
            image_url_builder=lambda _file_id, image_name: f"/{image_name}",
            parser=_Parser(),
        )

    assert len(uploaded_keys) == 1
    assert removed_keys == [
        ("users", attempted_keys[1]),
        ("users", attempted_keys[0]),
    ]
    assert ("users", "users/user-1/existing-object.txt") not in removed_keys


@pytest.mark.asyncio
async def test_service_returns_exact_keys_created_by_successful_upload(monkeypatch):
    uploaded_keys: list[str] = []

    async def fake_upload(*, object_name: str, **_kwargs):
        uploaded_keys.append(object_name)
        return object_name

    monkeypatch.setattr(user_file_service, "upload_bytes_to_minio", fake_upload)
    monkeypatch.setattr(user_file_service, "get_user_minio_bucket_name", lambda: "users")
    session = _Session()

    stored = await user_file_service.prepare_uploaded_user_file(
        session=session,
        owner_user_id="user-1",
        data=b"content",
        filename="notes.txt",
        mime_type="text/plain",
        storage_scope="personal",
        image_url_builder=lambda _file_id, image_name: f"/{image_name}",
        parser=_Parser(),
    )

    assert stored.storage_bucket == "users"
    assert stored.uploaded_object_keys == tuple(uploaded_keys)
    assert len(stored.uploaded_object_keys) == 2
    assert all(key.startswith("users/user-1/personal-files/") for key in uploaded_keys)
    assert session.added == [stored.user_file]


@pytest.mark.asyncio
async def test_personal_upload_commit_failure_rolls_back_current_objects(monkeypatch):
    stored = SimpleNamespace(
        file_id="uf-personal",
        storage_bucket="users",
        uploaded_object_keys=("new/main.txt", "new/image.png"),
    )
    cleaned = []

    async def fake_prepare(**_kwargs):
        return stored

    async def fake_cleanup(bucket, keys):
        cleaned.append((bucket, tuple(keys)))

    monkeypatch.setattr(my_files, "prepare_uploaded_user_file", fake_prepare)
    monkeypatch.setattr(my_files, "cleanup_uploaded_minio_objects", fake_cleanup)
    session = _Session(commit_error=RuntimeError("commit failed"))

    with pytest.raises(RuntimeError, match="commit failed"):
        await my_files.upload_personal_file(
            _request("/api/v1/files/upload"),
            _upload(),
            user=SimpleNamespace(user_id="user-1"),
            session=session,
        )

    assert session.rolled_back is True
    assert cleaned == [
        ("users", ("new/main.txt", "new/image.png")),
    ]


@pytest.mark.asyncio
async def test_chat_upload_commit_failure_rolls_back_only_current_file_objects(
    monkeypatch,
):
    stored = SimpleNamespace(
        file_id="uf-chat",
        storage_bucket="users",
        uploaded_object_keys=("chat/current.pdf", "chat/current-image.png"),
    )
    cleaned = []

    async def fake_prepare(**_kwargs):
        return stored

    async def fake_cleanup(bucket, keys):
        cleaned.append((bucket, tuple(keys)))

    monkeypatch.setattr(chat_files, "prepare_uploaded_user_file", fake_prepare)
    monkeypatch.setattr(chat_files, "cleanup_uploaded_minio_objects", fake_cleanup)
    session = _Session(commit_error=RuntimeError("chat commit failed"))

    with pytest.raises(RuntimeError, match="chat commit failed"):
        await chat_files.upload_chat_files(
            _request("/api/v2/chat-files/upload"),
            thread_id="thread-1",
            parse_mode="auto",
            files=[_upload("report.txt")],
            user=SimpleNamespace(user_id="user-1"),
            session=session,
        )

    assert session.rolled_back is True
    assert cleaned == [
        ("users", ("chat/current.pdf", "chat/current-image.png")),
    ]
