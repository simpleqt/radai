"""
BM25 索引实现

"""

import pickle
import re
from typing import Dict, List, Tuple

import numpy as np

from backend.config.loggers import get_logger

logger = get_logger(__name__)


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
    
    def _tokenize(self, text: str) -> List[str]:
        """
        分词函数 - 支持中英文混合分词
        使用滑动窗口分词策略，支持部分匹配
        """
        words = []
        
        chinese_pattern = re.compile(r'[\u4e00-\u9fff]+')
        for match in chinese_pattern.finditer(text):
            chinese_text = match.group()
            # 保留单字，改善短术语和缩写召回。
            for i in range(len(chinese_text)):
                for length in range(1, min(8, len(chinese_text) - i + 1)):
                    word = chinese_text[i:i+length]
                    words.append(word)
        
        english_pattern = re.compile(r'[a-zA-Z]{2,}')
        for match in english_pattern.finditer(text):
            words.append(match.group().lower())
        
        number_pattern = re.compile(r'\d+(?:\.\d+)?(?:\s*(?:keV|MeV|GeV|kev|mev|gev|ns|μs|ms|s|mm|cm|m))?')
        for match in number_pattern.finditer(text):
            words.append(match.group().lower())
        
        return words
    
    def build(self, documents: List[str]) -> None:
        """
        构建 BM25 索引
        """
        self.documents = documents
        self.doc_count = len(documents)
        self.doc_lengths = []
        self._tokenized_docs = []
        self._term_freqs_per_doc = []
        self.term_doc_freq = {}
        self.term_positions = {}
        
        for doc_idx, doc in enumerate(documents):
            tokens = self._tokenize(doc)
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
        
        logger.info(f"[BM25索引] 构建完成: 文档数={self.doc_count}, 词项数={len(self.term_doc_freq)}")
    
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
            
            for doc_idx, _ in self.term_positions.get(token, []):
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
        """
        序列化索引
        """
        data = {
            "k1": self.k1,
            "b": self.b,
            "doc_lengths": self.doc_lengths,
            "avg_doc_length": self.avg_doc_length,
            "doc_count": self.doc_count,
            "term_doc_freq": self.term_doc_freq,
            "term_positions": self.term_positions,
            "documents": self.documents,
            "_tokenized_docs": self._tokenized_docs,
            "_term_freqs_per_doc": self._term_freqs_per_doc,
        }
        return pickle.dumps(data)
    
    @classmethod
    def deserialize(cls, data: bytes) -> "BM25Index":
        """
        反序列化索引
        """
        obj = cls()
        loaded = pickle.loads(data)
        obj.k1 = loaded["k1"]
        obj.b = loaded["b"]
        obj.doc_lengths = loaded["doc_lengths"]
        obj.avg_doc_length = loaded["avg_doc_length"]
        obj.doc_count = loaded["doc_count"]
        obj.term_doc_freq = loaded["term_doc_freq"]
        obj.term_positions = loaded["term_positions"]
        obj.documents = loaded["documents"]
        obj._tokenized_docs = loaded.get("_tokenized_docs", [])
        obj._term_freqs_per_doc = loaded.get("_term_freqs_per_doc", [])
        if not obj._term_freqs_per_doc and obj._tokenized_docs:
            obj._term_freqs_per_doc = []
            for tokens in obj._tokenized_docs:
                freqs: Dict[str, int] = {}
                for token in tokens:
                    freqs[token] = freqs.get(token, 0) + 1
                obj._term_freqs_per_doc.append(freqs)
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
