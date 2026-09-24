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
from datetime import datetime, timedelta
from typing import Any

from app.core.llm import ChatMessage, LLMClient
from app.schema_rag.retriever import RetrievalResult
from app.schema_rag.metadata import SchemaMetadata

logger = logging.getLogger(__name__)


def _get_time_context() -> str:
    """
    生成当前时间上下文，注入到 LLM prompt 中。

    让 LLM 理解自然语言时间语义（如"本月"="2026-09"）。
    """
    now = datetime.now()
    last_month = (now.replace(day=1) - timedelta(days=1))
    quarter = (now.month - 1) // 3 + 1
    quarter_start = (quarter - 1) * 3 + 1

    return (
        f"当前时间信息：\n"
        f"  - 今天：{now.strftime('%Y-%m-%d')}（{'周' + '一二三四五六日'[now.weekday()]}）\n"
        f"  - 本月：{now.strftime('%Y-%m')}\n"
        f"  - 上月：{last_month.strftime('%Y-%m')}\n"
        f"  - 今年：{now.year}\n"
        f"  - 本季度：{now.year}年Q{quarter}（{now.strftime('%Y')}-{quarter_start:02d} ~ {now.strftime('%Y')}-{now.month:02d}）\n"
        f"注意：用户说'本月'时请用 '{now.strftime('%Y-%m')}'，"
        f"说'上月'时请用 '{last_month.strftime('%Y-%m')}'，"
        f"说'今年'时请用 LIKE '{now.year}-%'，"
        f"说'本季度'时请用 BETWEEN '{now.year}-{quarter_start:02d}' AND '{now.strftime('%Y-%m')}'"
    )

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
    memory_context: str = "",
) -> GenerationResult:
    """
    让 LLM 直接生成 SQL。

    Args:
        question: 改写后的问题
        retrieval_result: Schema 检索结果
        schema_context: 给 LLM 的 schema 上下文
        metadata: Schema 元数据
        llm: LLM 客户端
        memory_context: 语义记忆上下文（可选，由 memory.retriever 提供）

    Returns:
        GenerationResult: 包含 SQL 和解释
    """
    # 动态注入时间上下文
    time_context = _get_time_context()
    system_prompt = GENERATE_SYSTEM + "\n\n" + time_context

    # 注入语义记忆（如果有的话）
    if memory_context:
        system_prompt += "\n\n" + memory_context

    user_msg = f"可用表结构：\n{schema_context}\n\n用户问题：{question}"
    messages = [
        ChatMessage(role="system", content=system_prompt),
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
