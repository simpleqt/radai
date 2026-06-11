# backend/custom 审查 — 执行摘要（1 页）

**范围**：`backend/custom`（52 个 Python 文件）  
**方式**：只读审查，结论在 `plans/debug/`，**未改业务代码**  
**日期**：2026-05-30

---

## 结论一句话

**CSH 与 NE 在深度研究、轻量问答、知识库检索上仍存在域混用；主站 SSE 可能向用户泄漏模型思考内容；API 层大量重复且缺测试与鉴权，存在内存与成本风险。**

---

## 必须优先修复（P0）

| # | 问题 | 状态（2026-05-31） |
|---|------|-------------------|
| 1 | CSH 深研仍实例化 **NE** 编排器 | ✅ 已修复：`CSHDeepResearchOrchestratorAgent` + `agent_domain="csh"` |
| 2 | CSH/Explore 轻量 NE Prompt | ✅ 已修复：`prompt_domain` + `csh_prompts` 轻量模板 |
| 3 | 主站 SSE 未过滤思考标签 | ❌ 待办：`agent.py` / `csh_agent.py` |

**建议 PR 顺序**：A 思考过滤 → B 轻量 Prompt 域 → C CSH 深研编排器。  
**操作指引**：[backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md)

---

## 高优先级（P1，P0 之后）

- **~91% 重复**：`agent.py` / `csh_agent.py`（1200+ 行）→ 抽 `agent_stream_common.py`  
- **TaskManager** 从不 `remove_task` → 长跑内存涨  
- **Explore API** 多数无鉴权 → LLM/检索成本可被刷  
- **异常 `str(e)`** 进入 HTTP/SSE  
- **multi_kb** 硬编码 NE 两个 KB UUID → CSH 无法正确检索  
- **0 单测** 覆盖 custom 模块  

---

## 架构对齐度（相对 `agent_architecture_redesign.md`）

| Phase | 状态 |
|-------|------|
| 域路由 / CSH Agent | 部分完成 |
| `workflows/` 注册表 | **未落地** |
| 统一流式/thinking 过滤 | **部分**（guest/runner 有，主站无） |
| 配置化 KB | **多套来源并存** |

详情：[backend_custom_vs_architecture_redesign.md](./backend_custom_vs_architecture_redesign.md)

---

## 文档与契约

- `docs/docs_backend` 与实现多处不一致（CSH 架构图、explore 检索、SSE 字段名）  
- 前端 `agent-v2.ts` 与后端事件名需对照 [backend_custom_sse_and_api_contract_gaps.md](./backend_custom_sse_and_api_contract_gaps.md)

---

## 交付物索引

| 读者 | 入口 |
|------|------|
| 负责人 | 本文 + [backend_custom_p0_fix_checklist.md](./backend_custom_p0_fix_checklist.md) |
| 开发 | [backend_custom_p0_implementation_guide.md](./backend_custom_p0_implementation_guide.md) · [backend_custom_p1_implementation_guide_detailed.md](./backend_custom_p1_implementation_guide_detailed.md) |
| 逐文件 | [backend_custom_per_file_review_detailed.md](./backend_custom_per_file_review_detailed.md) |
| 开 Issue | [backend_custom_github_issues.md](./backend_custom_github_issues.md) · [backend_custom_issues.csv](./backend_custom_issues.csv) |
| 大工具 | [backend_custom_deep_dive_multi_kb_search_tool.md](./backend_custom_deep_dive_multi_kb_search_tool.md) · [backend_custom_deep_dive_html_ppt_tool.md](./backend_custom_deep_dive_html_ppt_tool.md) |

---

## 建议资源

- **P0**：1 名后端，约 **3–5 人日**（含回归）  
- **P1 首轮**（脱敏、任务回收、死代码）：约 **1 周**  
- **API 抽取 + Explore 鉴权**：再 **1–2 周**  

测试：至少为 thinking 过滤、CSH 轻量 Prompt、深研 `agent_domain` 各加 **契约/单元测试** 各 1 组。
