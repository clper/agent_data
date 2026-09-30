"""
权限控制：行级 + 列级权限（AST 级操作）。

设计要点：
- 列级：根据角色过滤 SELECT 中的敏感列
- 行级：根据角色向 WHERE 注入行级谓词
- 权限规则硬编码在代码中（不从 LLM 获取）
- 行级权限先于列级权限应用（使列级能感知"自身查询"场景）
- 所有操作基于 sqlglot AST，不依赖 QueryPlan
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlglot import exp

from app.models.state import UserContext
from app.schema_rag.metadata import SchemaMetadata, Table
from app.sql import compiler

logger = logging.getLogger(__name__)


@dataclass
class PermissionRule:
    """权限规则"""
    role: str
    # 列级：该角色不能看到的列 {表名: {列名集合}}
    hidden_columns: dict[str, set[str]]
    # 行级：该角色的行级谓词生成器
    row_predicate: RowPredicateGenerator | None = None


class RowPredicateGenerator:
    """行级谓词生成器"""

    def generate(self, user: UserContext, table_name: str) -> tuple[str, list[Any]] | None:
        """
        为指定表生成行级 WHERE 谓词。

        Returns:
            (condition_template, params) 或 None（不需要注入）
            condition_template 中的 %s 由 params 中的值填充
        """
        return None


class EmployeeRowPredicate(RowPredicateGenerator):
    """普通员工：只能查自己的数据"""

    def generate(self, user: UserContext, table_name: str) -> tuple[str, list[Any]] | None:
        if table_name in ("employee", "performance", "attendance"):
            # 转换为 int，避免字符串与 BIGINT 列的类型不匹配
            return f"{table_name}.emp_id = %s", [int(user.user_id)]
        return None


class DeptLeadRowPredicate(RowPredicateGenerator):
    """部门主管：只能查自己部门的数据"""

    def generate(self, user: UserContext, table_name: str) -> tuple[str, list[Any]] | None:
        if table_name in ("employee", "performance", "attendance"):
            return f"{table_name}.dept_id = %s", [user.department_id]
        if table_name in ("department",):
            return f"department.dept_id = %s", [user.department_id]
        # revenue 和 cost 表也有 dept_id，部门主管应能查看本部门财务数据
        if table_name in ("revenue", "cost"):
            return f"{table_name}.dept_id = %s", [user.department_id]
        return None


class BUHeadRowPredicate(RowPredicateGenerator):
    """事业部负责人：只能查自己事业部的数据"""

    def generate(self, user: UserContext, table_name: str) -> tuple[str, list[Any]] | None:
        if table_name == "department":
            return "department.bu_id = %s", [user.business_unit_id]
        return None


# 全局隐藏列：任何角色都不可查看（高度敏感数据）
GLOBAL_HIDDEN_COLUMNS: dict[str, set[str]] = {
    "employee": {"id_card"},
}


# 权限规则注册表
PERMISSION_RULES: dict[str, PermissionRule] = {
    "employee": PermissionRule(
        role="employee",
        hidden_columns={
            "employee": {"salary"},
            "performance": {"bonus"},
        },
        row_predicate=EmployeeRowPredicate(),
    ),
    "dept_lead": PermissionRule(
        role="dept_lead",
        hidden_columns={},
        row_predicate=DeptLeadRowPredicate(),
    ),
    "bu_head": PermissionRule(
        role="bu_head",
        hidden_columns={},
        row_predicate=BUHeadRowPredicate(),
    ),
    "exec": PermissionRule(
        role="exec",
        hidden_columns={},  # 高管可以看所有列（除全局隐藏列）
        row_predicate=None,  # 没有行级限制
    ),
}


class PermissionChecker:
    """
    权限检查器（AST 级操作）。

    职责：
    1. 行级权限：向 AST 的 WHERE 子句注入谓词
    2. 列级权限：过滤 AST 的 SELECT 列
    """

    def __init__(self, metadata: SchemaMetadata):
        self.metadata = metadata

    def apply_row_permissions(
        self, ast: exp.Select, user: UserContext
    ) -> exp.Select:
        """
        应用行级权限：向 WHERE 注入谓词。

        遍历 AST 中的所有表，如果该角色有行级限制，注入对应的 WHERE 条件。
        """
        rule = PERMISSION_RULES.get(user.role)
        if not rule or not rule.row_predicate:
            return ast

        alias_map = compiler.extract_alias_map(ast)
        tables_in_query = compiler.extract_tables(ast)

        for table_name in tables_in_query:
            predicate = rule.row_predicate.generate(user, table_name)
            if predicate:
                condition_template, param_values = predicate
                ast = compiler.inject_row_predicate(
                    ast, table_name, condition_template, alias_map,
                    param_values=param_values,
                )

        return ast

    def apply_column_permissions(
        self, ast: exp.Select, user: UserContext, original_sql: str = ""
    ) -> exp.Select:
        """
        应用列级权限：过滤 SELECT 中的敏感列。

        如果 SELECT *，展开为可见列列表。
        注意：必须在 apply_row_permissions 之后调用，
        以便检测"仅查自身数据"的场景（此时 salary 应可见）。
        """
        rule = PERMISSION_RULES.get(user.role)
        if not rule:
            return ast

        # 构建有效隐藏列集合（角色级 + 全局级）
        effective_hidden: dict[str, set[str]] = {}
        # 先合并全局隐藏列（如 id_card 任何角色不可看）
        for table, cols in GLOBAL_HIDDEN_COLUMNS.items():
            effective_hidden[table] = set(cols)
        # 再合并角色级隐藏列
        for table, cols in rule.hidden_columns.items():
            effective_hidden.setdefault(table, set()).update(cols)

        # 检测是否为"仅查自身数据"的查询
        is_self = compiler.is_self_query(ast, user.user_id, user.role, original_sql)
        if is_self:
            # 员工查自身数据时，salary 可见
            effective_hidden = {
                table: cols - {"salary"}
                for table, cols in effective_hidden.items()
            }
            logger.info("Self-query detected: salary visible for employee %s", user.user_id)

        # 使用 compiler 过滤列
        ast = compiler.filter_select_columns(ast, effective_hidden, self.metadata)

        return ast
