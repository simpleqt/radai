# 知识库召回效果评估与消融实验方案

> **文档状态**：📋 方案文档（部分已落地）。§二 流程图为早期 LightRAG 单路径描述；**现行实现**见 [知识库算法和架构.md](./知识库算法和架构.md)（DirectRetriever 四路 + Retrieval Profile）。  
> **可运行评测**：[eval_framework.md](./eval_framework.md) · `eval/datasets/paper_kb/` · `eval/datasets/hotpotqa/` · `eval/graphrag_bench/`

## 一、概述

本文档设计了一套完整的知识库召回效果评估体系，包括评估指标定义、测试数据构建、测试流程搭建以及消融实验方案，用于验证各模块的有效性。

---

## 二、当前系统架构分析

### 2.1 检索流程概览

```
用户查询 (Query)
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段0: 关键词提取                                            │
│  ├─ 高级关键词提取 (high_level_keywords)                       │
│  ├─ 低级关键词提取 (low_level_keywords)                        │
│  └─ 查询意图识别 (query_type: GENERAL/COMPARISON/ANALYSIS...)  │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段1: 过采样召回 (Stage1 Recall)                            │
│  ├─ 实体召回 (Entity Retrieval) → top_k * 1.5                 │
│  ├─ 关系召回 (Relation Retrieval)                             │
│  └─ 文本块召回 (Chunk Retrieval) → chunk_top_k * 2            │
│  来源: Milvus向量检索 + NebulaGraph图查询                      │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段2: 内容过滤与重排序 (Content Filter & Rerank)            │
│  ├─ PPR图过滤 (Personalized PageRank)                         │
│  │   └─ 基于种子节点权重传播，过滤低相关性实体/关系             │
│  ├─ Reranker重排序                                            │
│  │   └─ 语义相似度重排序 (cross-encoder)                       │
│  └─ MMR去重与多样性                                            │
│       └─ Maximal Marginal Relevance，平衡相关性与多样性        │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────────┐
│  阶段3: 时序加权 (Time-Aware Weighting)                       │
│  └─ 基于文档时间的权重调整                                     │
└─────────────────────────────────────────────────────────────┘
    │
    ▼
  最终结果 (FilteredData)
    ├─ entities (实体列表)
    ├─ relationships (关系列表)
    └─ chunks (文本块列表)
```

### 2.2 核心模块识别

| 模块 | 文件位置 | 功能描述 | 可消融点 |
|------|----------|----------|----------|
| 关键词提取 | `backend/rag/keywords/extractor.py` | 提取高级/低级关键词，识别查询意图 | 关闭关键词扩展 |
| 向量召回 | LightRAG + Milvus | 基于Embedding的向量检索 | 切换索引类型/相似度函数 |
| 图召回 | LightRAG + NebulaGraph | 基于知识图谱的实体/关系检索 | 关闭图检索 |
| PPR过滤 | `backend/rag/content_filter/ppr_filter.py` | 基于PageRank的图结构过滤 | 关闭PPR |
| Reranker | `backend/rag/content_filter/reranker.py` | 语义重排序 | 关闭Rerank |
| MMR | `backend/rag/content_filter/mmr_reranker.py` | 多样性去重 | 关闭MMR |
| 时序加权 | `backend/rag/content_filter/per_kb_filter.py` | 文档时间权重 | 关闭时序加权 |

### 2.3 现行架构（2026-05，生产）

> 下文 §2.1 图为 **LightRAG `aquery_data` 时代** 的抽象；线上 Stage-1 已切换为 **DirectRetriever 四路**，Stage-2 由 **Retrieval Profile** 选择（垂直库 `MMR_ONLY`，百科 `RAW`）。完整说明见 [知识库算法和架构.md](./知识库算法和架构.md)。

