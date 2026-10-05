"""
Few-shot 示例检索器：根据查询类型动态注入最相关的 SQL 示例。

设计要点：
- 从 data/few_shot_examples.json 加载示例库
- 根据检索到的表名 + 问题关键词匹配最相关的 2-3 个示例
- 匹配策略：表名重叠 > 标签匹配 > 问题关键词
- 零额外 LLM 调用，纯规则匹配
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_EXAMPLES_PATH = _PROJECT_ROOT / "data" / "few_shot_examples.json"


@dataclass
class FewShotExample:
    """单条 few-shot 示例"""
    id: str
    category: str
    question: str
    sql: str
    tables: list[str]
    tags: list[str]
    note: str = ""


def _load_examples() -> list[FewShotExample]:
    """加载示例库"""
    if not _EXAMPLES_PATH.exists():
        logger.warning("Few-shot examples file not found: %s", _EXAMPLES_PATH)
        return []

    with open(_EXAMPLES_PATH, "r", encoding="utf-8") as f:
        raw = json.load(f)

    return [
        FewShotExample(
            id=e["id"],
            category=e["category"],
            question=e["question"],
            sql=e["sql"],
            tables=e["tables"],
            tags=e["tags"],
            note=e.get("note", ""),
        )
        for e in raw
    ]


# 全局缓存（启动时加载一次）
_examples_cache: list[FewShotExample] | None = None


def _get_examples() -> list[FewShotExample]:
    global _examples_cache
    if _examples_cache is None:
        _examples_cache = _load_examples()
    return _examples_cache


def _score_example(example: FewShotExample, candidate_tables: list[str], question: str) -> float:
    """
    计算示例与当前查询的相关度分数。

    评分维度：
    1. 表名重叠（权重最高）：候选表与示例表的重合度
    2. 标签匹配：问题关键词与示例标签的匹配
    3. 问题相似度：简单的关键词重叠
    """
    score = 0.0

    # 1. 表名重叠（权重 3.0）
    if candidate_tables and example.tables:
        overlap = set(candidate_tables) & set(example.tables)
        # Jaccard 相似度 * 权重
        union = set(candidate_tables) | set(example.tables)
        if union:
            score += 3.0 * len(overlap) / len(union)
        # 完全匹配额外加分
        if overlap == set(candidate_tables) == set(example.tables):
            score += 2.0

    # 2. 标签关键词匹配（权重 1.5）
    question_lower = question.lower()
    tag_keywords = {
        "比率": ["率", "占比", "百分比", "比例"],
        "利润": ["利润", "毛利率", "净利率", "盈亏"],
        "排名": ["最高", "最低", "前", "top", "排名", "最"],
        "JOIN": ["和", "与", "对比", "关联"],
        "GROUP BY": ["各", "分别", "平均", "汇总", "总计", "每个"],
        "COUNT DISTINCT": ["多少", "几个", "人数", "数量", "去重"],
        "子查询": ["高于", "低于", "超过", "平均", "比较"],
        "时间范围": ["今年", "去年", "本月", "上月", "季度", "范围"],
        "CASE WHEN": ["率", "占比", "条件"],
        "LIKE": ["今年", "去年", "所有", "全部"],
        "LIMIT": ["前", "top", "最"],
        "ORDER BY": ["排序", "最高", "最低", "排名"],
    }
    for tag, keywords in tag_keywords.items():
        if tag in example.tags:
            for kw in keywords:
                if kw in question_lower:
                    score += 1.5
                    break  # 每个标签最多加一次

    # 3. 问题关键词重叠（权重 0.5）
    # 简单分词：按常见分隔符拆分
    q_words = set(question_lower.replace("的", " ").replace("是", " ").replace("多少", " ").split())
    e_words = set(example.question.lower().replace("的", " ").replace("是", " ").replace("多少", " ").split())
    common = q_words & e_words
    if q_words:
        score += 0.5 * len(common) / len(q_words)

    return score


def retrieve_few_shot_examples(
    candidate_tables: list[str],
    question: str,
    max_examples: int = 3,
) -> list[FewShotExample]:
    """
    检索与当前查询最相关的 few-shot 示例。

    Args:
        candidate_tables: Schema RAG 检索到的候选表名
        question: 用户问题（改写后）
        max_examples: 最多返回几个示例（默认 3）

    Returns:
        按相关度排序的示例列表
    """
    examples = _get_examples()
    if not examples:
        return []

    scored = []
    for ex in examples:
        s = _score_example(ex, candidate_tables, question)
        scored.append((s, ex))

    # 按分数降序，取 top N
    scored.sort(key=lambda x: x[0], reverse=True)
    top = [ex for s, ex in scored[:max_examples] if s > 0]

    logger.debug(
        "Few-shot retrieved %d examples for tables=%s, question=%s",
        len(top), candidate_tables, question[:50],
    )
    return top


def format_examples_for_prompt(examples: list[FewShotExample]) -> str:
    """
    将示例格式化为 LLM prompt 文本。

    输出格式与 GENERATE_SYSTEM 中的示例格式一致。
    """
    if not examples:
        return ""

    lines = ["参考示例（请严格遵循这些示例的 SQL 风格）："]
    for ex in examples:
        lines.append(f"问题：{ex.question}")
        lines.append(f'{{"sql": "{ex.sql}", "explanation": "{ex.note}"}}')
        lines.append("")

    return "\n".join(lines)
