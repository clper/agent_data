"""
权限控制测试：验证列级隐藏 + 行级注入。

测试策略：
- 各角色的敏感列过滤
- 各角色的行级谓词注入
- SELECT * 展开为可见列
"""
import pytest

from app.models.plan import QueryPlan
from app.models.state import UserContext
from app.schema_rag.metadata import Column, SchemaMetadata, Table
from app.security.permissions import PermissionChecker


@pytest.fixture
def metadata():
    tables = {
        "employee": Table(
            name="employee",
            cn_name="员工表",
            columns=[
                Column(name="emp_id", type="BIGINT", pk=True),
                Column(name="name", type="VARCHAR(64)"),
                Column(name="dept_id", type="BIGINT"),
                Column(name="salary", type="DECIMAL(12,2)", sensitive=True),
                Column(name="id_card", type="CHAR(18)", sensitive=True),
            ],
        ),
        "performance": Table(
            name="performance",
            cn_name="绩效表",
            columns=[
                Column(name="perf_id", type="BIGINT", pk=True),
                Column(name="emp_id", type="BIGINT"),
                Column(name="month", type="CHAR(7)"),
                Column(name="score", type="DECIMAL(5,2)"),
                Column(name="bonus", type="DECIMAL(12,2)", sensitive=True),
            ],
        ),
    }
    return SchemaMetadata(database="test_db", tables=tables)


@pytest.fixture
def checker(metadata):
    return PermissionChecker(metadata)


# ═══════════════════════════════════════════
# 列级权限测试
# ═══════════════════════════════════════════

class TestColumnPermissions:
    """列级权限测试"""

    def test_employee_cannot_see_salary(self, checker):
        """普通员工不能看 salary"""
        user = UserContext(user_id="101", role="employee")
        plan = QueryPlan(
            intent="data_query",
            target_tables=["employee"],
            select_columns=["name", "salary"],
            where_conditions=[],
            where_params=[],
        )
        result = checker.apply_column_permissions(plan, user)
        assert "salary" not in result.select_columns
        assert "name" in result.select_columns

    def test_employee_cannot_see_id_card(self, checker):
        """普通员工不能看 id_card"""
        user = UserContext(user_id="101", role="employee")
        plan = QueryPlan(
            intent="data_query",
            target_tables=["employee"],
            select_columns=["name", "id_card"],
            where_conditions=[],
            where_params=[],
        )
        result = checker.apply_column_permissions(plan, user)
        assert "id_card" not in result.select_columns

    def test_employee_cannot_see_bonus(self, checker):
        """普通员工不能看 bonus"""
        user = UserContext(user_id="101", role="employee")
        plan = QueryPlan(
            intent="data_query",
            target_tables=["performance"],
            select_columns=["score", "bonus"],
            where_conditions=[],
            where_params=[],
        )
        result = checker.apply_column_permissions(plan, user)
        assert "bonus" not in result.select_columns
        assert "score" in result.select_columns

    def test_exec_can_see_all(self, checker):
        """高管可以看所有列"""
        user = UserContext(user_id="admin", role="exec")
        plan = QueryPlan(
            intent="data_query",
            target_tables=["employee"],
            select_columns=["name", "salary", "id_card"],
            where_conditions=[],
            where_params=[],
        )
        result = checker.apply_column_permissions(plan, user)
        assert len(result.select_columns) == 3

    def test_select_star_expansion(self, checker):
        """SELECT * 展开为可见列"""
        user = UserContext(user_id="101", role="employee")
        plan = QueryPlan(
            intent="data_query",
            target_tables=["employee"],
            select_columns=["*"],
            where_conditions=[],
            where_params=[],
        )
        result = checker.apply_column_permissions(plan, user)
        # employee 表有 emp_id, name, dept_id, salary, id_card
        # employee 角色隐藏 salary, id_card
        # 展开后应只有 emp_id, name, dept_id
        assert "salary" not in " ".join(result.select_columns)
        assert "id_card" not in " ".join(result.select_columns)
        assert len(result.select_columns) == 3  # emp_id, name, dept_id


# ═══════════════════════════════════════════
# 行级权限测试
# ═══════════════════════════════════════════

class TestRowPermissions:
    """行级权限测试"""

    def test_employee_row_predicate(self, checker):
        """普通员工：注入 emp_id = %s"""
        user = UserContext(user_id="101", role="employee")
        plan = QueryPlan(
            intent="data_query",
            target_tables=["employee"],
            select_columns=["name"],
            where_conditions=[],
            where_params=[],
        )
        result = checker.apply_row_permissions(plan, user)
        assert len(result.where_conditions) == 1
        assert "emp_id = %s" in result.where_conditions[0]
        assert "101" in result.where_params  # user_id 是字符串

    def test_exec_no_row_predicate(self, checker):
        """高管：不注入行级谓词"""
        user = UserContext(user_id="admin", role="exec")
        plan = QueryPlan(
            intent="data_query",
            target_tables=["employee"],
            select_columns=["name"],
            where_conditions=[],
            where_params=[],
        )
        result = checker.apply_row_permissions(plan, user)
        assert len(result.where_conditions) == 0

    def test_dept_lead_row_predicate(self, checker):
        """部门主管：注入 dept_id = %s"""
        user = UserContext(user_id="lead1", role="dept_lead", department_id=1)
        plan = QueryPlan(
            intent="data_query",
            target_tables=["employee"],
            select_columns=["name"],
            where_conditions=[],
            where_params=[],
        )
        result = checker.apply_row_permissions(plan, user)
        assert len(result.where_conditions) == 1
        assert "dept_id = %s" in result.where_conditions[0]
