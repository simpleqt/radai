"""对话体验模块的四端一致性契约。

2026-08-13 新增的会话体验能力（回答反馈、composer 偏好持久化、任务完成
提醒、引用溯源）以「一份源 + 三份手工同步副本」的方式存在于四个 `ne-full`
前端。这些文件不含品牌差异，因此必须逐字节一致：

- `src/api/feedback.ts`：反馈接口客户端；
- `src/views/ne-full/components/FeedbackControls.vue`：赞/踩控件；
- `src/views/ne-full/composables/useComposerPrefs.ts`：偏好持久化；
- `src/views/ne-full/composables/useTaskNotifications.ts`：完成提醒；
- `src/views/ne-full/components/citationHydrator.ts`：引用溯源 hydrator；
- `src/views/ne-full/composables/useConversationExport.ts`：整会话导出；
- `src/views/ne-full/components/ConversationExportMenu.vue`：导出入口菜单。

修改时以 `frontend/` 为源，复制到另外三端（参照 process 模块的同步纪律）。
除文件一致性外，还断言每端 `MessageList.vue` / `index.vue` /
`MarkdownRenderer.vue` 已完成接线，避免出现「组件同步了但没人渲染」的静默
失效。`MarkdownRenderer.vue` 本身不要求逐字节一致：`frontend_audit` 没有
Excel 工具，因此也没有图表 hydration 那部分代码。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCE_FRONTEND = "frontend"
MIRROR_FRONTENDS = ("frontend_audit", "frontend_csh_ai", "frontend_fmca")
ALL_FRONTENDS = (SOURCE_FRONTEND, *MIRROR_FRONTENDS)

SHARED_FILES = (
    "src/api/feedback.ts",
    "src/views/ne-full/components/FeedbackControls.vue",
    "src/views/ne-full/composables/useComposerPrefs.ts",
    "src/views/ne-full/composables/useTaskNotifications.ts",
    "src/views/ne-full/components/citationHydrator.ts",
    "src/views/ne-full/composables/useConversationExport.ts",
    "src/views/ne-full/components/ConversationExportMenu.vue",
)

# 每个文件里必须出现的接线标记：(相对路径, 期望片段)
WIRING_MARKERS = (
    ("src/views/ne-full/components/MessageList.vue", "FeedbackControls"),
    ("src/views/ne-full/components/MessageList.vue", "regenerate-answer"),
    ("src/views/ne-full/index.vue", "handleRegenerate"),
    ("src/views/ne-full/index.vue", '@regenerate-answer="handleRegenerate"'),
    ("src/views/ne-full/index.vue", ':thread-id="String(route.params.threadId'),
    ("src/views/ne-full/composables/useAgentChat.ts", "applyStoredComposerPrefs"),
    ("src/views/ne-full/composables/useAgentChat.ts", "notifyTaskFinished"),
    ("src/views/ne-full/composables/useAgentChat.ts", "flashTitleWhileHidden"),
    ("src/components/layout/AppSidebar.vue", "handleTogglePin"),
    ("src/api/conversations.ts", "setPinned"),
    # 引用溯源：hydration + 点击标记 + 定位到参考文档
    ("src/views/ne-full/components/MarkdownRenderer.vue", "hydrateCitations"),
    ("src/views/ne-full/components/MarkdownRenderer.vue", "parseCitationReferences"),
    ("src/views/ne-full/components/MarkdownRenderer.vue", "data-citation"),
    ("src/views/ne-full/components/MarkdownRenderer.vue", "focusCitationReference"),
    ("src/views/ne-full/components/MarkdownRenderer.vue", ".citation-chip"),
    # 整会话导出：顶栏入口必须挂在 actions 槽位里
    ("src/views/ne-full/index.vue", "ConversationExportMenu"),
    ("src/views/ne-full/index.vue", ':messages="messages"'),
)


def test_chat_ux_files_exist_in_all_frontends() -> None:
    for frontend in ALL_FRONTENDS:
        for rel in SHARED_FILES:
            path = REPO_ROOT / frontend / rel
            assert path.is_file(), f"缺失文件: {frontend}/{rel}"


def test_chat_ux_shared_files_byte_identical() -> None:
    for rel in SHARED_FILES:
        source = (REPO_ROOT / SOURCE_FRONTEND / rel).read_bytes()
        for frontend in MIRROR_FRONTENDS:
            mirror = (REPO_ROOT / frontend / rel).read_bytes()
            assert mirror == source, (
                f"{frontend}/{rel} 与 frontend/{rel} 不一致；"
                "这些文件无品牌差异，必须逐字节相同。"
            )


def test_chat_ux_wiring_present_in_every_frontend() -> None:
    for frontend in ALL_FRONTENDS:
        for rel, marker in WIRING_MARKERS:
            path = REPO_ROOT / frontend / rel
            assert path.is_file(), f"缺失文件: {frontend}/{rel}"
            text = path.read_text(encoding="utf-8")
            assert marker in text, f"{frontend}/{rel} 缺少接线标记: {marker}"
