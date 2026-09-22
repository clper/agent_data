"""
修复循环：SQL 执行失败时，让模型修复并重试。

设计要点：
- 最多修复 N 轮（默认 2 轮）
- PermissionError 不修复（安全错误不可修复）
- 修复时把错误信息 + 原 SQL 给模型，让它生成新的 QueryPlan
- 修复后重新走校验链
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.llm import ChatMessage, LLMClient
from app.models.plan import QueryPlan
from app.schema_rag.metadata import SchemaMetadata

logger = logging.getLogger(__name__)

REPAIR_SYSTEM = """你是一个 SQL 修复专家。之前的查询执行失败了，请根据错误信息修复查询计划。

规则：
1. 只修复导致错误的部分，不要改变查询的语义
2. 保持 JSON 格式不变
3. 如果无法修复，返回原计划并说明原因

输出 JSON 格式与原查询计划相同。"""


def attempt_repair(
    original_plan: QueryPlan,
    error_message: str,
    schema_context: str,
    question: str,
    metadata: SchemaMetadata,
    llm: LLMClient,
) -> QueryPlan | None:
    """
    尝试修复失败的查询计划。

    Args:
        original_plan: 原始查询计划
        error_message: 执行错误信息
        schema_context: Schema 上下文
        question: 用户问题
        metadata: Schema 元数据
        llm: LLM 客户端

    Returns:
        修复后的 QueryPlan，如果无法修复返回 None
    """
    import json

    original_dict = {
        "intent": original_plan.intent,
        "target_tables": original_plan.target_tables,
        "select_columns": original_plan.select_columns,
        "where_conditions": original_plan.where_conditions,
        "where_params": original_plan.where_params,
        "joins": [
            {"table": j.table, "on_condition": j.on_condition, "join_type": j.join_type}
            for j in original_plan.joins
        ],
        "group_by": original_plan.group_by,
        "order_by": original_plan.order_by,
        "limit": original_plan.limit,
    }

    user_msg = (
        f"可用表结构：\n{schema_context}\n\n"
        f"用户问题：{question}\n\n"
        f"原查询计划：\n{json.dumps(original_dict, ensure_ascii=False, indent=2)}\n\n"
        f"执行错误：{error_message}\n\n"
        f"请修复查询计划。"
    )

    messages = [
        ChatMessage(role="system", content=REPAIR_SYSTEM),
        ChatMessage(role="user", content=user_msg),
    ]

    result_dict = llm.chat_json(messages, temperature=0.0)

    if "error" in result_dict:
        logger.warning("Repair failed: LLM returned error")
        return None

    # 解析修复后的计划
    from app.sql.generator import _parse_plan
    from app.schema_rag.retriever import RetrievalResult

    # 构造一个包含所有表的 retrieval result（修复时不做表限制）
    all_tables = metadata.get_all_table_names()
    retrieval_result = RetrievalResult(table_names=all_tables, scores={})

    return _parse_plan(result_dict, metadata, retrieval_result)
