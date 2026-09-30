"""
权限控制测试：验证 AST 级列过滤 + 行级注入。

测试策略：
- 各角色的敏感列过滤
- 各角色的行级谓词注入
- SELECT * 展开为可见列
"""
import pytest

from app.models.state import UserContext
from app.schema_rag.metadata import Column, SchemaMetadata, Table
from app.security.permissions import PermissionChecker
from app.sql import compiler


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
    """列级权限测试（AST 级）"""

    def test_employee_cannot_see_salary(self, checker):
        """普通员工不能看 salary"""
        user = UserContext(user_id="101", role="employee")
        ast = compiler.parse_sql("SELECT name, salary FROM employee")
        ast = checker.apply_column_permissions(ast, user)
        sql = compiler.ast_to_sql(ast)
        assert "salary" not in sql.lower()
        assert "name" in sql.lower()

    def test_employee_cannot_see_id_card(self, checker):
        """普通员工不能看 id_card"""
        user = UserContext(user_id="101", role="employee")
        ast = compiler.parse_sql("SELECT name, id_card FROM employee")
        ast = checker.apply_column_permissions(ast, user)
        sql = compiler.ast_to_sql(ast)
        assert "id_card" not in sql.lower()

    def test_employee_cannot_see_bonus(self, checker):
        """普通员工不能看 bonus"""
        user = UserContext(user_id="101", role="employee")
        ast = compiler.parse_sql("SELECT score, bonus FROM performance")
        ast = checker.apply_column_permissions(ast, user)
        sql = compiler.ast_to_sql(ast)
        assert "bonus" not in sql.lower()
        assert "score" in sql.lower()

    def test_exec_can_see_all_except_id_card(self, checker):
        """高管可以看所有列，但 id_card 是全局隐藏的"""
        user = UserContext(user_id="admin", role="exec")
        ast = compiler.parse_sql("SELECT name, salary, id_card FROM employee")
        ast = checker.apply_column_permissions(ast, user)
        cols = compiler.extract_columns(ast)
        # id_card 是全局隐藏列，任何角色都不可看
        assert len(cols) == 2
        sql = compiler.ast_to_sql(ast)
        assert "id_card" not in sql.lower()
        assert "salary" in sql.lower()

    def test_select_star_expansion(self, checker):
        """SELECT * 展开为可见列"""
        user = UserContext(user_id="101", role="employee")
        ast = compiler.parse_sql("SELECT * FROM employee")
        ast = checker.apply_column_permissions(ast, user)
        sql = compiler.ast_to_sql(ast)
        # employee 角色隐藏 salary, id_card
        assert "salary" not in sql.lower()
        assert "id_card" not in sql.lower()
        # 展开后应只有 emp_id, name, dept_id
        cols = compiler.extract_columns(ast)
        assert len(cols) == 3


# ═══════════════════════════════════════════
# 行级权限测试
# ═══════════════════════════════════════════

class TestRowPermissions:
    """行级权限测试（AST 级）"""

    def test_employee_row_predicate(self, checker):
        """普通员工：注入 emp_id 条件"""
        user = UserContext(user_id="101", role="employee")
        ast = compiler.parse_sql("SELECT name FROM employee")
        ast = checker.apply_row_permissions(ast, user)
        sql = compiler.ast_to_sql(ast)
        assert "emp_id" in sql.lower()
        assert "101" in sql  # 实际值被注入

    def test_exec_no_row_predicate(self, checker):
        """高管：不注入行级谓词"""
        user = UserContext(user_id="admin", role="exec")
        ast = compiler.parse_sql("SELECT name FROM employee")
        ast = checker.apply_row_permissions(ast, user)
        sql = compiler.ast_to_sql(ast)
        assert "emp_id" not in sql.lower()

    def test_dept_lead_row_predicate(self, checker):
        """部门主管：注入 dept_id 条件"""
        user = UserContext(user_id="lead1", role="dept_lead", department_id=1)
        ast = compiler.parse_sql("SELECT name FROM employee")
        ast = checker.apply_row_permissions(ast, user)
        sql = compiler.ast_to_sql(ast)
        assert "dept_id" in sql.lower()
