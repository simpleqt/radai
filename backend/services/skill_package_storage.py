"""Private object storage for immutable Skill packages."""

from __future__ import annotations

import asyncio
from io import BytesIO

from backend.storage import _get_minio_client, get_presigned_url, get_user_minio_bucket_name


class SkillPackageStorageError(RuntimeError):
    pass


class SkillPackageStorage:
    def __init__(self) -> None:
        self.bucket = get_user_minio_bucket_name()

    async def upload(self, *, object_name: str, content: bytes) -> None:
        def _put() -> None:
            client = _get_minio_client()
            client.put_object(
                self.bucket,
                object_name,
                BytesIO(content),
                length=len(content),
                content_type="application/zip",
            )

        try:
            await asyncio.to_thread(_put)
        except Exception as exc:
            raise SkillPackageStorageError("Skill 包上传 MinIO 失败") from exc

    async def download(self, *, bucket: str, object_name: str) -> bytes:
        def _get() -> bytes:
            response = _get_minio_client().get_object(bucket, object_name)
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        try:
            return await asyncio.to_thread(_get)
        except Exception as exc:
            raise SkillPackageStorageError("Skill 包从 MinIO 下载失败") from exc

    async def presigned_download(self, *, bucket: str, object_name: str) -> str | None:
        # presign 本身是本地计算,但 get_presigned_url 内部取客户端时的首次
        # 惰性初始化会做 bucket_exists/make_bucket 网络往返;调用方在异步
        # 路径,整体离线到线程(R4-SV-15)。
        return await asyncio.to_thread(
            get_presigned_url, object_name, expiry_seconds=600, bucket_name=bucket
        )

