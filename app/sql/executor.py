"""
SQL 执行器：在只读事务中执行参数化查询。

设计要点：
- 只读事务（SET TRANSACTION READ ONLY）
- 查询超时（防止慢查询拖垮数据库）
- 结果截断（防止大结果集撑爆内存）
- 参数化执行（禁止字符串拼接 SQL）
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from app.config import DBConfig
from app.models.plan import ExecResult

logger = logging.getLogger(__name__)


class SQLExecutor:
    """
    SQL 执行器。

    安全设计：
    - 只读连接（只读账号 + 只读事务）
    - 超时控制
    - 结果截断
    """

    def __init__(self, config: DBConfig):
        self.config = config
        self.engine: Engine = create_engine(
            config.url,
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_recycle=3600,
        )

    def execute(self, sql: str, params: list[Any]) -> ExecResult:
        """
        执行 SQL 查询。

        Args:
            sql: SQL 模板（%s 占位）
            params: 参数列表

        Returns:
            ExecResult: 执行结果

        Raises:
            TimeoutError: 查询超时
            Exception: 数据库错误
        """
        start_time = time.perf_counter()

        # 将 %s 占位符转换为 SQLAlchemy 的 :p0, :p1 格式
        sa_sql, sa_params = self._convert_params(sql, params)

        try:
            with self.engine.connect() as conn:
                # 设置只读事务
                conn.execute(text("SET TRANSACTION READ ONLY"))

                # 设置超时
                timeout_ms = self.config.query_timeout * 1000
                conn.execute(text(f"SET SESSION MAX_EXECUTION_TIME = {timeout_ms}"))

                # 执行查询（SQLAlchemy 需要 dict 参数）
                result = conn.execute(text(sa_sql), sa_params)

                # 获取列名
                columns = list(result.keys()) if result.returns_rows else []

                # 获取行（截断）
                rows: list[tuple] = []
                truncated = False
                if result.returns_rows:
                    for i, row in enumerate(result):
                        if i >= self.config.max_rows:
                            truncated = True
                            break
                        rows.append(tuple(row))

                elapsed_ms = (time.perf_counter() - start_time) * 1000

                return ExecResult(
                    sql=sql,
                    params=params,
                    columns=columns,
                    rows=rows,
                    row_count=len(rows),
                    truncated=truncated,
                    execution_time_ms=elapsed_ms,
                )

        except Exception as e:
            elapsed_ms = (time.perf_counter() - start_time) * 1000
            logger.error("SQL execution failed (%.1fms): %s", elapsed_ms, e)
            raise

    @staticmethod
    def _convert_params(sql: str, params: list[Any]) -> tuple[str, dict]:
        """
        将 %s 占位符转换为 SQLAlchemy 的 :p0, :p1 格式。

        SQLAlchemy text() 不接受 list 参数，必须用 dict + 命名占位符。
        """
        if not params:
            return sql, {}
        sa_params: dict[str, Any] = {}
        idx = 0
        def replacer(match: re.Match) -> str:
            nonlocal idx
            name = f"p{idx}"
            sa_params[name] = params[idx]
            idx += 1
            return f":{name}"
        sa_sql = re.sub(r"%s", replacer, sql)
        return sa_sql, sa_params

    def close(self) -> None:
        """关闭引擎"""
        self.engine.dispose()
