# PendingIngestProcessor 按 file_type 分流 — 精简实现方案

> **文档状态**：✅ 已实现 — Worker `md-direct` 路由 + `MDIngestor`。  
> **现行说明**：[docs/md_ingestion_design.md](../docs/md_ingestion_design.md) · [docs/md_ingestion_api.md](../docs/md_ingestion_api.md)

## 1. 目标

在现有上传链路基础上，Worker 识别 `.md` 文件后跳过 MinerU，直接读取内容入库。

**不改接口、不改表、不新增路由。**

---

## 2. 现状：当前完整流程

```
POST /uploads/batch (已支持 .md) → MinIO → PendingFile(status=0)
                                                      │
POST /uploads/ingest { kb_id, file_ids }  ←──────────┘
        │
        ▼  PendingFile.status: 0 → 1
        │
   ┌────┴────────────────────────────────┐
   │  ingest_worker.py (独立进程, 每2s轮询) │
   │  SELECT * FROM PendingFile WHERE status=1 │
   │              │                         │
   │              ▼                         │
   │  PendingIngestProcessor.process_file() │
   │    ├─ MinIO下载到本地 cache_dir        │
   │    ├─ rag_service.process_multimodal() │ ← 所有文件统一走MinerU
   │    └─ status变为 2/3                  │
   └───────────────────────────────────────┘
```

**问题**：`process_multimodal()` 内部强制走 MinerU 解析 PDF→MD。对 `.md` 文件，MinerU 完全多余（浪费 5~30 秒，且依赖 MinerU 可用性）。

---

## 3. 方案：只改两个文件

### 3.1 改动清单

| 文件 | 改动 | 行数 |
|------|------|------|
| [ingest_pending_processor.py](../backend/worker/components/ingest_pending_processor.py) | 新增 `_is_md_file()` + `_process_md_file()`，重构 `process_file()` | ~60 |
| [service.py](../backend/rag/service.py) | 新增 `process_md_content()` | ~70 |

### 3.2 分流后的流程

```
PendingIngestProcessor.process_file(file_id)
  │
  ├─ 查 PendingFile 获取 file_type/file_name
  │
  ├─ _is_md_file() 判断
  │   ├─ file_type == "text/markdown" → 是MD
  │   └─ file_name.lower().endswith(".md") → 是MD（兜底）
  │
  ├─ [是MD] _process_md_file()
  │   ├─ MinIO下载内容 → 内存.decode("utf-8")
  │   ├─ rag_service.process_md_content()   ← 新增, 跳过MinerU
  │   └─ status → 2
  │
  └─ [非MD] _process_pdf_file()  ← 原有逻辑原封不动
      ├─ MinIO下载 → 写本地cache_dir
      ├─ rag_service.process_multimodal()   ← 有MinerU
      ├─ status → 2
      └─ 清理本地缓存
```

---

## 4. 代码实现

### 4.1 文件一：[service.py](../backend/rag/service.py) — 新增 `process_md_content()`

在 `process_multimodal()` 方法**之后**插入以下方法：

```python
    async def process_md_content(
        self,
        md_content: str,
        kb_id: str | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        rag = await self._get_rag_instance(kb_id)

        doc_id = kwargs.pop("doc_id", None)
        file_name = kwargs.pop("file_name", "untitled.md")
        document_date = kwargs.pop("document_date", None)
        category_path = kwargs.pop("category_path", None)

        from backend.rag.raganything.md_ingestor import MDIngestor

        md_ingestor = MDIngestor(rag, config={
            'context_window': kwargs.get('context_window', 500),
        })

        try:
            result = await md_ingestor.ingest_from_content(
                md_content=md_content,
                doc_id=doc_id,
                file_path=file_name,
                base_url=None,
            )

            if result.get('success'):
                doc_id = result.get('doc_id')
                if doc_id:
                    final_chunk_count, final_entity_count, final_relation_count = \
                        await self._finalize_document_after_ingest(
                            rag=rag,
                            kb_id=kb_id,
                            doc_id=doc_id,
                            file_path=file_name,
                            file_name=file_name,
                            document_date=document_date,
                            category_path=category_path,
                            chunk_count=int(result.get('chunk_count') or 0),
                        )
                    result['chunk_count'] = final_chunk_count
                    result['entity_count'] = final_entity_count
                    result['relation_count'] = final_relation_count

            return {
                "success": result.get('success', False),
                "doc_id": result.get('doc_id'),
                "file_path": file_name,
                "file_name": file_name,
                "text_length": result.get('text_length', 0),
                "images_count": result.get('images_count', 0),
                "chunk_count": result.get('chunk_count', 0),
                "entity_count": result.get('entity_count', 0),
                "relation_count": result.get('relation_count', 0),
                "error": result.get('error'),
            }

        finally:
            await md_ingestor.close()
```

