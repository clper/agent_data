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

logger = logging.getLogger(__name__)

# 意图分类
INTENT_DATA_QUERY = "data_query"          # 数据查询
INTENT_META = "meta"                      # 元问题（有哪些表/列）
INTENT_OUT_OF_SCOPE = "out_of_scope"      # 超范围
INTENT_NEED_CLARIFICATION = "need_clarification"  # 需要追问

UNDERSTAND_SYSTEM = """你是一个企业数据问答助手。你的任务是分析用户的问题，判断其意图类型。

意图分类：
- data_query: 需要查询数据库才能得到答案的问题（如"上个月销售部业绩多少"）
- meta: 询问系统能力或数据库结构的问题（如"你能查哪些数据"、"有哪些表"）
- out_of_scope: 超出数据查询范围的问题（如"帮我写代码"、"今天天气怎样"）
- need_clarification: 问题缺少关键信息，需要追问（如"业绩怎么样"没说哪个部门/时间段）

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
) -> UnderstandResult:
    """
    分析用户问题的意图。

    Args:
        question: 用户原始问题
        recent_questions: 最近几轮的问题（用于上下文消解）
        llm: LLM 客户端

    Returns:
        UnderstandResult: 意图分类 + 改写后的问题
    """
    # 构建上下文
    context_parts = []
    if recent_questions:
        context_parts.append("历史问题：")
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
