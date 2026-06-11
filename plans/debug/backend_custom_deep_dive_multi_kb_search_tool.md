# 深度审查：`multi_kb_search_tool.py`

**文件**：`backend/custom/tools/multi_kb_search_tool.py`（约 1224 行）  
**日期**：2026-05-30  
**性质**：只读审查，未改源码

---

## 1. 职责与调用链

| 层级 | 符号 | 说明 |
|------|------|------|
| LangChain 工具 | `multi_kb_search`（`@tool`） | Agent 可调；参数含 `query`, `kb_ids`, `top_k`, `chunk_top_k` |
| 入口 | `execute_multi_kb_search` L876+ | 预算、锁、去重、超时 |
| 单次执行 | `_execute_multi_kb_search_once` L1035+ | 共享关键词 → 并发 `_search_single_kb_with_timeout` |
| 单库 | `_search_single_kb` L345+ | 委托 `rag_service.retrieve_context` |
| 合并 | `_merge_kb_results` / RRF | `merge_strategy` |

**主要调用方**：`NEFullFeatureAgent`、`LightweightQAAgent`（经 `tool_assembly`）、`core/tools/task_tool.py` 子智能体、`explore_learning._retrieve_context`（间接）。

---

## 2. 架构优点（值得保留）

1. **并发 + 错误隔离**：单库失败不拖垮整次检索（`gather` 后按 `success` 统计）。
2. **性能优化开关**：`MULTI_KB_PATH_A_QUERY_ONLY`、共享关键词、关闭 stage1 过采样（L110–111）。
3. **防滥用**：`get_multi_kb_request_lock()` 拒绝并行 multi_kb；`consume_tool_budget`；`TURN_MAX_EVIDENCE_CHARS` 字符预算。
4. **子智能体去重**：`subagent-exec-*` 线程下 `_INFLIGHT_MULTI_KB_TASKS` 复用相同 in-flight 任务（L960–1016）。
5. **运维友好**：模块头注释说明 Path-A 日志次数与调参方向（L13–21）。

---

## 3. 问题清单

### P1：KB ID 硬编码，与 CSH / 配置多套来源

```40:46:backend/custom/tools/multi_kb_search_tool.py
DEFAULT_KB_ID = "<kb_id_ne>"  # 核电子学知识库
PAPER_KB_ID = "<kb_id_paper>"  # 期刊论文库
```

- `kb_ids` 缺省时固定 NE 双库（L942），**无 `agent_domain` 或 profile 分支**。
- 与 `NEFullFeatureAgent.KB_IDS`、`config/kb_config.py`（未引用）、DB 绑定并存 → 改库需多处改 UUID。
- **CSH 全功能/轻量若误挂此工具**，会检索 NE 库而非 CSH 库（与 P0-2 轻量 Prompt 问题叠加）。

**建议**：`execute_multi_kb_search(..., domain: str | None)` 或从 `ToolRuntime` 读 `agent_code`，映射到 `settings`/registry 的 KB 列表；删除模块级常量或仅作 NE 默认 fallback。

---

### P1：主 Agent 路径无 in-flight 去重

- L962–1017：`async with kb_lock` 内，**仅** `in_subagent_thread` 才注册 `_INFLIGHT_MULTI_KB_TASKS`。
- 主对话若模型连续两次相同参数调用 multi_kb，第二次会被 L915–920 **整段拒绝**（“禁止并行”），而非等待第一次完成——行为正确但体验差；子智能体却能复用。
- **建议**：主线程也可对相同 `dedupe_key` 复用 in-flight task（仍保持全局 lock 串行化）。

---

### P2：异常信息可能进入工具返回字符串

- L1090–1091、L1099–1100：预计算失败仅 `logger.warning`，可接受。
- L312、L396、L463：单库 `except Exception as e` → `result["error"] = str(e)`，最终在合并文本中可能拼进 **LLM 可见上下文**（若上层未脱敏）。
- L1129：超时返回含 `query[:50]`，可能泄露用户查询片段到日志/返回。

**建议**：对用户/模型返回固定文案；`str(e)` 仅写日志。

---

### P2：线程锁 + asyncio 混用

- `_INFLIGHT_MULTI_KB_LOCK = threading.Lock()` 保护 `asyncio.Task` 字典（L220–221）。
- 在单事件循环内通常可行，但若未来多 loop 或多进程 worker，去重键 `id(loop)` 才在子智能体路径使用，**主路径无 dedupe**。
- `get_multi_kb_request_lock()` 为 **asyncio.Lock**（需读 `request_limits.py`）— 与 threading Lock 两套，审查时需一并文档化。

---

### P2：`rag_service._get_rag_instance` 私有 API

- L479：`await rag_service._get_rag_instance(kb_id)` — 与 `cross_cutting_duplication.md` 中「core/rag 私有方法」一致，升级 RAG 层易碎。

---

### P3：复杂度与可测性

- 单文件 1200+ 行，含 RRF、关键词归一化、Path-A 裁剪、字符截断等多职责。
- **仓库内无针对 multi_kb 的单测**；仅靠集成/E2E 难以覆盖超时、预算耗尽、单库失败合并。

**建议拆分**（非必须一次完成）：

| 模块 | 内容 |
|------|------|
| `multi_kb/constants.py` | KB 默认、超时、env 解析 |
| `multi_kb/merge.py` | RRF / weighted |
| `multi_kb/execute.py` | `execute_multi_kb_search` + 锁/预算 |
| `multi_kb/tool.py` | `@tool` 薄封装 |

---

## 4. 与文档/其他审查的交叉引用

| 主题 | 文档 |
|------|------|
| explore 检索策略与实现不一致 | `backend_custom_docs_vs_code_gaps.md`（expand 不检索 KB） |
| tool 预算与 request_limits | `deep_dive_multi_kb_explore_tools.md` |
| CSH 域 KB | `backend_custom_vs_architecture_redesign.md` Phase 3 |

---

## 5. 建议测试用例（实施修复时）

1. 双库均成功 → 返回含两库 label 与 chunk 数统计。  
2. 单库超时 → 另一库仍返回；`failed_kbs == 1`。  
3. `turn_remaining <= 0` → 固定拒绝文案，不调用 RAG。  
4. 并行第二次 multi_kb（主线程）→ 当前为拒绝；若改 in-flight 复用则断言只一次 RAG 调用。  
5. `domain=csh`（若实现）→ `effective_kb_ids` 为 CSH 配置，非 NE UUID。

---

*关联：[backend_custom_per_file_review_detailed.md](./backend_custom_per_file_review_detailed.md) · [deep_dive_multi_kb_explore_tools.md](./deep_dive_multi_kb_explore_tools.md)*
