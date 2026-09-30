"""
AST 校验器测试：验证安全防线。

测试策略：
- 攻击样例全拒（DROP/INSERT/UPDATE/DELETE/多语句/危险函数等）
- 正常查询全过（单表/JOIN/聚合/子查询等）
"""
import pytest

from app.schema_rag.metadata import Column, SchemaMetadata, Table
from app.sql.validator import SecurityValidator


@pytest.fixture
def metadata():
    """构造测试用 Schema 元数据"""
    tables = {
        "employee": Table(
            name="employee",
            cn_name="员工表",
            columns=[
                Column(name="emp_id", type="BIGINT", pk=True),
                Column(name="name", type="VARCHAR(64)"),
                Column(name="dept_id", type="BIGINT"),
                Column(name="salary", type="DECIMAL(12,2)", sensitive=True),
            ],
        ),
        "department": Table(
            name="department",
            cn_name="部门表",
            columns=[
                Column(name="dept_id", type="BIGINT", pk=True),
                Column(name="dept_name", type="VARCHAR(64)"),
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
            ],
        ),
    }
    return SchemaMetadata(database="test_db", tables=tables)


@pytest.fixture
def validator(metadata):
    return SecurityValidator(metadata)


# ═══════════════════════════════════════════
# 攻击样例：全部应该被拒绝
# ═══════════════════════════════════════════

class TestAttackRejection:
    """攻击样例测试"""

    def test_drop_table(self, validator):
        errors = validator.validate_sql("DROP TABLE employee", {"employee"})
        assert len(errors) > 0
        assert any(e.code == "forbidden_statement" for e in errors)

    def test_insert(self, validator):
        errors = validator.validate_sql(
            "INSERT INTO employee (name) VALUES ('hacker')", {"employee"}
        )
        assert len(errors) > 0

    def test_update(self, validator):
        errors = validator.validate_sql(
            "UPDATE employee SET salary = 999999 WHERE emp_id = 1", {"employee"}
        )
        assert len(errors) > 0

    def test_delete(self, validator):
        errors = validator.validate_sql(
            "DELETE FROM employee WHERE emp_id = 1", {"employee"}
        )
        assert len(errors) > 0

    def test_multiple_statements(self, validator):
        errors = validator.validate_sql(
            "SELECT 1; DROP TABLE employee", {"employee"}
        )
        assert len(errors) > 0
        assert any(e.code == "multiple_statements" for e in errors)

    def test_sleep_function(self, validator):
        errors = validator.validate_sql(
            "SELECT SLEEP(5)", set()
        )
        assert len(errors) > 0
        assert any(e.code == "dangerous_function" for e in errors)

    def test_benchmark_function(self, validator):
        errors = validator.validate_sql(
            "SELECT BENCHMARK(1000000, SHA256('test'))", set()
        )
        assert len(errors) > 0

    def test_table_not_allowed(self, validator):
        errors = validator.validate_sql(
            "SELECT * FROM secret_table", {"employee"}
        )
        assert len(errors) > 0
        assert any(e.code == "table_not_allowed" for e in errors)

    def test_for_update(self, validator):
        errors = validator.validate_sql(
            "SELECT * FROM employee FOR UPDATE", {"employee"}
        )
        assert len(errors) > 0

    def test_union_injection(self, validator):
        """UNION 注入测试（AST 层面允许 UNION SELECT，但表必须在白名单中）"""
        errors = validator.validate_sql(
            "SELECT name FROM employee UNION SELECT dept_name FROM secret_table",
            {"employee"},
        )
        assert len(errors) > 0  # secret_table 不在白名单


# ═══════════════════════════════════════════
# 正常查询：全部应该通过
# ═══════════════════════════════════════════

class TestNormalQueries:
    """正常查询测试"""

    def test_simple_select(self, validator):
        errors = validator.validate_sql(
            "SELECT name FROM employee WHERE dept_id = %s",
            {"employee"},
        )
        assert len(errors) == 0

    def test_select_with_join(self, validator):
        errors = validator.validate_sql(
            "SELECT e.name, d.dept_name FROM employee e "
            "INNER JOIN department d ON e.dept_id = d.dept_id",
            {"employee", "department"},
        )
        assert len(errors) == 0

    def test_aggregate_query(self, validator):
        errors = validator.validate_sql(
            "SELECT dept_id, AVG(score) AS avg_score FROM performance "
            "GROUP BY dept_id ORDER BY avg_score DESC",
            {"performance"},
        )
        assert len(errors) == 0

    def test_count_query(self, validator):
        errors = validator.validate_sql(
            "SELECT COUNT(*) AS total FROM employee WHERE dept_id = %s",
            {"employee"},
        )
        assert len(errors) == 0

    def test_multi_table_join(self, validator):
        errors = validator.validate_sql(
            "SELECT e.name, p.score, d.dept_name "
            "FROM employee e "
            "INNER JOIN performance p ON e.emp_id = p.emp_id "
            "INNER JOIN department d ON e.dept_id = d.dept_id "
            "WHERE p.month = %s",
            {"employee", "performance", "department"},
        )
        assert len(errors) == 0

    def test_subquery(self, validator):
        errors = validator.validate_sql(
            "SELECT name FROM employee WHERE dept_id IN "
            "(SELECT dept_id FROM department WHERE dept_name = %s)",
            {"employee", "department"},
        )
        assert len(errors) == 0

    def test_limit(self, validator):
        errors = validator.validate_sql(
            "SELECT name, score FROM performance ORDER BY score DESC LIMIT 10",
            {"performance"},
        )
        assert len(errors) == 0
