# AnonyRAG数据集导入与召回评估方案

> **状态**：📋 **规划文档** — `backend/eval/anonyrag/` 目录当前**未实现**，请勿按本文直接运行命令。
> 现行评测请使用 [eval_framework.md](./eval_framework.md)（HotpotQA / Paper KB / GraphRAG-Bench）。
> 若要把 AnonyRAG 真正接入当前仓库，应优先复用 `POST /api/v1/uploads/batch` + `/api/v1/uploads/ingest` 或 `PendingIngestUseCase`，并按 KB 的 `ingestion_backend` 进入 legacy LightRAG 或 self_ingestion；本文示例里的 `backend/eval/anonyrag/import_anonyrag_dataset.py`、直接 `rag.lightrag.ainsert()` 仍是草案。

## 一、数据集结构分析

### 1.1 数据集概述

| 文件 | 语言 | 内容 | 记录数 |
|------|------|------|--------|
| `annoyrag_chs_qa.csv` | 中文 | QA问答对 | ~6400条 |
| `annoyrag_chs_text_chunks.csv` | 中文 | 文本块（知识库内容） | 若干 |
| `annoyrag_eng_qa.csv` | 英文 | QA问答对 | 若干 |
| `annoyrag_eng_text_chunks.csv` | 英文 | 文本块 | 若干 |

### 1.2 QA数据格式

```csv
"question","answer","relations","entities","query_type"
```

**字段说明**:
- `question`: 包含匿名化实体的查询，如 `[PERSON#741]建议下被派往[LOCATION#101]`
- `answer`: 答案，包含实体还原信息，如 `PERSON#741——范仲淹`
- `relations`: 关系类型
- `entities`: 相关实体列表
- `query_type`: 查询类型
  - `Anonymity Reversion`: 匿名化反转任务
  - `Multiple Choice`: 多选题

### 1.3 文本块格式

```csv
"idx","title","chunk"
```

**特点**: 文本块中实体已被匿名化为 `[PERSON#xxx]` 和 `[LOCATION#xxx]`

---

## 二、评估目标

### 2.1 核心任务

**匿名化实体推理**: 给定包含匿名化实体的查询，检索相关文本块，并推理还原匿名实体的真实身份。

### 2.2 评估维度

| 维度 | 说明 | 指标 |
|------|------|------|
| **召回能力** | 能否检索到包含目标实体的文本块 | Recall@K, MRR |
| **实体推理** | 能否正确推理匿名实体的真实身份 | Entity Accuracy |
| **关系识别** | 能否识别实体间的关系 | Relation F1 |
| **跨粒度** | 实体/关系/文本块的综合召回 | Multi-Granularity Recall |

---

## 三、数据导入实现

### 3.1 导入脚本

