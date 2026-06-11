# 深度审查：`html_ppt_tool.py`

**文件**：`backend/custom/tools/html_ppt_tool.py`（约 950 行）  
**日期**：2026-05-30  
**性质**：只读审查

---

## 1. 职责

- 将 **Markdown 大纲**（`slides_outline`）渲染为 **单文件 HTML 幻灯片**，经 MinIO + artifact 管线暴露给前端预览。
- 入口：`@tool("generate_html_ppt")` → `generate_html_ppt_tool` L851+。
- 核心渲染：`_render_dynamic_html_deck` L659+、`_render_slide_html` L541+、`_render_inline_markdown` L440+（`html.escape`）。
- 产物路径：`html-ppt/{artifact_name}`，`users/{user_id}/threads/{external_thread_id}/html-ppt/{uuid}-{name}`（L797–813）。

**依赖**：`ToolRuntime` 解析 `thread_id`；模板/主题目录扫描 `_list_available_templates` / `_list_available_themes`。

---

## 2. 安全与 XSS

### 做得好的地方

- 正文经 `_render_inline_markdown`：**先 stash 链接/代码/公式，再 `escape(source)`，再恢复占位**（L440–492），降低模型输出中的 HTML 注入风险。
- 表格单元格、标题属性使用 `escape`（见 L786 `data-themes`、表格渲染路径）。
- 图片/链接 Markdown 转为带 caption 的 `<span>`，不嵌入任意 URL 的 `<img src>`（L453–463）— 减少 SSRF/追踪像素，但也 **无法展示真实图片**（产品取舍）。

### 残留风险（P2）

| 点 | 说明 |
|----|------|
| `css_class` 参数 | `_render_text_block(..., css_class=...)` 来自内部 slide 类型枚举，非用户直接输入；若未来允许自定义 class，需白名单。 |
| 主题/模板名 | L826–833 校验存在于目录列表，**拒绝路径穿越**；良好。 |
| 生成 HTML 体积 | 无显式 slide 数/字符上限；恶意超长 `slides_outline` 可导致 CPU/内存尖峰（DoS，P2）。 |
| MinIO 对象 | 任意登录用户仅能写自己的 `users/{user_id}/threads/...`（依赖上游 thread_id 可信）；**guest 线程**需确认 `user_id` 为 anonymous 时的隔离策略。 |

---

## 3. 功能与可维护性

### P2：单文件过大（~950 行）

内含：模板 IO、Markdown 解析、表格、主题 CSS 内联、deck 组装、MinIO 上传、LangGraph `Command` 更新。建议拆：

- `html_ppt/markdown_render.py`
- `html_ppt/deck_builder.py`
- `html_ppt/tool.py`（仅 `@tool` + artifact）

### P2：默认大纲依赖 runtime 消息

- L836–841：`slides_outline` 为空时 `_default_outline_for_request` 读 `_get_latest_user_request(runtime)`。
- 若 runtime 无用户消息或消息被截断，生成 **泛化模板页**（L198+ 起）— 可能不符合用户意图；应在 ToolMessage 中提示「请提供 slides_outline」。

### P3：与文档/前端契约

- 确认 `docs/docs_backend` 是否描述 `generate_html_ppt` 参数与 artifact MIME；若无，补文档或标为内部工具。
- SSE `artifact` 事件字段是否与 `save_deep_research_report` 一致 → 见 `backend_custom_sse_and_api_contract_gaps.md`。

---

## 4. 错误处理

- L804：`thread_id` 缺失 → `ValueError`；工具层应捕获并返回 `ToolMessage` 而非未处理异常（需核对 L851+ 外层 try/except）。
- 未知 template/theme → `ValueError` 带可用列表（L827–833），**对用户友好**。

---

## 5. 测试建议

1. `slides_outline` 含 `<script>alert(1)</script>` → 输出中无执行脚本。  
2. `**bold**` 与 `` `code` `` → 正确 HTML 标签。  
3. 10MB 级 outline → 应拒绝或截断（若加限制）。  
4. 无效 `template` → ToolMessage 错误，不 partially 上传 MinIO。

---

*关联：[backend_custom_round5_core_security_tests.md](./backend_custom_round5_core_security_tests.md) · [per_file_rag_custom.md](./per_file_rag_custom.md)*
