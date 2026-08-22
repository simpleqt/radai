"""export_document 通用文档导出工具契约测试。

覆盖：DOCX 渲染、PDF 转换（真实 LibreOffice，缺失则 skip）、PDF 失败降级、
图表 URL 物化、图片源 SSRF 清理、参数校验、存储降级，以及注册表/策略接线。
"""

from __future__ import annotations

import shutil

import pytest

from backend.application.tools.assembler import PolicyToolAssembler
from backend.application.tools.provider_loader import ToolProviderLoader
from backend.application.tools.providers import document_export_tool
from backend.application.tools.providers.document_export_tool import (
    _materialize_chart_images,
    _render_docx_bytes,
    _sanitize_image_sources,
    get_export_document_tool,
)
from backend.application.tools.registry import ToolRegistry


class _Ctx:
    def __init__(self, user_id="u-1", thread_id="t-1"):
        self.user_id = user_id
        self.thread_id = thread_id


@pytest.fixture()
def captured_artifacts(monkeypatch):
    stored: list[dict] = []

    async def _fake_upload(content, object_name, content_type=None, bucket_name=None):
        stored.append(
            {
                "bytes": content,
                "object_name": object_name,
                "content_type": content_type,
            }
        )
        return object_name

    monkeypatch.setattr(document_export_tool, "upload_bytes_to_minio", _fake_upload)
    monkeypatch.setattr(
        document_export_tool, "get_user_minio_bucket_name", lambda: "test-bucket"
    )
    return stored


async def _run(tool_item, **kwargs) -> str:
    return await tool_item.ainvoke(kwargs)


# ── DOCX 渲染 ─────────────────────────────────────────────────────────


def test_render_docx_bytes_is_valid_docx():
    data = _render_docx_bytes(title="标题", content="# 一级\n\n正文。\n\n- A\n- B\n")
    assert data[:2] == b"PK"
    assert len(data) > 1000


async def test_export_docx_stores_artifact(captured_artifacts):
    tool_item = get_export_document_tool(_Ctx())
    result = await _run(
        tool_item, title="季度报告", content="# 季度报告\n\n结论一二三。"
    )
    assert "【已导出 DOCX】" in result
    assert "季度报告.docx" in result
    assert len(captured_artifacts) == 1
    artifact = captured_artifacts[0]
    assert artifact["bytes"][:2] == b"PK"
    assert artifact["content_type"] == document_export_tool.DOCX_MIME_TYPE
    assert "/threads/t-1/exports/" in artifact["object_name"]


# ── PDF 转换 ──────────────────────────────────────────────────────────


@pytest.mark.skipif(
    not (shutil.which("soffice") or shutil.which("libreoffice")),
    reason="LibreOffice 不可用，跳过真实 PDF 转换",
)
async def test_export_pdf_real_libreoffice(captured_artifacts):
    tool_item = get_export_document_tool(_Ctx())
    result = await _run(
        tool_item,
        title="导出测试",
        content="# 导出测试\n\n正文段落。\n\n| 列1 | 列2 |\n|---|---|\n| a | b |\n",
        format="pdf",
    )
    assert "【已导出 PDF】" in result
    assert "导出测试.pdf" in result
    assert len(captured_artifacts) == 1
    assert captured_artifacts[0]["bytes"][:5] == b"%PDF-"
    assert captured_artifacts[0]["content_type"] == document_export_tool.PDF_MIME_TYPE


async def test_export_pdf_falls_back_to_docx(captured_artifacts, monkeypatch):
    monkeypatch.setattr(
        document_export_tool, "_convert_docx_to_pdf_bytes", lambda docx_bytes: None
    )
    tool_item = get_export_document_tool(_Ctx())
    result = await _run(
        tool_item, title="报告", content="# 报告\n\n正文。", format="pdf"
    )
    assert "降级为 DOCX" in result
    assert captured_artifacts[0]["bytes"][:2] == b"PK"
    assert captured_artifacts[0]["content_type"] == document_export_tool.DOCX_MIME_TYPE


# ── 参数校验 ──────────────────────────────────────────────────────────


async def test_empty_content_rejected(captured_artifacts):
    tool_item = get_export_document_tool(_Ctx())
    result = await _run(tool_item, title="x", content="   ")
    assert "【导出失败】" in result
    assert not captured_artifacts