```python
#!/usr/bin/env python3
# backend/eval/anonyrag/import_anonyrag_dataset.py

import asyncio
import csv
import json
import re
from pathlib import Path
from typing import List, Dict, Set, Tuple
from dataclasses import dataclass
from datetime import datetime

from backend.config.loggers import get_logger
from backend.rag.service import get_rag_service
from backend.db.connection import get_async_session_factory
from backend.rag.knowledge_base.manager import get_kb_manager

logger = get_logger(__name__)

@dataclass
class AnonyRAGQuestion:
    question_id: str
    question: str
    answer: str
    relations: str
    entities: List[str]
    query_type: str
    anonymized_entities: Dict[str, str]  # {PERSON#741: "范仲淹", ...}
    language: str

@dataclass
class AnonyRAGChunk:
    chunk_id: str
    title: str
    content: str
    anonymized_entities: Set[str]  # {"PERSON#741", "LOCATION#101", ...}
    language: str

class AnonyRAGImporter:
    """
    AnonyRAG数据集导入器
    
    支持:
    1. 导入文本块到知识库
    2. 构建QA测试集
    3. 建立实体映射表
    """
    
    ANONY_PATTERN = re.compile(r'\[(PERSON|LOCATION)#(\d+)\]')
    
    def __init__(self, kb_id: str, language: str = "chs"):
        self.kb_id = kb_id
        self.language = language
    
    def parse_qa_csv(self, csv_path: Path) -> List[AnonyRAGQuestion]:
        """解析QA CSV文件"""
        questions = []
        
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                question_text = row.get("question", "")
                answer_text = row.get("answer", "")
                
                anonymized_entities = self._parse_answer_entities(answer_text)
                
                entities_str = row.get("entities", "")
                entities = [e.strip() for e in entities_str.split("、") if e.strip()]
                
                questions.append(AnonyRAGQuestion(
                    question_id=f"anonyrag_{self.language}_{i:04d}",
                    question=question_text,
                    answer=answer_text,
                    relations=row.get("relations", ""),
                    entities=entities,
                    query_type=row.get("query_type", "Anonymity Reversion"),
                    anonymized_entities=anonymized_entities,
                    language=self.language,
                ))
        
        logger.info(f"解析QA文件: {csv_path}, 共 {len(questions)} 条")
        return questions
    
    def parse_chunks_csv(self, csv_path: Path) -> List[AnonyRAGChunk]:
        """解析文本块CSV文件"""
        chunks = []
        
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                content = row.get("chunk", "")
                anonymized = set(self.ANONY_PATTERN.findall(content))
                anonymized_str = {f"{typ}#{idx}" for typ, idx in anonymized}
                
                chunks.append(AnonyRAGChunk(
                    chunk_id=f"chunk_{self.language}_{row.get('idx', len(chunks))}",
                    title=row.get("title", ""),
                    content=content,
                    anonymized_entities=anonymized_str,
                    language=self.language,
                ))
        
        logger.info(f"解析文本块文件: {csv_path}, 共 {len(chunks)} 条")
        return chunks
    
    def _parse_answer_entities(self, answer: str) -> Dict[str, str]:
        """
        从答案中解析匿名化实体映射
        
        输入: "PERSON#741——范仲淹\nLOCATION#101——龙虎山"
        输出: {"PERSON#741": "范仲淹", "LOCATION#101": "龙虎山"}
        """
        mapping = {}
        pattern = re.compile(r'((?:PERSON|LOCATION)#\d+)——([^\\\n]+)')
        
        for match in pattern.finditer(answer):
            anon_id = match.group(1)
            real_name = match.group(2).strip()
            mapping[anon_id] = real_name
        
        return mapping
    
    async def import_chunks_to_kb(
        self,
        chunks: List[AnonyRAGChunk],
        batch_size: int = 10,
    ) -> Dict:
        """
        将文本块导入知识库
        
        策略:
        1. 将文本块合并为文档
        2. 通过RAG服务入库
        """
        rag_service = get_rag_service()
        rag = await rag_service._get_rag_instance(self.kb_id)
        
        if not rag or not rag.lightrag:
            raise RuntimeError(f"无法获取RAG实例: {self.kb_id}")
        
        imported = 0
        errors = []
        
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            
            try:
                for chunk in batch:
                    doc_content = f"# {chunk.title}\n\n{chunk.content}"
                    
                    await rag.lightrag.ainsert(doc_content)
                    imported += 1
                    
                    if imported % 50 == 0:
                        logger.info(f"已导入 {imported}/{len(chunks)} 个文本块")
                        
            except Exception as e:
                logger.error(f"导入批次 {i//batch_size} 失败: {e}")
                errors.append({
                    "batch": i // batch_size,
                    "error": str(e),
                })
        
        return {
            "total": len(chunks),
            "imported": imported,
            "errors": errors,
        }
    
    def save_test_dataset(
        self,
        questions: List[AnonyRAGQuestion],
        output_dir: Path,
    ) -> None:
        """
        保存测试数据集
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        
        queries_path = output_dir / "test_queries.jsonl"
        with open(queries_path, "w", encoding="utf-8") as f:
            for q in questions:
                f.write(json.dumps({
                    "query_id": q.question_id,
                    "query": q.question,
                    "query_type": q.query_type,
                    "expected_entities": q.anonymized_entities,
                    "all_entities": q.entities,
                    "language": q.language,
                }, ensure_ascii=False) + "\n")
        
        qrels_path = output_dir / "qrels" / "entity_qrels.jsonl"
        qrels_path.parent.mkdir(parents=True, exist_ok=True)
        with open(qrels_path, "w", encoding="utf-8") as f:
            for q in questions:
                for anon_id, real_name in q.anonymized_entities.items():
                    f.write(json.dumps({
                        "query_id": q.question_id,
                        "doc_id": real_name,
                        "relevance": 3,
                        "anonymized_id": anon_id,
                    }, ensure_ascii=False) + "\n")
        
        ground_truth_path = output_dir / "ground_truth.jsonl"
        with open(ground_truth_path, "w", encoding="utf-8") as f:
            for q in questions:
                f.write(json.dumps({
                    "query_id": q.question_id,
                    "answer": q.answer,
                    "entity_mapping": q.anonymized_entities,
                }, ensure_ascii=False) + "\n")
        
        logger.info(f"测试数据集已保存到: {output_dir}")

async def import_all_datasets(
    kb_id: str,
    data_dir: Path,
    output_dir: Path,
    import_chunks: bool = True,
):
    """
    导入所有数据集
    """
    importer_chs = AnonyRAGImporter(kb_id, "chs")
    importer_eng = AnonyRAGImporter(kb_id, "eng")
    
    all_questions = []
    
    chs_qa_path = data_dir / "annoyrag_chs_qa.csv"
    if chs_qa_path.exists():
        questions = importer_chs.parse_qa_csv(chs_qa_path)
        all_questions.extend(questions)
    
    eng_qa_path = data_dir / "annoyrag_eng_qa.csv"
    if eng_qa_path.exists():
        questions = importer_eng.parse_qa_csv(eng_qa_path)
        all_questions.extend(questions)
    
    if import_chunks:
        chs_chunks_path = data_dir / "annoyrag_chs_text_chunks.csv"
        if chs_chunks_path.exists():
            chunks = importer_chs.parse_chunks_csv(chs_chunks_path)
            result = await importer_chs.import_chunks_to_kb(chunks)
            logger.info(f"中文文本块导入结果: {result}")
        
        eng_chunks_path = data_dir / "annoyrag_eng_text_chunks.csv"
        if eng_chunks_path.exists():
            chunks = importer_eng.parse_chunks_csv(eng_chunks_path)
            result = await importer_eng.import_chunks_to_kb(chunks)
            logger.info(f"英文文本块导入结果: {result}")
    
    importer_chs.save_test_dataset(all_questions, output_dir)
    
    return {
        "total_questions": len(all_questions),
        "questions_by_lang": {
            "chs": len([q for q in all_questions if q.language == "chs"]),
            "eng": len([q for q in all_questions if q.language == "eng"]),
        },
    }

async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="导入AnonyRAG数据集")
    parser.add_argument("--kb-id", required=True, help="目标知识库ID")
    parser.add_argument("--data-dir", default="eval/datasets", help="数据集目录")
    parser.add_argument("--output-dir", default="eval/anonyrag/test_data", help="输出目录")
    parser.add_argument("--skip-chunks", action="store_true", help="跳过文本块导入")
    args = parser.parse_args()
    
    result = await import_all_datasets(
        kb_id=args.kb_id,
        data_dir=Path(args.data_dir),
        output_dir=Path(args.output_dir),
        import_chunks=not args.skip_chunks,
    )
    
    print(f"\n导入完成: {result}")

__ ==if __name "__main__":
    asyncio.run(main())
```

