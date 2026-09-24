"""
SQL 编译器：基于 sqlglot 的 AST 级 SQL 处理管线。

职责：
1. 解析 LLM 生成的 SQL 为 AST（%s 占位符 → 哨兵值）
2. 从 AST 中提取结构化信息（表名、别名映射、列名）
3. AST 级行权限注入（WHERE 谓词，值用哨兵标记）
4. AST 级列权限过滤（SELECT 列展开/移除）
5. 哨兵 → %s 替换 + 参数收集（defense in depth）

设计要点：
- LLM 输出带字面量的 SQL（不用 %s），由本模块统一提取参数
- 行权限注入使用哨兵字符串标记参数位置，最终统一替换为 %s
- SELECT * 展开依赖 SchemaMetadata 获取可见列
"""
from __future__ import annotations

import logging
import re
from typing import Any

import sqlglot
from sqlglot import exp

from app.schema_rag.metadata import SchemaMetadata

logger = logging.getLogger(__name__)

# 哨兵前缀：用于标记原始 SQL 中的字面量位置
_PARAM_SENTINEL = "###P"
_INJECT_SENTINEL = "__PARAM_"


# ═══════════════════════════════════════════════════════
# 1. 解析
# ═══════════════════════════════════════════════════════

def parse_sql(sql: str) -> exp.Select:
    """
    解析 SQL 为 sqlglot AST。

    将原始 SQL 中的字面量替换为哨兵标记，以便后续参数化。
    注入的谓词值不使用哨兵，保留为普通字面量。

    Raises:
        ValueError: SQL 解析失败
    """
    # 将字面量替换为哨兵标记
    marked_sql, literal_count = _mark_literals(sql)
    
    try:
        parsed = sqlglot.parse(marked_sql, read="mysql")
    except Exception as e:
        raise ValueError(f"SQL 解析失败: {e}")

    if not parsed or not parsed[0]:
        raise ValueError("SQL 解析结果为空")

    stmt = parsed[0]
    if not isinstance(stmt, exp.Select):
        raise ValueError(f"仅支持 SELECT 语句，收到: {type(stmt).__name__}")

    return stmt


# ═══════════════════════════════════════════════════════
# 2. 提取信息
# ═══════════════════════════════════════════════════════

def extract_tables(ast: exp.Select) -> list[str]:
    """从 AST 提取所有表名（去重）。"""
    return list({t.name.lower() for t in ast.find_all(exp.Table) if t.name})


def extract_alias_map(ast: exp.Select) -> dict[str, str]:
    """
    构建 别名 → 表名 映射。

    包含显式别名（FROM employee e）和隐式别名（FROM employee → "employee" → employee）。
    直接遍历 AST 中所有 Table 节点，兼容不同 sqlglot 版本。
    """
    alias_map: dict[str, str] = {}
    for table in ast.find_all(exp.Table):
        table_name = table.name.lower()
        if not table_name:
            continue
        alias_expr = table.args.get("alias")
        if alias_expr:
            alias_name = alias_expr.name.lower() if hasattr(alias_expr, "name") else ""
            if alias_name:
                alias_map[alias_name] = table_name
        alias_map.setdefault(table_name, table_name)
    return alias_map


def extract_columns(ast: exp.Select) -> list[str]:
    """从 AST 提取 SELECT 列的字符串表示。"""
    return [e.sql(dialect="mysql") for e in ast.expressions]


# ═══════════════════════════════════════════════════════
# 3. 行权限注入
# ═══════════════════════════════════════════════════════

