"""execute_python 沙箱子进程 runner。

以 ``python -I -B code_interpreter_runner.py <workdir>`` 方式独立执行，
与父进程（Agent API / worker）的唯一契约是 workdir 内的文件布局：

- ``config.json``   父进程写入的资源限制与输出上限；
- ``user_code.py``  待执行的用户代码；
- ``result.json``   本 runner 写回的结构化执行结果；
- ``chart_*.png``   自动捕获的 matplotlib 图表。

安全模型（防御目标是 LLM 生成代码与提示注入，而非恶意本地用户）：

1. 进程隔离：``-I`` 隔离模式 + 父进程传入的最小化环境变量（不继承任何
   凭据）；
2. 资源限制：RLIMIT_AS / RLIMIT_CPU / RLIMIT_FSIZE / RLIMIT_CORE；
3. 审计钩子（``sys.addaudithook``，安装后不可移除）：
   - 禁全部 socket 操作（联网）；
   - 禁 subprocess / os.exec / os.fork / os.system 等进程派生；
   - 禁 ctypes dlopen/dlsym（native 逃逸面）；
   - 写路径仅允许 workdir 与 matplotlib 缓存目录；
   - 读路径仅允许解释器安装目录、系统库/字体目录与 workdir，
     阻止读取项目 ``.env``、数据库文件等服务器敏感内容。

本模块不得 import backend 包：``-I`` 隐含 -P，脚本目录不入 sys.path。
"""

from __future__ import annotations

import ast
import ctypes
import io
import json
import os
import sys
import time
import traceback
from contextlib import suppress

_BLOCKED_EVENT_PREFIXES = (
    "socket.",
    "subprocess.",
    "ctypes.",
    "cffi.",
    "ftplib.",
    "smtplib.",
    "poplib.",
    "imaplib.",
    "telnetlib.",
    "pty.",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "os.fork",
    "os.system",
    "os.startfile",
    "winreg.",
)

_BLOCKED_IMPORT_ROOTS = frozenset(
    {
        "ctypes",
        "cffi",
        "_cffi_backend",
        "socket",
        "subprocess",
        "multiprocessing",
        "concurrent",
        "asyncio",
        "selectors",
        "signal",
        "importlib",
        "runpy",
        "webbrowser",
        "urllib",
        "http",
        "ftplib",
        "smtplib",
        "poplib",
        "imaplib",
        "telnetlib",
        "ssl",
        "requests",
        "httpx",
        "aiohttp",
        "builtins",
    }
)

_BLOCKED_CALLABLE_NAMES = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "input",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "vars",
        "globals",
        "locals",
    }
)

# 事件的全部字符串参数都按路径检查（写目录约束）。
_WRITE_PATH_EVENTS = frozenset(
    {
        "os.remove",
        "os.rmdir",
        "os.rename",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.chmod",
        "os.chown",
        "os.utime",
        "os.mkdir",
        "shutil.rmtree",
        "shutil.move",
        "shutil.chown",
        "shutil.copytree",
        "shutil.copyfile",
        "shutil.copymode",
        "shutil.copystat",
    }
)

_LIST_PATH_EVENTS = frozenset({"os.listdir", "os.scandir", "glob.glob"})


class _BoundedBuffer(io.TextIOBase):
    """截断超限输出而不是中断执行的文本缓冲。"""

    def __init__(self, limit_chars: int):
        self._chunks: list[str] = []
        self._length = 0
        self._limit = max(0, int(limit_chars))
        self.truncated = False

    def writable(self) -> bool:  # pragma: no cover - io 协议
        return True

    def write(self, text) -> int:
        text = str(text)
        if not text:
            return 0
        if self._length >= self._limit:
            self.truncated = True
            return len(text)
        remaining = self._limit - self._length
        if len(text) > remaining:
            self._chunks.append(text[:remaining])
            self._length = self._limit
            self.truncated = True
        else:
            self._chunks.append(text)
            self._length += len(text)
        return len(text)

    def getvalue(self) -> str:
        return "".join(self._chunks)


def _load_config(workdir: str) -> dict:
    try:
        with open(
            os.path.join(workdir, "config.json"), "r", encoding="utf-8"
        ) as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _apply_memory_rlimits(config: dict) -> None:
    import resource

    memory_bytes = int(config.get("memory_bytes") or 1024 * 1024 * 1024)
    fsize_bytes = int(config.get("fsize_bytes") or 64 * 1024 * 1024)
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_FSIZE, (fsize_bytes, fsize_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _apply_cpu_rlimit(config: dict) -> None:
    import resource

    cpu_seconds = int(config.get("cpu_seconds") or 20)
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 2))


