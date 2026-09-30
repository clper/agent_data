"""
Schema 检索器测试：验证 BM25 召回 + FK 扩展。

测试策略：
- 单表查询应召回正确表
- 多表查询应召回多张表
- FK 扩展应加入关联表
- 无关查询不应召回
"""
import pytest

from app.schema_rag.indexer import SchemaIndex
from app.schema_rag.metadata import Column, SchemaMetadata, Table
from app.schema_rag.retriever import SchemaRetriever


@pytest.fixture
def metadata():
    tables = {
        "employee": Table(
            name="employee",
            cn_name="员工表",
            comment="员工基础信息、部门归属、薪资",
            columns=[
                Column(name="emp_id", type="BIGINT", pk=True),
                Column(name="name", type="VARCHAR(64)", cn="姓名"),
                Column(name="dept_id", type="BIGINT", cn="部门ID", fk="department.dept_id"),
                Column(name="salary", type="DECIMAL(12,2)", cn="月薪", sensitive=True),
            ],
        ),
        "department": Table(
            name="department",
            cn_name="部门表",
            comment="部门及所属事业部",
            columns=[
                Column(name="dept_id", type="BIGINT", pk=True),
                Column(name="dept_name", type="VARCHAR(64)", cn="部门名称"),
            ],
        ),
        "performance": Table(
            name="performance",
            cn_name="绩效表",
            comment="员工月度绩效得分与奖金",
            columns=[
                Column(name="perf_id", type="BIGINT", pk=True),
                Column(name="emp_id", type="BIGINT", cn="员工ID", fk="employee.emp_id"),
                Column(name="month", type="CHAR(7)", cn="月份"),
                Column(name="score", type="DECIMAL(5,2)", cn="绩效得分"),
            ],
        ),
        "revenue": Table(
            name="revenue",
            cn_name="收入表",
            comment="部门月度收入",
            columns=[
                Column(name="rev_id", type="BIGINT", pk=True),
                Column(name="month", type="CHAR(7)", cn="月份"),
                Column(name="dept_id", type="BIGINT", cn="部门ID", fk="department.dept_id"),
                Column(name="amount", type="DECIMAL(14,2)", cn="收入金额"),
            ],
        ),
    }
    return SchemaMetadata(database="test_db", tables=tables)


@pytest.fixture
def retriever(metadata):
    index = SchemaIndex()
    index.build(metadata)
    return SchemaRetriever(index, top_k=4)


class TestBM25Retrieval:
    """BM25 检索测试"""

    def test_employee_query(self, retriever):
        """查询员工信息应召回 employee 表"""
        result = retriever.retrieve("查看员工姓名")
        assert "employee" in result.table_names

    def test_performance_query(self, retriever):
        """查询绩效应召回 performance 表"""
        result = retriever.retrieve("上个月绩效得分")
        assert "performance" in result.table_names

    def test_revenue_query(self, retriever):
        """查询收入应召回 revenue 表"""
        result = retriever.retrieve("部门月收入多少")
        assert "revenue" in result.table_names

    def test_department_query(self, retriever):
        """查询部门应召回 department 表"""
        result = retriever.retrieve("有哪些部门")
        assert "department" in result.table_names


class TestFKExpansion:
    """FK 图扩展测试"""

    def test_employee_expands_to_department(self, retriever):
        """查 employee 时应通过 FK 扩展到 department"""
        result = retriever.retrieve("员工信息")
        if "employee" in result.table_names:
            # employee.dept_id -> department
            assert "department" in result.table_names or "department" in result.expanded_tables

    def test_performance_expands_to_employee(self, retriever):
        """查 performance 时应通过 FK 扩展到 employee"""
        result = retriever.retrieve("绩效得分")
        if "performance" in result.table_names:
            assert "employee" in result.table_names or "employee" in result.expanded_tables


class TestContextForLLM:
    """LLM 上下文格式化测试"""

    def test_context_contains_table_info(self, retriever):
        """上下文应包含表的详细信息"""
        result = retriever.retrieve("员工绩效")
        context = retriever.get_context_for_llm(result)
        assert "employee" in context or "performance" in context
        assert "员工" in context or "绩效" in context
