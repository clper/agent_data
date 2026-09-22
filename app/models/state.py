"""
数据契约：用户上下文 + 会话状态 + 对话轮次。

核心设计：
- UserContext 是"不可变身份"，创建后不修改
- SessionState 是"可变状态"，每轮对话后更新
- Turn 是单轮对话的完整记录
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class UserContext:
    """
    用户身份上下文（不可变）。

    为什么 frozen=True？
    → 身份信息在整个请求链路中不应被任何模块篡改
    → 权限校验依赖 user_id/role 的不可变性保证安全
    """
    user_id: str
    role: str                          # "exec" | "employee" | "dept_lead" | "bu_head"
    department_id: int | None = None   # 所属部门 ID（行级权限依据）
    business_unit_id: int | None = None  # 所属事业部 ID（bu_head 用）
    extra: dict[str, Any] = field(default_factory=dict)  # 扩展字段


@dataclass
class Turn:
    """单轮对话记录"""
    question: str                      # 用户原始问题
    rewritten_question: str = ""       # 改写后的问题（指代消解）
    intent: str = ""                   # 识别的意图
    sql_executed: str = ""             # 执行的 SQL
    answer: str = ""                   # Agent 回答
    tables_used: list[str] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)
    error: str | None = None


@dataclass
class SessionState:
    """
    会话状态（可变，线程不安全——每个会话一个实例）。

    为什么用 list 而不是 deque？
    → 需要随机访问历史轮次（如回溯第 2 轮的表名）
    → 会话长度通常 < 50 轮，性能不是问题
    """
    session_id: str
    user: UserContext
    turns: list[Turn] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)

    def add_turn(self, turn: Turn) -> None:
        """追加一轮对话"""
        self.turns.append(turn)
        self.last_active = datetime.now()

    def recent_questions(self, n: int = 3) -> list[str]:
        """取最近 n 轮的问题（用于上下文改写）"""
        return [t.question for t in self.turns[-n:]]

    def recent_tables(self) -> list[str]:
        """取最近几轮用过的表名（用于上下文消歧）"""
        tables: list[str] = []
        for t in self.turns[-3:]:
            tables.extend(t.tables_used)
        return list(dict.fromkeys(tables))  # 去重保序