def _apply_process_isolation() -> None:
    """补一层 OS 级进程隔离,作为 Python 审计钩子/源码校验之上的纵深防御。

    - RLIMIT_NOFILE=256:防用户代码把 fd 表耗尽后影响后续通信。
    - PR_SET_NO_NEW_PRIVS:禁止子进程再经 setuid/文件能力提权。父进程以
      root 运行时这条尤为重要——它是内核强制的一劳永逸开关,审计钩子挡
      不住 native 代码直接 syscall。

    两者都刻意做成非致命:平台不支持(非 Linux、无 prctl)时降级为仅
    rlimit,不因加固而让沙箱整体不可用。
    """
    import resource

    with suppress(Exception):
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
    try:
        # PR_SET_NO_NEW_PRIVS = 38 (linux/prctl.h)。
        prctl = ctypes.CDLL(None, use_errno=True).prctl
        prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        prctl.restype = ctypes.c_int
        prctl(38, 1, 0, 0, 0)
    except Exception:
        # 无 prctl(非 Linux)或加载失败:保留审计钩子/资源上限兜底。
        pass


# 预导入并预热科学计算栈，让 numpy/scipy/matplotlib 在审计钩子安装之前
# 完成各自 native 库的 dlopen（openblas/freetype 等）。之后审计钩子封禁
# 全部 ctypes.dlopen——否则常规 `import numpy` 会在钩子安装后触发 dlopen 被
# 误拦，而放开 dlopen 又等于放开 `ctypes.CDLL("libc.so.6").system()` 这类
# 绕过子进程审计的 native 逃逸面。预热失败不致命（对应库对用户代码不可用）。
def _prewarm_scientific_stack() -> None:
    with suppress(Exception):
        import numpy as _np

        _np.zeros(2, dtype=float).sum()
    with suppress(Exception):
        import pandas as _pd  # noqa: F401
    with suppress(Exception):
        import scipy  # noqa: F401
    with suppress(Exception):
        import sympy  # noqa: F401
    with suppress(Exception):
        import matplotlib

        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as _plt

        _figure = _plt.figure()
        _plt.plot([0, 1], [0, 1])
        _plt.close(_figure)


def _normalize_dir(path: str) -> str:
    real = os.path.realpath(path)
    return real.rstrip(os.sep) or os.sep


def _path_allowed(path: str, allowed: tuple[str, ...]) -> bool:
    real = os.path.realpath(path)
    for prefix in allowed:
        if real == prefix or real.startswith(prefix + os.sep):
            return True
    return False


def _install_audit_hook(workdir: str, extra_write_dirs: list[str]) -> None:
    write_dirs = tuple(
        _normalize_dir(item) for item in [workdir, *extra_write_dirs, "/dev/null"]
    )
    read_prefixes = tuple(
        dict.fromkeys(
            _normalize_dir(item)
            for item in [
                workdir,
                *extra_write_dirs,
                # traceback/warnings 经 linecache 回读本文件渲染栈帧源码行。
                os.path.realpath(__file__),
                sys.prefix,
                sys.base_prefix,
                sys.exec_prefix,
                getattr(sys, "base_exec_prefix", sys.prefix),
                "/usr",
                "/lib",
                "/lib64",
                "/etc/fonts",
                "/etc/ld.so.cache",
                "/etc/localtime",
                "/etc/mime.types",
                "/var/cache/fontconfig",
                f"/proc/{os.getpid()}",
                "/proc/cpuinfo",
                "/proc/meminfo",
                "/proc/stat",
                "/proc/loadavg",
                "/proc/filesystems",
                "/proc/sys",
                "/dev/urandom",
                "/dev/random",
                "/dev/null",
                "/dev/zero",
            ]
        )
    )

    def _deny(event: str, detail: str) -> None:
        raise RuntimeError(f"沙箱已拦截受限操作: {event} ({detail})")

    def _check_paths(event: str, candidates, allowed: tuple[str, ...]) -> None:
        for candidate in candidates:
            if candidate is None or isinstance(candidate, int):
                continue
            if isinstance(candidate, bytes):
                candidate = os.fsdecode(candidate)
            if not isinstance(candidate, str):
                continue
            if not _path_allowed(candidate, allowed):
                _deny(event, candidate)

    def _hook(event: str, args) -> None:
        if event.startswith(_BLOCKED_EVENT_PREFIXES):
            _deny(event, "网络访问与进程派生在沙箱中被禁止")
        if event == "open":
            path, mode, flags = (
                args[0] if len(args) > 0 else None,
                args[1] if len(args) > 1 else None,
                args[2] if len(args) > 2 else 0,
            )
            if path is None or isinstance(path, int):
                return
            wants_write = False
            if isinstance(mode, str):
                wants_write = any(ch in mode for ch in "wax+")
            elif mode is None:
                write_flags = (
                    os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC
                )
                wants_write = bool(int(flags or 0) & write_flags)
            _check_paths(
                event,
                [path],
                write_dirs if wants_write else read_prefixes,
            )
            return
        if event in _WRITE_PATH_EVENTS:
            _check_paths(event, args, write_dirs)
            return
        if event in _LIST_PATH_EVENTS:
            _check_paths(event, args[:1], read_prefixes)

    sys.addaudithook(_hook)


