"""
语义记忆反思器：定期回顾生成洞察 + 记忆衰减 + 冲突消解。

触发时机：每 N 轮对话后自动触发（由 agent._maybe_reflect 调用）。

功能：
1. 洞察生成：用 LLM 回顾近期查询，生成高层洞察（如"用户关注成本趋势"）
2. 记忆衰减：删除有效权重低于阈值的旧记忆
3. 冲突消解：同一 key 不同 category 的记忆，保留高权重的
4. 记忆合并：相似记忆合并为更通用的记忆

设计要点：
- 反思是异步后台操作，不阻塞主流程
- 失败时静默处理（不影响正常问答）
- 洞察记忆初始置信度较低（0.6），随验证逐步提高
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.memory.store import (
    CATEGORY_INSIGHT,
    CATEGORY_PATTERN,
    CATEGORY_PREFERENCE,
    Memory,
    SemanticMemoryStore,
)
from app.models.state import Turn

if TYPE_CHECKING:
    from app.core.llm import LLMClient

logger = logging.getLogger(__name__)


def reflect_and_maintain(
    user_id: str,
    store: SemanticMemoryStore,
    llm: "LLMClient",
    recent_turns: list[Turn],
    decay_threshold: float = 0.15,
    request_id: str = "",
) -> dict[str, int]:
    """
    执行反思 + 记忆维护。

    Args:
        user_id: 用户 ID
        store: 语义记忆存储
        llm: LLM 客户端
        recent_turns: 最近的对话轮次
        decay_threshold: 衰减删除阈值
        request_id: 请求 ID（日志用）

    Returns:
        统计信息 {"insights": N, "decayed": N, "conflicts_resolved": N}
    """
    stats = {"insights": 0, "decayed": 0, "conflicts_resolved": 0}

    # 1. 洞察生成
    try:
        insights = _generate_insights(user_id, store, llm, recent_turns)
        stats["insights"] = insights
        if insights:
            logger.info(
                "[%s] Generated %d insights for user %s",
                request_id, insights, user_id,
            )
    except Exception as e:
        logger.warning("[%s] Insight generation failed: %s", request_id, e)

    # 2. 记忆衰减
    try:
        decayed = store.decay(threshold=decay_threshold)
        stats["decayed"] = decayed
        if decayed:
            logger.info(
                "[%s] Decayed %d memories for user %s",
                request_id, decayed, user_id,
            )
    except Exception as e:
        logger.warning("[%s] Memory decay failed: %s", request_id, e)

    # 3. 冲突消解
    try:
        resolved = store.resolve_conflicts(user_id)
        stats["conflicts_resolved"] = resolved
        if resolved:
            logger.info(
                "[%s] Resolved %d conflicts for user %s",
                request_id, resolved, user_id,
            )
    except Exception as e:
        logger.warning("[%s] Conflict resolution failed: %s", request_id, e)

    return stats


def _generate_insights(
    user_id: str,
    store: SemanticMemoryStore,
    llm: "LLMClient",
    recent_turns: list[Turn],
) -> int:
    """
    用 LLM 回顾近期查询，生成高层洞察。

    Returns: 新增的洞察数量
    """
    # 只对有实际查询的轮次做反思
    valid_turns = [t for t in recent_turns if t.sql_executed and not t.error]
    if len(valid_turns) < 3:
        return 0  # 数据太少不值得反思

    # 构建回顾输入
    qa_summary = []
    for t in valid_turns[-10:]:  # 最多取最近 10 轮
        qa_summary.append(f"Q: {t.question}")
        if t.answer:
            qa_summary.append(f"A: {t.answer[:100]}")
        if t.tables_used:
            qa_summary.append(f"  表: {', '.join(t.tables_used)}")

    # 获取现有记忆作为参考
    existing = store.retrieve(user_id, limit=10)
    existing_summary = ""
    if existing:
        existing_summary = "\n已有记忆：\n" + "\n".join(
            f"  - [{m.category}] {m.key}: {m.value}" for m in existing
        )

    from app.core.llm import ChatMessage

    reflect_prompt = f"""请回顾以下用户查询历史，生成 1-3 条有价值的洞察。

洞察类型：
- 用户的关注重点（如"主要关注成本分析"）
- 查询模式总结（如"偏好按月汇总各部门数据"）
- 潜在需求预测（如"可能会关注同比/环比趋势"）

查询历史：
{chr(10).join(qa_summary)}
{existing_summary}

请以 JSON 格式输出：
{{"insights": [
  {{"key": "简短标识", "value": "洞察内容"}}
]}}

如果没有有价值的洞察，返回 {{"insights": []}}"""

    messages = [
        ChatMessage(
            role="system",
            content="你是一个数据分析洞察助手。从用户查询历史中提取有价值的模式和洞察。只输出 JSON。",
        ),
        ChatMessage(role="user", content=reflect_prompt),
    ]

    result = llm.chat_json(messages, temperature=0.2)
    insights_list = result.get("insights", [])

    if not isinstance(insights_list, list):
        return 0

    count = 0
    for item in insights_list:
        if not isinstance(item, dict):
            continue
        key = item.get("key", "").strip()
        value = item.get("value", "").strip()
        if not key or not value:
            continue

        memory = Memory(
            user_id=user_id,
            category=CATEGORY_INSIGHT,
            key=f"insight:{key}",
            value=value,
            confidence=0.6,  # 洞察初始置信度较低
        )
        store.store(memory)
        count += 1

    return count
