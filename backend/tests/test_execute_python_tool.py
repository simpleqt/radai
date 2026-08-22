"""execute_python 沙箱工具契约测试。

覆盖：基本执行、错误上报、资源限制、网络/进程/文件系统拦截、
环境变量隔离、输出截断、图表捕获上传、会话文件注入，
以及注册表/策略接线。
"""

from __future__ import annotations

import pytest

from backend.application.tools.assembler import PolicyToolAssembler
from backend.application.tools.provider_loader import ToolProviderLoader
from backend.application.tools.providers import code_interpreter_tool
from backend.application.tools.providers.code_interpreter_tool import (
    get_execute_python_tool,
)
from backend.application.tools.registry import ToolRegistry


@pytest.fixture()
def execute_python():
    return get_execute_python_tool(None)


async def _run(tool_item, code: str, timeout_seconds: int = 30) -> str:
    return await tool_item.ainvoke({"code": code, "timeout_seconds": timeout_seconds})


# ── 基本执行 ──────────────────────────────────────────────────────────


async def test_basic_stdout(execute_python):
    result = await _run(execute_python, "print(1 + 1)")
    assert "【执行成功】" in result
    assert "2" in result


async def test_numpy_computation(execute_python):
    code = "import numpy as np\nprint(float(np.mean([1, 2, 3, 4])))"
    result = await _run(execute_python, code)
    assert "【执行成功】" in result
    assert "2.5" in result


async def test_runtime_error_reported(execute_python):
    result = await _run(execute_python, "x = 1 / 0")
    assert "【执行失败】" in result
    assert "ZeroDivisionError" in result


async def test_syntax_error_reported(execute_python):
    result = await _run(execute_python, "def broken(:\n    pass")
    assert "【执行失败】" in result
    assert "SyntaxError" in result


async def test_empty_code_rejected(execute_python):
    result = await _run(execute_python, "   ")
    assert "【执行失败】" in result


async def test_oversized_code_rejected(execute_python, monkeypatch):
    monkeypatch.setenv("PY_SANDBOX_MAX_CODE_CHARS", "1000")
    result = await _run(execute_python, "print(1)\n" + "# pad\n" * 400)
    assert "代码超长" in result


async def test_no_output_hint(execute_python):
    result = await _run(execute_python, "x = 41 + 1")
    assert "【执行成功】" in result
    assert "print()" in result


# ── 资源限制 ──────────────────────────────────────────────────────────


async def test_cpu_busy_loop_terminated(execute_python):
    result = await _run(execute_python, "while True:\n    pass", timeout_seconds=5)
    assert "【执行失败】" in result
    assert ("CPU" in result) or ("超时" in result) or ("终止" in result)


async def test_stdout_truncated(execute_python):
    result = await _run(execute_python, 'print("x" * 50000)')
    assert "【执行成功】" in result
    assert "截断" in result


# ── 安全边界 ──────────────────────────────────────────────────────────


async def test_network_blocked(execute_python):
    code = "import socket\n" "socket.create_connection(('127.0.0.1', 9), timeout=1)\n"
    result = await _run(execute_python, code)
    assert "【执行失败】" in result
    assert "沙箱已拦截" in result


async def test_subprocess_blocked(execute_python):
    code = "import subprocess\nsubprocess.run(['ls'], check=False)"
    result = await _run(execute_python, code)
    assert "【执行失败】" in result
    assert "沙箱已拦截" in result


async def test_env_secrets_stripped(execute_python, monkeypatch):
    monkeypatch.setenv("SANDBOX_SECRET_PROBE", "s3cr3t-value-777")
    code = (
        "import os\n"
        "print(repr(os.environ.get('SANDBOX_SECRET_PROBE')))\n"
        "print('MYSQL' in ' '.join(os.environ))\n"
    )
    result = await _run(execute_python, code)
    assert "【执行成功】" in result
    assert "s3cr3t-value-777" not in result
    assert "None" in result