---

## 四、召回评估实现

### 4.1 评估器

```python
# backend/eval/anonyrag/anonyrag_evaluator.py

import asyncio
import json
import re
from pathlib import Path
from typing import List, Dict, Set, Tuple
from dataclasses import dataclass
from collections import defaultdict

from backend.config.loggers import get_logger
from backend.api.routers.rag import retrieve_context

logger = get_logger(__name__)

@dataclass
class RetrievalResult:
    query_id: str
    query: str
    retrieved_chunks: List[Dict]
    retrieved_entities: List[Dict]
    retrieved_relations: List[Dict]
    latency_ms: float
    stats: Dict

@dataclass
class EvalMetrics:
    query_id: str
    chunk_recall: float
    entity_recall: float
    entity_inference_accuracy: float
    mrr: float
    ndcg: float

class AnonyRAGEvaluator:
    """
    AnonyRAG召回评估器
    
    评估维度:
    1. 文本块召回: 是否检索到包含目标匿名实体的chunk
    2. 实体召回: 是否检索到正确的实体
    3. 实体推理: 是否能从检索结果中推理出正确的实体映射
    """
    
    ANONY_PATTERN = re.compile(r'\[(PERSON|LOCATION)#(\d+)\]')
    
    def __init__(self, test_data_dir: Path):
        self.test_data_dir = test_data_dir
        self.queries = self._load_queries()
        self.ground_truth = self._load_ground_truth()
    
    def _load_queries(self) -> Dict[str, Dict]:
        queries = {}
        queries_path = self.test_data_dir / "test_queries.jsonl"
        
        if queries_path.exists():
            with open(queries_path, encoding="utf-8") as f:
                for line in f:
                    data = json.loads(line)
                    queries[data["query_id"]] = data
        
        return queries
    
    def _load_ground_truth(self) -> Dict[str, Dict]:
        gt = {}
        gt_path = self.test_data_dir / "ground_truth.jsonl"
        
        if gt_path.exists():
            with open(gt_path, encoding="utf-8") as f:
                for line in f:
                    data = json.loads(line)
                    gt[data["query_id"]] = data
        
        return gt
    
    async def retrieve_for_query(
        self,
        query_id: str,
        kb_id: str,
        top_k: int = 30,
        chunk_top_k: int = 20,
    ) -> RetrievalResult:
        """
        执行单次检索
        """
        query_data = self.queries.get(query_id)
        if not query_data:
            raise ValueError(f"未知查询ID: {query_id}")
        
        query_text = query_data["query"]
        
        import time
        start = time.time()
        
        try:
            filtered_data, prompt, images, stats = await retrieve_context(
                query=query_text,
                kb_id=kb_id,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
            )
            
            latency = (time.time() - start) * 1000
            
            return RetrievalResult(
                query_id=query_id,
                query=query_text,
                retrieved_chunks=filtered_data.chunks if filtered_data else [],
                retrieved_entities=filtered_data.entities if filtered_data else [],
                retrieved_relations=filtered_data.relationships if filtered_data else [],
                latency_ms=latency,
                stats=stats,
            )
            
        except Exception as e:
            logger.error(f"检索失败: {query_id}, {e}")
            return RetrievalResult(
                query_id=query_id,
                query=query_text,
                retrieved_chunks=[],
                retrieved_entities=[],
                retrieved_relations=[],
                latency_ms=0,
                stats={},
            )
    
    def compute_chunk_recall(
        self,
        result: RetrievalResult,
        k: int = 10,
    ) -> float:
        """
        计算文本块召回率
        
        判断: 检索到的chunk中是否包含查询中的匿名实体
        """
        query_data = self.queries.get(result.query_id, {})
        expected_entities = query_data.get("expected_entities", {})
        
        if not expected_entities:
            return 0.0
        
        query_anony_ids = set(expected_entities.keys())
        
        found_chunks = 0
        for chunk in result.retrieved_chunks[:k]:
            chunk_content = chunk.get("content", "")
            chunk_anony = set(self.ANONY_PATTERN.findall(chunk_content))
            chunk_anony_str = {f"{typ}#{idx}" for typ, idx in chunk_anony}
            
            if query_anony_ids & chunk_anony_str:
                found_chunks += 1
        
        return found_chunks / min(k, len(result.retrieved_chunks)) if result.retrieved_chunks else 0.0
    
    def compute_entity_recall(
        self,
        result: RetrievalResult,
        k: int = 10,
    ) -> float:
        """
        计算实体召回率
        
        判断: 检索到的实体中是否包含预期的真实实体
        """
        gt = self.ground_truth.get(result.query_id, {})
        expected_mapping = gt.get("entity_mapping", {})
        expected_real_names = set(expected_mapping.values())
        
        if not expected_real_names:
            return 0.0
        
        retrieved_names = set()
        for entity in result.retrieved_entities[:k]:
            name = entity.get("entity_name", "")
            if name:
                retrieved_names.add(name)
        
        recall = len(expected_real_names & retrieved_names) / len(expected_real_names)
        return recall
    
    def compute_entity_inference_accuracy(
        self,
        result: RetrievalResult,
        llm_func=None,
    ) -> Tuple[float, Dict]:
        """
        计算实体推理准确率
        
        从检索结果中推理匿名实体的真实身份
        """
        gt = self.ground_truth.get(result.query_id, {})
        expected_mapping = gt.get("entity_mapping", {})
        
        if not expected_mapping:
            return 0.0, {}
        
        if llm_func:
            inferred = self._llm_inference(result, llm_func)
        else:
            inferred = self._heuristic_inference(result)
        
        correct = 0
        total = len(expected_mapping)
        details = {}
        
        for anon_id, real_name in expected_mapping.items():
            inferred_name = inferred.get(anon_id)
            is_correct = inferred_name and inferred_name.strip() == real_name.strip()
            if is_correct:
                correct += 1
            details[anon_id] = {
                "expected": real_name,
                "inferred": inferred_name,
                "correct": is_correct,
            }
        
        accuracy = correct / total if total > 0 else 0.0
        return accuracy, details
    
    def _heuristic_inference(self, result: RetrievalResult) -> Dict[str, str]:
        """
        基于启发式规则的实体推理
        
        从检索到的实体和文本块中提取可能的真实实体
        """
        inferred = {}
        
        query_anony = set(self.ANONY_PATTERN.findall(result.query))
        query_anony_str = {f"{typ}#{idx}" for typ, idx in query_anony}
        
        for entity in result.retrieved_entities:
            name = entity.get("entity_name", "")
            desc = entity.get("description", "")
            
            for anon_id in query_anony_str:
                if anon_id not in inferred:
                    if name and len(name) > 1:
                        typ = anon_id.split("#")[0]
                        if typ == "PERSON" and self._is_person_name(name):
                            inferred[anon_id] = name
                        elif typ == "LOCATION" and self._is_location_name(name):
                            inferred[anon_id] = name
        
        return inferred
    
    def _llm_inference(self, result: RetrievalResult, llm_func) -> Dict[str, str]:
        """
        使用LLM进行实体推理
        """
        prompt = f"""根据以下检索结果，推断查询中匿名化实体的真实身份。

查询: {result.query}

检索到的实体:
{self._format_entities(result.retrieved_entities[:10])}

检索到的文本块:
{self._format_chunks(result.retrieved_chunks[:5])}

请推断每个 [PERSON#xxx] 和 [LOCATION#xxx] 的真实名称，返回JSON格式:
{{
    "PERSON#xxx": "真实人名",
    "LOCATION#xxx": "真实地名"
}}
"""
        try:
            response = llm_func(prompt)
            if asyncio.iscoroutine(response):
                response = asyncio.run(response)
            
            json_match = re.search(r'\{[\s\S]*\}', response)
            if json_match:
                return json.loads(json_match.group())
        except Exception as e:
            logger.warning(f"LLM推理失败: {e}")
        
        return {}
    
    def _is_person_name(self, name: str) -> bool:
        return len(name) >= 2 and len(name) <= 4 and not any(c.isdigit() for c in name)
    
    def _is_location_name(self, name: str) -> bool:
        keywords = ["山", "河", "城", "村", "镇", "府", "寺", "庙", "宫", "院", "州", "县"]
        return any(kw in name for kw in keywords) or len(name) >= 2
    
    def _format_entities(self, entities: List[Dict]) -> str:
        lines = []
        for e in entities:
            lines.append(f"- {e.get('entity_name', '')}: {e.get('description', '')[:100]}")
        return '\n'.join(lines)
    
    def _format_chunks(self, chunks: List[Dict]) -> str:
        lines = []
        for i, c in enumerate(chunks):
            lines.append(f"[{i}] {c.get('content', '')[:300]}")
        return '\n'.join(lines)
    
    async def evaluate_all(
        self,
        kb_id: str,
        top_k: int = 30,
        chunk_top_k: int = 20,
        sample_size: int = None,
        llm_func=None,
    ) -> Dict:
        """
        执行完整评估
        """
        query_ids = list(self.queries.keys())
        if sample_size and sample_size < len(query_ids):
            import random
            query_ids = random.sample(query_ids, sample_size)
        
        results = []
        all_metrics = []
        
        for i, query_id in enumerate(query_ids):
            logger.info(f"评估 {i+1}/{len(query_ids)}: {query_id}")
            
            result = await self.retrieve_for_query(
                query_id, kb_id, top_k, chunk_top_k
            )
            results.append(result)
            
            chunk_recall = self.compute_chunk_recall(result, k=10)
            entity_recall = self.compute_entity_recall(result, k=10)
            inference_acc, inference_details = self.compute_entity_inference_accuracy(
                result, llm_func
            )
            
            metrics = EvalMetrics(
                query_id=query_id,
                chunk_recall=chunk_recall,
                entity_recall=entity_recall,
                entity_inference_accuracy=inference_acc,
                mrr=0.0,
                ndcg=0.0,
            )
            all_metrics.append(metrics)
        
        summary = self._aggregate_metrics(all_metrics)
        
        return {
            "summary": summary,
            "detailed_results": results,
            "detailed_metrics": all_metrics,
        }
    
    def _aggregate_metrics(self, metrics: List[EvalMetrics]) -> Dict:
        import numpy as np
        
        return {
            "total_queries": len(metrics),
            "avg_chunk_recall": np.mean([m.chunk_recall for m in metrics]),
            "avg_entity_recall": np.mean([m.entity_recall for m in metrics]),
            "avg_inference_accuracy": np.mean([m.entity_inference_accuracy for m in metrics]),
            "metrics_by_type": self._group_by_query_type(metrics),
        }
    
    def _group_by_query_type(self, metrics: List[EvalMetrics]) -> Dict:
        import numpy as np
        
        by_type = defaultdict(list)
        for m in metrics:
            q_data = self.queries.get(m.query_id, {})
            q_type = q_data.get("query_type", "unknown")
            by_type[q_type].append(m)
        
        result = {}
        for q_type, type_metrics in by_type.items():
            result[q_type] = {
                "count": len(type_metrics),
                "avg_chunk_recall": np.mean([m.chunk_recall for m in type_metrics]),
                "avg_entity_recall": np.mean([m.entity_recall for m in type_metrics]),
                "avg_inference_accuracy": np.mean([m.entity_inference_accuracy for m in type_metrics]),
            }
        
        return result
    
    def save_results(self, results: Dict, output_path: Path):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        summary = results["summary"]
        detailed = []
        for result, metrics in zip(results["detailed_results"], results["detailed_metrics"]):
            detailed.append({
                "query_id": metrics.query_id,
                "query": result.query,
                "chunk_recall": metrics.chunk_recall,
                "entity_recall": metrics.entity_recall,
                "inference_accuracy": metrics.entity_inference_accuracy,
                "latency_ms": result.latency_ms,
                "retrieved_chunks": len(result.retrieved_chunks),
                "retrieved_entities": len(result.retrieved_entities),
            })
        
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump({
                "summary": summary,
                "detailed": detailed,
            }, f, ensure_ascii=False, indent=2)
        
        logger.info(f"评估结果已保存: {output_path}")

async def run_evaluation(
    kb_id: str,
    test_data_dir: str,
    output_path: str,
    sample_size: int = None,
):
    evaluator = AnonyRAGEvaluator(Path(test_data_dir))
    
    results = await evaluator.evaluate_all(
        kb_id=kb_id,
        sample_size=sample_size,
    )
    
    evaluator.save_results(results, Path(output_path))
    
    print("\n=== 评估结果 ===")
    summary = results["summary"]
    print(f"总查询数: {summary['total_queries']}")
    print(f"平均文本块召回率: {summary['avg_chunk_recall']:.4f}")
    print(f"平均实体召回率: {summary['avg_entity_recall']:.4f}")
    print(f"平均推理准确率: {summary['avg_inference_accuracy']:.4f}")
    
    print("\n按查询类型:")
    for q_type, stats in summary.get("metrics_by_type", {}).items():
        print(f"  {q_type}:")
        print(f"    数量: {stats['count']}")
        print(f"    文本块召回: {stats['avg_chunk_recall']:.4f}")
        print(f"    实体召回: {stats['avg_entity_recall']:.4f}")
        print(f"    推理准确率: {stats['avg_inference_accuracy']:.4f}")

async def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="AnonyRAG召回评估")
    parser.add_argument("--kb-id", required=True, help="知识库ID")
    parser.add_argument("--test-data", default="eval/anonyrag/test_data", help="测试数据目录")
    parser.add_argument("--output", default="eval/anonyrag/results/eval_results.json", help="输出路径")
    parser.add_argument("--sample", type=int, default=None, help="采样数量")
    args = parser.parse_args()
    
    await run_evaluation(
        kb_id=args.kb_id,
        test_data_dir=args.test_data,
        output_path=args.output,
        sample_size=args.sample,
    )

if __name__ == "__main__":
    asyncio.run(main())
```

