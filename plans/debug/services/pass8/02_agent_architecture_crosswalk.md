# 第八遍：`agent_architecture_redesign.md` 对照审查

> 用户关注文档：[docs/agent_architecture_redesign.md](../../../docs/agent_architecture_redesign.md)  
> 对照对象：`plans/debug` 全部审查结论 + 当前代码（2026-05-31 文档内标注）

---

## 1. 文档「已落地」表 — 审查核实

| 文档声称 | 审查核实 | 备注 |
|----------|----------|------|
| NEAgentRouter | ✓ `router.py` 存在 | 默认无 skill→lightweight |
| aquery_stream 仍 ~1040 行 | ✓ wc -l 1040 | 较 2241 改善，仍大 |
| NEAgentStreamHandler ~987 行 | ✓ | 与 services 无直接耦合 |
| ContextPruningMiddleware | 未审中间件实现 | 不在 services 范围 |
| multi_kb_search + RRF | ✓ | KB 域，见 pass7/03 |
| PerKB 关键词 hybrid | ✓ | 非 ChatFileIndex |
| Skill→Workflow 规划 | 未落地 | 仍提示词+工具注入 |

---

## 2. 文档未覆盖但审查已发现的主题

建议在 `agent_architecture_redesign.md` 或 `agent_engine_module.md` **增补一节**「会话级文件检索」：

| 主题 | 审查文档 | 关键 P0/P1 |
|------|----------|------------|
| ChatFileIndex / Parser | pass2–3 | overlap、多 worker、锁 |
| 个人文件夹/attach | pass4–5, design gap | 软删、last_used |
| agent.py 文件注入 | pass7 | Lightweight 工具提示矛盾 |
| export-docx 无鉴权 | pass6 | 安全 |
| 入库三消费端 | pass3/6 | worker vs startup |
| KB vs 会话检索边界 | pass7/03 | 勿混淆 |

---

## 3. 架构图（§3.0）准确性

文档 ASCII 图含：

- `LightweightQAAgent` 经 `FullFeature` 委托 — **准确**  
- `multi_kb_search → retrieve_context` — **准确**（非 services）  
- **缺失**：`agent.py` 文件注入层、`chat_files`/`my_files` API

建议在图下增加脚注：

```text
用户上传文件 → chat_files/my_files → ChatFileIndex（内存）
                              ↘ agent.py 拼接到 question

```

---

## 4. 重构目标（§二）与 services 问题映射

| 重构目标 | services 审查是否阻塞 |
|----------|----------------------|
| Skill 驱动路由 | 否 |
| 保留轻量通道 | 是 — 需修 Lightweight 工具提示（P1-12） |
| 主智能体精简 | 否（Agent 层） |
| 流式处理器独立 | 部分完成 |
| 子智能体可扩展 | 否 |
| 工具体系框架化 | chat_file_search 已框架化 scoped tool |

**结论**：`backend/services` 问题**不阻碍**架构重构主线条，但应在轻量通道修复后再改路由默认策略。

---

## 5. 目标架构（§3.1 规划图）标注

图中 `SkillWorkflowExecutor`、`DeerFlowAgent` 仍为规划——审查时**勿**将规划能力当作已实现去排查 services。

---

## 6. 对 `agent_engine_module.md` 的补充建议

该文档详述 middleware、multi_kb_search，但：

- **无** `chat_file_search`、`ChatFileIndex`、`file_ids` 描述  
- 轻量通道只写「委托 LightweightQAAgent」，未写内联检索 vs 工具差异  

建议在 §双通道路由 后增加「上传文件三策略」表（vision / context / indexed），链接 `personal_files_module.md`。

---

## 7. 审查闭环：architecture 文档本身的质量

| 项 | 评价 |
|----|------|
| 历史 vs 现状分离 | 好（已落地表 + 规划图分栏） |
| 与 agent_engine 交叉引用 | 好 |
| 会话文件 | **缺口** |
| 行数/文件路径 | 2026-05-31 同步，基本可信 |

---

## 8. 建议的 doc PR 清单（仅文档）

1. `agent_architecture_redesign.md` §3.0 脚注：会话文件链  
2. `agent_engine_module.md` 新 §：上传文件与检索  
3. `agent_architecture_redesign.md` 已落地表 +1 行：会话 BM25 ⚠️  
4. 链接 `plans/debug/EXECUTIVE_SUMMARY.md` 供实施参考（可选，内部）

---

*第八遍 — 架构文档对照*
