# RAG 评测框架

> 项目内置的检索质量与端到端 RAG 评测工具，覆盖 HotpotQA 通用基准与核电子学期刊论文库（Paper KB）领域评测。

---

## 1. 概述

评测框架位于 `eval/` 目录（见 [eval/README.md](../eval/README.md)），独立于主 API 运行，用于：

- **检索质量评估**：Recall@K、MRR、证据命中率；
- **消融实验**：PPR / Rerank / MMR 各阶段贡献对比；
- **端到端 Ragas 评测**：Faithfulness、Answer Relevancy、Context Precision；
- **LightRAG 模式对比**：mix / hybrid / local / global 等检索模式；
- **GraphRAG-Bench 官方指标**：context_relevancy（CR）+ evidence_recall（ER），LLM 判分。

评测结论已反哺生产配置（如 MMR_ONLY Profile、hybrid 提词、medical→domain / novel→general_qa），详见 [知识库算法和架构.md](./知识库算法和架构.md)。

---

## 2. 目录结构

```text
eval/
├── datasets/
│   ├── hotpotqa/                 # HotpotQA 通用 QA 基准
│   │   ├── import_hotpotqa.py    # 数据导入 LightRAG
│   │   ├── eval_retrieval_quality.py
│   │   ├── ablation_direct_retriever.py
│   │   ├── ablation_report.py
│   │   └── results/              # 评测结果 JSON
│   └── paper_kb/                 # 核电子学期刊论文库评测
│       ├── common.py             # 公共模块（KB ID、消融方案目录）
│       ├── test_queries.jsonl    # 17 道领域测试题
│       ├── eval_retrieval_quality.py
│       ├── ablation_direct_retriever.py
│       ├── eval_ragas_ablation.py
│       ├── eval_lightrag_modes.py
│       ├── eval_lightrag_modes_ragas.py
│       ├── ragas_report.py
│       ├── run_ragas_ablation.sh
│       ├── run_lightrag_modes_ragas.sh
│       └── results/              # 评测报告 Markdown + JSON
├── graphrag_bench/               # GraphRAG-Bench 官方 LLM 检索指标
│   ├── run_official_graphrag_eval.py  # 唯一对外结论口径（CR/ER）
│   ├── bench_common.py           # 消融方案、prod_profile 映射
│   ├── bench_data.py             # medical/novel 问题加载
│   ├── config_loader.py          # 裁判 LLM（默认 qwen3.5-35b）
│   ├── keyword_compare.py        # hybrid vs LLM 提词 Jaccard
│   ├── keyword_sources.py        # 提词来源封装
│   ├── health_check.py / import_to_kb.py
│   └── results/                  # official_ablation_summary_*.json
└── GraphRAG-Benchmark-main/      # 原始 benchmark 数据（medical / novel）
```

---

## 3. HotpotQA 评测

### 3.1 数据导入

```bash
cd eval/datasets/hotpotqa
venv314/bin/python import_hotpotqa.py \
  --sample 2000 \
  --output eval_output/
```

将 HotpotQA 子集导入临时 LightRAG 知识库，生成 `test_queries.jsonl` 与 `ground_truth.jsonl`。

### 3.2 检索质量评测

```bash
venv314/bin/python eval_retrieval_quality.py \
  --queries eval_output/test_queries.jsonl \
  --ground-truth eval_output/ground_truth.jsonl \
  --sample 30
```

**输出指标：**

| 指标 | 说明 |
| :--- | :--- |
| Recall@K | Top-K 检索结果中包含标准证据的比例 |
| MRR | Mean Reciprocal Rank |
| Evidence Hit Rate | 证据片段精确匹配率 |

### 3.3 消融实验

```bash
venv314/bin/python ablation_direct_retriever.py \
  --sample 30 \
  --modes raw,ppr,rerank,mmr
```

对比 DirectRetriever 各过滤阶段的检索质量变化，结果输出至 `results/ablation_direct_*.json`。

---

## 4. Paper KB（期刊论文库）评测

### 4.1 配置

- **知识库 ID**：`<kb_id_paper>`
- **KB Code**：`NuclearElectronicsPaperRepository`
- **测试题**：`eval/datasets/paper_kb/test_queries.jsonl`（17 道领域问题）

LLM 评测需配置 `eval/datasets/paper_kb/eval_llm.env`（参考 `eval_llm.env.example`）。

### 4.2 检索质量

```bash
cd eval/datasets/paper_kb
venv314/bin/python eval_retrieval_quality.py --sample 5
```

### 4.3 Ragas 端到端评测

```bash
bash run_ragas_ablation.sh
# 或
venv314/bin/python eval_ragas_ablation.py --sample 17
```

**Ragas 指标：**

| 指标 | 说明 |
| :--- | :--- |
| Faithfulness | 回答是否忠实于检索上下文 |
| Answer Relevancy | 回答与问题的相关度 |
| Context Precision | 检索上下文的精确度 |

报告输出：`results/ragas_ablation_17q.md`

### 4.4 LightRAG 模式对比

```bash
bash run_lightrag_modes_ragas.sh
```

对比 `mix`、`hybrid`、`local`、`global` 等 LightRAG 检索模式在 Paper KB 上的表现。

---

## 5. GraphRAG-Bench 评测

面向跨领域 GraphRAG 场景的官方检索指标评测，数据来自 `eval/GraphRAG-Benchmark-main/`（medical / novel 子集）。

### 5.1 生产 Profile 映射

| 子集 | 检索 Profile | Stage-2 | 说明 |
| :--- | :--- | :--- | :--- |
| medical | `domain` | MMR_ONLY | 垂直医学库，四路全开 |
| novel | `general_qa` | RAW | 百科/小说多跳，关 Path-A |

