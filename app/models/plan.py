"""
数据契约：执行结果 + Agent 响应。

这是各模块间传递数据的"合同"，任何修改都要考虑上下游模块的兼容性。

注意：QueryPlan 已废弃，SQL 生成改为 LLM 直接输出 SQL + sqlglot AST 处理。
参见 app/sql/generator.py 和 app/sql/compiler.py。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecResult:
    """SQL 执行结果"""
    sql: str                           # 最终执行的 SQL（含注入的行级条件）
    params: list[Any]                  # 参数列表
    columns: list[str]                 # 列名
    rows: list[tuple]                  # 数据行
    row_count: int                     # 实际返回行数
    truncated: bool = False            # 是否被截断（超过 max_rows）
    execution_time_ms: float = 0.0     # 执行耗时（毫秒）

    def to_markdown_table(self) -> str:
        """将结果渲染为 Markdown 表格（用于 LLM 解释）"""
        if not self.rows:
            return "（无数据）"
        header = "| " + " | ".join(self.columns) + " |"
        separator = "| " + " | ".join("---" for _ in self.columns) + " |"
        lines = [header, separator]
        for row in self.rows[:50]:  # Markdown 预览最多 50 行
            lines.append("| " + " | ".join(str(v) for v in row) + " |")
        if self.truncated:
            lines.append(f"\n（结果已截断，仅展示前 {len(self.rows)} 行）")
        return "\n".join(lines)


@dataclass
class AgentResponse:
    """Agent 最终返回给用户的响应"""
    answer: str                        # 自然语言回答
    sql: str = ""                      # 执行的 SQL（透明度）
    tables_used: list[str] = field(default_factory=list)  # 用到的表
    row_count: int = 0                 # 结果行数
    execution_time_ms: float = 0.0     # 总执行耗时
    error: str | None = None           # 错误信息（如果有）
    needs_clarification: bool = False  # 是否需要追问
    clarification_question: str = ""   # 追问内容
