"""前端-后端契约测试（对话式，静态）。

课程评价已改造为地下水式对话（/eval/chat）：前端走 /api/v2/agent-runtime/query
（agent 工具直接调 service 层），旧工作台 REST 页面/API 已整体移除。
本文件钉死「对话是唯一主入口 + 旧工作台代码不复活」：
  A. Chat.vue 用 agentCode=course_eval + basePath=/eval/chat；
  B. 旧工作台路由 / 页面 / course-eval.ts / useCourseEvalAgent.ts 必须不存在；
  C. course_eval profile 装配全部对话写工具。
"""

from __future__ import annotations

from pathlib import Path

from backend.application.agents.definition import AgentDefinitionValidator

REPO_ROOT = Path(__file__).resolve().parents[2]


def _read(repo_rel: str) -> str:
    path = REPO_ROOT / repo_rel
    return path.read_text(encoding="utf-8")


def test_course_eval_agent_contract() -> None:
    """对话式契约：薄壳页用 agentCode=course_eval、basePath=/eval/chat。"""
    chat_shell = _read("frontend_fmca/src/views/course-eval/Chat.vue")
    assert 'agent-code="course_eval"' in chat_shell
    assert 'base-path="/eval/chat"' in chat_shell

    router = _read("frontend_fmca/src/router/index.ts")
    assert "/eval/chat/:threadId?" in router

    ne_full = _read("frontend_fmca/src/views/ne-full/index.vue")
    assert "agentCode?: string" in ne_full
    assert "basePath?: string" in ne_full


def test_old_workbench_removed() -> None:
    """旧按钮式工作台代码必须整体移除（不复活）。"""
    removed_files = [
        "frontend_fmca/src/api/course-eval.ts",
        "frontend_fmca/src/views/course-eval/EvalLayout.vue",
        "frontend_fmca/src/views/course-eval/TaskList.vue",
        "frontend_fmca/src/views/course-eval/TaskDetail.vue",
        "frontend_fmca/src/views/course-eval/ScoreReview.vue",
        "frontend_fmca/src/views/course-eval/GoalMapping.vue",
        "frontend_fmca/src/views/course-eval/Statistics.vue",
        "frontend_fmca/src/views/course-eval/ReportEditor.vue",
        "frontend_fmca/src/views/course-eval/Archive.vue",
        "frontend_fmca/src/views/course-eval/components/StatusBadge.vue",
        "frontend_fmca/src/composables/useCourseEvalAgent.ts",
    ]
    for rel in removed_files:
        assert not (REPO_ROOT / rel).exists(), f"旧工作台文件不应存在: {rel}"

    router = _read("frontend_fmca/src/router/index.ts")
    assert "/eval/tasks" not in router  # 旧工作台子路由
    assert "EvalLayout" not in router

    navigation = _read("frontend_fmca/src/app/navigation.ts")
    assert "to: '/eval/chat'" in navigation  # 侧边栏指向对话


def test_course_eval_profile_assembles_dialog_tools() -> None:
    """course_eval profile 装配全部对话写工具（建任务/绑材料/改分/确认映射）。"""
    resolved = AgentDefinitionValidator().resolve("course_eval")
    tool_names = {t.name for t in resolved.tools}
    for dialog_tool in (
        "create_semester_task",
        "attach_material",
        "get_score_grid",
        "update_scores",
        "apply_mappings",
        "extract_grade_sheet",
        "extract_paper",
        "extract_syllabus",
        "run_validation",
        "compute_statistics",
        "generate_report",
        "transition_task",
    ):
        assert dialog_tool in tool_names, f"{dialog_tool} 必须在 course_eval 装配集内"


def test_no_rest_router_file() -> None:
    """旧 REST router 与 schemas 已删除（对话工具直接走 service 层）。"""
    for rel in (
        "backend/api/routers/course_eval.py",
        "backend/api/schemas/course_eval.py",
        "backend/application/course_eval/archive_service.py",
    ):
        assert not (REPO_ROOT / rel).exists(), f"旧 REST 文件不应存在: {rel}"

    http_server = _read("backend/api/http_server.py")
    assert "course_eval" not in http_server