async def test_read_outside_sandbox_blocked(execute_python, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-README", encoding="utf-8")
    result = await _run(
        execute_python,
        f"print(open({str(secret)!r}, encoding='utf-8').read())",
    )
    assert "【执行失败】" in result
    assert "TOPSECRET-README" not in result
    assert "沙箱已拦截" in result


async def test_write_outside_sandbox_blocked(execute_python, tmp_path):
    target = tmp_path / "evil.txt"
    code = f"open({str(target)!r}, 'w', encoding='utf-8').write('pwn')"
    result = await _run(execute_python, code)
    assert "【执行失败】" in result
    assert not target.exists()


async def test_cffi_native_escape_blocked_before_execution(execute_python, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOPSECRET-NATIVE", encoding="utf-8")
    code = (
        "import cffi\n"
        "ffi = cffi.FFI()\n"
        "ffi.cdef('int open(const char *, int, ...); long read(int, void *, unsigned long); int close(int);')\n"
        "libc = ffi.dlopen(None)\n"
        f"fd = libc.open({str(secret)!r}.encode(), 0)\n"
        "buf = ffi.new('char[64]')\n"
        "n = libc.read(fd, buf, 64)\n"
        "libc.close(fd)\n"
        "print(bytes(ffi.buffer(buf, n)).decode())\n"
    )
    result = await _run(execute_python, code)
    assert "【执行失败】" in result
    assert "TOPSECRET-NATIVE" not in result
    assert "沙箱已拦截" in result


async def test_ctypes_import_blocked(execute_python):
    result = await _run(execute_python, "import ctypes\nprint(ctypes)")
    assert "【执行失败】" in result
    assert "沙箱已拦截" in result


async def test_write_inside_sandbox_allowed(execute_python):
    code = (
        "with open('scratch.txt', 'w', encoding='utf-8') as fh:\n"
        "    fh.write('local ok')\n"
        "print(open('scratch.txt', encoding='utf-8').read())\n"
    )
    result = await _run(execute_python, code)
    assert "【执行成功】" in result
    assert "local ok" in result


# ── 图表捕获 ──────────────────────────────────────────────────────────


async def test_matplotlib_chart_captured(execute_python, monkeypatch):
    uploaded: list[bytes] = []

    async def _fake_store_chart(
        png_bytes, *, user_id=None, chart_id=None, option_json=None
    ):
        uploaded.append(png_bytes)
        return f"/api/v1/charts/test/{chart_id}?exp=1&sig=t"

    monkeypatch.setattr(code_interpreter_tool, "store_chart", _fake_store_chart)
    code = (
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3], [2, 4, 9])\n"
        "plt.title('demo')\n"
        "print('plotted')\n"
    )
    result = await _run(execute_python, code, timeout_seconds=55)
    assert "【执行成功】" in result
    assert "plotted" in result
    assert "![执行图表1](/api/v1/charts/test/" in result
    assert len(uploaded) == 1
    assert uploaded[0][:8] == b"\x89PNG\r\n\x1a\n"


# ── 会话文件注入 ──────────────────────────────────────────────────────


class _SandboxRuntimeContext:
    """最小 runtime_context 桩：仅提供文件注入所需的身份字段。"""

    request_id = "req-sbx"
    user_id = "user-123"
    agent_code = "ne"
    domain = "*"
    thread_id = "u_user-123__t_th-001"
    role = "user"


def _local_file_row(path, display_name: str, *, file_id: str = "uf_test_1") -> dict:
    return {
        "file_id": file_id,
        "thread_id": "th-001",
        "display_name": display_name,
        "mime_type": "text/csv",
        "size_bytes": path.stat().st_size,
        "source_type": "local",
        "storage_bucket": None,
        "storage_object": None,
        "local_abs_path": str(path),
    }


def _patch_thread_files(monkeypatch, rows: list[dict]) -> None:
    async def _fake_load(user_id: str, thread_id: str) -> list[dict]:
        assert user_id == "user-123"
        assert thread_id == "th-001"
        return rows

    monkeypatch.setattr(code_interpreter_tool, "_load_thread_files", _fake_load)


