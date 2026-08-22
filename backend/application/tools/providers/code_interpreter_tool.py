"""execute_python：受限 Python 代码执行沙箱工具。

父进程侧职责：
- 组建一次性 workdir（config.json + user_code.py），以最小化环境变量启动
  ``code_interpreter_runner.py`` 子进程（凭据类环境变量一律不继承）；
- 受控注入会话数据文件：按「当前用户 + 当前会话线程 + active」强约束解析
  ``input_files``，物化为 workdir/inputs/ 下的数据副本供代码读取（沙箱读
  路径本就允许 workdir，runner 无需任何放宽）；
- 强制墙钟超时（进程组 SIGKILL）；
- 校验并上传子进程捕获的 matplotlib 图表到既有 chart_image_store 双轨
  管道，返回签名代理 URL；
- 采集 workdir/outputs/ 下由代码生成的结果文件，走 thread-artifacts 通道
  （与 course_eval 报告同路径：写入会话工件目录 + 登记 user_files）返回
  属主校验的下载链接；
- 把结构化结果渲染为模型可读文本。

安全边界（审计钩子 + rlimit）在 runner 内实现，见其模块 docstring。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import uuid
import weakref
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from langchain.tools import tool

from backend.config.loggers import get_logger
from backend.core.thread_key import parse_internal_thread_key
from backend.services.chart_image_store import store_chart

logger = get_logger(__name__)

_RUNNER_PATH = Path(__file__).with_name("code_interpreter_runner.py")
_MPL_CACHE_DIR = Path(__file__).resolve().parents[3] / "data" / "sandbox" / "mpl-cache"
_INPUT_DIR_NAME = "inputs"
_OUTPUT_DIR_NAME = "outputs"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_CHART_BYTES = 3 * 1024 * 1024
_MAX_RESULT_JSON_BYTES = 2 * 1024 * 1024

_SANDBOX_SEMAPHORES: (
    "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore]"
) = weakref.WeakKeyDictionary()


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except ValueError:
        value = default
    return max(minimum, min(maximum, value))


def _max_code_chars() -> int:
    return _env_int("PY_SANDBOX_MAX_CODE_CHARS", 20000, minimum=1000, maximum=200000)


def _wall_timeout_ceiling() -> int:
    return _env_int("PY_SANDBOX_WALL_TIMEOUT_SECONDS", 55, minimum=5, maximum=300)


def _memory_bytes() -> int:
    return (
        _env_int("PY_SANDBOX_MEMORY_MB", 1024, minimum=128, maximum=8192) * 1024 * 1024
    )


def _max_stdout_chars() -> int:
    return _env_int("PY_SANDBOX_MAX_OUTPUT_CHARS", 12000, minimum=1000, maximum=100000)


def _max_charts() -> int:
    return _env_int("PY_SANDBOX_MAX_CHARTS", 6, minimum=1, maximum=12)


def _max_concurrency() -> int:
    return _env_int("PY_SANDBOX_MAX_CONCURRENCY", 2, minimum=1, maximum=8)


def _max_input_files() -> int:
    return _env_int("PY_SANDBOX_MAX_INPUT_FILES", 8, minimum=1, maximum=32)


def _max_input_file_bytes() -> int:
    return (
        _env_int("PY_SANDBOX_MAX_INPUT_FILE_MB", 64, minimum=1, maximum=512)
        * 1024
        * 1024
    )


def _max_output_files() -> int:
    return _env_int("PY_SANDBOX_MAX_OUTPUT_FILES", 5, minimum=1, maximum=20)


def _max_output_file_bytes() -> int:
    return (
        _env_int("PY_SANDBOX_MAX_OUTPUT_FILE_MB", 20, minimum=1, maximum=200)
        * 1024
        * 1024
    )


def _loop_semaphore() -> asyncio.Semaphore:
    """按事件循环隔离的并发闸门：asyncio 原语不得跨循环共享。"""

    loop = asyncio.get_running_loop()
    semaphore = _SANDBOX_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_max_concurrency())
        _SANDBOX_SEMAPHORES[loop] = semaphore
    return semaphore


def _ensure_mpl_cache_dir() -> Path:
    _MPL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _MPL_CACHE_DIR


def _child_env(workdir: Path) -> dict[str, str]:
    """最小化子进程环境：不继承父进程任何凭据/配置变量。"""

    return {
        "PATH": "/usr/bin:/bin",
        "HOME": str(workdir),
        "TMPDIR": str(workdir),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "MPLBACKEND": "Agg",
        "MPLCONFIGDIR": str(_ensure_mpl_cache_dir()),
        "OPENBLAS_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1",
        "MKL_NUM_THREADS": "1",
        "NUMEXPR_NUM_THREADS": "1",
        "VECLIB_MAXIMUM_THREADS": "1",
    }


def _safe_input_filename(name: str, *, fallback: str) -> str:
    """净化会话文件显示名，使其可安全落入 workdir/inputs 目录。

    display_name 完全由上传客户端控制，可含 ``..``、路径分隔与控制字符；
    取 basename、剔除危险字符并限长，防止物化路径逃逸 inputs 目录。
    """
    base = Path(str(name or "")).name
    cleaned = (
        "".join(ch for ch in base if ch.isprintable() and ch not in '/\\"\r\n\t')
        .strip()
        .strip(".")
    )
    return cleaned[:120] or fallback


async def _load_thread_files(user_id: str, thread_id: str) -> list[dict[str, Any]]:
    """列出当前用户在当前会话线程下的 active 文件（测试可替换的接缝）。"""

    from backend.db.connection import get_async_session_factory
    from backend.services.user_file_service import (
        list_active_files_for_thread_for_user,
    )

    factory = get_async_session_factory()
    async with factory() as session:
        return await list_active_files_for_thread_for_user(
            session=session,
            current_user_id=user_id,
            thread_id=thread_id,
            limit=200,
        )


async def _load_owned_file(user_id: str, file_id: str) -> dict[str, Any] | None:
    """按 file_id 查当前用户名下的 active 文件（测试可替换的接缝）。

    覆盖个人文件库与该用户其他会话的文件；严格 owner-only，不走授权/
    管理员通道。
    """
    from sqlalchemy import text

    from backend.db.connection import get_async_session_factory

    factory = get_async_session_factory()
    async with factory() as session:
        result = await session.execute(
            text(
                """
                SELECT file_id, display_name, mime_type, size_bytes,
                       source_type, storage_bucket, storage_object, local_abs_path
                FROM user_files
                WHERE file_id = :file_id
                  AND owner_user_id = :owner_user_id
                  AND status = 'active'
                LIMIT 1
                """
            ),
            {"file_id": file_id, "owner_user_id": user_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def _resolve_input_files(
    requested: list[str],
    *,
    user_id: str,
    thread_id: str,
) -> tuple[list[dict[str, Any]], str | None]:
    """把请求的文件名/file_id 解析为可注入沙箱的文件规格列表。

    强约束：仅允许当前认证用户在当前会话线程上传且仍 active 的文件；
    不走授权/管理员通道，防止沙箱变成越权读取任意用户文件的旁路。
    返回 (specs, error)；error 非空时调用方直接失败，不执行代码。
    """
    names: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        item = str(raw or "").strip()
        if item and item not in seen:
            names.append(item)
            seen.add(item)
    if not names:
        return [], None
    if len(names) > _max_input_files():
        return [], (
            f"input_files 数量超限（{len(names)} 个，上限 {_max_input_files()}）。"
            "请分批注入。"
        )

    normalized_user = str(user_id or "").strip()
    if (
        not normalized_user
        or normalized_user == "public"
        or normalized_user == "guest"
        or normalized_user.startswith("guest_")
    ):
        return [], "当前身份没有可注入的会话文件（需要登录用户的会话上下文）。"
    try:
        key_user, external_thread_id = parse_internal_thread_key(
            str(thread_id or "").strip()
        )
    except ValueError:
        return [], "当前会话缺少线程上下文，无法注入会话文件。"
    if key_user and key_user != normalized_user:
        return [], "会话线程与当前用户不匹配，已拒绝注入文件。"

    rows = await _load_thread_files(normalized_user, external_thread_id)

    per_file_limit = _max_input_file_bytes()
    specs: list[dict[str, Any]] = []
    missing: list[str] = []
    matched_ids: set[str] = set()
    used_names: set[str] = set()
    for item in names:
        row = next(
            (r for r in rows if str(r.get("file_id") or "") == item),
            None,
        )
        if row is None:
            lowered = item.lower()
            base_lowered = Path(item).name.lower()
            row = next(
                (
                    r
                    for r in rows
                    if str(r.get("display_name") or "").lower()
                    in (lowered, base_lowered)
                ),
                None,
            )
        if row is None and item.startswith("uf_"):
            # 会话内没有时，file_id 允许精确引用当前用户名下的任意 active
            # 文件（个人文件库/历史会话）；文件名匹配仍严格限当前会话。
            row = await _load_owned_file(normalized_user, item)
        if row is None:
            missing.append(item)
            continue
        file_id = str(row.get("file_id") or "")
        if file_id in matched_ids:
            continue
        matched_ids.add(file_id)

        display_name = str(row.get("display_name") or "")
        size_bytes = row.get("size_bytes")
        if isinstance(size_bytes, int) and size_bytes > per_file_limit:
            return [], (
                f"文件 {display_name} 过大（{size_bytes} 字节，单文件上限 "
                f"{per_file_limit} 字节）。请先筛选/拆分数据再分析。"
            )

        safe_name = _safe_input_filename(display_name, fallback=f"{file_id}.bin")
        base_name = safe_name
        ordinal = 2
        while safe_name in used_names:
            path_obj = Path(base_name)
            safe_name = f"{path_obj.stem}_{ordinal}{path_obj.suffix}"
            ordinal += 1
        used_names.add(safe_name)

        specs.append(
            {
                "file_id": file_id,
                "name": safe_name,
                "display_name": display_name,
                "storage_bucket": row.get("storage_bucket"),
                "storage_object": row.get("storage_object"),
                "local_abs_path": row.get("local_abs_path"),
            }
        )
    if missing:
        available = (
            "、".join(
                str(r.get("display_name") or r.get("file_id") or "")
                for r in rows[:20]
            )
            or "（无）"
        )
        return [], (
            "以下文件在当前会话中不存在: "
            + "、".join(missing)
            + f"。当前会话可用文件: {available}。"
        )
    return specs, None


async def _materialize_input_files(
    workdir: Path, specs: list[dict[str, Any]]
) -> tuple[list[str], str | None]:
    """把已解析的会话文件物化到 workdir/inputs/，返回 (相对路径列表, error)。"""

    if not specs:
        return [], None
    inputs_dir = workdir / _INPUT_DIR_NAME
    await asyncio.to_thread(inputs_dir.mkdir, parents=True, exist_ok=True)
    per_file_limit = _max_input_file_bytes()
    injected: list[str] = []
    for spec in specs:
        display_name = str(spec.get("display_name") or spec.get("name") or "")
        target = inputs_dir / str(spec["name"])
        try:
            local_path = str(spec.get("local_abs_path") or "").strip()
            bucket = str(spec.get("storage_bucket") or "").strip()
            storage_object = str(spec.get("storage_object") or "").strip()
            if local_path and await asyncio.to_thread(Path(local_path).is_file):
                await asyncio.to_thread(shutil.copyfile, local_path, target)
            elif bucket and storage_object:
                from backend.storage import download_minio_object_to_path

                await asyncio.to_thread(
                    download_minio_object_to_path,
                    storage_object,
                    target,
                    bucket_name=bucket,
                )
            else:
                return [], f"文件 {display_name} 缺少可用存储来源，无法注入。"
        except Exception:
            logger.warning(
                "[ExecutePython] 注入会话文件失败: %s",
                spec.get("file_id"),
                exc_info=True,
            )
            return [], f"文件 {display_name} 注入失败，请稍后重试。"
        # DB 的 size_bytes 可能缺失/过期，落盘后按实际大小再校验一次。
        size = (await asyncio.to_thread(target.stat)).st_size
        if size > per_file_limit:
            return [], (
                f"文件 {display_name} 过大（{size} 字节，单文件上限 "
                f"{per_file_limit} 字节）。请先筛选/拆分数据再分析。"
            )
        injected.append(f"{_INPUT_DIR_NAME}/{spec['name']}")
    return injected, None


def _thread_artifacts_dir(internal_thread_id: str) -> Path:
    """定位会话工件目录（测试可替换的接缝）。"""

    from backend.core.config.paths import get_paths

    return get_paths().thread_artifacts_dir(internal_thread_id)


async def _register_thread_artifact(
    internal_thread_id: str, filename: str
) -> tuple[str, str] | None:
    """把工件目录中的文件登记为用户文件（测试可替换的接缝）。"""

    from backend.services.user_file_service import register_local_artifact_for_thread

    return await register_local_artifact_for_thread(
        internal_thread_id=internal_thread_id,
        artifact_filename=filename,
    )


def _resolve_internal_thread_id(user_id: str, thread_id: str) -> str | None:
    """把运行时线程标识规整为内部键（u_<user>__t_<thread>）。

    运行时给工具的 thread_id 可能已经是内部键；再包一层会产生非法超长
    owner。与 course_eval `_register_report_artifact` 同一处理方式。
    """
    normalized_user = str(user_id or "").strip()
    normalized_thread = str(thread_id or "").strip()
    if not normalized_user or normalized_user == "public" or not normalized_thread:
        return None
    from backend.core.thread_key import build_internal_thread_key

    try:
        parsed_user, external_thread = parse_internal_thread_key(normalized_thread)
        if parsed_user:
            return normalized_thread
        return build_internal_thread_key(normalized_user, external_thread)
    except ValueError:
        return None


async def _persist_output_files(
    outputs_dir: Path, *, user_id: str, thread_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """把 outputs/ 下的结果文件持久化为会话工件，返回 (工件清单, 提示)。

    单个文件失败只跳过并提示，不让整次执行失败——代码本身已经跑完。
    """
    entries: list[dict[str, Any]] = []
    notes: list[str] = []

    def _scan() -> list[Path]:
        if not outputs_dir.is_dir():
            return []
        root = outputs_dir.resolve()
        found: list[Path] = []
        for candidate in sorted(outputs_dir.rglob("*")):
            # 沙箱本身已封死 os.symlink；这里再做一次防御性排除。
            if candidate.is_symlink() or not candidate.is_file():
                continue
            try:
                candidate.resolve().relative_to(root)
            except ValueError:
                continue
            found.append(candidate)
        return found

    files = await asyncio.to_thread(_scan)
    if not files:
        return [], []

    internal_thread_id = _resolve_internal_thread_id(user_id, thread_id)
    if internal_thread_id is None:
        return [], [
            f"代码在 {_OUTPUT_DIR_NAME}/ 生成了 {len(files)} 个文件，"
            "但当前运行时缺少会话上下文，未能保存为工件。"
        ]

    max_files = _max_output_files()
    if len(files) > max_files:
        notes.append(
            f"{_OUTPUT_DIR_NAME}/ 文件数超出上限（{len(files)} > {max_files}），"
            "仅保存前几个。"
        )
        files = files[:max_files]

    per_file_limit = _max_output_file_bytes()
    used_names: set[str] = set()
    for source in files:
        size = (await asyncio.to_thread(source.stat)).st_size
        display = source.name
        if size > per_file_limit:
            notes.append(
                f"产出文件 {display} 过大（{size} 字节，上限 {per_file_limit}），已跳过。"
            )
            continue
        safe_name = _safe_input_filename(display, fallback=f"output-{len(entries)}.bin")
        base_name = safe_name
        ordinal = 2
        while safe_name in used_names:
            path_obj = Path(base_name)
            safe_name = f"{path_obj.stem}_{ordinal}{path_obj.suffix}"
            ordinal += 1
        used_names.add(safe_name)
        try:
            artifacts_dir = await asyncio.to_thread(
                _thread_artifacts_dir, internal_thread_id
            )
            target = artifacts_dir / safe_name

            def _copy_atomic(src: Path = source, dst: Path = target) -> None:
                dst.parent.mkdir(parents=True, exist_ok=True)
                tmp = dst.with_name(f"{dst.name}.{uuid.uuid4().hex[:8]}.part")
                try:
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, dst)
                finally:
                    with suppress(OSError):
                        if tmp.exists():
                            tmp.unlink()

            await asyncio.to_thread(_copy_atomic)
            registered = await _register_thread_artifact(internal_thread_id, safe_name)
        except Exception:
            logger.warning(
                "[ExecutePython] 产出文件持久化失败: %s", display, exc_info=True
            )
            registered = None
        if registered is None:
            notes.append(f"产出文件 {display} 保存失败，已跳过。")
            continue
        _, download_url = registered
        entries.append(
            {"name": safe_name, "bytes": size, "download_url": download_url}
        )
    return entries, notes


async def _run_sandbox(
    code: str,
    *,
    wall_timeout: int,
    input_specs: list[dict[str, Any]] | None = None,
    output_persister: (
        Callable[[Path], Awaitable[tuple[list[dict[str, Any]], list[str]]]] | None
    ) = None,
) -> dict[str, Any]:
    """执行一段代码并返回结构化结果 dict（不抛业务异常）。"""

    workdir = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="pysbx-")).resolve()
    try:
        config = {
            "memory_bytes": _memory_bytes(),
            "cpu_seconds": wall_timeout,
            "max_stdout_chars": _max_stdout_chars(),
            "max_stderr_chars": max(1000, _max_stdout_chars() // 3),
            "max_charts": _max_charts(),
            "extra_write_dirs": [str(_ensure_mpl_cache_dir())],
        }
        await asyncio.to_thread(
            (workdir / "config.json").write_text,
            json.dumps(config, ensure_ascii=False),
            "utf-8",
        )
        await asyncio.to_thread((workdir / "user_code.py").write_text, code, "utf-8")

        injected_files, inject_error = await _materialize_input_files(
            workdir, input_specs or []
        )
        if inject_error:
            return {"ok": False, "infra_error": inject_error, "charts": []}
        # 预创建 outputs/，代码可直接写入结果文件而无需自行建目录。
        await asyncio.to_thread(
            (workdir / _OUTPUT_DIR_NAME).mkdir, parents=True, exist_ok=True
        )

        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-B",
            str(_RUNNER_PATH),
            str(workdir),
            cwd=str(workdir),
            env=_child_env(workdir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            raw_stdout, raw_stderr = await asyncio.wait_for(
                process.communicate(), timeout=wall_timeout + 5
            )
        except TimeoutError:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            with suppress(Exception):
                await process.wait()
            return {
                "ok": False,
                "infra_error": (
                    f"执行超时（墙钟 {wall_timeout}s），进程已被终止。"
                    "请缩小计算规模或提高 timeout_seconds。"
                ),
                "charts": [],
            }

        result_path = workdir / "result.json"
        payload: dict[str, Any] | None = None
        if (
            result_path.exists()
            and result_path.stat().st_size <= _MAX_RESULT_JSON_BYTES
        ):
            with suppress(Exception):
                loaded = json.loads(
                    await asyncio.to_thread(result_path.read_text, "utf-8")
                )
                if isinstance(loaded, dict):
                    payload = loaded
        if payload is None:
            detail = (raw_stderr or raw_stdout or b"").decode(
                "utf-8", errors="replace"
            )[-1500:]
            reason = "沙箱进程异常退出"
            if process.returncode == -signal.SIGXCPU:
                reason = f"CPU 时间超限（{wall_timeout}s）"
            elif process.returncode == -signal.SIGKILL:
                reason = "进程被强制终止（可能超出内存限制）"
            return {
                "ok": False,
                "infra_error": (
                    f"{reason}，exit_code={process.returncode}。"
                    + (f"\n诊断输出（截尾）:\n{detail}" if detail else "")
                ),
                "charts": [],
            }

        chart_files: list[Path] = []
        for name in payload.get("charts") or []:
            candidate = (workdir / str(name)).resolve()
            if not str(candidate).startswith(str(workdir) + os.sep):
                continue
            if not candidate.is_file() or candidate.stat().st_size > _MAX_CHART_BYTES:
                continue
            header = await asyncio.to_thread(
                lambda path: path.open("rb").read(8), candidate
            )
            if header == _PNG_MAGIC:
                chart_files.append(candidate)
        payload["chart_bytes"] = [
            await asyncio.to_thread(item.read_bytes) for item in chart_files
        ]
        payload["injected_files"] = injected_files
        # 产出文件必须在 workdir 清理前持久化；仅在代码执行成功时采集，
        # 失败运行的半成品文件不作为交付物。
        if payload.get("ok") and output_persister is not None:
            try:
                artifacts, notes = await output_persister(workdir / _OUTPUT_DIR_NAME)
                payload["output_artifacts"] = artifacts
                payload["output_notes"] = notes
            except Exception:
                logger.warning("[ExecutePython] 产出文件处理失败", exc_info=True)
                payload["output_notes"] = ["产出文件处理失败，已跳过。"]
        return payload
    finally:
        with suppress(Exception):
            await asyncio.to_thread(shutil.rmtree, workdir, ignore_errors=True)


async def _upload_charts(chart_bytes: list[bytes], *, user_id: str) -> list[str]:
    urls: list[str] = []
    for index, data in enumerate(chart_bytes, start=1):
        try:
            url = await store_chart(
                data,
                user_id=user_id,
                chart_id=f"py{uuid.uuid4().hex[:20]}",
            )
            urls.append(url)
        except Exception:
            logger.warning(
                "[ExecutePython] 图表 %s 上传失败，已跳过", index, exc_info=True
            )
    return urls


def _render_result(payload: dict[str, Any], chart_urls: list[str]) -> str:
    lines: list[str] = []
    infra_error = payload.get("infra_error")
    if infra_error:
        lines.append(f"【执行失败】{infra_error}")
        return "\n".join(lines)

    duration = payload.get("duration_ms")
    if payload.get("ok"):
        lines.append(f"【执行成功】耗时 {duration}ms")
    else:
        error = payload.get("error") or {}
        lines.append(
            f"【执行失败】{error.get('type', 'Error')}: {error.get('message', '')}"
        )
        trace = str(error.get("traceback") or "").strip()
        if trace:
            lines.append("--- traceback ---")
            lines.append(trace)

    injected = payload.get("injected_files") or []
    if injected:
        lines.append("已注入会话文件: " + "、".join(str(item) for item in injected))

    stdout = str(payload.get("stdout") or "").rstrip()
    if stdout:
        lines.append("--- stdout ---")
        lines.append(stdout)
        if payload.get("stdout_truncated"):
            lines.append("（stdout 已达上限被截断；如需完整数据请分批 print）")
    stderr = str(payload.get("stderr") or "").rstrip()
    if stderr:
        # 成功执行时的 stderr 多为 numpy/pandas 运行时警告（dtype 混杂、
        # 无效值等），是结果可信度的重要信号，必须让模型看到以便自纠。
        lines.append(
            "--- stderr（警告/诊断）---" if payload.get("ok") else "--- stderr ---"
        )
        lines.append(stderr)

    if chart_urls:
        lines.append("--- 图表 ---")
        for index, url in enumerate(chart_urls, start=1):
            lines.append(f"![执行图表{index}]({url})")
        if payload.get("charts_truncated"):
            lines.append("（图表数量超出上限，仅保留前几张）")
        lines.append("如需在回答中展示图表，请原样引用上述 Markdown 图片链接。")

    artifacts = payload.get("output_artifacts") or []
    if artifacts:
        lines.append("--- 产出文件 ---")
        for item in artifacts:
            lines.append(
                f"- [{item['name']}]({item['download_url']})（{item['bytes']} 字节）"
            )
        lines.append("如需向用户提供结果文件，请原样引用上述下载链接。")
    for note in payload.get("output_notes") or []:
        lines.append(f"（{note}）")

    if payload.get("ok") and not stdout and not chart_urls and not artifacts:
        lines.append("（代码执行完成但没有任何输出；请用 print() 输出计算结果。）")
    return "\n".join(lines)


def get_execute_python_tool(runtime_context: Any = None):
    """按运行时身份构造 execute_python 工具（provider 工厂）。"""

    user_id = str(getattr(runtime_context, "user_id", "") or "").strip() or "public"
    thread_id = str(getattr(runtime_context, "thread_id", "") or "").strip()

    @tool("execute_python", parse_docstring=True)
    async def execute_python(
        code: str,
        timeout_seconds: int = 30,
        input_files: list[str] | None = None,
    ) -> str:
        """在受限 Python 沙箱中执行代码，用于精确数值计算、公式验算、统计分析与绘图。

        代码必须自包含，用 print() 输出结果；可用 numpy / pandas / scipy /
        sympy / matplotlib（图表自动捕获并以 Markdown 图片链接返回，无需
        savefig/show）。沙箱内禁止联网、派生子进程与访问沙箱外文件。
        需要分析用户在当前会话上传的数据文件（CSV/Excel/JSON 等）时，把
        文件名传入 input_files，文件会以数据副本出现在工作目录 inputs/ 下，
        代码用相对路径读取，例如 pd.read_excel("inputs/成绩表.xlsx")。
        需要生成可下载的结果文件（处理后的 CSV/Excel 等）时，把文件写入
        outputs/ 目录（已预创建），执行成功后自动保存为会话工件并返回
        下载链接。

        Args:
            code: 要执行的自包含 Python 代码。
            timeout_seconds: 墙钟超时秒数（5-55，默认 30）。
            input_files: 需要注入沙箱的数据文件列表；按文件名匹配当前会话
                上传的文件，或按 file_id（uf_ 开头）精确引用当前用户个人
                文件库/历史会话中的文件。注入后代码从 inputs/<文件名> 读取。
        """
        cleaned = str(code or "").strip()
        if not cleaned:
            return "【执行失败】code 为空。请提供自包含的 Python 代码。"
        if len(cleaned) > _max_code_chars():
            return (
                f"【执行失败】代码超长（{len(cleaned)} 字符，上限 "
                f"{_max_code_chars()}）。请精简代码或分步执行。"
            )
        wall_timeout = max(5, min(int(timeout_seconds or 30), _wall_timeout_ceiling()))

        input_specs: list[dict[str, Any]] = []
        requested_inputs = [str(item) for item in (input_files or [])]
        if requested_inputs:
            input_specs, input_error = await _resolve_input_files(
                requested_inputs, user_id=user_id, thread_id=thread_id
            )
            if input_error:
                return f"【执行失败】{input_error}"

        async def _persist(outputs_dir: Path):
            return await _persist_output_files(
                outputs_dir, user_id=user_id, thread_id=thread_id
            )

        async with _loop_semaphore():
            payload = await _run_sandbox(
                cleaned,
                wall_timeout=wall_timeout,
                input_specs=input_specs,
                output_persister=_persist,
            )

        chart_urls = await _upload_charts(
            payload.pop("chart_bytes", []) or [], user_id=user_id
        )
        rendered = _render_result(payload, chart_urls)
        logger.info(
            "[ExecutePython] 执行完成: user=%s ok=%s duration_ms=%s charts=%s "
            "inputs=%s outputs=%s",
            user_id,
            payload.get("ok"),
            payload.get("duration_ms"),
            len(chart_urls),
            len(payload.get("injected_files") or []),
            len(payload.get("output_artifacts") or []),
        )
        return rendered

    return execute_python
