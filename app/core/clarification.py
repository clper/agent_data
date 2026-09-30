"""
澄清模块：判断是否需要追问用户 + 意图路由。

设计要点：
- 通过 LLM 判断问题是否缺少关键信息
- 判断问题是否超出 Agent 能力范围
- 返回结构化决策（data_query / meta / out_of_scope / need_clarification）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.core.llm import ChatMessage, LLMClient
from app.models.state import UserContext

logger = logging.getLogger(__name__)

# 意图分类
INTENT_DATA_QUERY = "data_query"          # 数据查询
INTENT_META = "meta"                      # 元问题（有哪些表/列）
INTENT_OUT_OF_SCOPE = "out_of_scope"      # 超范围
INTENT_NEED_CLARIFICATION = "need_clarification"  # 需要追问

UNDERSTAND_SYSTEM = """你是一个企业数据问答助手。你的任务是分析用户的问题，判断其意图类型。

意图分类：
- data_query: 需要查询数据库才能得到答案的问题
  - 示例："上个月销售部业绩多少"、"张三的入职日期"、"查看我的薪资"、"谁迟到了"、"缺勤过员工的绩效怎么样"
  - 注意：薪资、绩效、考勤、收入、成本等查询都属于 data_query，即使用户说"我的"也是
  - 注意：即使没有指定具体时间，只要能查到数据就算 data_query（如"缺勤过的员工"=查所有缺勤记录）
- meta: 询问系统能力或数据库结构的问题（如"你能查哪些数据"、"有哪些表"）
- out_of_scope: 完全与数据库无关的问题（如"帮我写代码"、"今天天气怎样"、"翻译这句话"）
  - 注意：只有完全无法通过数据库回答的问题才是 out_of_scope
- need_clarification: 问题严重模糊，完全无法确定查询方向时才需要追问
  - 示例："业绩怎么样"（没说哪个部门、什么指标，完全无法确定查询方向）
  - 注意：如果问题可以通过查询所有数据来回答，就不要追问，直接查

输出 JSON 格式：
{
  "intent": "data_query|meta|out_of_scope|need_clarification",
  "rewritten_question": "改写后的问题（消解指代、补全上下文）",
  "missing_info": "缺少的信息描述（仅 need_clarification 时需要）",
  "clarification_question": "追问用户的问题（仅 need_clarification 时需要）"
}"""


@dataclass
class UnderstandResult:
    """理解层输出"""
    intent: str
    rewritten_question: str
    missing_info: str = ""
    clarification_question: str = ""


def analyze_intent(
    question: str,
    recent_questions: list[str],
    llm: LLMClient,
    user: UserContext | None = None,
    session_context: str = "",
) -> UnderstandResult:
    """
    分析用户问题的意图。

    Args:
        question: 用户原始问题
        recent_questions: 最近几轮的问题（用于上下文消解）
        llm: LLM 客户端
        user: 用户身份上下文（可选，用于解析"我的"等指代）
        session_context: 会话上下文（包含摘要、最近问答、查询状态）

    Returns:
        UnderstandResult: 意图分类 + 改写后的问题
    """
    # 构建上下文
    context_parts = []

    # 注入用户身份信息，帮助 LLM 理解"我的"等指代
    if user:
        context_parts.append("当前用户身份：")
        context_parts.append(f"  - user_id (emp_id): {user.user_id}")
        context_parts.append(f"  - 角色: {user.role}")
        if user.department_id:
            context_parts.append(f"  - 部门 ID: {user.department_id}")
        context_parts.append('注意：当用户说"我的"、"我自己"时，指的就是当前用户。')
        context_parts.append("")

    # 注入工作记忆上下文（摘要 + 最近问答 + 查询状态）
    if session_context:
        context_parts.append(session_context)
        context_parts.append("")

    # 保留原有的 recent_questions 作为补充
    if recent_questions:
        context_parts.append("最近问题：")
        for q in recent_questions[-3:]:
            context_parts.append(f"  - {q}")
    context_parts.append(f"\n当前问题：{question}")
    user_msg = "\n".join(context_parts)

    messages = [
        ChatMessage(role="system", content=UNDERSTAND_SYSTEM),
        ChatMessage(role="user", content=user_msg),
    ]

    result_dict = llm.chat_json(messages, temperature=0.0)
    return _parse_understand_result(result_dict)


def _parse_understand_result(d: dict[str, Any]) -> UnderstandResult:
    """解析理解层 LLM 输出"""
    intent = d.get("intent", INTENT_DATA_QUERY)
    # 校验意图值
    valid_intents = {INTENT_DATA_QUERY, INTENT_META, INTENT_OUT_OF_SCOPE, INTENT_NEED_CLARIFICATION}
    if intent not in valid_intents:
        logger.warning("Invalid intent: %s, defaulting to data_query", intent)
        intent = INTENT_DATA_QUERY

    return UnderstandResult(
        intent=intent,
        rewritten_question=d.get("rewritten_question", ""),
        missing_info=d.get("missing_info", ""),
        clarification_question=d.get("clarification_question", ""),
    )
