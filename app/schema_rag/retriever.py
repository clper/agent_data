"""
Schema 检索器：BM25 + FK 图扩展，返回候选表集合。

设计要点：
- BM25 单路检索（MVP）
- FK 图扩展：检索到的表的关联表也纳入上下文
- 预留向量检索 + RRF 融合接口（Phase 2）
- 返回的表列表同时作为"提示面"和"授权面"的依据
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.schema_rag.indexer import SchemaIndex
from app.schema_rag.metadata import SchemaMetadata


@dataclass
class RetrievalResult:
    """检索结果"""
    table_names: list[str]             # 候选表名列表
    scores: dict[str, float]           # {表名: 检索分数}
    expanded_tables: list[str] = field(default_factory=list)  # FK 扩展的表


class SchemaRetriever:
    """
    Schema 检索器。

    检索流程：
    1. BM25 检索 top_k 张表
    2. FK 图扩展：加入直接关联的表
    3. 去重后返回
    """

    def __init__(self, index: SchemaIndex, top_k: int = 6):
        self.index = index
        self.top_k = top_k

    def retrieve(self, query: str, top_k: int | None = None) -> RetrievalResult:
        """
        检索与查询相关的表。

        Args:
            query: 用户查询（已改写）
            top_k: 返回的最大表数，None 使用默认值

        Returns:
            RetrievalResult: 包含候选表名、分数、扩展表
        """
        k = top_k or self.top_k
        metadata = self.index.metadata
        if not metadata:
            return RetrievalResult(table_names=[], scores={})

        # 1. BM25 检索
        bm25_results = self.index.bm25.score(query, top_k=k)
        scores: dict[str, float] = {name: score for name, score in bm25_results}

        # 2. FK 图扩展
        expanded: list[str] = []
        for table_name in list(scores.keys()):
            neighbors = metadata.get_fk_neighbors(table_name)
            for neighbor in neighbors:
                if neighbor not in scores and neighbor in metadata.tables:
                    expanded.append(neighbor)

        # 3. 合并结果（原始检索在前，扩展表在后）
        all_tables = list(scores.keys()) + expanded

        return RetrievalResult(
            table_names=all_tables,
            scores=scores,
            expanded_tables=expanded,
        )

    def get_context_for_llm(self, result: RetrievalResult) -> str:
        """
        将检索结果格式化为 LLM 可理解的上下文字符串。

        这是给模型的"提示面"——模型只能看到这些表的 schema。
        """
        metadata = self.index.metadata
        if not metadata:
            return ""

        parts: list[str] = []
        for table_name in result.table_names:
            table = metadata.get_table(table_name)
            if table:
                parts.append(table.to_doc())

        # 追加预定义指标
        if metadata.metrics:
            parts.append("\n预定义业务指标：")
            for name, formula in metadata.metrics.items():
                parts.append(f"  - {name}: {formula}")

        return "\n\n".join(parts)
