"""
审计日志：记录每次 Agent 调用的完整链路信息。

设计要点：
- 异步写入（不阻塞主流程）
- JSONL 格式（便于后续分析）
- 记录完整的请求链路：问题 → 改写 → SQL → 结果
- 可配置输出到文件或数据库
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class AuditLogger:
    """
    审计日志记录器。

    为什么用 JSONL 而不是普通日志？
    → 结构化数据便于后续分析（如统计高频查询、检测异常模式）
    → 可以导入数据分析工具（Pandas、ELK）
    → 每行一条记录，追加写入不影响性能
    """

    def __init__(self, log_path: str = "logs/audit.jsonl"):
        self.log_path = Path(log_path)
        self._lock = threading.Lock()
        # 确保目录存在
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        request_id: str,
        user_id: str,
        role: str,
        session_id: str,
        question: str,
        rewritten_question: str = "",
        generated_sql: str = "",
        candidate_tables: list[str] | None = None,
        tables_accessed: list[str] | None = None,
        columns_accessed: list[str] | None = None,
        answer: str = "",
        row_count: int = 0,
        execution_ms: float = 0.0,
        status: str = "success",
        error: str | None = None,
    ) -> None:
        """
        记录一条审计日志。

        线程安全：使用锁保护文件写入。
        """
        record = {
            "request_id": request_id,
            "user_id": user_id,
            "role": role,
            "session_id": session_id,
            "question": question,
            "rewritten_question": rewritten_question,
            "generated_sql": generated_sql,
            "candidate_tables": candidate_tables or [],
            "tables_accessed": tables_accessed or [],
            "columns_accessed": columns_accessed or [],
            "answer": answer,
            "row_count": row_count,
            "execution_ms": round(execution_ms, 2),
            "status": status,
            "error": error,
            "timestamp": datetime.now().isoformat(),
        }

        # 同步写入（保证日志不丢失）
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        """写入日志文件"""
        try:
            line = json.dumps(record, ensure_ascii=False)
            with self._lock:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:
            logger.error("Failed to write audit log: %s", e)