```
用户查询
  → PerKBKeywordExtractor（hybrid 提词）
  → resolve_retrieval_profile
  → Stage-1: Path-A/B/C/D 并行（Milvus + Redis + NebulaGraph）
  → Stage-2: RAW | MMR_ONLY | FULL …
  → FilteredData → Agent / HTTP retrieve
```

### 2.4 已落地评测入口（替代 §四–§五 中的草案目录）

| 评测集 | 脚本目录 | 指标 | 生产结论摘要 |
| :--- | :--- | :--- | :--- |
| 期刊论文库 17 题 | `eval/datasets/paper_kb/` | F1、Ragas CP/CR | `domain` + **MMR_ONLY**，F1≈0.63 |
| HotpotQA | `eval/datasets/hotpotqa/` | F1、Recall@K | `general_qa` + **RAW**，Direct 优于 LightRAG mix |
| GraphRAG-Bench | `eval/graphrag_bench/` | 官方 CR/ER（LLM） | medical→domain；novel→general_qa；hybrid 提词 |

运行方式见 [eval_framework.md](./eval_framework.md)。消融方案名与 `paper_kb/common.py`、`graphrag_bench/bench_common.py` 对齐，勿与本文 §六 早期命名混用。

---

## 三、评估指标体系

### 3.1 召回指标 (Recall Metrics)

| 指标 | 公式 | 说明 |
|------|------|------|
| **Recall@K** | $\frac{\|Retrieved_K \cap Relevant\|}{\|Relevant\|}$ | 前K个结果中相关文档占所有相关文档的比例 |
| **Precision@K** | $\frac{\|Retrieved_K \cap Relevant\|}{K}$ | 前K个结果中相关文档的比例 |
| **MRR** | $\frac{1}{\|Q\|}\sum_{q \in Q}\frac{1}{rank(q)}$ | 第一个相关结果的排名倒数的平均值 |
| **MAP** | $\frac{1}{\|Q\|}\sum_{q \in Q}\frac{1}{\|R_q\|}\sum_{r \in R_q}\frac{\|Retrieved_r \cap Relevant_r\|}{r}$ | 各查询AP的平均值 |
| **NDCG@K** | $DCG_K / IDCG_K$ | 考虑位置权重的累积增益，归一化后便于比较 |
| **Hit@K** | $\frac{1}{\|Q\|}\sum_{q \in Q}\mathbb{1}[Retrieved_K \cap Relevant_q \neq \emptyset]$ | 至少命中一个相关结果的查询比例 |

### 3.2 多粒度评估

由于召回结果包含三种粒度，需要分别评估：

```
召回结果结构:
├─ Entities (实体)
│   └─ 指标: Entity_Recall@K, Entity_Precision@K, Entity_MRR
├─ Relationships (关系)
│   └─ 指标: Relation_Recall@K, Relation_Precision@K
└─ Chunks (文本块)
    └─ 指标: Chunk_Recall@K, Chunk_Precision@K, Chunk_NDCG@K
```

### 3.3 端到端指标 (End-to-End Metrics)

| 指标 | 说明 |
|------|------|
| **Answer Relevance** | 生成答案与查询的相关性 (LLM评估) |
| **Answer Groundedness** | 生成答案对召回内容的依赖度 (LLM评估) |
| **Latency** | 端到端响应延迟 |
| **Token Efficiency** | 召回内容利用率 |

---

## 四、测试数据构建

### 4.1 测试数据集结构

```
eval/
├── datasets/
│   ├── paper_kb/                   # ✅ 17 题 + ablation / Ragas / LightRAG mode 对比
│   ├── hotpotqa/                   # ✅ 导入 + ablation_direct_retriever
│   └── (规划) test_queries.jsonl   # 本文 §4 草案，未统一落地
├── graphrag_bench/                 # ✅ 官方 CR/ER + full-ablation preset
├── GraphRAG-Benchmark-main/        # 原始 medical/novel 数据
└── results/                        # 各脚本输出 JSON / Markdown
```

统一评测说明：[eval_framework.md](./eval_framework.md)。

