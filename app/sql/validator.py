"""
AST 白名单校验器：用 sqlglot 解析后的 AST 进行安全校验。

这是整个安全链路的核心——即使 LLM 被注入，这一层也能拦住危险操作。

校验规则：
1. 只允许 SELECT（禁止 INSERT/UPDATE/DELETE/DROP/ALTER 等）
2. 禁止危险函数（SLEEP、BENCHMARK、LOAD_FILE 等）
3. 禁止危险表达式（INTO OUTFILE、FOR UPDATE 等）
4. 目标表必须在白名单中
5. 禁止多语句（; 分隔的多个 SQL）
6. 列名必须在 schema 白名单中（防止幻觉列）
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

import sqlglot
from sqlglot import exp

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
    - 第四道：列白名单检查（结合 schema）
    """

    def __init__(self, metadata: SchemaMetadata):
        self.metadata = metadata

    def validate_ast(
        self,
        ast: exp.Select,
        allowed_tables: set[str],
    ) -> list[ValidationError]:
        """
        AST 级安全校验。

        Args:
            ast: sqlglot 解析后的 AST
            allowed_tables: 允许的表名集合（来自 Schema RAG 检索结果）

        Returns:
            校验错误列表（空列表表示通过）
        """
        errors: list[ValidationError] = []

        # 1. 检查危险函数
        for func in ast.find_all(exp.Func):
            func_name = func.name.upper() if hasattr(func, "name") else ""
            if func_name in DANGEROUS_FUNCTIONS:
                errors.append(ValidationError(
                    code="dangerous_function",
                    message=f"禁止的函数: {func_name}",
                ))

        # 2. 检查禁止的表达式（INTO、LOCK 等）
        for expr_type in FORBIDDEN_EXPRESSIONS:
            for found in ast.find_all(expr_type):
                errors.append(ValidationError(
                    code="forbidden_expression",
                    message=f"禁止的表达式: {type(found).__name__}",
                ))

        # 3. 检查表白名单
        for table in ast.find_all(exp.Table):
            table_name = table.name.lower()
            if table_name and table_name not in allowed_tables:
                errors.append(ValidationError(
                    code="table_not_allowed",
                    message=f"表 {table_name} 不在允许列表中",
                ))

        # 4. 检查列白名单（宽松模式：只检查已知表中的列）
        errors.extend(self._validate_columns(ast))

        return errors

    def _validate_columns(self, ast: exp.Select) -> list[ValidationError]:
        """
        列白名单校验。

        策略：对于能确定所属表的列引用，检查列是否存在于 schema 中。
        对于无法确定所属表的（如聚合函数参数、别名引用），跳过检查。
        """
        errors: list[ValidationError] = []

        # 构建别名 → 表名映射
        alias_map: dict[str, str] = {}
        for table in ast.find_all(exp.Table):
            table_name = table.name.lower()
            alias_expr = table.args.get("alias")
            if alias_expr and hasattr(alias_expr, "name"):
                alias_map[alias_expr.name.lower()] = table_name
            alias_map[table_name] = table_name

        # 检查列引用
        checked: set[str] = set()
        for col in ast.find_all(exp.Column):
            col_name = col.name.lower()
            if col_name == "*":
                continue

            table_part = col.table
            if table_part:
                real_table = alias_map.get(table_part.lower(), table_part.lower())
                table_meta = self.metadata.get_table(real_table)
                if table_meta:
                    key = f"{real_table}.{col_name}"
                    if key not in checked:
                        checked.add(key)
                        if col_name not in table_meta.all_column_names:
                            errors.append(ValidationError(
                                code="unknown_column",
                                message=f"列 {col_name} 不存在于表 {real_table} 中",
                            ))

        return errors

    def validate_sql_string(self, sql: str, allowed_tables: set[str]) -> list[ValidationError]:
        """
        校验 SQL 字符串（用于多语句检测等 AST 解析前的检查）。
        """
        errors: list[ValidationError] = []

        # 检查多语句
        if _has_multiple_statements(sql):
            errors.append(ValidationError(
                code="multiple_statements",
                message="禁止多语句执行",
            ))

        return errors

    def validate_sql(self, sql: str, allowed_tables: set[str]) -> list[ValidationError]:
        """
        完整校验链：先检查 SQL 字符串，再解析 AST 并校验。

        兼容旧接口，供测试和外部调用。
        """
        errors = self.validate_sql_string(sql, allowed_tables)
        if errors:
            return errors

        # 解析 SQL 为 AST
        cleaned = sql.replace("%s", "?")
        try:
            parsed = sqlglot.parse(cleaned, read="mysql")
        except Exception as e:
            return [ValidationError(code="parse_error", message=f"SQL 解析失败: {e}")]

        if not parsed or not parsed[0]:
            return [ValidationError(code="empty_sql", message="SQL 为空")]

        stmt = parsed[0]
        if not isinstance(stmt, exp.Select):
            return [ValidationError(
                code="forbidden_statement",
                message=f"禁止的语句类型: {type(stmt).__name__}",
            )]

        return self.validate_ast(stmt, allowed_tables)


def _has_multiple_statements(sql: str) -> bool:
    """检查是否包含多条语句（用 ; 分隔）"""
    cleaned = re.sub(r"'[^']*'", "", sql)
    return ";" in cleaned.strip()


def validate_and_raise(
    validator: SecurityValidator,
    ast: exp.Select,
    allowed_tables: set[str],
) -> None:
    """
    执行完整校验链，有错误则抛出 PermissionError。

    为什么抛 PermissionError？
    → Agent 主循环中，PermissionError 会短路修复循环（不再重试）
    → 安全错误不可修复，不应浪费重试次数
    """
    errors = validator.validate_ast(ast, allowed_tables)
    if errors:
        msg = "; ".join(e.message for e in errors)
        logger.warning("AST validation failed: %s", msg)
        raise PermissionError(f"SQL 校验失败: {msg}")
