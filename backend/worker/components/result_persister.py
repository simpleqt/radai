import asyncio
import json
import os
from datetime import datetime, timedelta
from io import BytesIO
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from backend.application.blocking import run_blocking
from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db import async_session_context
from backend.db.models import Document, ExtractionResult, Task, TaskStatus
from backend.storage import storage_manager

logger = get_logger(__name__)

RESULT_OUTBOX_KEY = "_result_outbox"
RESULT_OUTBOX_KIND = "result"
_RESULT_OUTBOX_KEY = RESULT_OUTBOX_KEY


class TaskCompletionRejected(RuntimeError):
    """Raised when another terminal state wins the task completion race."""


def _bounded_float_env(name: str, default: float, *, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


async def _await_blocking_write(func, /, *args, **kwargs):
    """Run storage I/O in the bounded, lifecycle-owned blocking executor."""
    operation_timeout = _bounded_float_env(
        "STORAGE_BLOCKING_TIMEOUT_SECONDS",
        120.0,
        minimum=0.1,
    )
    try:
        return await run_blocking(
            func,
            *args,
            timeout_seconds=operation_timeout,
            **kwargs,
        )
    except TimeoutError as exc:
        raise TimeoutError(
            f"结果文件写入超过 {operation_timeout:.1f}s"
        ) from exc


async def _await_db(operation):
    """Bound worker DB operations so one locked query cannot stall the loop."""
    return await asyncio.wait_for(operation, timeout=30.0)


class ResultPersister:
    """结果持久化器"""

    async def save_result(
        self,
        task_id: str,
        document_id: str,
        user_id: str,
        file_type: str,
        output: dict,
        attempt_version: int = 0,
    ) -> str | None:
        """
        保存处理结果到数据库和文件

        Args:
            task_id: 任务ID
            document_id: 文档ID
            user_id: 用户ID
            file_type: 文件类型
            output: 处理结果输出

        Returns:
            本地文件路径（如果有）
        """
        markdown_content = output.get("markdown", "")
        output_path: str | None = None
        md_object_key: str | None = None
        md_download_url: str | None = None
        minio_client = None
        minio_bucket: str | None = None
        persisted = False
        result_event = {
            "event_id": str(uuid4()),
            "attempt": attempt_version,
            "state": "pending",
            "timestamp": int(datetime.now().timestamp() * 1000),
            "output": await asyncio.to_thread(
                self._serializable_result_output,
                output,
            ),
        }

        try:
            # External storage must not run while a database transaction holds
            # the Task row lock. Every attempt writes unique artifact names;
            # the short CAS transaction below chooses the winner and the
            # exception path cleans artifacts owned by a losing attempt.
            if settings.save_results_locally and markdown_content:
                try:
                    _file_id, output_path = await storage_manager.save_file(
                        markdown_content.encode("utf-8"),
                        f"{document_id}.md",
                        storage_type="results",
                    )
                    logger.info("结果已保存到本地文件: %s", output_path)
                except Exception as exc:
                    logger.warning("保存结果到本地文件失败: %s", exc)
                    output_path = None

            if markdown_content:
                try:
                    object_name = (
                        f"users/{user_id}/results/{document_id}/{task_id}/"
                        f"{result_event['event_id']}.md"
                    )
                    from backend.storage import (
                        _get_minio_client,
                        get_user_minio_bucket_name,
                    )

                    minio_client = _get_minio_client()
                    minio_bucket = get_user_minio_bucket_name()
                    markdown_bytes = markdown_content.encode("utf-8")
                    md_object_key = object_name
                    await _await_blocking_write(
                        minio_client.put_object,
                        minio_bucket,
                        object_name,
                        BytesIO(markdown_bytes),
                        length=len(markdown_bytes),
                        content_type="text/markdown",
                    )
                    md_download_url = await asyncio.to_thread(
                        minio_client.presigned_get_object,
                        minio_bucket,
                        object_name,
                        expires=timedelta(seconds=24 * 60 * 60),
                    )
                except Exception as exc:
                    logger.warning("上传结果到 MinIO 失败: %s", exc)
                    if md_object_key and minio_client is not None and minio_bucket:
                        try:
                            await asyncio.to_thread(
                                minio_client.remove_object,
                                minio_bucket,
                                md_object_key,
                            )
                        except Exception as cleanup_exc:
                            logger.warning(
                                "清理失败的 MinIO 结果上传失败: %s",
                                cleanup_exc,
                            )
                    md_object_key = None
                    md_download_url = None

            # Keep the database unit of work short: terminal CAS, related rows,
            # and the durable result outbox commit atomically here.
            async with async_session_context() as session:
                from backend.services.task_dispatch_outbox import (
                    ensure_outbox_capacity,
                )

                await _await_db(
                    ensure_outbox_capacity(
                        session,
                        kind=RESULT_OUTBOX_KIND,
                        excluding_task_id=task_id,
                    )
                )
                now = datetime.now()
                task_update = await _await_db(
                    session.execute(
                        update(Task)
                        .where(
                            Task.task_id == task_id,
                            func.coalesce(Task.retry_count, 0) == attempt_version,
                            Task.status.in_(
                                (
                                    TaskStatus.PENDING.value,
                                    TaskStatus.PROCESSING.value,
                                )
                            ),
                        )
                        .values(
                            status=TaskStatus.COMPLETED.value,
                            completed_at=now,
                            updated_at=now,
                            result={_RESULT_OUTBOX_KEY: result_event},
                            outbox_kind=RESULT_OUTBOX_KIND,
                            output_path=output_path,
                        )
                    )
                )
                if task_update.rowcount != 1:
                    raise TaskCompletionRejected(
                        f"任务已被取消、终止或由其他 worker 完成: {task_id}"
                    )

                await _await_db(
                    session.execute(
                        update(Document)
                        .where(Document.doc_id == document_id)
                        .values(status="completed", updated_at=now)
                    )
                )

                # 提取结果数据
                tables = output.get("tables", [])
                formulas = output.get("formulas", [])
                images = output.get("images", [])
                result_values = {
                    "doc_id": document_id,
                    "task_id": task_id,
                    "user_id": user_id,
                    "document_type": file_type,
                    "page_count": len(output.get("pages", [])),
                    "text_blocks_count": len(output.get("text_blocks", [])),
                    "tables_count": len(tables),
                    "formulas_count": len(formulas),
                    "images_count": len(images),
                    "result_data": json.dumps(
                        {
                            "markdown": markdown_content,
                            "success": output.get("success", True),
                            "document_type": output.get("document_type", "unknown"),
                            "quality_score": output.get("quality_score", 0.0),
                            "retry_count": output.get("retry_count", 0),
                            "language": output.get("language", "unknown"),
                            "messages": output.get("messages", []),
                            "md_object_key": md_object_key,
                            "md_download_url": md_download_url,
                        },
                        ensure_ascii=False,
                    ),
                    "text_content": markdown_content,
                    "markdown_content": markdown_content,
                    "processing_time": output.get("processing_time_ms", 0) / 1000.0,
                    "confidence_score": output.get("quality_score", 0.9),
                }
                dialect_name = session.get_bind().dialect.name
                if dialect_name == "mysql":
                    statement = mysql_insert(ExtractionResult).values(**result_values)
                    update_values = {
                        field: statement.inserted[field]
                        for field in result_values
                        if field != "task_id"
                    }
                    await _await_db(
                        session.execute(
                            statement.on_duplicate_key_update(**update_values)
                        )
                    )
                elif dialect_name == "sqlite":
                    statement = sqlite_insert(ExtractionResult).values(**result_values)
                    update_values = {
                        field: statement.excluded[field]
                        for field in result_values
                        if field != "task_id"
                    }
                    await _await_db(
                        session.execute(
                            statement.on_conflict_do_update(
                                index_elements=["task_id"],
                                set_=update_values,
                            )
                        )
                    )
                elif dialect_name == "postgresql":
                    statement = postgresql_insert(ExtractionResult).values(
                        **result_values
                    )
                    update_values = {
                        field: statement.excluded[field]
                        for field in result_values
                        if field != "task_id"
                    }
                    await _await_db(
                        session.execute(
                            statement.on_conflict_do_update(
                                index_elements=["task_id"],
                                set_=update_values,
                            )
                        )
                    )
                else:
                    existing = (
                        await _await_db(
                            session.execute(
                                select(ExtractionResult)
                                .where(ExtractionResult.task_id == task_id)
                                .with_for_update()
                                .limit(1)
                            )
                        )
                    ).scalar_one_or_none()
                    if existing is None:
                        session.add(ExtractionResult(**result_values))
                    else:
                        for field, value in result_values.items():
                            setattr(existing, field, value)

            persisted = True
        except BaseException:
            # A commit acknowledgement can be lost after MySQL has committed.
            # Verify the exact event before destructive compensation. If the
            # database is unavailable, prefer a uniquely named orphan over
            # deleting an artifact that may already be referenced by a commit.
            committed = False
            verification_failed = False
            if not persisted:
                try:
                    committed = await self.result_event_committed(
                        task_id,
                        attempt_version,
                        str(result_event["event_id"]),
                    )
                except BaseException as verification_error:
                    verification_failed = True
                    logger.warning(
                        "无法确认结果事务是否提交，跳过破坏性补偿: task=%s error=%s",
                        task_id,
                        verification_error,
                    )
            if not persisted and not committed and not verification_failed:
                await self._cleanup_uncommitted_artifacts(
                    output_path=output_path,
                    minio_client=minio_client,
                    minio_bucket=minio_bucket,
                    md_object_key=md_object_key,
                )
            raise

        return output_path

    async def result_event_committed(
        self,
        task_id: str,
        attempt_version: int,
        event_id: str,
    ) -> bool:
        """Check whether the exact completion event is durable in the task row."""
        async with async_session_context() as session:
            task = await _await_db(session.get(Task, task_id))
            if (
                task is None
                or task.status != TaskStatus.COMPLETED.value
                or int(task.retry_count or 0) != attempt_version
                or not isinstance(task.result, dict)
            ):
                return False
            event = task.result.get(_RESULT_OUTBOX_KEY)
            return bool(
                isinstance(event, dict)
                and event.get("event_id") == event_id
                and int(event.get("attempt", -1)) == attempt_version
                and event.get("state") in {"pending", "sent"}
            )

    async def result_attempt_published(
        self,
        task_id: str,
        attempt_version: int,
    ) -> bool:
        """Return whether this exact attempt's durable event is already sent."""
        async with async_session_context() as session:
            task = await _await_db(session.get(Task, task_id))
            if (
                task is None
                or task.status != TaskStatus.COMPLETED.value
                or int(task.retry_count or 0) != attempt_version
                or not isinstance(task.result, dict)
            ):
                return False
            event = task.result.get(_RESULT_OUTBOX_KEY)
            return bool(
                isinstance(event, dict)
                and event.get("state") == "sent"
                and int(event.get("attempt", -1)) == attempt_version
                and event.get("event_id")
            )

    @staticmethod
    def _serializable_result_output(output: dict) -> dict:
        """Build the minimal publisher contract with one canonical text body."""
        extracted_text = str(
            output.get("extracted_text")
            or output.get("text_content")
            or output.get("markdown")
            or ""
        )
        page_count = output.get("page_count")

        tables = []
        for table in output.get("tables") or []:
            if not isinstance(table, dict):
                continue
            compact_table = {
                "page": table.get("page", 0),
                "data": table.get("data", table.get("rows", [])),
            }
            if table.get("caption") is not None:
                compact_table["caption"] = table["caption"]
            tables.append(compact_table)

        formulas = []
        for formula in output.get("formulas") or []:
            if isinstance(formula, dict):
                compact_formula = {
                    "page": formula.get("page", 0),
                    "latex": formula.get("latex", formula.get("content", "")),
                    "formula_type": formula.get("formula_type", "unknown"),
                }
                if formula.get("bounding_box") is not None:
                    compact_formula["bounding_box"] = formula["bounding_box"]
            else:
                compact_formula = {
                    "page": 0,
                    "latex": str(formula),
                    "formula_type": "unknown",
                }
            formulas.append(compact_formula)

        images = []
        for image in output.get("images") or []:
            if not isinstance(image, dict):
                continue
            compact_image = {
                "page": image.get("page", 0),
                "path": image.get("path") or "",
            }
            if image.get("caption") is not None:
                compact_image["caption"] = image["caption"]
            images.append(compact_image)

        payload = {
            # Keep the established publisher field name so an older worker can
            # safely drain an event written during a rolling deployment.
            "extracted_text": extracted_text,
            "tables": tables,
            "formulas": formulas,
            "images": images,
            "page_count": (
                page_count if page_count is not None else len(output.get("pages") or [])
            ),
            "language": output.get("language") or "unknown",
            "quality_score": (
                output.get("quality_score")
                if output.get("quality_score") is not None
                else 0.9
            ),
            "processing_time_ms": output.get("processing_time_ms") or 0,
        }
        # Task.result is a JSON column. Normalize JSON-adjacent values only
        # after dropping processor-only fields such as raw HTML or image bytes.
        return json.loads(json.dumps(payload, ensure_ascii=False, default=str))

    async def get_pending_result_event(
        self,
        task_id: str,
        attempt_version: int,
    ) -> dict | None:
        """Return an unpublished result event for the exact task attempt."""
        async with async_session_context() as session:
            task = await _await_db(session.get(Task, task_id))
            if (
                task is None
                or task.status != TaskStatus.COMPLETED.value
                or int(task.retry_count or 0) != attempt_version
                or not isinstance(task.result, dict)
            ):
                return None
            event = task.result.get(_RESULT_OUTBOX_KEY)
            if (
                not isinstance(event, dict)
                or event.get("state") != "pending"
                or int(event.get("attempt", -1)) != attempt_version
                or not event.get("event_id")
                or not isinstance(event.get("output"), dict)
            ):
                return None
            return dict(event)

    async def mark_result_published(
        self,
        task_id: str,
        attempt_version: int,
        event_id: str,
    ) -> bool:
        """Mark the embedded outbox event sent after Kafka acknowledges it."""
        async with async_session_context() as session:
            task = (
                await _await_db(
                    session.execute(
                        select(Task)
                        .where(Task.task_id == task_id)
                        .with_for_update()
                        .limit(1)
                    )
                )
            ).scalar_one_or_none()
            if (
                task is None
                or task.status != TaskStatus.COMPLETED.value
                or int(task.retry_count or 0) != attempt_version
                or not isinstance(task.result, dict)
            ):
                return False

            event = task.result.get(_RESULT_OUTBOX_KEY)
            if (
                isinstance(event, dict)
                and event.get("state") == "sent"
                and event.get("event_id") == event_id
                and int(event.get("attempt", -1)) == attempt_version
            ):
                task.outbox_kind = None
                # Opportunistically compact events written by older workers,
                # which retained the full output even after Kafka acknowledged it.
                if "output" in event:
                    compact_event = {
                        key: value for key, value in event.items() if key != "output"
                    }
                    task.result = {
                        **task.result,
                        _RESULT_OUTBOX_KEY: compact_event,
                    }
                return True
            if (
                not isinstance(event, dict)
                or event.get("state") != "pending"
                or event.get("event_id") != event_id
                or int(event.get("attempt", -1)) != attempt_version
            ):
                return False

            sent_event = {
                **{key: value for key, value in event.items() if key != "output"},
                "state": "sent",
                "sent_at": datetime.now().isoformat(),
            }
            task.result = {**task.result, _RESULT_OUTBOX_KEY: sent_event}
            task.outbox_kind = None
            return True

    async def prepare_retry(self, task_id: str, retry_attempt: int) -> bool:
        """Advance a retry attempt atomically, or resume the same prepared one."""
        if retry_attempt <= 0:
            return False

        async with async_session_context() as session:
            now = datetime.now()
            advanced = await _await_db(
                session.execute(
                    update(Task)
                    .where(
                        Task.task_id == task_id,
                        func.coalesce(Task.retry_count, 0) == retry_attempt - 1,
                        Task.status == TaskStatus.PROCESSING.value,
                        func.coalesce(Task.max_retries, 3) >= retry_attempt,
                    )
                    .values(
                        retry_count=retry_attempt,
                        status=TaskStatus.PENDING.value,
                        progress=0,
                        result=None,
                        outbox_kind=None,
                        output_path=None,
                        error=None,
                        error_detail=None,
                        started_at=None,
                        completed_at=None,
                        updated_at=now,
                    )
                )
            )
            if advanced.rowcount == 1:
                return True

            # The retry record can be redelivered after its DB transition but
            # before its Kafka offset commit. Resuming the same non-terminal
            # attempt is safe because all later writes carry this fence value.
            current = await _await_db(
                session.execute(
                    select(Task.status, Task.retry_count, Task.max_retries).where(
                        Task.task_id == task_id
                    )
                )
            )
            row = current.one_or_none()
            return bool(
                row
                and int(row.retry_count or 0) == retry_attempt
                and retry_attempt
                <= int(row.max_retries if row.max_retries is not None else 3)
                and row.status
                in (TaskStatus.PENDING.value, TaskStatus.PROCESSING.value)
            )

    async def schedule_retry_dispatch(self, retry_message) -> None:
        """Persist a delayed retry in the DB outbox without pausing Kafka."""
        from backend.kafka.schemas import TaskMessage
        from backend.services.task_dispatch_outbox import (
            TASK_DISPATCH_OUTBOX_KIND,
            build_task_dispatch_event,
            ensure_outbox_capacity,
            task_dispatch_result,
        )

        retry_attempt = int(retry_message.body.retry_attempt)
        if retry_attempt <= 0 or not retry_message.body.original_task:
            raise ValueError("retry requires a positive attempt and original_task")

        task_message = TaskMessage.model_validate(retry_message.body.original_task)
        event = build_task_dispatch_event(task_message, retry_attempt)
        if retry_message.body.scheduled_at:
            event["available_at"] = str(retry_message.body.scheduled_at)

        async with async_session_context() as session:
            await _await_db(
                ensure_outbox_capacity(
                    session,
                    kind=TASK_DISPATCH_OUTBOX_KIND,
                    excluding_task_id=retry_message.body.task_id,
                )
            )
            now = datetime.now()
            scheduled = await _await_db(
                session.execute(
                    update(Task)
                    .where(
                        Task.task_id == retry_message.body.task_id,
                        func.coalesce(Task.retry_count, 0) == retry_attempt - 1,
                        Task.status == TaskStatus.PROCESSING.value,
                        func.coalesce(Task.max_retries, 3) >= retry_attempt,
                    )
                    .values(
                        retry_count=retry_attempt,
                        status=TaskStatus.PENDING.value,
                        progress=0,
                        result=task_dispatch_result(event),
                        outbox_kind=TASK_DISPATCH_OUTBOX_KIND,
                        output_path=None,
                        error=None,
                        error_detail=None,
                        started_at=None,
                        completed_at=None,
                        updated_at=now,
                    )
                )
            )
            if scheduled.rowcount == 1:
                return

            # Redelivery after the scheduling transaction committed is safe.
            task = await _await_db(
                session.get(Task, retry_message.body.task_id)
            )
            existing = task.result if task is not None else None
            existing_event = (
                existing.get("_task_dispatch_outbox")
                if isinstance(existing, dict)
                else None
            )
            if (
                task is not None
                and task.status == TaskStatus.PENDING.value
                and int(task.retry_count or 0) == retry_attempt
                and isinstance(existing_event, dict)
                and existing_event.get("event_id") == event["event_id"]
            ):
                return
            raise TaskCompletionRejected(
                f"任务重试版本已变化，无法调度 attempt={retry_attempt}: "
                f"{retry_message.body.task_id}"
            )

    async def mark_attempt_failed(
        self,
        task_id: str,
        attempt_version: int,
        error: Exception,
    ) -> bool:
        """Persist a terminal failure only for the still-current attempt."""
        async with async_session_context() as session:
            now = datetime.now()
            result = await _await_db(
                session.execute(
                    update(Task)
                    .where(
                        Task.task_id == task_id,
                        func.coalesce(Task.retry_count, 0) == attempt_version,
                        Task.status.in_(
                            (
                                TaskStatus.PENDING.value,
                                TaskStatus.PROCESSING.value,
                            )
                        ),
                    )
                    .values(
                        status=TaskStatus.FAILED.value,
                        outbox_kind=None,
                        error=str(error)[:2000],
                        completed_at=now,
                        updated_at=now,
                    )
                )
            )
            return result.rowcount == 1

    @staticmethod
    async def _cleanup_uncommitted_artifacts(
        *,
        output_path: str | None,
        minio_client,
        minio_bucket: str | None,
        md_object_key: str | None,
    ) -> None:
        if md_object_key and minio_client is not None and minio_bucket:
            try:
                await asyncio.to_thread(
                    minio_client.remove_object,
                    minio_bucket,
                    md_object_key,
                )
            except Exception as exc:
                logger.warning("清理未提交 MinIO 结果失败: %s", exc)
        if output_path:
            try:
                await storage_manager.delete_file(output_path)
            except Exception as exc:
                logger.warning("清理未提交本地结果失败: %s", exc)

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        attempt_version: int = 0,
    ) -> bool:
        """仅在任务未终止且执行版本仍有效时更新处理状态。"""
        async with async_session_context() as session:
            now = datetime.now()
            values = {"status": status, "updated_at": now}
            if status == TaskStatus.PROCESSING.value:
                values["started_at"] = now
                # A task dispatch outbox is sent before a worker can acquire
                # this row. It is no longer needed once processing is claimed,
                # and must not leak through task-result APIs.
                values["result"] = None
                values["outbox_kind"] = None
            result = await _await_db(
                session.execute(
                    update(Task)
                    .where(
                        Task.task_id == task_id,
                        func.coalesce(Task.retry_count, 0) == attempt_version,
                        Task.status.in_(
                            (
                                TaskStatus.PENDING.value,
                                TaskStatus.PROCESSING.value,
                            )
                        ),
                    )
                    .values(**values)
                )
            )
            await session.commit()
            return result.rowcount == 1