### 4.2 测试查询格式

```json
{
    "query_id": "q001",
    "query": "什么是核电子学中的时间分辨率?",
    "query_type": "definition",
    "difficulty": "medium",
    "expected_entity_types": ["概念", "技术指标"],
    "expected_keywords": ["时间分辨率", "核电子学", "探测器"],
    "ground_truth_answer": "时间分辨率是指...",
    "metadata": {
        "source": "expert_annotation",
        "annotator": "user1",
        "created_at": "2025-01-15"
    }
}
```

### 4.3 相关性标注格式 (qrels)

```json
{
    "query_id": "q001",
    "entity_id": "ent_12345",
    "relevance": 3,
    "granularity": "entity"
}
```

相关性等级:
- 3: 高度相关 (核心概念/直接答案)
- 2: 相关 (背景知识/相关概念)
- 1: 弱相关 (边缘相关)
- 0: 不相关

### 4.4 查询类型覆盖

```python
QUERY_TYPE_DISTRIBUTION = {
    "definition": 0.20,      # 定义类: "什么是X?"
    "comparison": 0.15,      # 比较类: "X和Y的区别?"
    "procedure": 0.15,       # 流程类: "如何实现X?"
    "relationship": 0.20,    # 关系类: "X和Y有什么关系?"
    "quantitative": 0.10,    # 量化类: "X的参数是多少?"
    "analysis": 0.10,        # 分析类: "分析X的影响因素"
    "open_ended": 0.10,      # 开放类: "关于X的综述"
}
```

---

## 五、评估框架实现

### 5.1 核心评估器类

```python
# backend/eval/evaluator.py

from dataclasses import dataclass
from typing import List, Dict, Set, Optional
import numpy as np

@dataclass
class RetrievalResult:
    query_id: str
    entities: List[Dict]
    relationships: List[Dict]
    chunks: List[Dict]
    latency_ms: float
    module_stats: Dict

@dataclass
class QrelEntry:
    query_id: str
    doc_id: str
    relevance: int
    granularity: str  # entity, relation, chunk

class RecallEvaluator:
    def __init__(self, qrels: Dict[str, Set[str]]):
        self.qrels = qrels
    
    def recall_at_k(self, retrieved: List[str], k: int) -> float:
        relevant = self.qrels.get(self.current_query_id, set())
        retrieved_k = set(retrieved[:k])
        if len(relevant) == 0:
            return 0.0
        return len(retrieved_k & relevant) / len(relevant)
    
    def precision_at_k(self, retrieved: List[str], k: int) -> float:
        relevant = self.qrels.get(self.current_query_id, set())
        retrieved_k = set(retrieved[:k])
        if k == 0:
            return 0.0
        return len(retrieved_k & relevant) / k
    
    def mrr(self, rankings: List[List[str]]) -> float:
        reciprocal_ranks = []
        for query_id, retrieved in enumerate(rankings):
            relevant = self.qrels.get(query_id, set())
            for rank, doc_id in enumerate(retrieved, 1):
                if doc_id in relevant:
                    reciprocal_ranks.append(1.0 / rank)
                    break
            else:
                reciprocal_ranks.append(0.0)
        return np.mean(reciprocal_ranks)
    
    def ndcg_at_k(self, retrieved: List[str], relevances: Dict[str, int], k: int) -> float:
        dcg = sum(
            (2 ** relevances.get(doc_id, 0) - 1) / np.log2(i + 1)
            for i, doc_id in enumerate(retrieved[:k], 1)
        )
        ideal_order = sorted(relevances.values(), reverse=True)[:k]
        idcg = sum(
            (2 ** rel - 1) / np.log2(i + 1)
            for i, rel in enumerate(ideal_order, 1)
        )
        return dcg / idcg if idcg > 0 else 0.0
```

### 5.2 消融实验控制