---

## 五、执行流程

### 5.1 完整执行命令

```bash
# 1. 激活环境
source /home/inspur/myenv/langchian_project/venv314/bin/activate

# 2. 导入数据集到知识库
python backend/eval/anonyrag/import_anonyrag_dataset.py \
    --kb-id c056bf59-6091-45ed-98cb-e2e860580a69 \
    --data-dir eval/datasets \
    --output-dir eval/anonyrag/test_data

# 3. 执行评估
python backend/eval/anonyrag/anonyrag_evaluator.py \
    --kb-id c056bf59-6091-45ed-98cb-e2e860580a69 \
    --test-data eval/anonyrag/test_data \
    --output eval/anonyrag/results/eval_results.json \
    --sample 100
```

### 5.2 分步执行

```bash
# 仅解析QA数据，不导入文本块
python backend/eval/anonyrag/import_anonyrag_dataset.py \
    --kb-id YOUR_KB_ID \
    --skip-chunks

# 仅评估，使用已有测试数据
python backend/eval/anonyrag/anonyrag_evaluator.py \
    --kb-id YOUR_KB_ID \
    --sample 50
```

---

## 六、评估指标说明

### 6.1 核心指标

| 指标 | 计算方式 | 说明 |
|------|----------|------|
| **Chunk Recall@K** | 命中目标实体的chunk数 / K | 检索到的文本块是否包含目标匿名实体 |
| **Entity Recall@K** | 正确实体数 / 预期实体数 | 检索到的实体是否匹配预期 |
| **Inference Accuracy** | 正确推理数 / 总匿名实体数 | 能否从检索结果推理出正确的实体映射 |