**本质**：把 `process_multimodal()` 的 MinerU 那一段（~40行）删掉，其余完全一样。

---

### 4.2 文件二：[ingest_pending_processor.py](../backend/worker/components/ingest_pending_processor.py) — 重构

```python
"""待入库文件处理组件（供独立 ingest worker 使用）"""

import uuid
from pathlib import Path

from sqlalchemy import select, update

from backend.config import settings
from backend.config.loggers import get_logger
from backend.db.connection import async_session_context
from backend.db.models import KnowledgeBaseDocument, PendingFile
from backend.rag.service import get_rag_service
from backend.storage import _get_minio_client

logger = get_logger(__name__)

MD_MIME_TYPES = {"text/markdown", "text/x-markdown"}


class PendingIngestProcessor:
    """处理 kb_pending_files 中 status=1 的文件。"""

    def __init__(self):
        self.rag_service = get_rag_service()
        self.cache_dir = Path(__file__).resolve().parents[2] / "data" / "cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _is_md_file(self, pf: PendingFile) -> bool:
        if pf.file_type in MD_MIME_TYPES:
            return True
        if pf.file_name and pf.file_name.lower().endswith(".md"):
            return True
        return False

    async def process_file(self, file_id: str) -> bool:

        from backend.db.redis import get_redis
        redis_client = get_redis()
        lock_key = f"kb_ingest_lock:{file_id}"

        try:
            acquired = await redis_client.set(lock_key, "1", nx=True, ex=7200)
            if not acquired:
                logger.debug(f"[ingest-worker] 文件正在由其他 worker 处理，跳过: {file_id}")
                return False

            try:
                pending_file_name = file_id
                kb_id = ""
                is_md = False

                try:
                    async with async_session_context() as session:
                        result = await session.execute(
                            select(PendingFile).where(PendingFile.file_id == file_id)
                        )
                        pending_file = result.scalar_one_or_none()

                        if not pending_file:
                            logger.warning(f"未找到待入库文件: {file_id}")
                            return False

                        if pending_file.status != 1:
                            logger.info(f"跳过非入库中状态文件: {file_id}, status={pending_file.status}")
                            return False

                        pending_file_name = pending_file.file_name
                        kb_id = pending_file.kb_id
                        is_md = self._is_md_file(pending_file)

                    route = "md-direct" if is_md else "mineru"
                    logger.info(
                        f"[ingest-worker] 开始处理: {pending_file_name}, "
                        f"file_id={file_id}, route={route}"
                    )

                    if is_md:
                        return await self._process_md_file(pending_file, file_id)
                    else:
                        return await self._process_pdf_file(pending_file, file_id)

                except Exception as exc:
                    logger.error(f"[ingest-worker] 入库失败: file_id={file_id}, error={exc}", exc_info=True)
                    try:
                        async with async_session_context() as session:
                            await session.execute(
                                update(PendingFile)
                                .where(PendingFile.file_id == file_id)
                                .values(status=3, error_message=str(exc))
                            )
                            await session.commit()
                    except Exception as db_err:
                        logger.warning(f"[ingest-worker] 更新失败状态失败: file_id={file_id}, error={db_err}")
                    return False

            finally:
                await redis_client.delete(lock_key)
        finally:
            await redis_client.aclose()

    async def _process_md_file(self, pending_file: PendingFile, file_id: str) -> bool:
        try:
            content = self._download_from_minio(pending_file.minio_path)
            md_content = content.decode("utf-8")
        except Exception as e:
            logger.error(f"[ingest-worker] MinIO读取MD失败: {e}")
            raise

        doc_id = str(uuid.uuid4())
        logger.info(
            f"[ingest-worker] MD直接入库: {pending_file.file_name}, "
            f"doc_id={doc_id}, md_length={len(md_content)} chars"
        )

        ingest_result = await self.rag_service.process_md_content(
            md_content=md_content,
            kb_id=pending_file.kb_id,
            doc_id=doc_id,
            file_name=pending_file.file_name,
            document_date=pending_file.document_date,
            category_path=pending_file.category_path,
        )

        async with async_session_context() as session:
            await session.execute(
                update(PendingFile)
                .where(PendingFile.file_id == file_id)
                .values(status=2, error_message=None)
            )
            await session.commit()

        logger.info(f"[ingest-worker] MD入库成功: {pending_file.file_name}, doc_id={doc_id}")
        return True

    async def _process_pdf_file(self, pending_file: PendingFile, file_id: str) -> bool:
        cache_file_path: Path | None = None
        try:
            content = self._download_from_minio(pending_file.minio_path)
            cache_file_path = self.cache_dir / pending_file.file_name
            cache_file_path.write_bytes(content)

            doc_id = str(uuid.uuid4())
            ingest_result = await self.rag_service.process_multimodal(
                file_path=str(cache_file_path),
                kb_id=pending_file.kb_id,
                doc_id=doc_id,
                file_name=pending_file.file_name,
                document_date=pending_file.document_date,
                category_path=pending_file.category_path,
            )
            doc_id = ingest_result.get("doc_id", doc_id)

            async with async_session_context() as session:
                await session.execute(
                    update(PendingFile)
                    .where(PendingFile.file_id == file_id)
                    .values(status=2, error_message=None)
                )
                await session.commit()

            logger.info(f"[ingest-worker] PDF入库成功: {pending_file.file_name}, doc_id={doc_id}")
            return True

        finally:
            if cache_file_path and cache_file_path.exists():
                cache_file_path.unlink()

    @staticmethod
    def _download_from_minio(minio_path: str) -> bytes:
        client = _get_minio_client()
        response = client.get_object(settings.minio_bucket_name, minio_path)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()
```

