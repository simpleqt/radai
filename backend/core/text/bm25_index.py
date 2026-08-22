"""
BM25 索引实现

"""

import json
import math
from typing import Dict, List, Tuple

import numpy as np

from backend.config.loggers import get_logger
from backend.core.text.tokenization import (
    get_hanlp_tokenizer_metadata,
    tokenize_fallback_texts,
    tokenize_mixed_text,
    tokenize_mixed_texts,
)

logger = get_logger(__name__)

_SERIALIZATION_PREFIX = b"BM25JSON1\n"
_MAX_SERIALIZED_BYTES = 256 * 1024 * 1024
_SERIALIZATION_VERSION = 2


class BM25Index:
    """
    简化的 BM25 索引实现
    
    使用词频统计和 IDF 计算，支持中文分词
    """
    
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.doc_lengths: List[int] = []
        self.avg_doc_length: float = 0.0
        self.doc_count: int = 0
        self.term_doc_freq: Dict[str, int] = {}
        self.term_positions: Dict[str, List[Tuple[int, int]]] = {}
        self.documents: List[str] = []
        self._tokenized_docs: List[List[str]] = []
        self._term_freqs_per_doc: List[Dict[str, int]] = []
        self.tokenizer_metadata: Dict[str, object] | None = None
        self.serialization_needs_upgrade = False
    
    def _tokenize(self, text: str) -> List[str]:
        """
        使用统一 HanLP 模型处理混合文本，并保留检索兜底词项。
        """
        return tokenize_mixed_text(text)
    
    def build(
        self,
        documents: List[str],
        *,
        require_hanlp: bool = False,
    ) -> None:
        """
        构建 BM25 索引
        """
        try:
            tokenized_documents = tokenize_mixed_texts(
                documents,
                require_hanlp=True,
            )
            self.tokenizer_metadata = get_hanlp_tokenizer_metadata(strict=True)
        except RuntimeError:
            if require_hanlp:
                raise
            logger.warning("[BM25索引] HanLP 不可用，使用检索兜底分词构建")
            tokenized_documents = tokenize_mixed_texts(documents)
            self.tokenizer_metadata = {
                "engine": "fallback",
                "method": "regex+number_unit+cjk_bigram",
                "reason": "hanlp_unavailable",
            }
        self._build_from_tokenized_documents(
            documents,
            tokenized_documents,
        )
        logger.info(f"[BM25索引] 构建完成: 文档数={self.doc_count}, 词项数={len(self.term_doc_freq)}")

    def _build_from_tokenized_documents(
        self,
        documents: List[str],
        tokenized_documents: List[List[str]],
    ) -> None:
        if len(documents) != len(tokenized_documents):
            raise ValueError("BM25 documents and tokenized documents length mismatch")

        self.documents = list(documents)
        self.doc_count = len(documents)
        self.doc_lengths = []
        self._tokenized_docs = []
        self._term_freqs_per_doc = []
        self.term_doc_freq = {}
        self.term_positions = {}

        for doc_idx, tokens in enumerate(tokenized_documents):
            tokens = list(tokens)
            self._tokenized_docs.append(tokens)
            self.doc_lengths.append(len(tokens))

            term_freq_in_doc: Dict[str, int] = {}
            for pos, token in enumerate(tokens):
                if token not in term_freq_in_doc:
                    term_freq_in_doc[token] = 0
                term_freq_in_doc[token] += 1

                if token not in self.term_positions:
                    self.term_positions[token] = []
                self.term_positions[token].append((doc_idx, pos))

            self._term_freqs_per_doc.append(term_freq_in_doc)

            for term in term_freq_in_doc:
                if term not in self.term_doc_freq:
                    self.term_doc_freq[term] = 0
                self.term_doc_freq[term] += 1

        self.avg_doc_length = sum(self.doc_lengths) / self.doc_count if self.doc_count > 0 else 0
    
    def search(self, query: str, top_k: int = 15) -> List[Tuple[int, float]]:
        """
        BM25 搜索
        
        返回: [(doc_idx, score), ...]
        """
        query_tokens = list(set(self._tokenize(query)))
        
        if not query_tokens:
            return []
        
        scores: Dict[int, float] = {}
        
        for token in query_tokens:
            if token not in self.term_doc_freq:
                continue
            
            idf = np.log((self.doc_count - self.term_doc_freq[token] + 0.5) / 
                        (self.term_doc_freq[token] + 0.5) + 1)
            
            # term_positions 每个出现位置一条记录;必须按文档去重,否则同一
            # (词, 文档) 会被累加 tf 次,单项贡献≈tf×BM25(tf),k1 饱和失效、
            # 重复词多的长块被系统性抬高(R4-SV-4)。每个 (词,文档) 只计一次。
            seen_docs: set[int] = set()
            for doc_idx, _ in self.term_positions.get(token, []):
                if doc_idx in seen_docs:
                    continue
                seen_docs.add(doc_idx)
                term_freq_in_doc = self._term_freqs_per_doc[doc_idx] if doc_idx < len(self._term_freqs_per_doc) else {}
                tf = term_freq_in_doc.get(token, 0)
                if tf <= 0:
                    continue
                
                doc_length = self.doc_lengths[doc_idx]
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * doc_length / self.avg_doc_length)
                
                bm25_score = idf * numerator / denominator
                
                if doc_idx not in scores:
                    scores[doc_idx] = 0
                scores[doc_idx] += bm25_score
        
        sorted_results = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
        
        return sorted_results
    
    def serialize(self) -> bytes:
        """使用无代码执行能力的结构化格式序列化索引。

        保存原文与经过验证的分词结果，恢复时只重建倒排结构，不重复运行模型。
        """
        data = {
            "version": _SERIALIZATION_VERSION,
            "k1": self.k1,
            "b": self.b,
            "documents": self.documents,
            "tokenized_documents": self._tokenized_docs,
            "tokenizer": self.tokenizer_metadata,
        }
        payload = json.dumps(
            data,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > _MAX_SERIALIZED_BYTES:
            raise ValueError("BM25 index is too large to serialize safely")
        return _SERIALIZATION_PREFIX + payload
    
    @classmethod
    def deserialize(cls, data: bytes) -> "BM25Index":
        """从版本化 JSON 载荷恢复索引，拒绝旧 pickle 和畸形状态。"""
        if not isinstance(data, bytes):
            raise TypeError("BM25 serialized data must be bytes")
        if len(data) > len(_SERIALIZATION_PREFIX) + _MAX_SERIALIZED_BYTES:
            raise ValueError("BM25 serialized data exceeds the safety limit")
        if not data.startswith(_SERIALIZATION_PREFIX):
            raise ValueError("unsupported or legacy BM25 serialization format")

        try:
            loaded = json.loads(data[len(_SERIALIZATION_PREFIX) :].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid BM25 JSON serialization") from exc
        if not isinstance(loaded, dict) or loaded.get("version") not in {1, 2}:
            raise ValueError("unsupported BM25 serialization version")

        version = loaded["version"]
        k1 = loaded.get("k1")
        b = loaded.get("b")
        documents = loaded.get("documents")
        if (
            isinstance(k1, bool)
            or not isinstance(k1, (int, float))
            or not math.isfinite(float(k1))
            or float(k1) <= 0
        ):
            raise ValueError("invalid BM25 k1 value")
        if (
            isinstance(b, bool)
            or not isinstance(b, (int, float))
            or not math.isfinite(float(b))
            or not 0 <= float(b) <= 1
        ):
            raise ValueError("invalid BM25 b value")
        if not isinstance(documents, list) or not all(
            isinstance(document, str) for document in documents
        ):
            raise ValueError("invalid BM25 documents payload")

        obj = cls(k1=float(k1), b=float(b))
        if version == 1:
            logger.warning(
                "[BM25索引] 检测到旧版缓存，使用快速兜底分词迁移: 文档数=%d",
                len(documents),
            )
            obj._build_from_tokenized_documents(
                documents,
                tokenize_fallback_texts(documents),
            )
            obj.tokenizer_metadata = {
                "engine": "fallback",
                "method": "regex+number_unit+cjk_bigram",
                "reason": "legacy-cache-migration",
            }
            obj.serialization_needs_upgrade = True
            return obj

        tokenizer_metadata = loaded.get("tokenizer")
        if tokenizer_metadata is not None and (
            not isinstance(tokenizer_metadata, dict)
            or not all(isinstance(key, str) for key in tokenizer_metadata)
        ):
            raise ValueError("invalid BM25 tokenizer metadata")

        tokenized_documents = loaded.get("tokenized_documents")
        if (
            not isinstance(tokenized_documents, list)
            or len(tokenized_documents) != len(documents)
            or not all(
                isinstance(tokens, list)
                and all(
                    isinstance(token, str) and 0 < len(token) <= 48
                    for token in tokens
                )
                for tokens in tokenized_documents
            )
        ):
            raise ValueError("invalid BM25 tokenized documents payload")
        obj._build_from_tokenized_documents(documents, tokenized_documents)
        obj.tokenizer_metadata = (
            dict(tokenizer_metadata)
            if tokenizer_metadata is not None
            else None
        )
        return obj
    
    def add_document(self, document: str) -> None:
        """
        增量添加文档
        """
        doc_idx = self.doc_count
        tokens = self._tokenize(document)
        
        self.documents.append(document)
        self._tokenized_docs.append(tokens)
        self.doc_lengths.append(len(tokens))
        
        term_freq_in_doc: Dict[str, int] = {}
        for pos, token in enumerate(tokens):
            if token not in term_freq_in_doc:
                term_freq_in_doc[token] = 0
            term_freq_in_doc[token] += 1
            
            if token not in self.term_positions:
                self.term_positions[token] = []
            self.term_positions[token].append((doc_idx, pos))

        self._term_freqs_per_doc.append(term_freq_in_doc)
        
        for term in term_freq_in_doc:
            if term not in self.term_doc_freq:
                self.term_doc_freq[term] = 0
            self.term_doc_freq[term] += 1
        
        self.doc_count += 1
        self.avg_doc_length = sum(self.doc_lengths) / self.doc_count if self.doc_count > 0 else 0
    
    def get_stats(self) -> Dict[str, int]:
        """
        获取索引统计信息
        """
        return {
            "doc_count": self.doc_count,
            "term_count": len(self.term_doc_freq),
            "avg_doc_length": self.avg_doc_length,
        }
