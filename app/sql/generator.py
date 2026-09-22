"""
SQL 生成器：让 LLM 生成结构化查询计划（QueryPlan），而非直接生成 SQL。

设计要点：
- 模型输出结构化 JSON → 映射为 QueryPlan 对象
- 只给模型"检索到的表"的 schema（提示面约束）
- 敏感列标记 [敏感]，引导模型不选这些列
- 强制参数化：WHERE 条件中的值用 %s 占位
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.llm import ChatMessage, LLMClient
from app.models.plan import JoinClause, QueryPlan
from app.schema_rag.retriever import RetrievalResult
from app.schema_rag.metadata import SchemaMetadata

logger = logging.getLogger(__name__)

GENERATE_SYSTEM = """你是一个企业数据查询助手。根据用户问题和可用的数据库表结构，生成查询计划。

规则：
1. 只使用提供的表，不要编造不存在的表或列
2. 标记 [敏感] 的列不要出现在 SELECT 中
3. WHERE 条件中的具体值用 %s 占位，参数单独列出
4. 必须指定 intent：data_query / meta / out_of_scope
5. 如果问题无法用提供的表回答，intent 设为 out_of_scope

输出 JSON 格式：
{
  "intent": "data_query",
  "target_tables": ["table1", "table2"],
  "select_columns": ["col1", "COUNT(*) AS total"],
  "where_conditions": ["col1 = %s", "col2 > %s"],
  "where_params": ["value1", 100],
  "joins": [{"table": "table2", "on_condition": "table1.id = table2.id", "join_type": "INNER"}],
  "group_by": ["col1"],
  "order_by": ["total DESC"],
  "limit": 10,
  "explanation": "查询某部门某月的业绩汇总"
}"""


def generate_plan(
    question: str,
    retrieval_result: RetrievalResult,
    schema_context: str,
    metadata: SchemaMetadata,
    llm: LLMClient,
) -> QueryPlan:
    """
    让 LLM 生成查询计划。

    Args:
        question: 改写后的问题
        retrieval_result: Schema 检索结果
        schema_context: 给 LLM 的 schema 上下文
        metadata: Schema 元数据（用于校验）
        llm: LLM 客户端

    Returns:
        QueryPlan: 结构化查询计划
    """
    user_msg = f"可用表结构：\n{schema_context}\n\n用户问题：{question}"
    messages = [
        ChatMessage(role="system", content=GENERATE_SYSTEM),
        ChatMessage(role="user", content=user_msg),
    ]

    result_dict = llm.chat_json(messages, temperature=0.0)
    return _parse_plan(result_dict, metadata, retrieval_result)


def _parse_plan(
    d: dict[str, Any],
    metadata: SchemaMetadata,
    retrieval_result: RetrievalResult,
) -> QueryPlan:
    """
    解析 LLM 输出为 QueryPlan。

    安全关键：校验目标表是否在检索结果中（防止模型幻觉访问未授权表）
    """
    intent = d.get("intent", "data_query")
    target_tables = [t.lower() for t in d.get("target_tables", [])]

    # 安全检查：目标表必须在检索结果中
    allowed_tables = set(retrieval_result.table_names)
    valid_tables = [t for t in target_tables if t in allowed_tables]
    invalid_tables = [t for t in target_tables if t not in allowed_tables]
    if invalid_tables:
        logger.warning("Model requested tables not in retrieval result: %s", invalid_tables)
    if not valid_tables and intent == "data_query":
        # 没有合法表，降级为 out_of_scope
        intent = "out_of_scope"

    # 解析 JOIN
    joins: list[JoinClause] = []
    for j in d.get("joins", []):
        join_table = j.get("table", "").lower()
        if join_table in allowed_tables:
            joins.append(JoinClause(
                table=join_table,
                on_condition=j.get("on_condition", ""),
                join_type=j.get("join_type", "INNER").upper(),
            ))

    return QueryPlan(
        intent=intent,
        target_tables=valid_tables,
        select_columns=d.get("select_columns", []),
        where_conditions=d.get("where_conditions", []),
        where_params=d.get("where_params", []),
        joins=joins,
        group_by=d.get("group_by", []),
        order_by=d.get("order_by", []),
        limit=d.get("limit"),
        explanation=d.get("explanation", ""),
    )
