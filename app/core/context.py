"""
执行上下文：存储多步推理的中间结果。

设计要点：
- 每个子问题执行后，将结果存入 ExecutionContext
- 后续子问题可以引用前面的结果（通过 {result_N} 占位符）
- 支持多种数据类型：标量值、行记录、表格数据

示例：
子问题 1："研发部绩效最高的员工是谁？" → 返回 {"name": "王五", "score": 85}
子问题 2："王五的入职日期是什么时候？" → 引用 result_1 中的 name
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class SubResult:
    """单个子问题的执行结果"""
    id: int
    question: str
    answer: str  # 自然语言回答
    sql: str = ""  # 生成的 SQL
    columns: list[str] = field(default_factory=list)  # 列名
    rows: list[tuple] = field(default_factory=list)  # 查询结果（tuple 列表）
    error: str = ""  # 如果有错误

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "answer": self.answer,
            "sql": self.sql,
            "rows": self.rows,
            "error": self.error,
        }


class ExecutionContext:
    """
    执行上下文：管理多步推理的中间状态。

    用法：
        ctx = ExecutionContext()
        ctx.add_result(1, SubResult(...))
        placeholder_text = ctx.resolve_placeholders("{result_1} 的入职日期")
        # → "王五 的入职日期"
    """

    def __init__(self):
        self.results: dict[int, SubResult] = {}

    def add_result(self, sub_id: int, result: SubResult) -> None:
        """添加子问题结果"""
        self.results[sub_id] = result
        logger.info("Added result for sub-question %d", sub_id)

    def get_result(self, sub_id: int) -> SubResult | None:
        """获取子问题结果"""
        return self.results.get(sub_id)

    def resolve_placeholders(self, text: str) -> str:
        """
        解析文本中的 {result_N} 占位符。

        支持的格式：
        - {result_N}：单行→替换为第一个字段值；多行→替换为所有值的列表
        - {result_N.field}：替换为指定字段的值

        示例：
        >>> ctx.resolve_placeholders("{result_1} 的入职日期")
        "王五 的入职日期"
        >>> ctx.resolve_placeholders("{result_1.name}")
        "王五"
        >>> # 多行结果：
        >>> ctx.resolve_placeholders("查询 {result_1} 的业绩")
        "查询 101, 102 的业绩"
        """
        import re

        def replacer(match: re.Match) -> str:
            expr = match.group(1)  # result_1 或 result_1.name
            parts = expr.split(".")
            sub_id = int(parts[0].replace("result_", ""))

            result = self.results.get(sub_id)
            if not result or not result.rows:
                return match.group(0)  # 保留原样

            if len(parts) > 1:
                # 指定了字段名 → 取所有行该字段的值
                field_name = parts[1]
                if field_name in result.columns:
                    idx = result.columns.index(field_name)
                    if len(result.rows) == 1:
                        return str(result.rows[0][idx])
                    else:
                        values = [str(row[idx]) for row in result.rows if idx < len(row)]
                        return ", ".join(values)
                else:
                    return match.group(0)  # 字段不存在
            else:
                # 未指定字段名
                if len(result.rows) == 1:
                    # 单行：取第一个字段（保持原有行为）
                    return str(result.rows[0][0]) if result.rows[0] else ""
                else:
                    # 多行：取所有行的第一个字段值
                    values = [str(row[0]) for row in result.rows if row]
                    return ", ".join(values)

        # 匹配 {result_N} 或 {result_N.field}
        pattern = r"\{(result_\d+)(?:\.(\w+))?\}"
        resolved = re.sub(pattern, replacer, text)

        logger.debug("Resolved placeholders: '%s' → '%s'", text, resolved)
        return resolved

    def get_dependency_context(self, sub_q) -> str:
        """
        为有依赖的子问题生成上下文描述。

        将依赖的前序子问题结果格式化为自然语言 + 数据表，
        帮助 LLM 生成正确的 SQL（如 IN 子句）。

        Args:
            sub_q: SubQuestion 对象（需含 depends_on 属性）

        Returns:
            上下文描述字符串，无依赖时返回空字符串
        """
        if not getattr(sub_q, 'depends_on', None):
            return ""

        parts = []
        for dep_id in sub_q.depends_on:
            result = self.results.get(dep_id)
            if not result:
                continue

            if result.error:
                parts.append(f"[注意：子问题 {dep_id} 执行失败: {result.error}]")
                continue

            if not result.rows:
                parts.append(f"[注意：子问题 {dep_id} 返回空结果]")
                continue

            # 构建数据表
            lines = [f"子问题 {dep_id} 的结果（{len(result.rows)} 行）："]
            # 列名
            lines.append("  " + " | ".join(result.columns))
            # 数据行（最多 10 行）
            for row in result.rows[:10]:
                lines.append("  " + " | ".join(str(v) for v in row))
            if len(result.rows) > 10:
                lines.append(f"  ... 共 {len(result.rows)} 行")

            # 第一列值列表（用于 SQL IN 子句提示）
            first_col_values = [str(row[0]) for row in result.rows if row]
            lines.append(f"  第一列所有值: {', '.join(first_col_values)}")

            parts.append("\n".join(lines))

        if parts:
            return "\n\n前序查询结果：\n" + "\n\n".join(parts)
        return ""

    def summarize_all(self) -> str:
        """
        汇总所有子问题的结果为一段文本。

        用于传递给 Merger 进行最终综合。
        """
        lines = []
        for sub_id in sorted(self.results.keys()):
            result = self.results[sub_id]
            lines.append(f"【子问题 {sub_id}】{result.question}")
            if result.error:
                lines.append(f"  ❌ 错误：{result.error}")
            else:
                lines.append(f"  ✅ 回答：{result.answer}")
                if result.rows:
                    lines.append(f"  📊 数据：{len(result.rows)} 行")

        return "\n".join(lines)

    def is_complete(self) -> bool:
        """检查是否所有子问题都已执行"""
        return len(self.results) > 0