def inject_row_predicate(
    ast: exp.Select,
    table_name: str,
    condition_sql: str,
    alias_map: dict[str, str],
    param_values: list[Any] | None = None,
) -> exp.Select:
    """
    在 AST 的 WHERE 子句中注入行级权限谓词。

    Args:
        ast: SQL AST
        table_name: 目标表名（如 "employee"）
        condition_sql: 条件模板（如 "employee.emp_id = %s"）
        alias_map: 别名 → 表名 映射
        param_values: 条件中 %s 对应的实际值列表

    关键：处理表别名。如果 SQL 中用了别名（如 FROM employee e），
    注入时需要用别名前缀（e.emp_id）而非表名（employee.emp_id）。
    """
    # 找到该表在查询中使用的引用名（别名或原名）
    ref_name = _find_table_ref(table_name, alias_map)

    # 替换条件中的表名前缀
    adjusted = re.sub(
        rf"\b{re.escape(table_name)}\b",
        ref_name,
        condition_sql,
        flags=re.IGNORECASE,
    )

    # 将 %s 替换为 NULL 占位符（用于 sqlglot 解析）
    adjusted = adjusted.replace("%s", "NULL")

    # 解析条件为 AST 表达式
    try:
        cond_ast = sqlglot.parse(f"SELECT 1 WHERE {adjusted}", read="mysql")
        if not cond_ast or not cond_ast[0]:
            logger.warning("Failed to parse predicate: %s", adjusted)
            return ast
        predicate = cond_ast[0].args.get("where")
        if predicate:
            predicate = predicate.this
        else:
            return ast
    except Exception as e:
        logger.warning("Failed to parse predicate '%s': %s", adjusted, e)
        return ast

    # 将 NULL 占位符替换为实际值
    if param_values:
        nulls = list(predicate.find_all(exp.Null))
        for i, null_node in enumerate(nulls):
            if i < len(param_values):
                val = param_values[i]
                if isinstance(val, str):
                    null_node.replace(exp.Literal.string(val))
                elif isinstance(val, (int, float)):
                    null_node.replace(exp.Literal.number(val))
                else:
                    null_node.replace(exp.Literal.string(str(val)))

    # 注入到主查询的 WHERE 子句
    existing_where = ast.args.get("where")
    if existing_where:
        ast.set("where", exp.Where(this=exp.And(this=existing_where.this, expression=predicate)))
    else:
        ast.set("where", exp.Where(this=predicate))

    logger.info("Row predicate injected for %s: %s (ref=%s)", table_name, condition_sql, ref_name)
    return ast


def _find_table_ref(table_name: str, alias_map: dict[str, str]) -> str:
    """找到表在查询中的引用名（优先返回别名）。"""
    for alias, tname in alias_map.items():
        if tname == table_name and alias != table_name:
            return alias
    return table_name


# _replace_params_with_sentinels 已废弃，不再使用


# ═══════════════════════════════════════════════════════
# 4. 列权限过滤
# ═══════════════════════════════════════════════════════

def filter_select_columns(
    ast: exp.Select,
    hidden_columns: dict[str, set[str]],
    metadata: SchemaMetadata,
) -> exp.Select:
    """
    过滤 SELECT 列：移除敏感列，展开 SELECT *。

    Args:
        ast: SQL AST
        hidden_columns: {表名: {被隐藏的列名集合}}
        metadata: Schema 元数据（用于 SELECT * 展开）
    """
    if not hidden_columns:
        return ast

    new_exprs: list[exp.Expression] = []
    alias_map = extract_alias_map(ast)
    tables_in_query = extract_tables(ast)

    for expr in ast.expressions:
        if isinstance(expr, exp.Star):
            # SELECT * → 展开为所有可见列
            expanded = _expand_star(None, tables_in_query, alias_map, hidden_columns, metadata)
            new_exprs.extend(expanded)

        elif isinstance(expr, exp.Column):
            col_name = expr.name.lower()
            table_name = _resolve_column_table(expr, alias_map)
            if _is_column_hidden(col_name, table_name, tables_in_query, hidden_columns):
                logger.info("Column filtered out (sensitive): %s", expr.sql())
            else:
                new_exprs.append(expr)

        else:
            # 函数、别名、CASE 等复杂表达式 → 保留
            new_exprs.append(expr)

    if new_exprs:
        ast.set("expressions", new_exprs)
    return ast