```python
# backend/eval/ablation.py

from dataclasses import dataclass
from enum import Flag, auto

class ModuleFlag(Flag):
    KEYWORD_EXTRACTION = auto()
    VECTOR_RETRIEVAL = auto()
    GRAPH_RETRIEVAL = auto()
    PPR_FILTER = auto()
    RERANKER = auto()
    MMR = auto()
    TIME_AWARE = auto()
    FULL_PIPELINE = (
        KEYWORD_EXTRACTION | VECTOR_RETRIEVAL | GRAPH_RETRIEVAL |
        PPR_FILTER | RERANKER | MMR | TIME_AWARE
    )

@dataclass
class AblationConfig:
    modules: ModuleFlag
    custom_params: Dict = None
    
    def is_enabled(self, module: ModuleFlag) -> bool:
        return bool(self.modules & module)

class AblationController:
    def __init__(self, config: AblationConfig):
        self.config = config
    
    def get_retrieval_params(self) -> Dict:
        params = {
            "enable_keywords": self.config.is_enabled(ModuleFlag.KEYWORD_EXTRACTION),
            "enable_vector": self.config.is_enabled(ModuleFlag.VECTOR_RETRIEVAL),
            "enable_graph": self.config.is_enabled(ModuleFlag.GRAPH_RETRIEVAL),
            "enable_ppr": self.config.is_enabled(ModuleFlag.PPR_FILTER),
            "enable_rerank": self.config.is_enabled(ModuleFlag.RERANKER),
            "enable_mmr": self.config.is_enabled(ModuleFlag.MMR),
            "enable_time_aware": self.config.is_enabled(ModuleFlag.TIME_AWARE),
        }
        if self.config.custom_params:
            params.update(self.config.custom_params)
        return params
```

### 5.3 完整评估流程

```python
# backend/eval/pipeline.py

import asyncio
import json
from pathlib import Path
from datetime import datetime
from typing import List, Dict

from backend.eval.evaluator import RecallEvaluator, RetrievalResult
from backend.eval.ablation import AblationConfig, AblationController, ModuleFlag
from backend.api.routers.rag import retrieve_context

class EvaluationPipeline:
    def __init__(
        self,
        test_queries_path: Path,
        qrels_path: Path,
        output_dir: Path,
    ):
        self.test_queries = self._load_queries(test_queries_path)
        self.qrels = self._load_qrels(qrels_path)
        self.output_dir = output_dir
        self.evaluator = RecallEvaluator(self.qrels)
    
    async def run_ablation_study(
        self,
        kb_id: str,
        configs: List[AblationConfig],
        top_k: int = 30,
        chunk_top_k: int = 10,
    ) -> Dict:
        results = {}
        
        for config in configs:
            config_name = self._config_name(config)
            print(f"Running ablation: {config_name}")
            
            config_results = await self._evaluate_with_config(
                kb_id=kb_id,
                config=config,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
            )
            results[config_name] = config_results
        
        report = self._generate_report(results)
        self._save_results(report)
        return report
    
    async def _evaluate_with_config(
        self,
        kb_id: str,
        config: AblationConfig,
        top_k: int,
        chunk_top_k: int,
    ) -> Dict:
        controller = AblationController(config)
        params = controller.get_retrieval_params()
        
        query_results = []
        
        for query_data in self.test_queries:
            query_id = query_data["query_id"]
            query = query_data["query"]
            
            start_time = time.time()
            try:
                filtered_data, prompt, images, stats = await retrieve_context(
                    query=query,
                    kb_id=kb_id,
                    top_k=top_k,
                    chunk_top_k=chunk_top_k,
                    **params,
                )
                latency = (time.time() - start_time) * 1000
                
                result = RetrievalResult(
                    query_id=query_id,
                    entities=filtered_data.entities,
                    relationships=filtered_data.relationships,
                    chunks=filtered_data.chunks,
                    latency_ms=latency,
                    module_stats=stats,
                )
                query_results.append(result)
            except Exception as e:
                print(f"Query {query_id} failed: {e}")
                query_results.append(None)
        
        return self._compute_metrics(query_results)
    
    def _compute_metrics(self, results: List[RetrievalResult]) -> Dict:
        metrics = {
            "entity": {"recall@5": [], "recall@10": [], "precision@5": [], "mrr": []},
            "relation": {"recall@5": [], "recall@10": [], "precision@5": []},
            "chunk": {"recall@5": [], "recall@10": [], "ndcg@10": [], "mrr": []},
            "latency_ms": [],
        }
        
        for result in results:
            if result is None:
                continue
            
            metrics["latency_ms"].append(result.latency_ms)
            
            for granularity in ["entity", "relation", "chunk"]:
                if granularity == "entity":
                    retrieved = [e["entity_name"] for e in result.entities]
                elif granularity == "relation":
                    retrieved = [r["src_id"] + "->" + r["tgt_id"] for r in result.relationships]
                else:
                    retrieved = [c["id"] for c in result.chunks]
                
                for k in [5, 10]:
                    recall = self.evaluator.recall_at_k(retrieved, k)
                    metrics[granularity][f"recall@{k}"].append(recall)
                
                if granularity != "relation":
                    metrics[granularity]["mrr"].append(
                        self.evaluator.mrr([retrieved])
                    )
        
        return {k: np.mean(v) if isinstance(v, list) else {kk: np.mean(vv) for kk, vv in v.items()} 
                for k, v in metrics.items()}
```