async def test_input_file_injected_and_readable(tmp_path, monkeypatch):
    source = tmp_path / "src.csv"
    source.write_text("score,name\n90,alice\n", encoding="utf-8")
    _patch_thread_files(monkeypatch, [_local_file_row(source, "成绩表.csv")])

    tool_item = get_execute_python_tool(_SandboxRuntimeContext())
    result = await tool_item.ainvoke(
        {
            "code": "print(open('inputs/成绩表.csv', encoding='utf-8').read().strip())",
            "input_files": ["成绩表.csv"],
        }
    )
    assert "【执行成功】" in result
    assert "已注入会话文件: inputs/成绩表.csv" in result
    assert "alice" in result


async def test_input_file_unknown_name_fails_fast(tmp_path, monkeypatch):
    source = tmp_path / "src.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    _patch_thread_files(monkeypatch, [_local_file_row(source, "data.csv")])

    tool_item = get_execute_python_tool(_SandboxRuntimeContext())
    result = await tool_item.ainvoke(
        {
            "code": "print('SHOULD_NOT_RUN')",
            "input_files": ["nope.csv"],
        }
    )
    assert "【执行失败】" in result
    assert "不存在" in result
    assert "data.csv" in result
    assert "SHOULD_NOT_RUN" not in result


async def test_input_file_display_name_sanitized(tmp_path, monkeypatch):
    source = tmp_path / "src.csv"
    source.write_text("payload-ok\n", encoding="utf-8")
    _patch_thread_files(monkeypatch, [_local_file_row(source, "../../evil.csv")])

    tool_item = get_execute_python_tool(_SandboxRuntimeContext())
    result = await tool_item.ainvoke(
        {
            "code": "print(open('inputs/evil.csv', encoding='utf-8').read().strip())",
            "input_files": ["../../evil.csv"],
        }
    )
    assert "【执行成功】" in result
    assert "已注入会话文件: inputs/evil.csv" in result
    assert "payload-ok" in result


async def test_input_file_too_large_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("PY_SANDBOX_MAX_INPUT_FILE_MB", "1")
    source = tmp_path / "src.csv"
    source.write_text("a,b\n", encoding="utf-8")
    row = _local_file_row(source, "big.csv")
    row["size_bytes"] = 2 * 1024 * 1024
    _patch_thread_files(monkeypatch, [row])

    tool_item = get_execute_python_tool(_SandboxRuntimeContext())
    result = await tool_item.ainvoke(
        {"code": "print('SHOULD_NOT_RUN')", "input_files": ["big.csv"]}
    )
    assert "【执行失败】" in result
    assert "过大" in result
    assert "SHOULD_NOT_RUN" not in result


async def test_input_files_require_authenticated_thread_scope(execute_python):
    result = await execute_python.ainvoke(
        {"code": "print('SHOULD_NOT_RUN')", "input_files": ["a.csv"]}
    )
    assert "【执行失败】" in result
    assert "登录用户" in result
    assert "SHOULD_NOT_RUN" not in result


async def test_input_files_reject_foreign_thread_key():
    class _ForeignThreadContext(_SandboxRuntimeContext):
        thread_id = "u_someone-else__t_th-001"

    tool_item = get_execute_python_tool(_ForeignThreadContext())
    result = await tool_item.ainvoke(
        {"code": "print('SHOULD_NOT_RUN')", "input_files": ["a.csv"]}
    )
    assert "【执行失败】" in result
    assert "不匹配" in result
    assert "SHOULD_NOT_RUN" not in result


