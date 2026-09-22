"""
AST 白名单校验器：用 sqlglot 解析 SQL，检查是否符合安全规则。

这是整个安全链路的核心——即使 LLM 被注入，这一层也能拦住危险操作。

校验规则：
1. 只允许 SELECT（禁止 INSERT/UPDATE/DELETE/DROP/ALTER 等）
2. 禁止子查询中的危险函数（SLEEP、BENCHMARK、LOAD_FILE 等）
3. 禁止 INTO OUTFILE / INTO DUMPFILE
4. 禁止多语句（; 分隔的多个 SQL）
5. 目标表必须在白名单中
6. 禁止 FOR UPDATE / LOCK IN SHARE MODE
7. 列名必须在 schema 白名单中（防止注入）
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

from app.models.plan import QueryPlan
from app.schema_rag.metadata import SchemaMetadata

logger = logging.getLogger(__name__)

# 危险函数黑名单
DANGEROUS_FUNCTIONS = {
    "SLEEP", "BENCHMARK", "LOAD_FILE", "SYSTEM_USER", "VERSION",
    "INTO OUTFILE", "INTO DUMPFILE", "EXEC", "EXECUTE", "xp_cmdshell",
}

# 禁止的表达式类型
FORBIDDEN_EXPRESSIONS = {
    exp.Into,           # INTO OUTFILE / DUMPFILE
    exp.Lock,           # FOR UPDATE / LOCK IN SHARE MODE
}

# 允许的语句类型
ALLOWED_STATEMENTS = {exp.Select}


@dataclass
class ValidationError:
    """校验错误"""
    code: str                          # "forbidden_statement" | "dangerous_function" | ...
    message: str
    detail: str = ""


class SecurityValidator:
    """
    SQL 安全校验器。

    深度防御策略：
    - 第一道：AST 结构检查（只允许 SELECT）
    - 第二道：函数黑名单检查
    - 第三道：表白名单检查
    - 第四道：列白名单检查（结合权限层）
    """

    def __init__(self, metadata: SchemaMetadata):
        self.metadata = metadata

    def validate_plan(self, plan: QueryPlan) -> list[ValidationError]:
        """
        校验查询计划（在编译为 SQL 之前）。

        检查：
        - 目标表是否在 schema 中
        - SELECT 列是否合法
        """
        errors: list[ValidationError] = []

        # 检查目标表
        for table_name in plan.target_tables:
            if not self.metadata.get_table(table_name):
                errors.append(ValidationError(
                    code="unknown_table",
                    message=f"表 {table_name} 不在 schema 中",
                ))

        # 检查 SELECT 列（防止注入）
        for col_expr in plan.select_columns:
            if self._contains_injection(col_expr):
                errors.append(ValidationError(
                    code="column_injection",
                    message=f"SELECT 列包含可疑内容: {col_expr}",
                ))

        # 检查 WHERE 条件（防止注入）
        for cond in plan.where_conditions:
            if self._contains_injection(cond):
                errors.append(ValidationError(
                    code="condition_injection",
                    message=f"WHERE 条件包含可疑内容: {cond}",
                ))

        return errors

    def validate_sql(self, sql: str, allowed_tables: set[str]) -> list[ValidationError]:
        """
        校验最终编译的 SQL 字符串（AST 级别）。

        这是最后的安全防线——即使前面所有层都失败了，这里也能拦住。
        """
        errors: list[ValidationError] = []

        # 1. 检查多语句
        if self._has_multiple_statements(sql):
            errors.append(ValidationError(
                code="multiple_statements",
                message="禁止多语句执行",
            ))
            return errors  # 多语句直接拒绝，不再解析

        # 2. 解析 AST（先将 %s 替换为 ? 以便 sqlglot 解析）
        sanitized_sql = sql.replace("%s", "?")
        try:
            parsed = sqlglot.parse(sanitized_sql)
        except Exception as e:
            errors.append(ValidationError(
                code="parse_error",
                message=f"SQL 解析失败: {e}",
            ))
            return errors

        if not parsed:
            errors.append(ValidationError(
                code="empty_sql",
                message="SQL 为空",
            ))
            return errors

        # 3. 只允许 SELECT
        for stmt in parsed:
            if not isinstance(stmt, tuple(ALLOWED_STATEMENTS)):
                errors.append(ValidationError(
                    code="forbidden_statement",
                    message=f"禁止的语句类型: {type(stmt).__name__}",
                ))
                return errors

        # 4. 检查危险函数
        for stmt in parsed:
            for func in stmt.find_all(exp.Func):
                func_name = func.name.upper() if hasattr(func, "name") else ""
                if func_name in DANGEROUS_FUNCTIONS:
                    errors.append(ValidationError(
                        code="dangerous_function",
                        message=f"禁止的函数: {func_name}",
                    ))

        # 5. 检查禁止的表达式（INTO、LOCK 等）
        for stmt in parsed:
            for expr_type in FORBIDDEN_EXPRESSIONS:
                for found in stmt.find_all(expr_type):
                    errors.append(ValidationError(
                        code="forbidden_expression",
                        message=f"禁止的表达式: {type(found).__name__}",
                    ))

        # 6. 检查表白名单
        for stmt in parsed:
            for table in stmt.find_all(exp.Table):
                table_name = table.name.lower()
                if table_name and table_name not in allowed_tables:
                    errors.append(ValidationError(
                        code="table_not_allowed",
                        message=f"表 {table_name} 不在允许列表中",
                    ))

        return errors

    def _has_multiple_statements(self, sql: str) -> bool:
        """检查是否包含多条语句（用 ; 分隔）"""
        # 去除字符串字面量中的 ;
        cleaned = re.sub(r"'[^']*'", "", sql)
        return ";" in cleaned.strip()

    def _contains_injection(self, text: str) -> bool:
        """
        检查文本是否包含 SQL 注入特征。

        这是启发式检查，AST 校验是更可靠的后盾。
        """
        suspicious_patterns = [
            r";\s*(DROP|INSERT|UPDATE|DELETE|ALTER|CREATE|EXEC)",
            r"UNION\s+(ALL\s+)?SELECT",
            r"--\s*$",
            r"/\*.*\*/",
            r"OR\s+1\s*=\s*1",
            r"'\s*OR\s*'",
        ]
        for pattern in suspicious_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False


def validate_and_raise(
    validator: SecurityValidator,
    plan: QueryPlan,
    sql: str,
    allowed_tables: set[str],
) -> None:
    """
    执行完整校验链，有错误则抛出 PermissionError。

    为什么抛 PermissionError 而不是返回错误列表？
    → Agent 主循环中，PermissionError 会短路修复循环（不再重试）
    → 安全错误不可修复，不应浪费重试次数
    """
    # 计划级校验
    plan_errors = validator.validate_plan(plan)
    if plan_errors:
        msg = "; ".join(e.message for e in plan_errors)
        logger.warning("Plan validation failed: %s", msg)
        raise PermissionError(f"查询计划校验失败: {msg}")

    # SQL 级校验
    sql_errors = validator.validate_sql(sql, allowed_tables)
    if sql_errors:
        msg = "; ".join(e.message for e in sql_errors)
        logger.warning("SQL validation failed: %s", msg)
        raise PermissionError(f"SQL 校验失败: {msg}")
