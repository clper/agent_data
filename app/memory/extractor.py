"""
语义记忆提取器：从对话中提取并写入语义记忆。

提取时机：每轮成功查询后调用。

提取内容：
1. 查询模式（pattern）：从 SQL 中提取常用表、过滤条件、聚合方式
2. 用户偏好（preference）：从问答中提取用户关注的指标、部门等
3. 纠错记录（correction）：当用户纠正回答时记录

设计要点：
- 轻量级提取（规则 + 关键词，不额外调用 LLM）
- 去重：相同 key 的记忆会被更新而非重复创建
- 置信度：根据提取来源设定不同初始置信度
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.memory.store import (
    CATEGORY_CORRECTION,
    CATEGORY_PATTERN,
    CATEGORY_PREFERENCE,
    Memory,
    SemanticMemoryStore,
)
from app.models.state import Turn

logger = logging.getLogger(__name__)


def extract_from_turn(
    turn: Turn,
    user_id: str,
    store: SemanticMemoryStore,
) -> int:
    """
    从一轮对话中提取语义记忆。

    Args:
        turn: 完成的对话轮次
        user_id: 用户 ID
        store: 语义记忆存储

    Returns: 提取/更新的记忆数量
    """
    count = 0

    if turn.error or not turn.sql_executed:
        return 0

    # 1. 提取查询模式
    count += _extract_query_patterns(turn, user_id, store)

    # 2. 提取用户偏好
    count += _extract_preferences(turn, user_id, store)

    logger.debug("Extracted %d memories from turn (user=%s)", count, user_id)
    return count


def record_correction(
    user_id: str,
    original_question: str,
    correction_text: str,
    store: SemanticMemoryStore,
) -> int:
    """
    记录用户纠错。

    当用户说"不对"、"你搞错了"、"应该是..."时调用。

    Args:
        user_id: 用户 ID
        original_question: 原始问题
        correction_text: 用户纠正的内容
        store: 语义记忆存储

    Returns: 1（固定新增一条纠错）
    """
    # 从原始问题中提取关键词作为 key
    key = _extract_key_from_question(original_question)

    memory = Memory(
        user_id=user_id,
        category=CATEGORY_CORRECTION,
        key=f"correction:{key}",
        value=correction_text,
        confidence=0.95,  # 纠错置信度最高
        metadata={"original_question": original_question},
    )
    store.store(memory)
    logger.info(
        "Recorded correction for user %s: %s -> %s",
        user_id, original_question[:50], correction_text[:50],
    )
    return 1


# ── 内部提取逻辑 ────────────────────────────────────────


def _extract_query_patterns(
    turn: Turn,
    user_id: str,
    store: SemanticMemoryStore,
) -> int:
    """从 SQL 中提取查询模式"""
    count = 0
    sql = turn.sql_executed.lower()

    # 模式 1: 常用表
    for table in turn.tables_used:
        memory = Memory(
            user_id=user_id,
            category=CATEGORY_PATTERN,
            key=f"frequent_table:{table}",
            value=f"经常查询 {table} 表",
            confidence=0.6,
        )
        store.store(memory)
        count += 1

    # 模式 2: 过滤条件（部门、月份等）
    dept_match = re.search(r"dept_name\s*=\s*'([^']+)'", sql)
    if dept_match:
        dept = dept_match.group(1)
        memory = Memory(
            user_id=user_id,
            category=CATEGORY_PATTERN,
            key=f"frequent_dept:{dept}",
            value=f"经常查询部门: {dept}",
            confidence=0.6,
        )
        store.store(memory)
        count += 1

    month_match = re.search(r"month\s*=\s*'([^']+)'", sql)
    if month_match:
        memory = Memory(
            user_id=user_id,
            category=CATEGORY_PATTERN,
            key="time_granularity:monthly",
            value="习惯按月查询",
            confidence=0.5,
        )
        store.store(memory)
        count += 1

    # 模式 3: 聚合方式
    if "group by" in sql:
        group_match = re.search(r"group by\s+(.+?)(?:order|limit|$)", sql)
        if group_match:
            group_cols = group_match.group(1).strip()
            memory = Memory(
                user_id=user_id,
                category=CATEGORY_PATTERN,
                key=f"group_by:{group_cols[:50]}",
                value=f"常用分组: {group_cols[:80]}",
                confidence=0.5,
            )
            store.store(memory)
            count += 1

    return count


def _extract_preferences(
    turn: Turn,
    user_id: str,
    store: SemanticMemoryStore,
) -> int:
    """从问答中提取用户偏好"""
    count = 0
    question = turn.question

    # 偏好 1: 关注的指标类型
    metric_keywords = {
        "收入": "revenue", "营收": "revenue", "销售额": "revenue",
        "成本": "cost", "费用": "cost",
        "利润": "profit",
        "绩效": "performance", "考核": "performance",
        "人力": "headcount", "员工": "headcount", "人员": "headcount",
        "项目": "project",
        "考勤": "attendance", "缺勤": "attendance",
    }

    found_metrics = set()
    for cn, en in metric_keywords.items():
        if cn in question and en not in found_metrics:
            found_metrics.add(en)
            memory = Memory(
                user_id=user_id,
                category=CATEGORY_PREFERENCE,
                key=f"interested_metric:{en}",
                value=f"关注 {cn} 相关指标",
                confidence=0.5,
            )
            store.store(memory)
            count += 1

    # 偏好 2: 关注的部门
    dept_match = re.search(r"(销售部|研发部|市场部|人事部|财务部|运营部)", question)
    if dept_match:
        dept = dept_match.group(1)
        memory = Memory(
            user_id=user_id,
            category=CATEGORY_PREFERENCE,
            key=f"interested_dept:{dept}",
            value=f"关注 {dept} 的数据",
            confidence=0.5,
        )
        store.store(memory)
        count += 1

    return count


def _extract_key_from_question(question: str) -> str:
    """从问题中提取简短 key（用于纠错记忆）"""
    # 去除常见疑问词
    key = question.strip()
    key = re.sub(r"[？?。！!，,]", "", key)
    # 截取前 30 字符
    if len(key) > 30:
        key = key[:30]
    return key
