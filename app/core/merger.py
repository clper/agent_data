"""
结果综合器：将多个子问题的结果综合为最终回答。

设计要点：
- LLM 根据所有子问题的结果，生成自然语言综合回答
- 支持不同的合并策略（sequential/parallel/conditional）
- 保持回答的连贯性和可读性

示例：
子问题 1："研发部绩效最高的员工是谁？" → "王五（85分）"
子问题 2："王五的入职日期是什么时候？" → "2023-05-15"
→ 综合回答："研发部绩效最高的员工是王五（绩效得分85分），他于2023年5月15日入职。"
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.context import ExecutionContext
from app.core.llm import ChatMessage, LLMClient

logger = logging.getLogger(__name__)

# 结果综合提示词
MERGE_SYSTEM = """你是一个企业数据查询助手的结果综合专家。

你的任务是将多个子问题的执行结果综合为一个连贯、自然的最终回答。

【输入格式】
你会收到：
1. 用户的原始问题
2. 合并策略（sequential/parallel/conditional）
3. 所有子问题的结果（包括问题和答案）

【输出要求】
1. **直接回答问题**：不要复述 SQL 或技术细节
2. **连贯自然**：像人类一样组织语言，避免机械拼接
3. **数据呈现**：用表格或列表展示数据，清晰易读
4. **逻辑完整**：如果子问题之间有因果关系，要体现出来
5. **简洁明了**：控制在 200 字以内，除非用户明确要求详细分析

【合并策略说明】
- **sequential（顺序）**：子问题按顺序执行，后一个依赖前一个
  - 例：先查"谁绩效最高"，再查"他的入职日期"
  - 综合时要体现这种依赖关系："绩效最高的员工是XXX，他于YYY入职"

- **parallel（并行）**：子问题独立执行，可以对比或汇总
  - 例：分别查"销售部平均绩效"和"研发部平均绩效"
  - 综合时要对比或总结："销售部平均绩效为XX，研发部为YY，销售部更高"

- **conditional（条件）**：根据前一步结果决定后续
  - 例：如果有迟到员工，再查他们的薪资
  - 综合时要说明条件逻辑："发现有N个迟到员工，他们的薪资分别是..."

【重要规则】
1. 不要编造数据中没有的信息
2. 如果某个子问题执行失败，说明原因并给出部分回答
3. 如果结果为空，解释可能的原因
4. 保持客观，不要过度解读数据"""

MERGE_USER_TEMPLATE = """用户原始问题：{original_question}

合并策略：{merge_strategy}
{merge_description}

各子问题结果：
{sub_results_summary}

请综合以上信息，给出最终回答。"""


def merge_results(
    original_question: str,
    ctx: ExecutionContext,
    merge_strategy: str,
    merge_description: str,
    llm: LLMClient,
) -> str:
    """
    综合多个子问题的结果。

    Args:
        original_question: 用户原始问题
        ctx: 执行上下文（包含所有子问题结果）
        merge_strategy: 合并策略（sequential/parallel/conditional）
        merge_description: 合并策略描述
        llm: LLM 客户端

    Returns:
        综合后的自然语言回答
    """
    sub_results_summary = ctx.summarize_all()

    user_prompt = MERGE_USER_TEMPLATE.format(
        original_question=original_question,
        merge_strategy=merge_strategy,
        merge_description=f"策略说明：{merge_description}" if merge_description else "",
        sub_results_summary=sub_results_summary,
    )

    messages = [
        ChatMessage(role="system", content=MERGE_SYSTEM),
        ChatMessage(role="user", content=user_prompt),
    ]

    response = llm.chat(messages, temperature=0.3)
    logger.info("Merge result generated (%d chars)", len(response))

    return response
