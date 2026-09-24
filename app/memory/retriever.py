"""
语义记忆检索器：获取相关记忆并格式化为 LLM prompt 注入。

设计要点：
- 从 SemanticMemoryStore 获取与当前问题相关的记忆
- 按类别分组格式化（纠错 > 偏好 > 模式 > 洞察）
- 控制注入长度，不超过 token 预算
- 空记忆时返回空字符串（零开销）
"""
from __future__ import annotations

import logging

from app.memory.store import (
    CATEGORY_CORRECTION,
    CATEGORY_INSIGHT,
    CATEGORY_PATTERN,
    CATEGORY_PREFERENCE,
    SemanticMemoryStore,
)

logger = logging.getLogger(__name__)

# 注入 prompt 的最大字符数（约 200 token）
MAX_PROMPT_CHARS = 600


def get_memory_context(
    user_id: str,
    question: str,
    store: SemanticMemoryStore,
    max_chars: int = MAX_PROMPT_CHARS,
) -> str:
    """
    获取与当前问题相关的记忆上下文，格式化为 prompt 片段。

    Args:
        user_id: 用户 ID
        question: 用户问题
        store: 语义记忆存储
        max_chars: 最大字符数限制

    Returns:
        格式化的记忆上下文字符串（无相关记忆时返回空字符串）
    """
    memories = store.get_relevant(user_id, question, limit=8)
    if not memories:
        return ""

    # 按类别分组
    corrections = []
    preferences = []
    patterns = []
    insights = []

    for mem in memories:
        if mem.category == CATEGORY_CORRECTION:
            corrections.append(mem)
        elif mem.category == CATEGORY_PREFERENCE:
            preferences.append(mem)
        elif mem.category == CATEGORY_PATTERN:
            patterns.append(mem)
        elif mem.category == CATEGORY_INSIGHT:
            insights.append(mem)

    # 按优先级拼接（纠错最重要，放最前面）
    parts = []

    if corrections:
        lines = ["用户历史纠错（请特别注意）："]
        for mem in corrections[:3]:
            lines.append(f"  - {mem.value}")
        parts.append("\n".join(lines))

    if preferences:
        lines = ["用户偏好："]
        for mem in preferences[:2]:
            lines.append(f"  - {mem.value}")
        parts.append("\n".join(lines))

    if patterns:
        lines = ["查询习惯："]
        for mem in patterns[:2]:
            lines.append(f"  - {mem.value}")
        parts.append("\n".join(lines))

    if insights:
        lines = ["历史洞察："]
        for mem in insights[:2]:
            lines.append(f"  - {mem.value}")
        parts.append("\n".join(lines))

    result = "\n".join(parts)

    # 截断保护
    if len(result) > max_chars:
        result = result[:max_chars] + "..."

    logger.debug(
        "Memory context for user %s: %d memories, %d chars",
        user_id, len(memories), len(result),
    )
    return result
