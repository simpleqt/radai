from __future__ import annotations

import asyncio
import io
import zipfile

import pytest

from backend.application.agents.prompt_builder import PromptBuildContext, PromptBuilder
from backend.application.mcp.query_service import build_mcp_agent_request, validate_mcp_arguments
from backend.application.skills.materializer import SkillMaterializer
from backend.application.tools.execution import (
    invoke_sync_with_timeout,
    tool_timeout_seconds,
)
from backend.application.tools.provider_loader import ToolProviderLoader
from backend.application.tools.providers.excel_engine import (
    _new_safe_connection,
    validate_read_only_sql,
)
from backend.domain.tools.runtime_context import ToolRuntimeContext
from backend.core.agents.context.project_instructions import load_project_instructions


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv_auto('/etc/hosts')",
        "SELECT * FROM glob('/etc/*')",
        "SELECT * FROM read_json_auto('/tmp/secrets.json')",
        "SELECT * FROM data; COPY data TO '/tmp/out.csv'",
        "PRAGMA enable_external_access",
        "SELECT * FROM read_blob('/tmp/secret.bin')",
        "SELECT * FROM write_text('/tmp/out.txt', 'x')",
        "SELECT * FROM write_blob('/tmp/out.bin', 'x')",
        "SELECT * FROM copy_database('main', 'evil')",
        "SELECT * FROM export_database('/tmp/evil', FORMAT PARQUET)",
    ],
)
def test_excel_sql_rejects_external_reads_and_mutations(sql: str) -> None:
    with pytest.raises(ValueError):
        validate_read_only_sql(sql)


def test_excel_sql_allows_literals_and_single_read_query() -> None:
    assert validate_read_only_sql("SELECT 'read_csv' AS label") == "SELECT 'read_csv' AS label"
    assert validate_read_only_sql("```sql\nSELECT 1\n```") == "SELECT 1"


def test_duckdb_connection_disables_external_access() -> None:
    connection = _new_safe_connection()
    try:
        with pytest.raises(Exception):
            connection.execute("SELECT * FROM read_csv_auto('/etc/hosts')").fetchall()
    finally:
        connection.close()


def test_timeout_metadata_accepts_mapping_and_rejects_non_finite(monkeypatch) -> None:
    assert tool_timeout_seconds("demo", {"timeout_seconds": 2}) == 2
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "nan")
    assert tool_timeout_seconds("demo") == 180


def test_sync_tool_timeout_is_bounded() -> None:
    with pytest.raises(TimeoutError):
        invoke_sync_with_timeout(lambda: asyncio.run(asyncio.sleep(1)), timeout=0.01, label="slow")


def test_provider_loader_passes_positional_and_scoped_context() -> None:
    context = ToolRuntimeContext(
        request_id="r",
        user_id="u",
        agent_code="a",
        domain="d",
        thread_id="t",
    )

    def provider(runtime_context, /, user_id):
        return runtime_context.request_id, user_id

    assert ToolProviderLoader().invoke(provider, runtime_context=context) == ("r", "u")


def test_provider_loader_preserves_optional_positional_only_parameters() -> None:
    context = ToolRuntimeContext(
        request_id="r",
        user_id="u",
        agent_code="a",
        domain="d",
        thread_id="t",
    )

    def provider(unused="fallback", runtime_context=None, /):
        return unused, runtime_context.request_id

    assert ToolProviderLoader().invoke(provider, runtime_context=context) == ("fallback", "r")


def _skill_zip(name: str, body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "SKILL.md",
            f"---\nname: {name}\nversion: 1.0.0\ndescription: test\ndomain: general\n---\n{body}",
        )
    return buffer.getvalue()


def test_skill_materializer_rolls_back_and_persists_owner(tmp_path) -> None:
    materializer = SkillMaterializer(root=tmp_path)
    materializer.publish(
        skill_key="hardening-skill",
        content=_skill_zip("hardening-skill", "first"),
        root_prefix="",
        owner_id="owner-1",
    )
    materializer.finalize("hardening-skill")
    materializer.publish(
        skill_key="hardening-skill",
        content=_skill_zip("hardening-skill", "second"),
        root_prefix="",
        owner_id="owner-2",
    )
    target = tmp_path / "custom" / "hardening-skill" / "SKILL.md"
    assert "owner-2" in target.read_text(encoding="utf-8")
    materializer.rollback("hardening-skill")
    restored = target.read_text(encoding="utf-8")
    assert "owner-1" in restored
    assert "first" in restored


