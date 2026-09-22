"""
BM25 索引 + 检索：将 Schema 元数据构建为可检索的文档集合。

设计要点：
- 使用 jieba 分词处理中文查询
- 每张表作为一个文档，表文档包含表名、中文名、列信息
- BM25 参数 k1/b 可配置
- 预留向量检索扩展接口
"""
from __future__ import annotations

import math
import pickle
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jieba

from app.schema_rag.metadata import SchemaMetadata


@dataclass
class BM25Index:
    """
    BM25 倒排索引。

    为什么不直接用现成库（如 rank_bm25）？
    → 自研更透明，便于教学理解原理
    → 避免额外依赖
    → 生产环境可替换为 rank_bm25 或 Elasticsearch
    """
    # 文档列表（每个元素是表的文档字符串）
    docs: list[str] = field(default_factory=list)
    # 文档对应的表名
    doc_table_names: list[str] = field(default_factory=list)
    # 分词后的文档
    tokenized_docs: list[list[str]] = field(default_factory=list)
    # 倒排索引：{词项: {文档索引: 词频}}
    inverted_index: dict[str, dict[int, int]] = field(default_factory=lambda: defaultdict(dict))
    # 文档长度
    doc_lengths: list[int] = field(default_factory=list)
    # 平均文档长度
    avg_dl: float = 0.0
    # BM25 参数
    k1: float = 1.5
    b: float = 0.75

    def build(self, docs: list[str], table_names: list[str]) -> None:
        """构建 BM25 索引"""
        self.docs = docs
        self.doc_table_names = table_names
        self.tokenized_docs = [list(jieba.cut(doc)) for doc in docs]
        self.doc_lengths = [len(d) for d in self.tokenized_docs]
        self.avg_dl = sum(self.doc_lengths) / len(self.tokenized_docs) if self.tokenized_docs else 0

        # 构建倒排索引
        self.inverted_index = defaultdict(dict)
        for doc_idx, tokens in enumerate(self.tokenized_docs):
            term_freq: dict[str, int] = defaultdict(int)
            for token in tokens:
                term_freq[token] += 1
            for term, freq in term_freq.items():
                self.inverted_index[term][doc_idx] = freq

    def score(self, query: str, top_k: int = 6) -> list[tuple[str, float]]:
        """
        对查询进行 BM25 评分，返回 top_k 个表名及分数。

        BM25 公式：
        score(D, Q) = Σ IDF(qi) * (f(qi,D) * (k1+1)) / (f(qi,D) + k1*(1 - b + b*|D|/avgdl))
        """
        if not self.docs:
            return []

        query_tokens = list(jieba.cut(query))
        n_docs = len(self.docs)
        scores: dict[int, float] = defaultdict(float)

        for term in query_tokens:
            if term not in self.inverted_index:
                continue
            # IDF: log((N - n + 0.5) / (n + 0.5) + 1)
            df = len(self.inverted_index[term])
            idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)

            for doc_idx, tf in self.inverted_index[term].items():
                dl = self.doc_lengths[doc_idx]
                numerator = tf * (self.k1 + 1)
                denominator = tf + self.k1 * (1 - self.b + self.b * dl / self.avg_dl)
                scores[doc_idx] += idf * numerator / denominator

        # 排序取 top_k
        sorted_indices = sorted(scores.keys(), key=lambda i: scores[i], reverse=True)[:top_k]
        return [(self.doc_table_names[i], scores[i]) for i in sorted_indices]


@dataclass
class SchemaIndex:
    """
    Schema 索引：封装 BM25 索引 + 元数据。

    为什么单独封装一层？
    → 未来加入向量索引时，只需在这里扩展，不影响上层
    → 提供统一的 save/load 接口
    """
    metadata: SchemaMetadata | None = None
    bm25: BM25Index = field(default_factory=BM25Index)
    # 向量检索预留（Phase 2）
    doc_embeddings: list[list[float]] = field(default_factory=list)
    embedding_dim: int = 0

    @property
    def has_vector(self) -> bool:
        return len(self.doc_embeddings) > 0

    def build(self, metadata: SchemaMetadata, k1: float = 1.5, b: float = 0.75) -> None:
        """从元数据构建索引"""
        self.metadata = metadata
        docs: list[str] = []
        table_names: list[str] = []
        for table in metadata.tables.values():
            docs.append(table.to_doc())
            table_names.append(table.name)
        self.bm25.k1 = k1
        self.bm25.b = b
        self.bm25.build(docs, table_names)

    def vector_search(
        self, query_embedding: list[float], top_k: int = 6
    ) -> list[tuple[str, float]]:
        """
        向量检索（Phase 2 实现）。
        当前返回空列表。
        """
        if not self.has_vector:
            return []
        # 余弦相似度
        scores: list[tuple[str, float]] = []
        for i, emb in enumerate(self.doc_embeddings):
            sim = _cosine_similarity(query_embedding, emb)
            scores.append((self.bm25.doc_table_names[i], sim))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]

    def save(self, path: str | Path) -> None:
        """持久化索引"""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as f:
            pickle.dump(self, f)

    @staticmethod
    def load(path: str | Path) -> SchemaIndex:
        """加载索引"""
        with open(path, "rb") as f:
            return pickle.load(f)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度"""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