### 6.2 评估报告示例

```json
{
  "summary": {
    "total_queries": 100,
    "avg_chunk_recall": 0.7234,
    "avg_entity_recall": 0.6512,
    "avg_inference_accuracy": 0.5891,
    "metrics_by_type": {
      "Anonymity Reversion": {
        "count": 85,
        "avg_chunk_recall": 0.7456,
        "avg_entity_recall": 0.6734,
        "avg_inference_accuracy": 0.6123
      },
      "Multiple Choice": {
        "count": 15,
        "avg_chunk_recall": 0.5678,
        "avg_entity_recall": 0.5123,
        "avg_inference_accuracy": 0.4567
      }
    }
  }
}
```

---

## 七、注意事项

### 7.1 数据集特点

1. **实体已匿名化**: 文本中的实体被替换为 `[PERSON#xxx]` 和 `[LOCATION#xxx]`
2. **答案包含映射**: 答案中包含匿名实体到真实实体的映射
3. **两种查询类型**: Anonymity Reversion (推理任务) 和 Multiple Choice (选择题)

### 7.2 评估适配

1. **召回目标**: 检索包含目标匿名实体的文本块
2. **推理任务**: 从检索结果推断匿名实体的真实身份
3. **多语言支持**: 同时支持中文和英文数据集

### 7.3 可选增强

1. **LLM辅助推理**: 使用LLM从检索结果推理实体映射
2. **消融实验**: 关闭不同模块评估对推理准确率的影响
3. **跨语言评估**: 评估模型在混合语言场景的表现