def _expand_star(
    star_table: str | None,
    query_tables: list[str],
    alias_map: dict[str, str],
    hidden_columns: dict[str, set[str]],
    metadata: SchemaMetadata,
) -> list[exp.Expression]:
    """展开 SELECT * 为具体的可见列。"""
    cols: list[exp.Expression] = []
    target_tables = [star_table] if star_table else query_tables

    for tname in target_tables:
        real_table = alias_map.get(tname, tname)
        table_meta = metadata.get_table(real_table)
        if not table_meta:
            continue
        hidden = hidden_columns.get(real_table, set())
        for col_name in table_meta.all_column_names:
            if col_name not in hidden:
                cols.append(exp.Column(this=exp.to_identifier(col_name)))

    return cols


def _resolve_column_table(col_expr: exp.Column, alias_map: dict[str, str]) -> str | None:
    """解析列表达式中的表前缀。"""
    table_part = col_expr.table
    if table_part:
        return alias_map.get(table_part.lower(), table_part.lower())
    return None


def _is_column_hidden(
    col_name: str,
    col_table: str | None,
    query_tables: list[str],
    hidden_columns: dict[str, set[str]],
) -> bool:
    """检查列是否应被隐藏。"""
    tables_to_check = [col_table] if col_table else query_tables
    for tname in tables_to_check:
        if tname and col_name in hidden_columns.get(tname, set()):
            return True
    return False


# ═══════════════════════════════════════════════════════
# 5. 参数化输出
# ═══════════════════════════════════════════════════════

def ast_to_sql(ast: exp.Select) -> str:
    """将 AST 转回 SQL 字符串（MySQL 方言）。"""
    return ast.sql(dialect="mysql")


def extract_params(ast: exp.Select, original_sql: str = "") -> tuple[str, list[Any]]:
    """
    从 AST 中提取原始 SQL 的字面量值，替换为 %s 占位符。

    策略：
    1. parse_sql() 已将原始字面量标记为哨兵字符串
    2. 注入的谓词值是普通字面量（无哨兵）
    3. 只将哨兵替换为 %s，注入值保留为字面量

    Returns:
        (参数化SQL, 参数值列表)
    """
    params: list[Any] = []

    # 收集所有哨兵值（按 AST 遍历顺序）
    for literal in ast.find_all(exp.Literal):
        val = literal.this
        if isinstance(val, str) and val.startswith(_INJECT_SENTINEL) and val.endswith("###"):
            try:
                idx = int(val[len(_INJECT_SENTINEL):-3])
                while len(params) <= idx:
                    params.append(None)
                # 从原始 SQL 中提取对应位置的字面量值
                original_literals = _extract_literals_from_sql(original_sql) if original_sql else []
                if idx < len(original_literals):
                    params[idx] = original_literals[idx]
            except (ValueError, IndexError):
                pass

    # 将哨兵替换为 NULL
    for literal in list(ast.find_all(exp.Literal)):
        val = literal.this
        if isinstance(val, str) and val.startswith(_INJECT_SENTINEL) and val.endswith("###"):
            literal.replace(exp.Null())

    sql = ast.sql(dialect="mysql")

    # 将 NULL 替换为 %s
    sql = _replace_nulls_with_params(sql, len(params))

    return sql, params


def _mark_literals(sql: str) -> tuple[str, int]:
    """
    将 SQL 中的字面量替换为哨兵字符串。
    返回 (标记后的SQL, 字面量数量)。
    """
    result = []
    count = 0
    i = 0
    while i < len(sql):
        if sql[i] == "'":
            j = i + 1
            while j < len(sql):
                if sql[j] == "'" and (j + 1 >= len(sql) or sql[j + 1] != "'"):
                    break
                if sql[j] == "'" and j + 1 < len(sql) and sql[j + 1] == "'":
                    j += 2
                    continue
                j += 1
            result.append(f"'{_INJECT_SENTINEL}{count}###'")
            count += 1
            i = j + 1
        elif sql[i].isdigit() or (sql[i] == '-' and i + 1 < len(sql) and sql[i + 1].isdigit()):
            j = i
            if sql[j] == '-':
                j += 1
            while j < len(sql) and (sql[j].isdigit() or sql[j] == '.'):
                j += 1
            result.append(f"'{_INJECT_SENTINEL}{count}###'")
            count += 1
            i = j
        else:
            result.append(sql[i])
            i += 1
    return ''.join(result), count