# ── 前端对话全家端点 ↔ 后端路由表比对 ──

import re  # noqa: E402

from backend.api.routers import (  # noqa: E402
    agent_runtime,
    asr,
    chat_files,
    conversations,
    files,
    guest_agent,
    my_files,
)

# 前端 ne-full 全家实际调用的后端端点（方法, 路径模板），与后端路由表逐条比对。
# 防止对话主链路任何一个端点 404/405（如 rename 曾用 PUT 而后端是 PATCH）。
FRONTEND_BACKEND_ENDPOINTS: list[tuple[str, str]] = [
    # agent 对话
    ("POST", "/api/v2/agent-runtime/query"),
    ("POST", "/api/v2/guest/query"),
    ("GET", "/api/v2/agent-runtime/profiles/{code}/info"),
    ("GET", "/api/v2/agent-runtime/{code}/thread/{tid}/task"),
    ("GET", "/api/v2/agent-runtime/{code}/task/{task_id}/status"),
    ("GET", "/api/v2/agent-runtime/{code}/task/{task_id}/stream"),
    ("POST", "/api/v2/agent-runtime/{code}/task/{task_id}/cancel"),
    # 对话文件
    ("POST", "/api/v2/chat-files/upload"),
    ("GET", "/api/v2/chat-files/{tid}"),
    ("DELETE", "/api/v2/chat-files/{file_id}"),
    # 会话管理
    ("GET", "/api/v2/conversations"),
    ("GET", "/api/v2/conversations/{tid}/messages"),
    ("PATCH", "/api/v2/conversations/{tid}"),
    ("DELETE", "/api/v2/conversations/{tid}"),
    ("POST", "/api/v2/conversations/{tid}/rollback"),
    # 文件
    ("GET", "/api/v2/files"),
    ("GET", "/api/v2/files/{file_id}/download"),
    ("POST", "/api/v2/files/export-docx"),
    ("POST", "/api/v1/sessions/{session_id}/files/attach"),
    ("GET", "/api/v1/my-files"),
    ("POST", "/api/v1/asr/transcribe"),
]


def _backend_route_table() -> set[tuple[str, str]]:
    """收集前端依赖的后端 router 路由表（含挂载前缀）。"""
    mounts = [
        (agent_runtime.router, "/api/v2"),
        (chat_files.router, "/api/v2"),
        (conversations.router, "/api/v2"),
        (conversations.shared_router, "/api/v2"),
        (files.router, "/api/v2"),
        (my_files.router, ""),
        (asr.router, "/api/v1"),
        # 访客对话（guest_agent router 自带 /guest 前缀，经 agent_runtime_v2 挂 /api/v2）
        (guest_agent.router, "/api/v2"),
    ]
    table: set[tuple[str, str]] = set()
    for router, prefix in mounts:
        for route in router.routes:
            for method in getattr(route, "methods", []) or []:
                if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                    table.add((method, f"{prefix}{route.path}"))
    return table


def _norm(path: str) -> str:
    """把 {param} 参数段归一化为 {p} 以匹配后端 {xxx}。"""
    return re.sub(r"\{[^}]+\}", "{p}", path)


def test_frontend_dialog_endpoints_exist_in_backend() -> None:
    """前端对话全家调用的每个端点都必须存在于后端路由表（防 404/405）。"""
    table = {
        (method, _norm(path))
        for method, path in _backend_route_table()
    }
    missing: list[tuple[str, str]] = []
    for method, path in FRONTEND_BACKEND_ENDPOINTS:
        norm = _norm(path)
        if (method, norm) not in table:
            missing.append((method, path))
    assert not missing, f"前端调用但后端缺失/方法不符的端点: {missing}"


def test_guest_query_endpoint_mounted() -> None:
    """访客对话端点已挂载（guest_agent router，自带 /guest 前缀）。"""
    import backend.api.routers.guest_agent as guest_agent

    paths = {
        (m, r.path)
        for r in guest_agent.router.routes
        for m in (getattr(r, "methods", []) or [])
        if m in {"POST", "GET"}
    }
    assert ("POST", "/guest/query") in paths
