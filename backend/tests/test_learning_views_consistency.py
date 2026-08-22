"""自定义学习 / 学习风格视图的三端一致性契约。

这组页面（custom-learning index/path、learning-style）在 `frontend/`、
`frontend_audit/`、`frontend_fmca/` 是手工同步的副本，只有两处合法差异：

1. `brand-name="..."`（fmca 品牌名不同）；
2. custom-learning index.vue 的课程路径网格块（audit/fmca 不用容器查询语法）。

其余任何漂移都意味着有人只改了其中一端。修改时以 `frontend/` 为源，
复制到两个副本后恢复上述两处差异（参照 process 模块的同步纪律）。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FRONTEND = "frontend"
MIRROR_FRONTENDS = ("frontend_audit", "frontend_fmca")

# 视图副本允许品牌名与路径网格差异；API 客户端与模板数据必须逐字节一致。
VIEW_FILES = (
    "src/views/custom-learning/index.vue",
    "src/views/custom-learning/path.vue",
    "src/views/learning-style/index.vue",
)
API_FILES = (
    "src/api/custom-learning.ts",
    "src/api/learning-style.ts",
    "src/views/custom-learning/templates.ts",
)

_BRAND_RE = re.compile(r'brand-name="[^"]+"')

_PATH_GRID_MAIN = (
    '              <div class="@container pb-2">\n'
    '                <div class="relative grid grid-cols-2 gap-4 @cozy:grid-cols-3 @wide:grid-cols-6">\n'
    '                  <div class="absolute left-10 right-10 top-10 hidden h-1 rounded-full bg-surface-sunken @wide:block" />'
)
_PATH_GRID_MIRROR = (
    '              <div class="overflow-x-auto pb-2">\n'
    '                <div class="relative grid min-w-[760px] grid-cols-6 gap-4">\n'
    '                  <div class="absolute left-10 right-10 top-10 h-1 rounded-full bg-surface-sunken" />'
)


def _normalized_view(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    text = _BRAND_RE.sub('brand-name="<BRAND>"', text)
    text = text.replace(_PATH_GRID_MAIN, "<PATH_GRID>")
    text = text.replace(_PATH_GRID_MIRROR, "<PATH_GRID>")
    return text


def _first_divergence(left: str, right: str) -> str:
    left_lines = left.splitlines()
    right_lines = right.splitlines()
    for index, (a, b) in enumerate(zip(left_lines, right_lines, strict=False), start=1):
        if a != b:
            return f"第 {index} 行不一致: {a!r} != {b!r}"
    return f"行数不一致: {len(left_lines)} != {len(right_lines)}"


def test_learning_view_files_exist_in_all_frontends() -> None:
    for frontend in (SOURCE_FRONTEND, *MIRROR_FRONTENDS):
        for rel in (*VIEW_FILES, *API_FILES):
            path = REPO_ROOT / frontend / rel
            assert path.is_file(), f"缺失文件: {frontend}/{rel}"


def test_learning_views_identical_after_known_normalization() -> None:
    for rel in VIEW_FILES:
        source = _normalized_view(REPO_ROOT / SOURCE_FRONTEND / rel)
        for frontend in MIRROR_FRONTENDS:
            mirror = _normalized_view(REPO_ROOT / frontend / rel)
            assert mirror == source, (
                f"{frontend}/{rel} 与 frontend/{rel} 存在已知差异之外的漂移；"
                f"请以 frontend/ 为源同步。{_first_divergence(source, mirror)}"
            )


def test_learning_api_clients_byte_identical() -> None:
    for rel in API_FILES:
        source = (REPO_ROOT / SOURCE_FRONTEND / rel).read_bytes()
        for frontend in MIRROR_FRONTENDS:
            mirror = (REPO_ROOT / frontend / rel).read_bytes()
            assert mirror == source, (
                f"{frontend}/{rel} 与 frontend/{rel} 不一致；API 客户端必须逐字节相同。"
            )
