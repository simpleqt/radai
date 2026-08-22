"""课程评价工具的共享运行时辅助。

各工具模块统一以 ``_shared.xxx`` 属性方式调用本模块函数，
使测试可以在单一位置 patch（如 ``_llm_json_call`` / ``_read_material_bytes`` /
``_resolve_file_display``）对所有工具生效。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

from backend.config.loggers import get_logger

logger = get_logger(__name__)


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _operator_id(runtime_context) -> str:
    role = str(getattr(runtime_context, "role", "") or "").strip().lower()
    if role in ("guest", "anonymous"):
        raise ValueError("写操作需登录教师身份（访客无权修改课程评价数据）")
    user_id = str((getattr(runtime_context, "user_id", None) or "") or "").strip()
    if not user_id:
        raise ValueError("写操作需要登录用户身份（runtime_context.user_id）")
    return user_id


def _is_admin_role(runtime_context) -> bool:
    role = str(getattr(runtime_context, "role", "") or "").strip().lower()
    return role in ("admin", "superadmin", "super_admin")


def _viewer_scope(runtime_context) -> tuple[str, bool]:
    """返回 (user_id, is_admin)：课程评价只读工具的属主收敛依据。

    课程评价数据含学生 PII，读也需登录教师身份；非管理员只能访问
    owner_user_id 属于自己的任务，管理员放行全部。访客/匿名/未登录一律拒绝。
    """
    user_id = _operator_id(runtime_context)
    return user_id, _is_admin_role(runtime_context)


async def _assert_task_owner(
    session,
    task_id: str,
    *,
    user_id: str,
    is_admin: bool,
    for_update: bool = False,
    require_editable: bool = False,
):
    """加载任务并强制属主校验：非管理员且非属主抛 CourseEvalAccessDenied。

    这是读/写工具统一的对象级授权收敛点——任一登录教师都拿不到、也改不了
    owner_user_id 不属于自己的任务（管理员例外）。

    ``require_editable=True`` 追加归档软锁校验（见 :func:`_load_task`）。
    """
    from backend.application.course_eval.errors import CourseEvalAccessDenied
    from backend.application.course_eval.task_service import (
        assert_can_edit_data,
        get_task,
    )

    task = await get_task(session, task_id, for_update=for_update)
    if not is_admin and str(getattr(task, "owner_user_id", "") or "") != str(
        user_id or ""
    ):
        raise CourseEvalAccessDenied()
    if require_editable:
        assert_can_edit_data(task)
    return task


async def _open_session():
    from backend.db.connection import get_async_session_factory

    factory = get_async_session_factory()
    return factory()


async def _read_material_bytes(
    session, file_id: str, user_id: str
) -> tuple[bytes, str]:
    """读取材料文件字节（个人文件：本地路径或 MinIO）。返回 (bytes, display_name)。"""
    from backend.services.user_file_service import resolve_user_file_for_download

    row, allowed, reason = await resolve_user_file_for_download(
        session=session, file_id=file_id, current_user_id=user_id
    )
    if not allowed:
        raise PermissionError(f"无权访问材料文件（{reason}）")
    if row.local_abs_path:
        path = Path(row.local_abs_path)
        if not path.is_file():
            raise FileNotFoundError(f"材料文件缺失: {row.local_abs_path}")
        data = await asyncio.to_thread(path.read_bytes)
        return data, row.display_name or file_id
    if row.storage_object:
        from backend.storage import _get_minio_client, get_user_minio_bucket_name

        client = _get_minio_client()
        bucket = row.storage_bucket or get_user_minio_bucket_name()
        response = await asyncio.to_thread(
            client.get_object, bucket, row.storage_object
        )
        try:
            data = await asyncio.to_thread(response.read)
        finally:
            await asyncio.to_thread(response.close)
        return data, row.display_name or file_id
    raise ValueError(f"材料文件无可用存储: {file_id}")


async def _load_task(
    session,
    semester_task_id: str,
    *,
    runtime_context=None,
    for_update: bool = False,
    require_editable: bool = False,
):
    """加载任务,可选强制属主校验与「可编辑(未归档)」校验。

    ``require_editable=True`` 让归档软锁在这个统一收敛点生效:归档任务拒绝
    数据写入(抛 TaskArchived)。此前只有 material/score/mapping 三个 service
    调用 assert_can_edit_data,成绩单/试卷/大纲抽取、统计与报告生成都能穿透
    归档态改数据。状态机自身的 transition_task 必须能解档,所以这里由调用方
    显式声明,而不是对所有 for_update 一律拦截。
    """
    from sqlalchemy import select

    from backend.db.models.course_eval import CeSemesterTask

    stmt = select(CeSemesterTask).where(
        CeSemesterTask.semester_task_id == semester_task_id,
        CeSemesterTask.is_deleted.is_(False),
    )
    if for_update:
        # 写工具必须把加锁读作为事务第一条语句:REPEATABLE READ 的读视图
        # 建立在首个普通 SELECT 上,先普通读再加锁会让后续读仍看旧快照;
        # populate_existing 防止身份映射把持锁前的旧属性对象递回来。
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    task = (await session.execute(stmt)).scalar_one_or_none()
    if task is None:
        raise ValueError(f"学期任务不存在: {semester_task_id}")
    if runtime_context is not None:
        # 属主收敛：传入 runtime_context 即强制校验，非管理员不得读/改他人任务。
        from backend.application.course_eval.errors import CourseEvalAccessDenied

        user_id, is_admin = _viewer_scope(runtime_context)
        if not is_admin and str(getattr(task, "owner_user_id", "") or "") != user_id:
            raise CourseEvalAccessDenied()
    if require_editable:
        from backend.application.course_eval.task_service import assert_can_edit_data

        assert_can_edit_data(task)
    return task


def _llm_json_parse(text: str) -> dict:
    """从模型输出中提取 JSON（容忍代码块包裹）。"""
    import re

    cleaned = str(text).strip()
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.S)
    if m:
        cleaned = m.group(1).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        raise json.JSONDecodeError("no json object", cleaned, 0)
    return json.loads(cleaned[start : end + 1])


async def _llm_json_call(prompt: str, *, temperature: float | None = None) -> dict:
    """调用平台 chat 模型并解析 JSON 输出。

    结构化抽取/映射建议这类任务传 temperature=0：同样的大纲和试卷应产出
    同样的结果，否则重跑一次评价映射就变、达成度跨次不可比。
    """
    from backend.core.models.factory import create_scoped_chat_model_async

    kwargs: dict = {}
    if temperature is not None:
        kwargs["temperature"] = temperature
    model = await create_scoped_chat_model_async("chat", **kwargs)
    # 工具内部的模型调用必须打上内部标签：LangGraph 会把嵌套模型回调冒泡到
    # 主事件流，没有标签时其流式输出（``​`json {...}` 之类的控制面数据）会被
    # 当成主模型正文转发进聊天气泡（2026-08-13 生产实录根因）。
    resp = await model.ainvoke(
        prompt,
        config={
            "tags": ["tool-internal-llm", "course-eval-internal"],
            "metadata": {"purpose": "course_eval_internal_json"},
        },
    )
    return _llm_json_parse(str(resp.content))


def _auto_material_type(display_name: str, mime: str | None = None) -> str:
    """按文件名/类型推断材料类型（与前端命名规则一致）。"""
    name = display_name or ""
    if "成绩单" in name:
        return "grade_sheet"
    if "大纲" in name:
        return "syllabus"
    if "试卷" in name and (mime or "").lower() == "application/pdf":
        return "paper"
    return "other"


async def _resolve_file_display(session, file_id: str, user_id: str):
    """解析对话上传文件的展示名与 mime（供材料类型自动识别）。"""
    from backend.services.user_file_service import resolve_user_file_for_download

    row, allowed, reason = await resolve_user_file_for_download(
        session=session, file_id=file_id, current_user_id=user_id
    )
    return row, allowed, reason


def _read_snapshot(snapshot: dict | None) -> dict:
    """从统计快照 dict 提取可读视图（只读，不触发计算）。"""
    if not snapshot:
        return {"error": "暂无统计快照，请先通过工作台执行计算"}
    return {
        "scope": snapshot.get("scope"),
        "final_score_stats": snapshot.get("final_score_stats"),
        "section_rates": snapshot.get("section_rates"),
        "blocking_issues": snapshot.get("blocking_issues"),
    }


async def _register_report_artifact(
    runtime_context, *, filename: str, payload: bytes
) -> tuple[str, str] | None:
    """把渲染好的报告写进会话工件目录并登记为用户文件，返回 (file_id, 下载 URL)。

    走平台既有的 thread-artifacts 通道：文件归当前教师所有，下载经
    /api/v2/files/{id}/download 的属主校验。任何一步失败返回 None（调用方降级）。
    """
    try:
        user_id = str(getattr(runtime_context, "user_id", "") or "").strip()
        thread_id = str(getattr(runtime_context, "thread_id", "") or "").strip()
        if not user_id or not thread_id:
            logger.warning("[CourseEvalTool] 缺少会话上下文，报告文件未登记")
            return None
        from backend.core.config import (
            build_internal_thread_key,
            parse_internal_thread_key,
        )
        from backend.core.config.paths import get_paths
        from backend.services.user_file_service import (
            register_local_artifact_for_thread,
        )

        # 运行时给工具的 thread_id 可能已经是内部键（u_<user>__t_<thread>）。
        # 再包一层会让下游把「<user>__t_u_<user>」整段当成 owner_user_id（超长报错），
        # 这里先解析：能解析出 user 的直接原样使用。
        parsed_user, external_thread = parse_internal_thread_key(thread_id)
        if parsed_user:
            internal_id = thread_id
        else:
            internal_id = build_internal_thread_key(user_id, external_thread)
        artifacts_dir = get_paths().thread_artifacts_dir(internal_id)
        await asyncio.to_thread(artifacts_dir.mkdir, parents=True, exist_ok=True)
        target = artifacts_dir / filename

        def _write_atomic() -> None:
            # 同名工件可能被并发/重复生成覆盖:直接 O_TRUNC 写目标路径,两个
            # 写者交错会产生撕裂的 docx(教师下载到损坏文件)。写唯一临时名
            # 后 os.replace 原子换名,读者任何时刻看到的都是完整文件。
            tmp = target.with_name(
                f"{target.name}.{os.getpid()}-{threading.get_ident()}.part"
            )
            try:
                tmp.write_bytes(payload)
                os.replace(tmp, target)
            finally:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass

        await asyncio.to_thread(_write_atomic)
        return await register_local_artifact_for_thread(
            internal_thread_id=internal_id,
            artifact_filename=filename,
        )
    except Exception as exc:  # noqa: BLE001 - 登记失败必须降级而不是丢报告
        logger.warning("[CourseEvalTool] 报告文件登记失败: %s", exc)
        return None


def _extract_text_bytes(data: bytes) -> str:
    """从 PDF/docx/txt 提取文本（大纲/制度文件）。"""
    try:
        import pymupdf

        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            return "\n".join(page.get_text() for page in doc)
        finally:
            doc.close()
    except Exception:
        pass
    try:
        from docx import Document

        document = Document(__import__("io").BytesIO(data))
        return "\n".join(p.text for p in document.paragraphs)
    except Exception:
        pass
    try:
        return data.decode("utf-8", errors="ignore")
    except Exception:
        return ""