---

## 六、消融实验设计

### 6.1 单模块消融实验

逐一关闭单个模块，评估其对整体效果的影响：

| 实验编号 | 配置 | 目的 |
|----------|------|------|
| E0 | FULL_PIPELINE | 基线 (所有模块开启) |
| E1 | FULL - KEYWORD | 关键词提取的贡献 |
| E2 | FULL - VECTOR | 向量召回的贡献 |
| E3 | FULL - GRAPH | 图召回的贡献 |
| E4 | FULL - PPR | PPR过滤的贡献 |
| E5 | FULL - RERANK | Reranker的贡献 |
| E6 | FULL - MMR | MMR去重的贡献 |
| E7 | FULL - TIME_AWARE | 时序加权的贡献 |

### 6.2 组合消融实验

评估模块间的相互作用：

| 实验编号 | 配置 | 目的 |
|----------|------|------|
| C1 | VECTOR only | 纯向量检索基线 |
| C2 | GRAPH only | 纯图检索基线 |
| C3 | VECTOR + GRAPH | 混合检索无重排序 |
| C4 | VECTOR + GRAPH + RERANK | 混合检索+重排序 |
| C5 | VECTOR + GRAPH + PPR | 混合检索+图过滤 |
| C6 | VECTOR + GRAPH + PPR + RERANK | 混合检索+图过滤+重排序 |
| C7 | FULL - KEYWORD - PPR | 无关键词+无PPR |

### 6.3 超参数敏感性实验

#### 6.3.1 召回参数

```python
RECALL_PARAM_SWEEP = {
    "top_k": [10, 20, 30, 50, 100],
    "chunk_top_k": [5, 10, 20, 30],
    "stage1_multiplier": [1.0, 1.5, 2.0, 3.0],
}
```

#### 6.3.2 PPR参数

```python
PPR_PARAM_SWEEP = {
    "alpha": [0.15, 0.3, 0.5, 0.7],  # 阻尼系数
    "max_iterations": [10, 20, 50],
    "convergence_threshold": [1e-4, 1e-6, 1e-8],
}
```

#### 6.3.3 MMR参数

```python
MMR_PARAM_SWEEP = {
    "lambda_param": [0.3, 0.5, 0.7, 0.9],  # 相关性vs多样性权衡
}
```

### 6.4 实验结果分析模板