def _validate_source(source: str) -> None:
    """Fail closed on source constructs that reach outside the sandbox model.

    The audit hook cannot see native calls issued through cffi or process
    primitives imported before execution, so the runner also uses a narrow
    source-level allow policy for high-risk modules/builtins.  This is still
    only a defense layer, not a substitute for OS-level isolation.
    """

    try:
        tree = ast.parse(source, filename="<用户代码>", mode="exec")
    except SyntaxError:
        raise

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            for name in names:
                root = str(name or "").split(".", 1)[0].lower()
                if root in _BLOCKED_IMPORT_ROOTS:
                    raise RuntimeError(f"沙箱已拦截受限导入: {name}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BLOCKED_CALLABLE_NAMES:
                raise RuntimeError(f"沙箱已拦截受限操作: {func.id}")
            if isinstance(func, ast.Attribute) and func.attr in _BLOCKED_CALLABLE_NAMES:
                raise RuntimeError(f"沙箱已拦截受限操作: {func.attr}")


def _format_error(exc: BaseException, limit_chars: int = 4000) -> dict:
    try:
        lines = traceback.format_exception(type(exc), exc, exc.__traceback__)
        text = "".join(lines)
    except Exception:
        # 栈帧源码回读可能被审计钩子拦截；降级为无源码行的摘要。
        text = "".join(
            f'  File "{frame.filename}", line {frame.lineno}, in {frame.name}\n'
            for frame in traceback.extract_tb(exc.__traceback__)
        )
    if len(text) > limit_chars:
        text = "…（已截断）\n" + text[-limit_chars:]
    return {
        "type": type(exc).__name__,
        "message": str(exc)[:2000],
        "traceback": text,
    }


def _collect_charts(workdir: str, max_charts: int) -> tuple[list[str], bool]:
    if "matplotlib" not in sys.modules:
        return [], False
    try:
        import matplotlib.pyplot as plt

        fignums = plt.get_fignums()
        saved: list[str] = []
        for index, num in enumerate(fignums[:max_charts], start=1):
            figure = plt.figure(num)
            filename = f"chart_{index}.png"
            figure.savefig(
                os.path.join(workdir, filename),
                dpi=110,
                format="png",
                bbox_inches="tight",
            )
            saved.append(filename)
        plt.close("all")
        return saved, len(fignums) > max_charts
    except Exception:
        return [], False


def main() -> int:
    workdir = os.path.realpath(sys.argv[1])
    os.chdir(workdir)
    config = _load_config(workdir)

    with open(os.path.join(workdir, "user_code.py"), "r", encoding="utf-8") as handle:
        source = handle.read()

    _apply_memory_rlimits(config)
    _prewarm_scientific_stack()
    # CPU 预算从用户代码开始计时，不含预热耗时。
    _apply_cpu_rlimit(config)
    # 在审计钩子安装前补 OS 级隔离(prctl/NOFILE)。prctl 一经设置不可撤销,
    # 且这里发生在任何用户代码编译/执行之前,天然排除了逃逸窗口。
    _apply_process_isolation()

    stdout_limit = int(config.get("max_stdout_chars") or 12000)
    stderr_limit = int(config.get("max_stderr_chars") or 4000)
    max_charts = int(config.get("max_charts") or 6)
    extra_write_dirs = [
        str(item)
        for item in (config.get("extra_write_dirs") or [])
        if isinstance(item, str) and item
    ]

    out_buffer = _BoundedBuffer(stdout_limit)
    err_buffer = _BoundedBuffer(stderr_limit)

    _install_audit_hook(workdir, extra_write_dirs)

    result: dict = {"ok": True, "error": None}
    started = time.monotonic()
    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buffer, err_buffer
    try:
        _validate_source(source)
        compiled = compile(source, "<用户代码>", "exec")
        namespace = {"__name__": "__main__", "__builtins__": __builtins__}
        exec(compiled, namespace)  # noqa: S102 - 沙箱即为执行任意代码而生
    except SystemExit as exc:
        if exc.code not in (None, 0):
            result["ok"] = False
            result["error"] = _format_error(exc)
    except BaseException as exc:  # noqa: BLE001 - 全量兜底并结构化上报
        result["ok"] = False
        result["error"] = _format_error(exc)
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr

    charts, charts_truncated = _collect_charts(workdir, max_charts)
    result.update(
        {
            "stdout": out_buffer.getvalue(),
            "stdout_truncated": out_buffer.truncated,
            "stderr": err_buffer.getvalue(),
            "stderr_truncated": err_buffer.truncated,
            "charts": charts,
            "charts_truncated": charts_truncated,
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    )
    with open(os.path.join(workdir, "result.json"), "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
