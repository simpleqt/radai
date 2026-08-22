"""文件存储管理模块"""

import asyncio  # noqa: F401  (测试经 backend.storage.asyncio 对象 patch)
import time  # noqa: F401  (测试经 backend.storage.time 对象 patch)

from backend.storage.minio_ops import (  # noqa: F401  (兼容 re-export)
    _await_thread_completion,
    _bounded_float_env,
    _bounded_int_env,
    _build_kb_image_proxy_signature,
    _encode_minio_path_for_url,
    _get_minio_client,
    _normalize_minio_object_name,
    build_kb_image_proxy_url,
    download_minio_object_to_path,
    extract_kb_image_object_name,
    get_minio_image_proxy_url,
    get_presigned_url,
    get_public_url,
    get_user_minio_bucket_name,
    normalize_minio_url,
    sha256_file,
    upload_bytes_to_minio,
    upload_bytes_to_minio_sync,
    verify_kb_image_proxy_signature,
)
from backend.storage.chunked_upload import (  # noqa: F401  (兼容 re-export)
    ChunkedUploadManager,
    chunked_upload_manager,
)
from backend.storage.local_storage import (  # noqa: F401  (兼容 re-export)
    StorageManager,
    storage_manager,
)