```markdown
## 实验结果汇总

### 单模块消融 (Δ = E0 - Ei)

| 模块 | Entity R@10 Δ | Chunk R@10 Δ | MRR Δ | Latency Δ |
|------|---------------|--------------|-------|-----------|
| KEYWORD | +X% | +X% | +X% | +Xms |
| PPR | +X% | +X% | +X% | +Xms |
| RERANK | +X% | +X% | +X% | +Xms |
| MMR | +X% | +X% | +X% | +Xms |

### 模块重要性排序 (按Chunk R@10贡献)

1. PPR: 贡献 X%
2. RERANK: 贡献 X%
3. KEYWORD: 贡献 X%
...

### 建议

- 必须保留模块: [PPR, RERANK]
- 可选模块: [TIME_AWARE]
- 建议优化模块: [KEYWORD]
```

---

## 七、测试执行脚本

### 7.1 完整评估脚本

```python
#!/usr/bin/env python3
# backend/eval/run_evaluation.py

import asyncio
import argparse
from pathlib import Path
from datetime import datetime

from backend.eval.pipeline import EvaluationPipeline
from backend.eval.ablation import AblationConfig, ModuleFlag

async def main():
    parser = argparse.ArgumentParser(description="知识库召回评估")
    parser.add_argument("--kb-id", required=True, help="知识库ID")
    parser.add_argument("--queries", default="datasets/test_queries.jsonl", help="测试查询文件")
    parser.add_argument("--qrels", default="datasets/qrels/", help="相关性标注目录")
    parser.add_argument("--output", default="results/", help="输出目录")
    parser.add_argument("--mode", choices=["full", "ablation", "sensitivity"], default="ablation")
    args = parser.parse_args()
    
    pipeline = EvaluationPipeline(
        test_queries_path=Path(args.queries),
        qrels_path=Path(args.qrels),
        output_dir=Path(args.output),
    )
    
    if args.mode == "ablation":
        configs = [
            AblationConfig(modules=ModuleFlag.FULL_PIPELINE),
            AblationConfig(modules=ModuleFlag.FULL_PIPELINE - ModuleFlag.KEYWORD_EXTRACTION),
            AblationConfig(modules=ModuleFlag.FULL_PIPELINE - ModuleFlag.PPR_FILTER),
            AblationConfig(modules=ModuleFlag.FULL_PIPELINE - ModuleFlag.RERANKER),
            AblationConfig(modules=ModuleFlag.FULL_PIPELINE - ModuleFlag.MMR),
            AblationConfig(modules=ModuleFlag.FULL_PIPELINE - ModuleFlag.TIME_AWARE),
        ]
        results = await pipeline.run_ablation_study(
            kb_id=args.kb_id,
            configs=configs,
        )
    
    print(f"评估完成，结果已保存到: {args.output}")

if __name__ == "__main__":
    asyncio.run(main())
```

### 7.2 快速验证脚本

```python
#!/usr/bin/env python3
# backend/eval/quick_test.py

import asyncio
from backend.api.routers.rag import retrieve_context

async def quick_test():
    test_queries = [
        "什么是核电子学中的时间分辨率?",
        "探测器噪声有哪些来源?",
        "闪烁体和半导体探测器的区别是什么?",
    ]
    
    for query in test_queries:
        print(f"\n{'='*60}")
        print(f"Query: {query}")
        print('='*60)
        
        filtered_data, prompt, images, stats = await retrieve_context(
            query=query,
            kb_id=None,
            top_k=30,
            chunk_top_k=10,
        )
        
        print(f"Entities: {stats['entities']}")
        print(f"Relations: {stats['relationships']}")
        print(f"Chunks: {stats['chunks']}")
        print(f"\nTop 3 Entities:")
        for e in filtered_data.entities[:3]:
            print(f"  - {e.get('entity_name', 'N/A')}")
        print(f"\nTop 3 Chunks:")
        for c in filtered_data.chunks[:3]:
            print(f"  - {c.get('content', '')[:100]}...")

if __name__ == "__main__":
    asyncio.run(quick_test())
```

