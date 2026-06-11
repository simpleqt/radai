---
name: academic-search
description: 学术文献多源检索技能。当用户搜索论文、查找引用/DOI/arXiv、梳理研究现状、写综述或 related work、查作者/方法/高被引文献时使用。触发词包括「论文」「文献」「引用」「DOI」「arXiv」「PubMed」「预印本」「综述」「related work」「research paper」「citation」「literature review」。通过 `academic_search` 并发检索 arXiv、Crossref、OpenAlex、Semantic Scholar、PubMed，输出结构化 Markdown；时效新闻/政策类问题用 `web_search` 补充，不得用网页搜索替代学术检索。
tools:
  - academic_search
  - web_search
---

# Academic Search（学术文献检索）

面向论文与科研文献的结构化检索技能。核心工具为 `academic_search`，与 `web_search` 分工协作。

## 何时使用

**立即启用本 skill**，当用户意图属于以下任一类：

- 搜索某主题的学术论文、预印本或期刊文章
- 查找 DOI、arXiv ID、PMID、作者、发表 venue、引用数
- 为综述、related work、文献调研收集代表性论文
- 对比方法、梳理研究趋势、筛选高被引或近年文献
- 询问「有没有相关论文」「这个方向最近发了什么」

**不要用 `academic_search` 的场景**（改用 `web_search` 或知识库）：

- 行业新闻、政策解读、产品发布、市场动态
- 核电子学等领域知识库内已有权威教材/标准（优先 `multi_kb_search`）

## 核心规则

1. **文献类问题必须调用 `academic_search`**，禁止仅用 `web_search` 代替。
2. **关键词优先英文**：如「大语言模型」→ `large language models`；中文 query 效果差时再换英文。
3. **同一轮最多调用 2 次**（深度研究子任务可放宽）；结果不足时调整 `year_range`、`databases` 或 `sort_by` 后重试，避免重复相同参数。
4. **组合策略**：
   - 领域背景 → `multi_kb_search`（若可用）
   - 学术论文 → `academic_search`
   - 最新资讯/政策 → `web_search`
5. 回答时保留工具返回的 DOI/arXiv/链接，按 `[1][2]` 或列表规范引用，勿编造未检索到的论文。

## 工具：`academic_search`

### 参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| query | string | **必填** | 检索词，建议英文 |
| max_results | int | 5 | 返回篇数（1–100） |
| databases | list[str] | 全部 5 源 | 见下表 |
| year_range | string | "" | 如 `"2020-2024"`，空则不限 |
| sort_by | string | `"citations"` | `relevance` / `citations` / `year` |
| min_citations | int | 0 | 最小引用数过滤 |
| open_access_only | bool | false | 仅开放获取 |

### 数据源选型

| 值 | 适用领域 | 备注 |
|----|----------|------|
| `arxiv` | CS、物理、数学预印本 | 无限流，优先 |
| `crossref` | 期刊、DOI | 覆盖面广 |
| `openalex` | 跨学科、引用统计 | 默认启用 |
| `semantic_scholar` | AI/CS、引用网络 | 无 Key 易限流，可单独指定 |
| `pubmed` | 生物医学、生命科学 | 医学方向优先 |

默认并发顺序：`arxiv` → `crossref` → `openalex` → `semantic_scholar` → `pubmed`（易限流源靠后）。

### 调用示例

**基础检索**

```json
{
  "query": "transformer attention mechanism",
  "max_results": 5
}
```

**近五年高被引**

```json
{
  "query": "large language model alignment",
  "max_results": 8,
  "year_range": "2021-2026",
  "sort_by": "citations",
  "min_citations": 50
}
```

**生物医学 + 仅 OA**

```json
{
  "query": "CRISPR gene editing therapy",
  "databases": ["pubmed", "openalex"],
  "open_access_only": true,
  "max_results": 5
}
```

## 与 `web_search` 的分工

| 工具 | 用途 |
|------|------|
| `academic_search` | 论文元数据、摘要、DOI、引用、预印本、期刊 |
| `web_search` | 新闻、政策、产品、非结构化网页、时效动态 |

推荐流程：先 `academic_search` 收文献 → 若需「最新行业动态」再 `web_search` 补充。

## 回答规范

- 工具返回 Markdown 可直接引用；提炼结论时保留标题、作者、年份、venue、引用数、链接。
- 多篇文献对比优先用表格（标题 / 方法 / 年份 / 引用 / 链接）。
- 若某数据源限流或零结果，在回答中说明并建议缩小范围或换英文关键词。
- 综述类任务：按主题聚类归纳，避免平铺标题列表，并给出结构化小节与对比表。

## 输出示例（节选）

```markdown
# 📚 文献检索结果

**检索词**: transformer attention
**结果数量**: 3篇
**数据源**: arxiv ✅ 4篇, crossref ✅ 3篇, semantic_scholar ❌ 限流

### 1. Attention Is All You Need
- **作者**: Ashish Vaswani, Noam Shazeer 等
- **出版信息**: 2017年 | NeurIPS
- **引用数**: 5421
- **标识符**: DOI: 10.48550/arXiv.1706.03762 | arXiv: 1706.03762
- **访问**: [原文链接](...) | 🔓 开放获取
```
