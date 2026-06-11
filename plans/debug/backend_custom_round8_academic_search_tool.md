# 第八轮深度审查：`acdemic_search_tool.py`

**文件**：`backend/custom/tools/acdemic_search_tool.py`（约 974 行）  
**工具名**：`academic_search`（模块文件名拼写 **acdemic**）  
**日期**：2026-05-30 · **只读**

---

## 1. 架构

| 层 | 内容 |
|----|------|
| 数据模型 | `Paper` dataclass、`Database` 枚举 |
| 适配器 | Semantic Scholar、Crossref、arXiv、PubMed、OpenAlex、百度学术 |
| 核心 | `_async_search` → `asyncio.gather` 并发 |
| 后处理 | `_deduplicate`、`_sort_papers`、`_generate_statistics` |
| 出口 | `@tool("academic_search")` + `consume_tool_budget("academic_search")` |

**接入**：`ToolAssemblyPipeline` 在 skill 声明 `academic_search` 时加载；`NEFullFeatureAgent` / `CSHFullFeatureAgent` 预算：普通 2 次、深研 5 次（且需 `academic-search` skill 才设 2）。

---

## 2. 优点

1. **并发 + 单源容错**：`_search_with_fallback` 限流/超时/HTTP 错误分源记录（L600–634）。  
2. **去重**：DOI + 标题指纹，保留高引用版本（L705+）。  
3. **预算集成**：与 `request_limits` 一致，耗尽时返回明确文案（L902–908）。  
4. **输出**：Markdown 含统计与各源状态，便于模型引用。

---

## 3. 问题清单

### P2：`_run_async` 在已有事件循环中阻塞线程

```91:109:backend/custom/tools/acdemic_search_tool.py
def _run_async(coro):
    ...
    if loop and loop.is_running():
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
```

- LangGraph `ToolNode` 同步 `invoke()` 时，在 **async worker** 内会占满线程池线程直至检索结束（默认 6 源 × 15s 超时量级）。
- 高并发时可能 **线程池饥饿**；更稳妥：`await` 路径用 async tool，或 `asyncio.run_coroutine_threadsafe` 回当前 loop。

### P2：模块级 `logging.basicConfig`（L33）

- import 时可能 **覆盖** 应用全局日志级别/Handler。

### P2：异常信息进入工具返回

- `_search_with_fallback` 末次失败：`f"{type(e).__name__}: {str(e)[:80]}"` 写入 `source_status`，最终进 Markdown 给模型（可接受）；若上层未过滤，用户可见路径取决于是否展示 tool 结果。

### P3：默认并发打满外部 API

- `databases=None` → `DEFAULT_SOURCES` 全源 `gather`（L651–663）。
- 无 **每请求源数量上限**、无全局限流；Semantic Scholar 无 Key 时易触发 `RateLimitError`（代码有处理，但体验差）。
- **建议**：默认 2–3 源；env 配置 `ACADEMIC_SEARCH_DEFAULT_SOURCES`。

### P3：文件名与工具名不一致

- 文件 `acdemic_search_tool.py`，工具 `academic_search`，import 路径易错（已在 `tool_assembly` L56 固化）。

### P3：`if __name__ == "__main__"` 测试块（L970+）

- 生产包内保留 CLI 测试；无危害但宜迁到 `tests/` 或 `scripts/`。

### P3：与 CSH 域

- 检索 query 无领域过滤；CSH 用户搜中文场地修复标准 **合理**，但 Prompt 未在工具层区分 — 依赖主 Agent Prompt。

---

## 4. 配置与密钥

| 变量/配置 | 用途 |
|-----------|------|
| `SEMANTIC_SCHOLAR_API_KEY` | 提高 SS 限额（注释 L122–123） |
| `DEFAULT_TIMEOUT = 15` | httpx 客户端 |
| `MAX_RETRIES = 2` | 单源重试 |

**审查建议**：文档化必选/可选 Key；监控各源失败率。

---

## 5. 建议测试

1. `consume_tool_budget` 耗尽 → 第二次调用拦截。  
2. 单源 `RateLimitError` → 其他源仍返回。  
3. 在 running loop 内 `academic_search.invoke` → 不死锁、超时可控。  
4. 重复 DOI 论文 → 去重后 1 条。

---

*关联：[backend_custom_deep_dive_research_draft_and_tool_assembly.md](./backend_custom_deep_dive_research_draft_and_tool_assembly.md) · Issue P3-1*
