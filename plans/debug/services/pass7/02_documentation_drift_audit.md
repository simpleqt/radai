# 第七遍：设计文档与实现漂移审查

> 对照 `docs/`、`plans/` 与 `backend/services` 及集成代码。  
> 便于技术写作同步，**非**代码修改。

---

## 1. 漂移矩阵（会话文件相关）

| 文档 | 声明 | 代码现实 | 严重度 |
|------|------|----------|--------|
| [chat_file_upload_design.md](../../../docs/chat_file_upload_design.md) | MinerU 未接入对话上传 | `ChatFileParser._try_mineru` 已接入 | 中 |
| 同上 | 单路由 `/agent/chat-upload` | `/api/v2/chat-files/upload` | 低（已注明） |
| 同上 | 索引持久化待完成 | 仍内存 LRU，无 Redis | 中（准确待完成） |
| [personal_files_module.md](../../../docs/docs_backend/personal_files_module.md) | MinerU ❌ | Parser 有，路由未强制 | 中 |
| 同上 | BM25 ⚠️ 部分 | 核心已有，缺 overlap/多 worker | 低 |
| [个人文件夹…设计方案](../../个人文件夹文件管理与上传复用设计方案.md) | attach 更新 `last_used_at` | 未实现 | 中 |
| 同上 | 删除后再引用失效 | 索引/metadata 仍可用 | **高** |
| [agent_architecture_redesign.md](../../../docs/agent_architecture_redesign.md) | 轻量通道保留 | ✓ LightweightQAAgent | — |
| 同上 | 未描述会话文件索引 | 已实现但未建档 | 低 |

---

## 2. 三域概念澄清（防审查混淆）

```text
┌─────────────────────┬──────────────────────┬─────────────────────┐
│ 会话文件检索         │ 知识库 RAG            │ 用户文件元数据       │
├─────────────────────┼──────────────────────┼─────────────────────┤
│ ChatFileIndex       │ DirectRetriever      │ user_file_service   │
│ chat_file_parser    │ PerKBKeywordExtractor│ user_files 表       │
│ 内存 BM25+TF-IDF    │ Milvus/Redis/Nebula  │ MinIO 二进制        │
│ thread_id 作用域    │ kb_id 作用域         │ owner 作用域        │
└─────────────────────┴──────────────────────┴─────────────────────┘

```

审查报告中的「分词不一致」仅指 **ChatFileIndex 内 BM25 vs TF-IDF**，与 [关键词提取落地方案](../../关键词提取落地方案.md) **无关**。

---

## 3. 建议修订的文档（优先级）

| 优先级 | 文档 | 建议修改 |
|--------|------|----------|
| P0 文案 | `chat_file_upload_design.md` | MinerU：已接入（依赖配置），复杂 PDF 仍可能失败 |
| P0 文案 | `personal_files_module.md` §8 | 更新 BM25/MinIO/删除一致性 |
| P1 | `个人文件夹…设计方案.md` | 增加「实现差异」节：last_used、删除与索引 |
| P1 | `agent_engine_module.md` | 新增会话文件 + Lightweight vs Graph 表 |
| P2 | `agent_architecture_redesign.md` | 脚注：会话文件不在此文范围，见 personal_files_module |
| P2 | [EXECUTIVE_SUMMARY.md](../../EXECUTIVE_SUMMARY.md) | 已含 P0，可链到本文档 |

---

## 4. 审查报告自身维护

| 文件 | 状态 |
|------|------|
| `plans/debug/services/*` | 与源码同步至 2026-05-30 |
| 代码变更后 | 应更新 pass3 问题是否仍成立 |
| `fix_backlog` | 实现项完成后勾选 |

建议在 `REVIEW_INDEX.md` 增加「文档同步 checklist」。

---

## 5. 与 `agent_architecture_redesign.md` 关系

用户当前打开的架构重构文档聚焦：

- 巨石 agent 拆分、Skill 路由、SSE 统一

**会话文件 services 问题** 不阻塞架构重构，但影响：

- `lightweight_mode` 产品体验（提示/工具不一致）
- `agent.py` 注入层 token 成本

建议在架构 doc「已落地 vs 原方案」表增一行：

| 会话级文件 BM25 | ⚠️ services 已实现，多 worker/删除/文档漂移待治理 |

---

*第七遍 — 文档漂移*
