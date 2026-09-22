"""
数据契约：查询计划 + 执行结果 + Agent 响应。

这是各模块间传递数据的"合同"，任何修改都要考虑上下游模块的兼容性。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryPlan:
    """
    模型生成的查询计划（结构化 JSON 的 Python 表示）。

    为什么不让模型直接生成 SQL？
    → 结构化计划让 AST 校验器有明确的"白名单"可以比对
    → 比直接校验自由文本 SQL 更安全、更可解释
    """
    intent: str                          # "data_query" | "meta" | "out_of_scope"
    target_tables: list[str]             # 目标表名（必须在 schema 内）
    select_columns: list[str]            # 查询列（支持别名，如 "COUNT(*) AS total"）
    where_conditions: list[str]          # WHERE 条件（参数化，%s 占位）
    where_params: list[Any]              # WHERE 参数值
    joins: list[JoinClause] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)
    limit: int | None = None
    explanation: str = ""                # 模型对查询的自然语言解释

    def to_sql(self) -> tuple[str, list[Any]]:
        """
        将查询计划编译为 SQL 字符串 + 参数列表。
        返回 (sql_template, params)，sql_template 中用 %s 占位。
        """
        # SELECT 子句
        cols = ", ".join(self.select_columns) if self.select_columns else "*"
        sql = f"SELECT {cols}"

        # FROM 子句
        primary = self.target_tables[0] if self.target_tables else ""
        sql += f" FROM {primary}"

        # JOIN 子句
        for j in self.joins:
            sql += f" {j.join_type} JOIN {j.table} ON {j.on_condition}"

        # WHERE 子句
        if self.where_conditions:
            where_str = " AND ".join(self.where_conditions)
            sql += f" WHERE {where_str}"

        # GROUP BY
        if self.group_by:
            sql += f" GROUP BY {', '.join(self.group_by)}"

        # ORDER BY
        if self.order_by:
            sql += f" ORDER BY {', '.join(self.order_by)}"

        # LIMIT
        if self.limit is not None:
            sql += f" LIMIT {self.limit}"

        return sql, list(self.where_params)


@dataclass
class JoinClause:
    """JOIN 子句的结构化表示"""
    table: str
    on_condition: str
    join_type: str = "INNER"  # INNER / LEFT / RIGHT


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
