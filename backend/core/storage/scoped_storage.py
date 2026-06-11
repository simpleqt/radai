from io import BytesIO
from backend.storage import _get_minio_client, get_user_minio_bucket_name

class ScopedStorage:
    """MinIO 存储的作用域封装，确保用户只能访问自己的数据"""

    def __init__(self, user_id: str, thread_id: str):
        self.user_id = user_id
        self.thread_id = thread_id
        self.prefix = f"users/{user_id}/threads/{thread_id}/artifacts/"
        self.client = _get_minio_client()
        self.bucket = get_user_minio_bucket_name()

    def read_file(self, filename: str) -> bytes:
        """读取用户目录下的文件，包含旧版路径的兼容（双读策略）"""
        object_name = f"{self.prefix}{filename}"
        try:
            return self._read_object(object_name)
        except Exception:
            # Fallback for legacy data
            legacy_name = f"artifacts/{self.thread_id}/{filename}"
            try:
                return self._read_object(legacy_name)
            except Exception as e:
                raise FileNotFoundError(f"文件不存在: {filename} ({e})")

    def _read_object(self, object_name: str) -> bytes:
        """内部同步读取方法"""
        response = self.client.get_object(self.bucket, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def save_file(self, filename: str, content: bytes, content_type: str = "application/octet-stream") -> str:
        """保存文件到用户目录"""
        object_name = f"{self.prefix}{filename}"
        self.client.put_object(
            self.bucket,
            object_name,
            data=BytesIO(content),
            length=len(content),
            content_type=content_type,
        )
        return object_name
        
    def save_local_file(self, object_name: str, local_file_path: str) -> str:
        """上传本地文件到指定对象名。确保 object_name 包含该用户的安全前缀 users/{user_id}/"""
        if not object_name.startswith(f"users/{self.user_id}/"):
            raise PermissionError(f"越权访问：尝试写入不属于本用户的路径 {object_name}")
        self.client.fput_object(self.bucket, object_name, local_file_path)
        return object_name
        
    def list_objects(self, prefix: str = "", recursive: bool = True) -> list[str]:
        """列出作用域前缀下的所有对象，可选附加前缀"""
        search_prefix = f"{self.prefix}{prefix}" if prefix else self.prefix
        objects = self.client.list_objects(self.bucket, prefix=search_prefix, recursive=recursive)
        return [obj.object_name for obj in objects]

    def delete_all_files(self):
        """删除该作用域下的所有文件（包含旧版路径资产）"""
        from minio.deleteobjects import DeleteObject
        from backend.config.loggers import get_logger
        
        logger = get_logger(__name__)
        
        # 1. 规范路径前缀与旧版路径前缀
        prefixes = [self.prefix, f"artifacts/{self.thread_id}/"]
        
        for p in prefixes:
            try:
                # 列出所有待删除对象
                objects = self.client.list_objects(self.bucket, prefix=p, recursive=True)
                delete_list = [DeleteObject(obj.object_name) for obj in objects]
                
                if not delete_list:
                    continue
                    
                logger.info(f"[ScopedStorage] 准备清理 MinIO 路径: {p}, 共 {len(delete_list)} 个对象")
                
                # 执行批量删除
                errors = self.client.remove_objects(self.bucket, delete_list)
                for error in errors:
                    logger.error(f"[ScopedStorage] 删除对象失败: {error}")
                    
            except Exception as e:
                logger.warning(f"[ScopedStorage] 清理路径 {p} 时发生异常: {e}")
