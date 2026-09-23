"""
问题分解器：将复杂问题拆分为有序子问题列表。

设计要点：
- LLM 判断是否为复合问题
- 如果是，拆分为有序子问题列表（后一个可能依赖前一个的结果）
- 输出结构化 JSON → 解析为 DecompositionResult 对象
- 每个子问题包含：问题文本、依赖关系、所需表名（可选）

示例：
用户问："研发部绩效最高的员工是谁？他的入职日期是什么时候？"
→ 分解为：
  1. "研发部绩效最高的员工是谁？" （无依赖）
  2. "{emp_name} 的入职日期是什么时候？" （依赖第 1 步的结果）
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.core.llm import ChatMessage, LLMClient

logger = logging.getLogger(__name__)

# 问题分解提示词
DECOMPOSE_SYSTEM = """你是一个企业数据查询助手的问题分解专家。

你的任务是将用户的复杂问题拆分为多个有序的子问题，或者判断为简单问题无需分解。

【判断标准】
- **简单问题**：只需一次 SQL 查询就能回答（即使涉及多表 JOIN）
  - 例："张三的入职日期？" → 简单
  - 例："研发部有哪些员工？" → 简单
  - 例："2026年6月各部门的收入是多少？" → 简单（虽然要 JOIN，但一次查询搞定）

- **复合问题**：需要多次独立查询，或后一步依赖前一步结果
  - 例："研发部绩效最高的员工是谁？他的入职日期是什么时候？" → 复合（先查最高绩效员工，再查入职日期）
  - 例："销售部和研发部的平均绩效分别是多少？哪个部门更高？" → 复合（两个独立查询 + 比较）
  - 例："迟到的员工有哪些？他们的薪资分别是多少？" → 复合（先查迟到名单，再查薪资）

【输出格式】
必须输出严格的 JSON 格式：

```json
{
  "is_composite": true/false,
  "sub_questions": [
    {
      "id": 1,
      "question": "子问题文本",
      "depends_on": [],
      "description": "这个子问题的目的"
    },
    {
      "id": 2,
      "question": "子问题文本（可以用 {result_1} 引用第1步的结果）",
      "depends_on": [1],
      "description": "这个子问题的目的"
    }
  ],
  "merge_strategy": "sequential|parallel|conditional",
  "merge_description": "如何综合所有子问题的结果来回答原问题"
}
```

【合并策略】
- **sequential**：顺序执行，后一步依赖前一步结果（最常见）
- **parallel**：并行执行，各子问题独立（如对比两个部门）
- **conditional**：条件执行，根据前一步结果决定后续步骤

【重要规则】
1. 如果问题是简单的，直接返回 `{"is_composite": false, "sub_questions": [], "merge_strategy": "", "merge_description": ""}`
2. 子问题必须按执行顺序排列（id 从 1 开始递增）
3. 如果子问题依赖前面的结果，用 `{result_N}` 占位（N 是依赖的子问题 id）
4. merge_strategy 和 merge_description 必须准确描述如何综合结果
5. 不要编造数据库中没有的字段或表名"""

DECOMPOSE_USER_TEMPLATE = """用户问题：{question}

请判断是否需要分解，如果需要，输出分解后的子问题列表。"""


@dataclass
class SubQuestion:
    """子问题"""
    id: int
    question: str
    depends_on: list[int] = field(default_factory=list)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "depends_on": self.depends_on,
            "description": self.description,
        }


@dataclass
class DecompositionResult:
    """分解结果"""
    is_composite: bool
    sub_questions: list[SubQuestion] = field(default_factory=list)
    merge_strategy: str = ""  # sequential | parallel | conditional
    merge_description: str = ""

    @classmethod
    def from_json(cls, json_str: str) -> "DecompositionResult":
        """从 JSON 字符串解析"""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.error("Failed to parse decomposition JSON: %s", e)
            # 降级：视为简单问题
            return cls(is_composite=False)

        sub_questions = []
        for item in data.get("sub_questions", []):
            sub_questions.append(SubQuestion(
                id=item["id"],
                question=item["question"],
                depends_on=item.get("depends_on", []),
                description=item.get("description", ""),
            ))

        return cls(
            is_composite=data.get("is_composite", False),
            sub_questions=sub_questions,
            merge_strategy=data.get("merge_strategy", ""),
            merge_description=data.get("merge_description", ""),
        )


def decompose_question(
    question: str,
    llm: LLMClient,
) -> DecompositionResult:
    """
    分解用户问题。

    Args:
        question: 用户原始问题
        llm: LLM 客户端

    Returns:
        DecompositionResult：分解结果
    """
    messages = [
        ChatMessage(role="system", content=DECOMPOSE_SYSTEM),
        ChatMessage(role="user", content=DECOMPOSE_USER_TEMPLATE.format(question=question)),
    ]

    response = llm.chat(messages, temperature=0.1)
    logger.info("Decomposition response: %s", response[:200])

    result = DecompositionResult.from_json(response)

    if result.is_composite:
        logger.info(
            "Question decomposed into %d sub-questions (strategy: %s)",
            len(result.sub_questions),
            result.merge_strategy,
        )
    else:
        logger.info("Question classified as simple (no decomposition needed)")

    return result
