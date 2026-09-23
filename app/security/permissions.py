"""
权限控制：行级 + 列级权限。

设计要点：
- 列级：根据角色隐藏敏感列（如 employee 不能看 salary/id_card）
- 行级：根据角色注入 WHERE 谓词（如 employee 只能查自己）
- 权限规则硬编码在代码中（不从 LLM 获取）
- 权限检查在 AST 校验之后、执行之前
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.models.plan import QueryPlan
from app.models.state import UserContext
from app.schema_rag.metadata import SchemaMetadata, Table

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
            (condition, params) 或 None（不需要注入）
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
        # 其他表需要通过 department 关联
        return None


# 权限规则注册表
PERMISSION_RULES: dict[str, PermissionRule] = {
    "employee": PermissionRule(
        role="employee",
        hidden_columns={
            "employee": {"salary", "id_card"},
            "performance": {"bonus"},
        },
        row_predicate=EmployeeRowPredicate(),
    ),
    "dept_lead": PermissionRule(
        role="dept_lead",
        hidden_columns={
            "employee": {"id_card"},
        },
        row_predicate=DeptLeadRowPredicate(),
    ),
    "bu_head": PermissionRule(
        role="bu_head",
        hidden_columns={
            "employee": {"id_card"},
        },
        row_predicate=BUHeadRowPredicate(),
    ),
    "exec": PermissionRule(
        role="exec",
        hidden_columns={},  # 高管可以看所有列
        row_predicate=None,  # 没有行级限制
    ),
}


class PermissionChecker:
    """
    权限检查器。

    职责：
    1. 过滤敏感列（从 QueryPlan 的 select_columns 中移除）
    2. 注入行级谓词（向 QueryPlan 的 where_conditions 中添加）
    """

    def __init__(self, metadata: SchemaMetadata):
        self.metadata = metadata

    def apply_column_permissions(
        self, plan: QueryPlan, user: UserContext
    ) -> QueryPlan:
        """
        应用列级权限：从 SELECT 中移除敏感列。

        如果 SELECT *，展开为可见列列表。
        注意：必须在 apply_row_permissions 之后调用，
        以便检测"仅查自身数据"的场景（此时 salary 应可见）。
        """
        rule = PERMISSION_RULES.get(user.role)
        if not rule:
            return plan

        # 检测是否为"仅查自身数据"的查询
        is_self_query = self._is_self_query(plan, user)

        # 展开 SELECT *
        expanded_cols: list[str] = []
        for col_expr in plan.select_columns:
            if col_expr.strip() == "*":
                # 展开为所有可见列
                for table_name in plan.target_tables:
                    table = self.metadata.get_table(table_name)
                    if table:
                        hidden = rule.hidden_columns.get(table_name, set())
                        for col in table.visible_columns:
                            if col not in hidden:
                                expanded_cols.append(f"{table_name}.{col}")
            else:
                expanded_cols.append(col_expr)

        # 过滤敏感列
        filtered_cols: list[str] = []
        for col_expr in expanded_cols:
            if self._is_column_allowed(col_expr, plan.target_tables, user.role, is_self_query):
                filtered_cols.append(col_expr)
            else:
                logger.info("Column permission denied for: %s", col_expr)

        plan.select_columns = filtered_cols
        return plan

    def apply_row_permissions(
        self, plan: QueryPlan, user: UserContext
    ) -> QueryPlan:
        """
        应用行级权限：注入 WHERE 谓词。

        对每个目标表，如果该角色有行级限制，注入对应的 WHERE 条件。
        """
        rule = PERMISSION_RULES.get(user.role)
        if not rule or not rule.row_predicate:
            return plan

        for table_name in plan.target_tables:
            predicate = rule.row_predicate.generate(user, table_name)
            if predicate:
                condition, params = predicate
                plan.where_conditions.append(condition)
                plan.where_params.extend(params)
                logger.info("Row predicate injected for %s: %s", table_name, condition)

        return plan

    def _is_self_query(self, plan: QueryPlan, user: UserContext) -> bool:
        """
        检测当前查询是否仅限于用户自身数据。

        如果 WHERE 条件中已注入 emp_id = user_id（行级权限），
        则该查询只返回用户自己的数据，此时 salary 等敏感列应可见。
        """
        if user.role != "employee":
            return False
        emp_id_str = str(int(user.user_id))
        for cond in plan.where_conditions:
            if f"emp_id = %s" in cond and any(
                str(p) == emp_id_str for p in plan.where_params
            ):
                return True
        return False

    def _is_column_allowed(
        self, col_expr: str, tables: list[str], role: str, is_self_query: bool = False
    ) -> bool:
        """检查列是否被允许访问"""
        rule = PERMISSION_RULES.get(role)
        if not rule:
            return True

        # 简单解析列名（处理 table.column 格式）
        col_name = col_expr.strip().split(".")[-1].split()[0].lower()

        # 函数调用（如 COUNT(*)）放行
        if "(" in col_expr:
            return True

        # 员工查自身数据时，salary 可见
        if is_self_query and col_name == "salary" and role == "employee":
            return True

        for table_name in tables:
            hidden = rule.hidden_columns.get(table_name, set())
            if col_name in hidden:
                return False

        return True