生产对照方案 `prod_profile` 使用 **hybrid 提词**（非 LightRAG LLM 提词）。

### 5.2 常用命令

```bash
source venv314/bin/activate
cd <project_root>

# 生产 vs LightRAG hybrid 快测
python eval/graphrag_bench/run_official_graphrag_eval.py \
  --kb medical,novel --sample 30 \
  --schemes prod_profile,lightrag_hybrid \
  --top-k 10 --lightrag-top-k 5 --llm-model qwen3.5-35b

# 全模块消融（每库 23 方案 + LightRAG 五 mode）
python eval/graphrag_bench/run_official_graphrag_eval.py \
  --preset full-ablation --kb medical,novel --sample 30 \
  --top-k 10 --lightrag-top-k 5 --llm-model qwen3.5-35b \
  --eval-concurrency 35 --run-tag ablation_full_YYYYMMDD

# 关键词专项：hybrid vs LLM 提词
python eval/graphrag_bench/run_official_graphrag_eval.py \
  --preset prod-kw-compare --kb medical,novel --sample 30 \
  --top-k 10 --llm-model qwen3.5-35b --run-tag prod_kw_compare_YYYYMMDD
```

### 5.3 指标与产物

| 指标 | 说明 |
| :--- | :--- |
| context_relevancy (CR) | 检索上下文与问题的相关度（LLM 判分） |
| evidence_recall (ER) | 标准证据在检索结果中的召回（LLM 判分） |

- 汇总：`eval/graphrag_bench/results/official_ablation_summary_{run_tag}.json`
- 详细命令与 preset 说明 → [eval/graphrag_bench/README.md](../eval/graphrag_bench/README.md)
- 结论与数据表 → [知识库算法和架构.md](./知识库算法和架构.md) §10.3、§11.5–11.6

> 已废弃模糊子串 Recall 脚本（`test_kb_bench*.py`），不可与官方 leaderboard 对比。

---

## 6. 评测结果归档

| 路径 | 内容 |
| :--- | :--- |
| `hotpotqa/results/` | HotpotQA 检索质量与消融 JSON |
| `paper_kb/results/` | Paper KB 检索、Ragas、LightRAG 模式对比 |
| `paper_kb/results/*.md` | 人类可读的评测报告 |
| `graphrag_bench/results/` | GraphRAG-Bench 官方 CR/ER 汇总与逐题 JSON |

**关键结论（2026-05-30）：**

- 生产 domain/lightweight Profile 采用 **MMR_ONLY**（关闭 PPR）；
- Paper KB 17 题：Direct MMR_ONLY 规则 F1≈0.63，优于 LightRAG 各原生 mode；
- GraphRAG-Bench full-ablation（`ablation_full_20260529`）：medical prod ER **0.733**；novel prod **0.600**，naive **0.744**（待 Profile 决策）；
- 关键词专项（`prod_kw_compare_20260530`）：医学库 hybrid ER **0.70** vs LLM **0.50**；生产固定 **hybrid**；禁止 `_our_kw` 注入 LightRAG；
- 跨库 RRF 合并阶段 Rerank 引入为 P1 路线图项。

---

## 7. 与 CI 集成（路线图）

当前评测为手动运行。路线图 P2 项建议：

- 将 Paper KB 5 题 smoke test 纳入 CI；
- HotpotQA 30 题检索 Recall 回归阈值告警；
- 定期（周/月）全量 Ragas 评测任务。

### 7.1 Agent Runtime 回归评测

智能体平台规范化后，需要补充独立于 RAG 召回质量的运行时回归评测：

| 类型 | 覆盖内容 | 建议阈值 |
| :--- | :--- | :--- |
| 路由契约 smoke | `/api/v2/agent-runtime/profiles`、`/query`、任务状态/取消/SSE 路由存在且旧 `/api/v2/agent/*` 不注册 | 必须通过 |
| SSE 协议 smoke | 正常 chunk/status/event/error/done 均能编码，模型异常不会导致 ASGI 未捕获异常 | 必须通过 |
| Skill 加载 smoke | `read_local_skill_file` 能加载 profile 默认 Skill，脏参数如 `html-ppt</parameter` 会被规范化 | 必须通过 |
| ToolPolicy smoke | `selected_skills` 能启用对应工具，未声明工具不会注入 | 必须通过 |
| 深研任务 smoke | confirmed research plan 能创建后台任务，状态流、取消、活动任务查询可用 | 必须通过 |
| 前端协议回归 | RadAI/CSH-AI 登录态 query 默认走 `/api/v2/agent-runtime/query`，访客仍走 `/api/v2/guest/query` | 必须通过 |

短期建议将 `backend/tests/test_agent_platform_standardization.py` 作为 CI 基础用例；中期增加启动真实 FastAPI app 的 HTTP smoke；长期将端到端问答样例接入定期评测。

详见 [下一步开发需求.md](./下一步开发需求.md)。

---

## 8. 相关文档

| 文档 | 说明 |
| :--- | :--- |
| [知识库算法和架构.md](./知识库算法和架构.md) | 生产 Profile、DirectRetriever、评测结论 |
| [../eval/graphrag_bench/README.md](../eval/graphrag_bench/README.md) | GraphRAG-Bench 脚本说明 |
| [../plans/知识库算法和架构.md](../plans/知识库算法和架构.md) | 完整演进记录与 §十五详表 |
| [knowledge_base_recall_evaluation_plan.md](./knowledge_base_recall_evaluation_plan.md) | 召回评测计划 |
| [chunk_recall_optimization.md](./chunk_recall_optimization.md) | Chunk 召回优化 |
| [docs_backend/rag_module.md](./docs_backend/rag_module.md) | RAG 引擎模块 |

---

© 2026 核电子学智能助手项目组
