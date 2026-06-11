# 深度审查：`research_draft_agent` + `tool_assembly`

**日期**：2026-05-30 · **只读**

---

## Part A — `research_draft_agent.py`（178 行）

### 职责

- 深度研究入口前生成 **纯文本分点大纲**（非 JSON）。
- `build_draft` 同步一次性；`build_draft_stream` 流式；`normalize_confirmed_plan` 确认后规范化。

### 优点

- Prompt **领域中立**（核电子学/CSH 共用合理）。
- `_sanitize_thinking_text` 去除 `` / `<thought>`（L17–42）。
- 失败有 **fallback 五条默认大纲**（L87–96），不阻断流程。

### 问题

#### P2：`build_draft_stream` 流式阶段未过滤思考标签

- L137–141：原始 `chunk.content` 经 `yield {"content": content}` 推到 API。
- L144–149：仅在流结束后 `_sanitize_thinking_text(full_content)` 写 `final_outline`。
- **API 层**（`agent.py` L626–629、`csh_agent.py` 同类）将 `draft_chunk` **原样 SSE 转发**：

```626:629:backend/custom/api/v2/agent.py
                    if chunk.get("content"):
                        draft_outline += chunk["content"]
                        yield f"data: {json.dumps({'type': 'draft_chunk', 'content': chunk['content']}, ensure_ascii=False)}\n\n"
```

→ 与主对话 P0-3 同类：**流式草案阶段可能向用户展示思考内容**。

**建议**：流式路径复用 `thinking_filter` 状态机，或 chunk 级 `_sanitize_thinking_text`（注意性能）。

#### P3：Prompt 重复

- `build_draft` L55–74 与 `build_draft_stream` L113–132 **全文重复**。
- 抽 `_build_draft_prompt(question, seed_text, intent_json)` 单点维护。

#### P3：`intent_payload` 仅写入 Prompt

- 含 `intent`/`confidence`/`reasons`，模型可能忽略；无 schema 校验。
- 可接受，但 API 传入的 `intent_payload` 在 agent.py L578–599 已写死逻辑，与 draft agent 内字段略冗余。

#### P3：fallback 流式「逐字打出」

- L161–162：`for char in fallback_text: yield {"content": char}` — 人为放慢，前端体验怪异；可单次 yield 全文。

---

## Part B — `tool_assembly.py`（~250 行）

### 职责

`ToolAssemblyPipeline.assemble()`：合并 core 工具、multi_kb 闭包、web_search、深研报告、skill 声明工具、chat_file_search。

### 关键逻辑

| 行为 | 行号 | 说明 |
|------|------|------|
| `lightweight_mode` | L124–126 | 返回 `[]`，正确 |
| `multi_kb` 闭包 | L72–121 | 使用 `self._kb_ids`；skill 未加载则 JSON 错误 |
| `deep_research` | L147–149 | 移除 `ask_clarification` |
| 沙箱工具移除 | L158–161 | bash/write_file 等 |
| `academic_search` | L55–57 | import **`acdemic_search_tool`**（拼写错误） |

### 问题

#### P1：`multi_kb` 闭包无 domain，仅 `kb_ids`

- L109–118：`execute_multi_kb_search(..., kb_ids=list(self._kb_ids))`。
- CSH Agent 若 `kb_ids` 配置正确可检索 CSH 库，但 **defaults 仍可能为空或错配**（见 NE agent 初始化）；与 `multi_kb` 模块内 NE 硬编码默认值叠加风险。

#### P2：`nuclear-electronics` skill 与 multi_kb 硬耦合

- L94–107：未 `has_loaded_skill("nuclear-electronics")` 则拦截并返回英文错误 JSON。
- CSH 用户若选其他 skill 但仍注入 multi_kb，行为依赖 `kb_ids` 非空（L138–144），**无 CSH 专用 skill 名**。

#### P2：`excel` 工具批量注入无路径策略

- L59–69：`get_excel_analysis_tools()` 返回 5 工具，**含任意 `file_path` 能力**（见 round7 excel 文档）。

#### P3：`chat_file_search` 依赖外部 thread_id

- L187–201：`has_thread_file_index(self._thread_id)` — API 必须传 **外部** `thread_id`（与 `build_internal_thread_key` 一致）；不一致则静默不注入。

#### P3：`_load_declared_skill_tool` 无扩展点

- 新 skill 工具需改本文件 switch；架构文档中的 registry 未落地。

---

## Part C — `acdemic_search_tool.py`（974 行，本轮摘要）

| 项 | 严重度 | 说明 |
|----|--------|------|
| 文件名 `acdemic` | P3 | 与 `academic_search` 工具名不一致，增加维护成本 |
| `logging.basicConfig` L33 | P2 | 模块 import 副作用，可能覆盖全局日志配置 |
| 外部 API | P2 | 多源 httpx；需 rate limit、超时（文件内已有部分逻辑，未全读） |
| 体积 | P3 | 宜拆 `clients/` + `formatters/` + `@tool` 薄封装 |

详细 API 行为可单开第八轮；本轮不重复展开。

---

## Part D — 交叉：深研后台与 tool_assembly

- `DeepResearchOrchestratorAgent` 内部仍用 **NE 系** `ToolAssemblyPipeline` / `NEFullFeatureAgent` 模式（P0-1）。
- CSH `run_deep_research_background` 虽传 `bg_kb_ids`（`csh_agent.py` L652–663），**编排器类仍为 NE** → Prompt/子智能体域错误与 KB 部分正确 **并存**。

---

## 测试建议

1. `build_draft_stream` + 模型输出含 `` → `draft_chunk` SSE 无标签。  
2. `ToolAssemblyPipeline(kb_ids=[])` → 无 `multi_kb_search`。  
3. skill `nuclear-electronics` 未 load → multi_kb 返回 `skill_not_loaded`。  
4. excel skill 注入后 `file_path=/etc/passwd` → 拒绝（修复后）。

---

*关联：[backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md) · [backend_custom_round7_tasks_streaming_misc.md](./backend_custom_round7_tasks_streaming_misc.md)*
