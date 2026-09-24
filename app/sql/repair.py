"""
修复循环：SQL 执行失败时，让 LLM 直接修复 SQL 并重试。

设计要点：
- 最多修复 N 轮（默认 2 轮）
- PermissionError 不修复（安全错误不可修复）
- LLM 直接修复 SQL 字符串，不再经过 QueryPlan 中转
- 修复后重新走校验链
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.llm import ChatMessage, LLMClient
from app.sql.generator import GenerationResult
from app.schema_rag.metadata import SchemaMetadata

logger = logging.getLogger(__name__)

REPAIR_SYSTEM = """你是一个 SQL 修复专家。之前的查询执行失败了，请根据错误信息修复 SQL。

规则：
1. 只修复导致错误的部分，不要改变查询的语义
2. WHERE 条件中的值直接写入 SQL（系统会自动参数化）
3. 只生成 SELECT 语句
4. 如果无法修复，返回空 SQL

输出 JSON：
{"sql": "修复后的 SQL", "explanation": "修复说明"}"""


def attempt_repair(
    original_sql: str,
    error_message: str,
    schema_context: str,
    question: str,
    metadata: SchemaMetadata,
    llm: LLMClient,
) -> GenerationResult | None:
    """
    尝试修复失败的 SQL。

    Args:
        original_sql: 原始 SQL
        error_message: 执行错误信息
        schema_context: Schema 上下文
        question: 用户问题
        metadata: Schema 元数据
        llm: LLM 客户端

    Returns:
        修复后的 GenerationResult，如果无法修复返回 None
    """
    user_msg = (
        f"可用表结构：\n{schema_context}\n\n"
        f"用户问题：{question}\n\n"
        f"原 SQL：\n{original_sql}\n\n"
        f"执行错误：{error_message}\n\n"
        f"请修复 SQL。"
    )

    messages = [
        ChatMessage(role="system", content=REPAIR_SYSTEM),
        ChatMessage(role="user", content=user_msg),
    ]

    result_dict = llm.chat_json(messages, temperature=0.0)

    sql = result_dict.get("sql", "").strip()
    explanation = result_dict.get("explanation", "")

    if not sql:
        logger.warning("Repair failed: LLM returned empty SQL")
        return None

    logger.info("Repair SQL: %s", sql[:200])
    return GenerationResult(sql=sql, explanation=explanation)
