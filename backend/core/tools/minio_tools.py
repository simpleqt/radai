import os
from datetime import timedelta

from langchain_core.tools import tool

from backend.config.loggers import get_logger
from langchain_core.runnables.config import RunnableConfig

logger = get_logger(__name__)


def upload_local_folder_to_minio(
    local_path: str,
    config: RunnableConfig,
    relative_prefix: str = "",
) -> list[str]:
    """
    递归上传本地文件夹下的所有文件到 MinIO

    Args:
        local_path: 本地文件夹路径
        config: RunnableConfig，用于提取当前用户身份
        relative_prefix: 用户作用域下的相对前缀（可选）

    Returns:
        上传成功的文件对象名称列表
    """
    from backend.core.auth.identity import get_current_identity, require_user_id
    from backend.core.storage.scoped_storage import ScopedStorage

    user_id = require_user_id(config)
    _, external_thread_id = get_current_identity(config)
    storage = ScopedStorage(user_id=user_id, thread_id=external_thread_id)

    # 仅允许调用方提供相对路径，统一拼接到当前用户线程作用域下
    clean_relative_prefix = relative_prefix.strip().strip("/")
    if ".." in clean_relative_prefix.split("/"):
        raise PermissionError(f"越权拦截：非法相对路径 {relative_prefix}")
    minio_prefix = storage.prefix.rstrip("/")
    if clean_relative_prefix:
        minio_prefix = f"{minio_prefix}/{clean_relative_prefix}"

    uploaded_files = []

    if not os.path.exists(local_path):
        return []

    try:
        for root, _, files in os.walk(local_path):
            for file in files:
                local_file_path = os.path.join(root, file)

                rel_path = os.path.relpath(local_file_path, local_path)
                object_name = os.path.join(minio_prefix, rel_path)

                # 使用封装的上传本地文件的方法保证安全性
                storage.save_local_file(object_name, local_file_path)
                uploaded_files.append(object_name)
                logger.info(f"Uploaded {local_file_path} to {object_name}")

        return uploaded_files
    except Exception as e:
        logger.error(f"Failed to upload folder {local_path}: {e}")
        raise e


@tool
def list_minio_objects(
    prefix: str,
    config: RunnableConfig,
) -> str:
    """
    列出当前用户在 MinIO 中指定前缀下的所有对象

    Args:
        prefix: 前缀路径（相对于用户专属目录，例如直接传空字符串则为当前 thread 默认 prefix）

    Returns:
        对象名称列表的字符串表示
    """
    logger.info(f"Tool invoked: list_minio_objects for {prefix}")
    try:
        from backend.core.auth.identity import require_user_id, get_current_identity
        from backend.core.storage.scoped_storage import ScopedStorage
        
        user_id = require_user_id(config)
        _, external_thread_id = get_current_identity(config)
        
        storage = ScopedStorage(user_id=user_id, thread_id=external_thread_id)
        
        clean_prefix = prefix
        expected_start = f"threads/{external_thread_id}/artifacts/"
        if clean_prefix.startswith(expected_start):
            clean_prefix = clean_prefix[len(expected_start):]
            
        # 使用 ScopedStorage 封装方法
        obj_names = storage.list_objects(prefix=clean_prefix, recursive=True)
        return str(obj_names)
    except PermissionError as e:
        logger.error(f"越权拦截: {e}")
        return f"Error: {str(e)}"
    except Exception as e:
        logger.error(f"Error listing objects: {e}")
        return f"Error: {str(e)}"


@tool
def save_report_to_minio(
    content: str,
    object_name: str,
    config: RunnableConfig,
    content_type: str = "text/markdown"
) -> str:
    """
    将文本报告保存到 MinIO（用户隔离版本）

    Args:
        content: 报告内容
        object_name: 保存的对象名称（仅文件名部分）
        config: RunnableConfig，用于提取用户身份
        content_type: 内容类型，默认为 text/markdown
    """
    try:
        from backend.core.auth.identity import require_user_id, get_current_identity
        from backend.core.storage.scoped_storage import ScopedStorage

        user_id = require_user_id(config)
        _, external_thread_id = get_current_identity(config)
        
        # 使用抽象的作用域存储层
        storage = ScopedStorage(user_id=user_id, thread_id=external_thread_id)

        # 剥除可能携带的目录路径，确保仅使用文件名
        filename = object_name.split("/")[-1]
        
        # 调用封装的 save_file 方法，它会自动加上 users/{user_id}/threads/{thread_id}/artifacts/ 前缀
        saved_object_name = storage.save_file(filename, content.encode("utf-8"), content_type=content_type)

        # 生成时效下载链接
        url = storage.client.presigned_get_object(
            storage.bucket, saved_object_name, expires=timedelta(days=7)
        )
        return url
    except PermissionError as e:
        logger.error(f"越权拦截: {e}")
        raise e
    except Exception as e:
        logger.error(f"Failed to save report to {object_name}: {e}")
        raise e