---

## 5. 关键设计决策

| 决策 | 说明 |
|------|------|
| **MD 不写本地缓存** | `_process_md_file()` 在内存中 `decode("utf-8")` 后直接传给 `process_md_content()`，不产生临时文件 |
| **PDF 保留原有写缓存逻辑** | MinerU 需要本地文件路径，`_process_pdf_file()` 继续走写 → 传路径 → 删除的流程 |
| **双重判断** | `_is_md_file()` 同时检查 `file_type` MIME 和文件名后缀，防止漏判 |
| **返回格式一致** | `process_md_content()` 和 `process_multimodal()` 返回字段完全一致，上层不需要改动 |
| **分布式锁不变** | 复用 Redis 锁，MD 和 PDF 文件平等竞争，worker 互斥不冲突 |

---

## 6. 与现有 PDF 流程对比

| 环节 | PDF | MD（改造后） |
|------|-----|------------|
| 上传 | `POST /uploads/batch` | 同左 |
| 存储 | MinIO `pending/.../*.pdf` | MinIO `pending/.../*.md` |
| 触发 | `POST /uploads/ingest` status→1 | 同左 |
| Worker 轮询 | status=1 | 同左 |
| 解析 | MinerU 解析 PDF→MD（5~30秒） | **跳过**，直接读 MD |
| 入库 | MDIngestor + _finalize | 同左 |
| 结果 | status→2 | 同左 |

---

## 7. 实施步骤

| 步 | 内容 | 文件 |
|----|------|------|
| 1 | 在 `service.py` 中 `process_multimodal()` 之后新增 `process_md_content()` | [service.py](../backend/rag/service.py) |
| 2 | 用上述完整代码覆盖 `ingest_pending_processor.py` | [ingest_pending_processor.py](../backend/worker/components/ingest_pending_processor.py) |
| 3 | 启动 Worker 验证 | `python -m backend.worker.ingest_worker` |