def test_skill_materializer_accepts_chinese_skill_key(tmp_path) -> None:
    materializer = SkillMaterializer(root=tmp_path)
    skill_key = "数据分析"

    target = materializer.publish(
        skill_key=skill_key,
        content=_skill_zip(skill_key, "中文 Skill"),
        root_prefix="",
    )

    assert target == tmp_path / "custom" / skill_key
    assert (target / "SKILL.md").is_file()


def test_mcp_router_exposes_single_tool_call_endpoint() -> None:
    from backend.api.routers.mcp import router

    assert any(route.path == "/tools/call" for route in router.routes)


def test_project_instructions_load_parent_to_child_and_stay_in_workspace(tmp_path) -> None:
    (tmp_path / ".git").mkdir()
    child = tmp_path / "packages" / "agent"
    child.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("root guidance", encoding="utf-8")
    (tmp_path / "packages" / "AGENTS.md").write_text("package guidance", encoding="utf-8")
    (child / "AGENTS.md").write_text("agent guidance", encoding="utf-8")

    resolved = load_project_instructions(child, allowed_roots=[tmp_path])

    assert resolved.files == (
        str(tmp_path / "AGENTS.md"),
        str(tmp_path / "packages" / "AGENTS.md"),
        str(child / "AGENTS.md"),
    )
    assert resolved.content.index("root guidance") < resolved.content.index("package guidance")
    assert resolved.content.index("package guidance") < resolved.content.index("agent guidance")
    assert not load_project_instructions(tmp_path.parent, allowed_roots=[tmp_path]).content


def test_prompt_builder_injects_validated_project_instructions(tmp_path, monkeypatch) -> None:
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("use the repository formatter", encoding="utf-8")
    monkeypatch.setenv("AGENT_ALLOWED_WORKSPACE_ROOTS", str(tmp_path))
    context = PromptBuildContext(
        domain="general",
        selected_skills=None,
        subagent_enabled=False,
        use_search=False,
        request_metadata={"cwd": str(tmp_path)},
    )

    runtime_context = PromptBuilder._build_runtime_context(context)

    assert "<project_instructions>" in runtime_context
    assert "use the repository formatter" in runtime_context
    assert "cannot override system safety rules" in runtime_context


def test_mcp_thread_alias_is_preserved_and_conflicts_are_rejected() -> None:
    from backend.api.routers.mcp import McpToolCallRequest

    request = McpToolCallRequest(tool_name="web_search", thread_id="thread-123")
    assert request.thread_id == "thread-123"
    agent_request = build_mcp_agent_request(
        question="hello",
        user_id="user-1",
        source="test",
        thread_id="thread-123",
    )
    assert agent_request.conversation_id == "thread-123"
    with pytest.raises(ValueError):
        McpToolCallRequest(
            tool_name="web_search",
            thread_id="thread-a",
            conversation_id="thread-b",
        )


def test_mcp_argument_envelope_is_bounded() -> None:
    with pytest.raises(ValueError, match="128 KiB"):
        validate_mcp_arguments({"payload": "x" * (128 * 1024)})


def test_prompt_builder_injects_uploaded_file_manifest() -> None:
    """`_uploaded_file_manifest` 注入 file_id→文件名清单，供模型识别材料类型。"""
    context = PromptBuildContext(
        domain="course-outcome-evaluation",
        selected_skills=None,
        subagent_enabled=False,
        use_search=False,
        request_metadata={
            "file_ids": ["uf-a", "uf-b"],
            "_uploaded_file_manifest": {
                "uf-a": {
                    "display_name": "材料力学课程成绩单.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": 122880,
                },
                "uf-b": {
                    "display_name": "材料力学学生答题试卷.pdf",
                    "mime_type": "application/pdf",
                    "size_bytes": None,
                },
            },
        },
    )

    runtime_context = PromptBuilder._build_runtime_context(context)

    assert "<uploaded_file_manifest>" in runtime_context
    assert "uf-a" in runtime_context
    assert "材料力学课程成绩单.pdf" in runtime_context
    assert "uf-b" in runtime_context
    assert "attach_material" in runtime_context
    # 原 table 注入仍保留
    assert "<uploaded_table_files>" in runtime_context


def test_prompt_builder_skips_manifest_when_absent() -> None:
    """无 manifest 时不得出现 `<uploaded_file_manifest>`。"""
    context = PromptBuildContext(
        domain="course-outcome-evaluation",
        selected_skills=None,
        subagent_enabled=False,
        use_search=False,
        request_metadata={"file_ids": ["uf-a"]},
    )

    runtime_context = PromptBuilder._build_runtime_context(context)

    assert "<uploaded_file_manifest>" not in runtime_context
    assert "<uploaded_table_files>" in runtime_context
    assert "uf-a" in runtime_context