async def test_unknown_format_rejected(captured_artifacts):
    tool_item = get_export_document_tool(_Ctx())
    result = await _run(tool_item, title="x", content="正文", format="rtf")
    assert "【导出失败】" in result
    assert not captured_artifacts


async def test_missing_thread_rejected(captured_artifacts):
    tool_item = get_export_document_tool(_Ctx(thread_id=""))
    result = await _run(tool_item, title="x", content="正文")
    assert "【导出失败】" in result
    assert not captured_artifacts


# ── 存储降级 ──────────────────────────────────────────────────────────


async def test_minio_failure_falls_back_to_local(monkeypatch):
    async def _boom(*args, **kwargs):
        raise RuntimeError("minio down")

    saved: list[dict] = []

    def _fake_local(*, thread_id, filename, content):
        saved.append({"thread_id": thread_id, "filename": filename})
        return f"/local/{thread_id}/{filename}"

    monkeypatch.setattr(document_export_tool, "upload_bytes_to_minio", _boom)
    monkeypatch.setattr(document_export_tool, "get_user_minio_bucket_name", lambda: "b")
    monkeypatch.setattr(document_export_tool, "_save_locally", _fake_local)

    tool_item = get_export_document_tool(_Ctx())
    result = await _run(tool_item, title="本地", content="# 本地\n\n正文。")
    assert "【已导出 DOCX】" in result
    assert saved and saved[0]["filename"] == "本地.docx"


# ── 图表物化 / SSRF 清理 ──────────────────────────────────────────────


def test_materialize_chart_images(monkeypatch, tmp_path):
    monkeypatch.setattr(
        document_export_tool,
        "run_in_fresh_loop",
        lambda factory: b"\x89PNG\r\n\x1a\nfake",
    )

    import backend.services.chart_image_store as chart_store

    monkeypatch.setattr(chart_store, "verify_chart_signature", lambda *a, **k: True)
    md = "![图](/api/v1/charts/u1/c1?exp=99&sig=abc)"
    out = _materialize_chart_images(md, tmp_path)
    assert "/api/v1/charts/" not in out
    assert (tmp_path / "chart-u1-c1.png").exists()
    assert str(tmp_path) in out


def test_sanitize_strips_remote_images(tmp_path):
    local_chart = tmp_path / "chart-x.png"
    local_chart.write_bytes(b"\x89PNG\r\n\x1a\n")
    md = (
        f"![本地图]({local_chart.as_posix()})\n\n"
        "![远程](http://169.254.169.254/latest/meta-data)\n\n"
        "正文 <img src='http://evil/x.png'> 结尾"
    )
    out = _sanitize_image_sources(md, tmp_path)
    assert local_chart.as_posix() in out
    assert "169.254.169.254" not in out
    assert "<img" not in out
    assert "本地图" in out


# ── 注册表 / 策略接线 ─────────────────────────────────────────────────


def test_export_document_spec_contract():
    spec = ToolRegistry().get("export_document")
    assert spec.risk_level == "write"
    assert spec.side_effect == "artifact_write"


def test_export_document_in_full_policies_not_lightweight():
    registry = ToolRegistry()
    for policy_name, domain in (
        ("ne_default", "nuclear-electronics"),
        ("csh_default", "csh-hydrogeology"),
        ("fmca_default", "fundamental-mechanics-course"),
        ("course_eval_default", "course-outcome-evaluation"),
    ):
        full_names = {
            t.name
            for t in registry.resolve_policy_tools(
                policy_name=policy_name,
                domain=domain,
                workflow="full",
                selected_skills=set(),
            )
        }
        assert "export_document" in full_names, policy_name
        light_names = {
            t.name
            for t in registry.resolve_policy_tools(
                policy_name=policy_name,
                domain=domain,
                workflow="lightweight",
                selected_skills=set(),
            )
        }
        assert "export_document" not in light_names, policy_name


def test_export_document_provider_materializes():
    ToolProviderLoader.validate_provider_contract(get_export_document_tool)
    tools = PolicyToolAssembler().materialize_tool_name("export_document")
    assert len(tools) == 1
    assert tools[0].name == "export_document"
    metadata = tools[0].metadata or {}
    assert metadata.get("side_effect") == "artifact_write"
