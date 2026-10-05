"""
答案自验证模块：SQL 执行成功后，验证结果是否真正回答了用户的问题。

设计要点：
- 仅对复杂查询触发（多表 JOIN、子查询），简单查询跳过以减少延迟
- LLM 判断结果是否与问题匹配
- 返回 MATCH 或 MISMATCH + 原因，供上层决定是否重试
- 单次 LLM 调用，延迟约 1-2 秒
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from app.core.llm import ChatMessage, LLMClient
from app.models.plan import ExecResult

logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """验证结果"""
    is_match: bool           # 结果是否匹配问题
    reason: str = ""         # 不匹配的原因（用于重试时提示 LLM）
    confidence: float = 1.0  # 置信度


# 验证系统 prompt
_VERIFY_SYSTEM = """你是一个 SQL 查询结果验证器。你的任务是判断 SQL 的查询结果是否真正回答了用户的问题。

判断规则：
1. 结果的数据维度是否与问题匹配（如问"各部门"就应该有多个部门的数据，而非全公司汇总）
2. 时间范围是否正确（如问"本月"是否用了正确的月份）
3. 过滤条件是否完整（如问"销售部"是否确实只查了销售部）
4. 聚合方式是否正确（如问"平均"是否用了 AVG 而非 SUM）
5. 排序/排名是否正确（如问"最高"是否确实取了最大值）

注意：
- 如果结果看起来合理但无法确定，判为 MATCH
- 只有明显不匹配时才判为 MISMATCH
- 不要过于严格，轻微差异可以接受

只输出 JSON，不要输出其他内容：
{"verdict": "MATCH" 或 "MISMATCH", "reason": "简要原因"}"""


def _is_complex_query(sql: str) -> bool:
    """
    判断是否为复杂查询。

    只有复杂查询才需要验证（简单查询准确率已经很高，跳过验证减少延迟）。
    """
    sql_upper = sql.upper()
    # 多表 JOIN（2 个以上 JOIN）
    join_count = sql_upper.count(" JOIN ")
    if join_count >= 2:
        return True
    # 子查询
    if sql_upper.count("SELECT") >= 2:
        return True
    # 复杂聚合（CASE WHEN + GROUP BY）
    if "CASE WHEN" in sql_upper and "GROUP BY" in sql_upper:
        return True
    # 比率计算（除法 + 聚合）
    if "/" in sql and ("SUM(" in sql_upper or "COUNT(" in sql_upper or "AVG(" in sql_upper):
        return True
    return False


def verify_result(
    question: str,
    sql: str,
    exec_result: ExecResult,
    llm: LLMClient,
) -> VerifyResult:
    """
    验证 SQL 执行结果是否匹配用户问题。

    Args:
        question: 用户原始问题
        sql: 最终执行的 SQL
        exec_result: SQL 执行结果
        llm: LLM 客户端

    Returns:
        VerifyResult: 验证结果
    """
    # 简单查询跳过验证
    if not _is_complex_query(sql):
        logger.debug("Simple query, skipping verification")
        return VerifyResult(is_match=True, reason="simple_query")

    # 空结果跳过验证（空结果由其他逻辑处理）
    if exec_result.row_count == 0:
        logger.debug("Empty result, skipping verification")
        return VerifyResult(is_match=True, reason="empty_result")

    # 构建验证输入
    result_table = exec_result.to_markdown_table()
    # 截断过长的结果，避免 prompt 过长
    if len(result_table) > 1500:
        result_table = result_table[:1500] + "\n... (结果已截断)"

    user_msg = f"""用户问题：{question}

生成的 SQL：
```sql
{sql}
```

查询结果（{exec_result.row_count} 行）：
{result_table}

请判断查询结果是否回答了用户的问题。"""

    messages = [
        ChatMessage(role="system", content=_VERIFY_SYSTEM),
        ChatMessage(role="user", content=user_msg),
    ]

    try:
        response = llm.chat(messages, temperature=0.0).strip()
        logger.info("[%s] Verification response: %s", "verify", response[:200])

        # 解析 JSON 响应
        verdict, reason = _parse_verify_response(response)
        is_match = verdict == "MATCH"

        if not is_match:
            logger.warning(
                "Verification MISMATCH: question='%s', reason='%s'",
                question[:50], reason,
            )

        return VerifyResult(is_match=is_match, reason=reason)

    except Exception as e:
        # 验证失败不阻塞主流程，默认视为 MATCH
        logger.warning("Verification failed: %s", e)
        return VerifyResult(is_match=True, reason=f"verify_error: {e}")


def _parse_verify_response(response: str) -> tuple[str, str]:
    """
    解析 LLM 的验证响应。

    Returns:
        (verdict, reason): verdict 为 "MATCH" 或 "MISMATCH"
    """
    # 尝试提取 JSON
    json_match = re.search(r'\{[^}]*\}', response)
    if json_match:
        try:
            data = json.loads(json_match.group())
            verdict = data.get("verdict", "MATCH").upper()
            reason = data.get("reason", "")
            if verdict in ("MATCH", "MISMATCH"):
                return verdict, reason
        except (json.JSONDecodeError, KeyError):
            pass

    # 降级：关键词匹配
    response_upper = response.upper()
    if "MISMATCH" in response_upper:
        return "MISMATCH", response[:100]
    return "MATCH", response[:100]
