"""
SQL 生成器：让 LLM 直接生成 SQL，而非结构化 QueryPlan。

设计要点：
- LLM 输出带字面量值的 SQL（不用 %s 占位）
- 参数化由 compiler.extract_params() 在 AST 级统一处理
- 只给 LLM "检索到的表" 的 schema（提示面约束）
- 敏感列标记 [敏感]，引导 LLM 不选这些列
- 支持完整 SQL 表达力：CTE、窗口函数、子查询、CASE WHEN 等
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.llm import ChatMessage, LLMClient
from app.schema_rag.retriever import RetrievalResult
from app.schema_rag.metadata import SchemaMetadata

logger = logging.getLogger(__name__)

GENERATE_SYSTEM = """你是一个企业数据查询助手。根据用户问题和可用的数据库表结构，生成 MySQL 查询。

规则：
1. 只使用提供的表，不要编造不存在的表或列
2. 标记 [敏感] 的列：如果用户没有明确要求查看这些列，则不要出现在 SELECT 中；但如果用户明确要求（如"查看我的薪资"），则可以 SELECT，权限系统会自动判断是否允许
3. WHERE 条件中的值直接写入 SQL（如 dept_name = '销售部'），系统会自动参数化
4. 只生成 SELECT 语句，禁止 INSERT/UPDATE/DELETE/DROP
5. 如果问题无法用提供的表回答，在 explanation 中说明原因，sql 留空

输出 JSON 格式：
{"sql": "SELECT ...", "explanation": "查询说明"}

示例：
问题：销售部2026年6月的收入是多少
{"sql": "SELECT SUM(r.amount) AS total_revenue FROM revenue r INNER JOIN department d ON r.dept_id = d.dept_id WHERE d.dept_name = '销售部' AND r.month = '2026-06'", "explanation": "查询销售部2026年6月的总收入"}

问题：查看我的薪资
{"sql": "SELECT salary FROM employee WHERE emp_id = 101", "explanation": "查询员工101的薪资"}

问题：各部门的平均绩效得分
{"sql": "SELECT d.dept_name, AVG(p.score) AS avg_score FROM performance p INNER JOIN employee e ON p.emp_id = e.emp_id INNER JOIN department d ON e.dept_id = d.dept_id GROUP BY d.dept_name", "explanation": "按部门统计平均绩效得分"}"""


@dataclass
class GenerationResult:
    """LLM SQL 生成结果"""
    sql: str                           # LLM 生成的原始 SQL（带字面量）
    explanation: str = ""              # 自然语言解释


def generate_sql(
    question: str,
    retrieval_result: RetrievalResult,
    schema_context: str,
    metadata: SchemaMetadata,
    llm: LLMClient,
) -> GenerationResult:
    """
    让 LLM 直接生成 SQL。

    Args:
        question: 改写后的问题
        retrieval_result: Schema 检索结果
        schema_context: 给 LLM 的 schema 上下文
        metadata: Schema 元数据
        llm: LLM 客户端

    Returns:
        GenerationResult: 包含 SQL 和解释
    """
    user_msg = f"可用表结构：\n{schema_context}\n\n用户问题：{question}"
    messages = [
        ChatMessage(role="system", content=GENERATE_SYSTEM),
        ChatMessage(role="user", content=user_msg),
    ]

    result_dict = llm.chat_json(messages, temperature=0.0)

    sql = result_dict.get("sql", "").strip()
    explanation = result_dict.get("explanation", "")

    if not sql:
        logger.warning("LLM returned empty SQL")
        return GenerationResult(sql="", explanation=explanation or "无法生成查询")

    logger.info("LLM generated SQL: %s", sql[:200])
    return GenerationResult(sql=sql, explanation=explanation)
