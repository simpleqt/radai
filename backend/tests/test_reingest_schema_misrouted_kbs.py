# noqa: E402
"""schema 路由受害库重建脚本的纯逻辑契约。"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = (
    REPO_ROOT / "backend/scripts/reingest_schema_misrouted_kbs.py"
)

SPEC = importlib.util.spec_from_file_location(
    "reingest_schema_misrouted_kbs", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
reingest = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reingest)


def test_classify_kb_verdicts():
    # 受害：图非空、typed=0、schema 词表含 typed 关系。
    assert (
        reingest.classify_kb(
            schema_id="nuclear_electronics",
            typed_relation_vocab=True,
            total_edges=535263,
            typed_edges=0,
        )
        == reingest.AFFECTED
    )
    # 修复后/健康库。
    assert (
        reingest.classify_kb(
            schema_id="nuclear_electronics",
            typed_relation_vocab=True,
            total_edges=100,
            typed_edges=60,
        )
        == reingest.OK
    )
    # subject 本就是 generic 的评测重建库：co_occurs-only 属设计。
    assert (
        reingest.classify_kb(
            schema_id="generic",
            typed_relation_vocab=False,
            total_edges=21840,
            typed_edges=0,
        )
        == reingest.GENERIC_BY_DESIGN
    )
    # 空图（新建库 worker 未跑）与无法解析出 typed 词表分别独立判定。
    assert (
        reingest.classify_kb(
            schema_id="nuclear_electronics",
            typed_relation_vocab=True,
            total_edges=0,
            typed_edges=0,
        )
        == reingest.EMPTY_GRAPH
    )
    assert (
        reingest.classify_kb(
            schema_id="something_unmapped",
            typed_relation_vocab=False,
            total_edges=10,
            typed_edges=0,
        )
        == reingest.UNRESOLVED
    )


def test_new_kb_code_and_manifest_roundtrip(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(reingest, "RUNTIME_ROOT", tmp_path)
    code = "rag_nuclear_electronics"
    assert reingest.new_kb_code(code) == code + reingest.NEW_CODE_SUFFIX

    reingest.save_manifest(
        code,
        {"old_code": code, "new_kb_id": "kb-2", "files": [{"source_hash": "abc"}]},
    )
    loaded = reingest.load_manifest(code)
    assert loaded["new_kb_id"] == "kb-2"
    assert loaded["files"][0]["source_hash"] == "abc"
    assert (tmp_path / code / "manifest.json").exists()

    # 无 manifest 时返回骨架，不抛异常（status/switch 各自有显式报错路径）。
    assert reingest.load_manifest("never-prepared")["files"] == []
    payload = json.loads(
        (tmp_path / code / "manifest.json").read_text(encoding="utf-8")
    )
    assert payload["old_code"] == code
