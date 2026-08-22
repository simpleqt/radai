"""四端共享过程模块的一致性契约。

`frontend/src/features/process` 是唯一源，其余三端是同步副本。
漂移意味着有人绕过 `scripts/sync_frontend_process_module.sh` 直接改了副本，
清洗/归组行为会重新分叉——这正是本次重构要消灭的问题。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPO_ROOT / "frontend" / "src" / "features" / "process"
MIRRORS = [
    REPO_ROOT / name / "src" / "features" / "process"
    for name in ("frontend_audit", "frontend_csh_ai", "frontend_fmca")
]


def _tree_digest(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digests[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return digests


def test_source_module_exists_and_is_nonempty() -> None:
    assert SOURCE.is_dir(), "共享源目录缺失: frontend/src/features/process"
    source_digest = _tree_digest(SOURCE)
    assert source_digest, "共享源目录为空"
    for required in ("types.ts", "humanize.ts", "normalizeEvent.ts", "ProcessPanel.vue"):
        assert required in source_digest, f"共享源缺少 {required}"


def test_all_frontends_share_identical_process_module() -> None:
    source_digest = _tree_digest(SOURCE)
    for mirror in MIRRORS:
        assert mirror.is_dir(), f"副本缺失: {mirror.relative_to(REPO_ROOT)}"
        mirror_digest = _tree_digest(mirror)
        assert mirror_digest == source_digest, (
            f"{mirror.relative_to(REPO_ROOT)} 与源不一致；"
            "请在 frontend/ 下修改后运行 scripts/sync_frontend_process_module.sh"
        )
