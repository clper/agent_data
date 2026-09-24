"""
时间参数规范化器：AST 级兜底，将非标准时间值替换为标准格式。

设计要点：
- 作为 Prompt 注入的兜底层
- 遍历 AST WHERE 子树，检测时间列的非标准值
- 支持常见自然语言时间表达式

示例：
- month = '本月' → month = '2026-09'
- month = '上月' → month = '2026-08'
- month = '今年' → month LIKE '2026-%'
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Callable

import sqlglot.expressions as exp

logger = logging.getLogger(__name__)

# 时间列名（小写）
TIME_COLUMNS = {"month", "work_date", "hire_date", "date"}

# 标准格式正则
STANDARD_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
STANDARD_YEAR_RE = re.compile(r"^\d{4}-%$")
STANDARD_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _get_now() -> datetime:
    """获取当前时间（可测试时 mock）"""
    return datetime.now()


def _normalize_time_value(value: str, col_name: str) -> str | None:
    """
    将单个时间值规范化。

    Args:
        value: 原始值（如 '本月', '上月', '今年'）
        col_name: 列名（month, work_date 等）

    Returns:
        规范化后的值，如果已是标准格式或无法识别则返回 None
    """
    value = value.strip().strip("'\"")
    now = _get_now()

    # 已是标准格式，无需处理
    if col_name == "month":
        if STANDARD_MONTH_RE.match(value) or STANDARD_YEAR_RE.match(value):
            return None
    elif col_name in ("work_date", "hire_date", "date"):
        if STANDARD_DATE_RE.match(value):
            return None

    # 自然语言时间表达式映射
    mappings: dict[str, Callable[[datetime], str]] = {
        # 月级别
        "本月": lambda n: n.strftime("%Y-%m"),
        "这个月": lambda n: n.strftime("%Y-%m"),
        "当月": lambda n: n.strftime("%Y-%m"),
        "上月": lambda n: (n.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
        "上个月": lambda n: (n.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
        "上月月": lambda n: (n.replace(day=1) - timedelta(days=1)).strftime("%Y-%m"),
        # 年级别
        "今年": lambda n: f"{n.year}-%",
        "本年": lambda n: f"{n.year}-%",
        "去年": lambda n: f"{n.year - 1}-%",
        "前年": lambda n: f"{n.year - 2}-%",
        # 周级别（转为月份）
        "本周": lambda n: n.strftime("%Y-%m"),
        "这周": lambda n: n.strftime("%Y-%m"),
        "上周": lambda n: (n - timedelta(days=7)).strftime("%Y-%m"),
        # 季度级别
        "本季度": lambda n: n.strftime("%Y-%m"),
        "这季度": lambda n: n.strftime("%Y-%m"),
        "上季度": lambda n: _get_last_quarter(n),
    }

    # 检查是否匹配映射
    normalized = mappings.get(value)
    if normalized:
        result = normalized(now)
        logger.info("Time normalization: '%s' -> '%s'", value, result)
        return result

    # 尝试解析 "2026年6月" 格式
    cn_month_match = re.match(r"^(\d{4})年(\d{1,2})月$", value)
    if cn_month_match:
        year, month = cn_month_match.groups()
        result = f"{year}-{int(month):02d}"
        logger.info("Time normalization: '%s' -> '%s'", value, result)
        return result

    # 尝试解析 "2026年" 格式
    cn_year_match = re.match(r"^(\d{4})年$", value)
    if cn_year_match:
        year = cn_year_match.group(1)
        result = f"{year}-%"
        logger.info("Time normalization: '%s' -> '%s'", value, result)
        return result

    return None


def _get_last_quarter(now: datetime) -> str:
    """获取上季度的末月"""
    current_quarter = (now.month - 1) // 3 + 1
    if current_quarter == 1:
        # 上季度是去年 Q4
        return f"{now.year - 1}-12"
    else:
        # 上季度末月
        last_quarter_end_month = (current_quarter - 1) * 3
        return f"{now.year}-{last_quarter_end_month:02d}"


def normalize_time_params(ast: exp.Select) -> exp.Select:
    """
    遍历 AST，将 WHERE 子句中的非标准时间值替换为标准格式。

    Args:
        ast: sqlglot 解析后的 AST

    Returns:
        修改后的 AST（原地修改）
    """
    where = ast.args.get("where")
    if not where:
        return ast

    # 遍历 WHERE 子树中的所有比较节点
    for node in list(where.find_all(exp.EQ)):
        _normalize_comparison(node)

    # 处理 BETWEEN 节点
    for node in list(where.find_all(exp.Between)):
        _normalize_between(node)

    return ast


def _normalize_comparison(eq_node: exp.EQ) -> None:
    """规范化 EQ 比较节点中的时间值"""
    left = eq_node.this
    right = eq_node.expression

    # 检查是否是 column = literal 形式
    col_node, val_node = None, None
    if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
        col_node, val_node = left, right
    elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
        col_node, val_node = right, left

    if not col_node or not val_node:
        return

    col_name = col_node.name.lower()
    if col_name not in TIME_COLUMNS:
        return

    # 获取字面量值
    value = val_node.this
    if not isinstance(value, str):
        return

    # 尝试规范化
    normalized = _normalize_time_value(value, col_name)
    if normalized:
        # 如果是 LIKE 模式（如 '今年' -> '2026-%'），需要转换节点类型
        if normalized.endswith("-%") or normalized.endswith("-%-%"):
            # 将 EQ 转换为 LIKE
            parent = eq_node.parent
            like_node = exp.Like(this=col_node.copy(), expression=exp.Literal.string(normalized))
            eq_node.replace(like_node)
            logger.info("Converted EQ to LIKE for time pattern: %s", normalized)
        else:
            # 直接替换字面量值
            val_node.set("this", normalized)


def _normalize_between(between_node: exp.Between) -> None:
    """规范化 BETWEEN 节点中的时间值"""
    # BETWEEN 的处理较复杂，暂不实现
    # 生产环境中，Prompt 注入已能覆盖大部分 BETWEEN 场景
    pass


def apply_time_normalization(sql: str, ast: exp.Select) -> str:
    """
    对 SQL 应用时间参数规范化。

    Args:
        sql: 原始 SQL 字符串
        ast: 解析后的 AST

    Returns:
        规范化后的 SQL 字符串
    """
    from sqlglot import Dialects

    # 原地修改 AST
    normalize_time_params(ast)

    # 重新生成 SQL
    return ast.sql(dialect=Dialects.MYSQL)