async def test_xlsx_input_readable_with_pandas(tmp_path, monkeypatch):
    openpyxl = pytest.importorskip("openpyxl")
    source = tmp_path / "src.xlsx"
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["学号", "总分"])
    sheet.append(["S001", 87.5])
    workbook.save(source)
    row = _local_file_row(source, "成绩.xlsx")
    row["mime_type"] = (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    _patch_thread_files(monkeypatch, [row])

    tool_item = get_execute_python_tool(_SandboxRuntimeContext())
    result = await tool_item.ainvoke(
        {
            "code": (
                "import pandas as pd\n"
                "df = pd.read_excel('inputs/成绩.xlsx')\n"
                "print(df.loc[0, '学号'], float(df['总分'].mean()))\n"
            ),
            "timeout_seconds": 55,
            "input_files": ["成绩.xlsx"],
        }
    )
    assert "【执行成功】" in result
    assert "S001" in result
    assert "87.5" in result


async def test_owned_file_id_injectable(tmp_path, monkeypatch):
    source = tmp_path / "personal.csv"
    source.write_text("k,v\npersonal,1\n", encoding="utf-8")
    _patch_thread_files(monkeypatch, [])
    requested: list[tuple[str, str]] = []

    async def _fake_owned(user_id: str, file_id: str):
        requested.append((user_id, file_id))
        return _local_file_row(source, "历史成绩.csv", file_id=file_id)

    monkeypatch.setattr(code_interpreter_tool, "_load_owned_file", _fake_owned)

    tool_item = get_execute_python_tool(_SandboxRuntimeContext())
    result = await tool_item.ainvoke(
        {
            "code": "print(open('inputs/历史成绩.csv', encoding='utf-8').read().strip())",
            "input_files": ["uf_personal_1"],
        }
    )
    assert "【执行成功】" in result
    assert "personal,1" in result
    assert requested == [("user-123", "uf_personal_1")]


async def test_success_stderr_warnings_surfaced(execute_python):
    code = (
        "import warnings\n"
        "warnings.warn('dtype-mixed-demo')\n"
        "print('computed')\n"
    )
    result = await execute_python.ainvoke({"code": code})
    assert "【执行成功】" in result
    assert "computed" in result
    assert "警告" in result
    assert "dtype-mixed-demo" in result


# ── 产出文件采集 ──────────────────────────────────────────────────────


def _patch_artifact_sinks(monkeypatch, artifacts_dir) -> list[str]:
    registered: list[str] = []

    def _fake_artifacts_dir(internal_thread_id: str):
        assert internal_thread_id == "u_user-123__t_th-001"
        return artifacts_dir

    async def _fake_register(internal_thread_id: str, filename: str):
        assert internal_thread_id == "u_user-123__t_th-001"
        registered.append(filename)
        return f"uf_out_{len(registered)}", f"/api/v2/files/uf_out_{len(registered)}/download"

    monkeypatch.setattr(
        code_interpreter_tool, "_thread_artifacts_dir", _fake_artifacts_dir
    )
    monkeypatch.setattr(
        code_interpreter_tool, "_register_thread_artifact", _fake_register
    )
    return registered


async def test_output_file_persisted_as_artifact(tmp_path, monkeypatch):
    artifacts_dir = tmp_path / "artifacts"
    registered = _patch_artifact_sinks(monkeypatch, artifacts_dir)

    tool_item = get_execute_python_tool(_SandboxRuntimeContext())
    result = await tool_item.ainvoke(
        {
            "code": (
                "with open('outputs/结果.csv', 'w', encoding='utf-8') as fh:\n"
                "    fh.write('a,b\\n1,2\\n')\n"
                "print('done')\n"
            ),
        }
    )
    assert "【执行成功】" in result
    assert "--- 产出文件 ---" in result
    assert "[结果.csv](/api/v2/files/uf_out_1/download)" in result
    assert registered == ["结果.csv"]
    assert (artifacts_dir / "结果.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"


async def test_output_without_thread_context_not_persisted(execute_python):
    result = await execute_python.ainvoke(
        {
            "code": (
                "with open('outputs/x.csv', 'w', encoding='utf-8') as fh:\n"
                "    fh.write('a\\n')\n"
                "print('done')\n"
            ),
        }
    )
    assert "【执行成功】" in result
    assert "缺少会话上下文" in result
    assert "--- 产出文件 ---" not in result


async def test_oversized_output_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("PY_SANDBOX_MAX_OUTPUT_FILE_MB", "1")
    artifacts_dir = tmp_path / "artifacts"
    registered = _patch_artifact_sinks(monkeypatch, artifacts_dir)

    tool_item = get_execute_python_tool(_SandboxRuntimeContext())
    result = await tool_item.ainvoke(
        {
            "code": (
                "with open('outputs/big.bin', 'wb') as fh:\n"
                "    fh.write(b'x' * (2 * 1024 * 1024))\n"
                "print('done')\n"
            ),
        }
    )
    assert "【执行成功】" in result
    assert "过大" in result
    assert "已跳过" in result
    assert registered == []
    assert "--- 产出文件 ---" not in result


# ── 注册表 / 策略接线 ─────────────────────────────────────────────────


def test_execute_python_spec_contract():
    registry = ToolRegistry()
    spec = registry.get("execute_python")
    assert spec.risk_level == "write"
    assert spec.side_effect == "compute"
    assert spec.max_attempts == 1


def test_execute_python_in_full_policies_not_lightweight():
    registry = ToolRegistry()
    for policy_name, domain in (
        ("ne_default", "nuclear-electronics"),
        ("csh_default", "csh-hydrogeology"),
        ("fmca_default", "fundamental-mechanics-course"),
        ("course_eval_default", "course-outcome-evaluation"),
    ):
        full_names = {
            tool.name
            for tool in registry.resolve_policy_tools(
                policy_name=policy_name,
                domain=domain,
                workflow="full",
                selected_skills=set(),
            )
        }
        assert "execute_python" in full_names, policy_name
        lightweight_names = {
            tool.name
            for tool in registry.resolve_policy_tools(
                policy_name=policy_name,
                domain=domain,
                workflow="lightweight",
                selected_skills=set(),
            )
        }
        assert "execute_python" not in lightweight_names, policy_name


def test_execute_python_provider_materializes():
    ToolProviderLoader.validate_provider_contract(get_execute_python_tool)
    assembler = PolicyToolAssembler()
    tools = assembler.materialize_tool_name("execute_python")
    assert len(tools) == 1
    assert tools[0].name == "execute_python"
    metadata = tools[0].metadata or {}
    assert metadata.get("risk_level") == "write"
    assert metadata.get("side_effect") == "compute"


# ── 进程级隔离(prctl / NOFILE) ────────────────────────────────────────


async def test_runner_applies_process_isolation(monkeypatch):
    import resource as real_resource

    from backend.application.tools.providers import code_interpreter_runner

    calls: list[tuple[int, tuple[int, int]]] = []
    monkeypatch.setattr(
        real_resource,
        "setrlimit",
        lambda res, limits: calls.append((res, limits)),
    )

    prctl_calls: list[tuple] = []

    class _FakePrctl:
        def __init__(self):
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            prctl_calls.append(args)

    class _FakeLib:
        def __init__(self):
            self.prctl = _FakePrctl()

    class _FakeCtypesModule:
        c_int = "c_int"
        c_ulong = "c_ulong"

        def CDLL(self, *_args, **_kwargs):
            return _FakeLib()

    monkeypatch.setattr(code_interpreter_runner, "ctypes", _FakeCtypesModule)

    code_interpreter_runner._apply_process_isolation()

    assert (real_resource.RLIMIT_NOFILE, (256, 256)) in calls
    assert prctl_calls == [(38, 1, 0, 0, 0)]


async def test_runner_process_isolation_nonfatal_on_prctl_failure(monkeypatch):
    import resource as real_resource

    from backend.application.tools.providers import code_interpreter_runner

    monkeypatch.setattr(real_resource, "setrlimit", lambda *_a, **_k: None)

    class _BoomCtypes:
        def CDLL(self, *_args, **_kwargs):
            raise OSError("no prctl")

    monkeypatch.setattr(code_interpreter_runner, "ctypes", _BoomCtypes)

    # 不抛异常:平台缺 prctl 时仅回退到 rlimit/审计钩子兜底。
    code_interpreter_runner._apply_process_isolation()