def _extract_literals_from_sql(sql: str) -> list[Any]:
    """从 SQL 字符串中提取所有字面量值（按出现顺序）。"""
    params: list[Any] = []
    i = 0
    while i < len(sql):
        if sql[i] == "'":
            j = i + 1
            while j < len(sql):
                if sql[j] == "'" and (j + 1 >= len(sql) or sql[j + 1] != "'"):
                    break
                if sql[j] == "'" and j + 1 < len(sql) and sql[j + 1] == "'":
                    j += 2
                    continue
                j += 1
            params.append(sql[i + 1:j])
            i = j + 1
        elif sql[i].isdigit() or (sql[i] == '-' and i + 1 < len(sql) and sql[i + 1].isdigit()):
            j = i
            if sql[j] == '-':
                j += 1
            while j < len(sql) and (sql[j].isdigit() or sql[j] == '.'):
                j += 1
            num_str = sql[i:j]
            if '.' in num_str:
                params.append(float(num_str))
            else:
                params.append(int(num_str))
            i = j
        else:
            i += 1
    return params


def _replace_nulls_with_params(sql: str, param_count: int) -> str:
    """将 SQL 中的 NULL 占位符替换为 %s。"""
    if param_count == 0:
        return sql
    result = sql
    for _ in range(param_count):
        result = re.sub(r"\bNULL\b", "%s", result, count=1, flags=re.IGNORECASE)
    return result


# ═══════════════════════════════════════════════════════
# 6. 辅助函数
# ═══════════════════════════════════════════════════════

def is_self_query(ast: exp.Select, user_id: str, role: str, original_sql: str = "") -> bool:
    """
    检测当前查询是否仅限于用户自身数据。

    在 AST 的 WHERE 子树中查找 emp_id = <value> 模式。
    支持哨兵标记（parse_sql 后的 AST）和实际值。
    """
    if role != "employee":
        return False

    emp_id_int = int(user_id)
    where = ast.args.get("where")
    if not where:
        return False

    # 在 WHERE 子树中查找所有 EQ 表达式
    for eq_node in where.find_all(exp.EQ):
        left = eq_node.this
        right = eq_node.expression

        # 检查是否为 emp_id = <value> 模式
        is_emp_id_eq = False
        value_node = None

        if isinstance(left, exp.Column) and left.name.lower() == "emp_id":
            is_emp_id_eq = True
            value_node = right
        elif isinstance(right, exp.Column) and right.name.lower() == "emp_id":
            is_emp_id_eq = True
            value_node = left

        if not is_emp_id_eq or value_node is None:
            continue

        # 检查值是否匹配 user_id
        if isinstance(value_node, exp.Literal):
            val = value_node.this
            # 哨兵标记：从原始 SQL 中提取实际值
            if isinstance(val, str) and val.startswith(_INJECT_SENTINEL) and val.endswith("###"):
                try:
                    idx = int(val[len(_INJECT_SENTINEL):-3])
                    original_literals = _extract_literals_from_sql(original_sql) if original_sql else []
                    if idx < len(original_literals):
                        original_val = original_literals[idx]
                        if int(original_val) == emp_id_int:
                            return True
                except (ValueError, IndexError):
                    pass
            # 直接值比较
            else:
                try:
                    if int(val) == emp_id_int:
                        return True
                except (ValueError, TypeError):
                    pass

    return False


def build_alias_map(ast: exp.Select) -> dict[str, str]:
    """公开接口：构建别名映射（同 extract_alias_map）。"""
    return extract_alias_map(ast)