---

## 八、实施步骤

### Phase 1: 基础设施搭建 (1-2天)

1. **创建评估目录结构**
   ```bash
   mkdir -p backend/eval/{datasets,scripts,results}
   mkdir -p backend/eval/datasets/qrels
   mkdir -p backend/eval/datasets/meta
   ```

2. **实现评估框架**
   - `backend/eval/evaluator.py` - 核心评估器
   - `backend/eval/ablation.py` - 消融控制
   - `backend/eval/pipeline.py` - 评估流水线

3. **修改检索接口**
   - 为 `retrieve_context` 函数添加模块开关参数
   - 支持细粒度控制各模块启用/禁用

### Phase 2: 测试数据准备 (2-3天)

1. **采样测试查询**
   - 从日志中提取真实查询
   - 按查询类型分层采样
   - 补充边界case

2. **相关性标注**
   - 建立标注工具
   - 进行专家标注
   - 多人标注+一致性校验

3. **验证数据质量**
   - 检查标注覆盖率
   - 分析查询难度分布

### Phase 3: 消融实验执行 (2-3天)

1. **单模块消融**
   - 依次关闭各模块运行评估
   - 记录详细指标

2. **组合消融**
   - 执行预设组合配置
   - 分析模块交互效应

3. **参数敏感性分析**
   - 对关键参数进行网格搜索
   - 绘制参数-性能曲线

### Phase 4: 结果分析与报告 (1-2天)

1. **生成评估报告**
   - 各模块贡献度分析
   - 模块交互效应分析
   - 性能-延迟权衡分析

2. **可视化**
   - 绘制消融实验对比图
   - 参数敏感性曲线图
   - 查询难度分布图

3. **优化建议**
   - 基于实验结果提出优化方向
   - 识别冗余模块

---

## 九、扩展方向

### 9.1 在线评估 (A/B Testing)

```python
class OnlineEvaluator:
    async def log_retrieval_event(
        self,
        query: str,
        results: RetrievalResult,
        config: AblationConfig,
        user_feedback: Optional[str] = None,
    ):
        pass
    
    async def compute_online_metrics(self, time_window: str) -> Dict:
        pass
```

### 9.2 自动化标注

- 使用LLM辅助生成相关性标注
- 人工校验LLM标注结果
- 持续扩展测试集

### 9.3 对比学习

- 基于用户反馈优化排序模型
- Fine-tune reranker on domain data

---

## 十、附录

### A. 评估指标实现参考

```python
def compute_all_metrics(retrieved: List[str], relevant: Set[str], k_values: List[int] = [5, 10, 20]):
    metrics = {}
    for k in k_values:
        retrieved_k = set(retrieved[:k])
        metrics[f"recall@{k}"] = len(retrieved_k & relevant) / max(len(relevant), 1)
        metrics[f"precision@{k}"] = len(retrieved_k & relevant) / max(k, 1)
    
    for rank, doc_id in enumerate(retrieved, 1):
        if doc_id in relevant:
            metrics["mrr"] = 1.0 / rank
            break
    else:
        metrics["mrr"] = 0.0
    
    return metrics
```

### B. 查询采样策略

```python
def stratified_sampling(queries: List[Dict], n_per_type: int = 50) -> List[Dict]:
    by_type = defaultdict(list)
    for q in queries:
        by_type[q["query_type"]].append(q)
    
    sampled = []
    for query_type, type_queries in by_type.items():
        sampled.extend(random.sample(type_queries, min(n_per_type, len(type_queries))))
    return sampled
```

### C. 统计显著性检验

```python
from scipy import stats

def paired_t_test(baseline: List[float], treatment: List[float]) -> Tuple[float, float]:
    t_stat, p_value = stats.ttest_rel(baseline, treatment)
    return t_stat, p_value

def is_significant(p_value: float, alpha: float = 0.05) -> bool:
    return p_value < alpha
```
