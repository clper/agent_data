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
class QueryState:
    """
    查询状态快照：保存最近一轮查询的关键信息。

    用于多轮对话中的上下文消解（如"那研发部呢？"）。
    """
    tables: list[str] = field(default_factory=list)       # 使用的表名
    filters: dict[str, str] = field(default_factory=dict) # 过滤条件 {"dept_name": "销售部", "month": "2026-06"}
    group_by: list[str] = field(default_factory=list)     # 聚合维度
    metrics: list[str] = field(default_factory=list)      # 聚合指标

    def to_context_str(self) -> str:
        """格式化为 LLM 可理解的上下文"""
        parts = []
        if self.tables:
            parts.append(f"查询的表: {', '.join(self.tables)}")
        if self.filters:
            filters_str = ", ".join(f"{k}={v}" for k, v in self.filters.items())
            parts.append(f"过滤条件: {filters_str}")
        if self.group_by:
            parts.append(f"分组维度: {', '.join(self.group_by)}")
        if self.metrics:
            parts.append(f"聚合指标: {', '.join(self.metrics)}")
        return "; ".join(parts) if parts else ""


@dataclass
class SessionState:
    """
    会话状态（可变，线程不安全——每个会话一个实例）。

    工作记忆增强：
    - summary: 旧轮对话的压缩摘要（超过 5 轮时生成）
    - query_state: 最近一轮查询的状态快照（用于上下文消解）
    """
    session_id: str
    user: UserContext
    turns: list[Turn] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_active: datetime = field(default_factory=datetime.now)
    summary: str = ""                           # 旧轮对话摘要
    query_state: QueryState | None = None       # 最近查询状态快照

    # 工作记忆配置
    MAX_TURNS_BEFORE_SUMMARY = 5                # 超过此轮次触发摘要
    KEEP_RECENT_TURNS = 5                       # 保留最近 N 轮完整

    def add_turn(self, turn: Turn) -> None:
        """追加一轮对话，并更新查询状态快照"""
        self.turns.append(turn)
        self.last_active = datetime.now()
        # 更新查询状态快照
        self._update_query_state(turn)

    def _update_query_state(self, turn: Turn) -> None:
        """从 Turn 中提取查询状态快照"""
        if not turn.sql_executed:
            return

        state = QueryState(tables=list(turn.tables_used))

        # 简单解析 SQL 提取过滤条件和聚合维度
        sql_lower = turn.sql_executed.lower()

        # 提取 WHERE 条件中的键值对（简化版）
        if "where" in sql_lower:
            where_part = sql_lower.split("where")[-1].split("group")[0].split("order")[0]
            # 提取常见过滤条件
            for pattern in [r"dept_name\s*=\s*'([^']+)'", r"month\s*=\s*'([^']+)'",
                           r"status\s*=\s*'([^']+)'", r"item\s*=\s*'([^']+)'"]:
                import re
                match = re.search(pattern, where_part)
                if match:
                    key = pattern.split(r"\s")[0].replace(r"\s*=\s*'", "").strip()
                    state.filters[key] = match.group(1)

        # 提取 GROUP BY 字段
        if "group by" in sql_lower:
            group_part = sql_lower.split("group by")[-1].split("order")[0].split("limit")[0]
            state.group_by = [g.strip().split('.')[-1] for g in group_part.split(",") if g.strip()]

        self.query_state = state

    def recent_questions(self, n: int = 3) -> list[str]:
        """取最近 n 轮的问题（用于上下文改写）"""
        return [t.question for t in self.turns[-n:]]

    def recent_tables(self) -> list[str]:
        """取最近几轮用过的表名（用于上下文消歧）"""
        tables: list[str] = []
        for t in self.turns[-3:]:
            tables.extend(t.tables_used)
        return list(dict.fromkeys(tables))  # 去重保序

    def get_context_for_understanding(self) -> str:
        """
        获取用于理解层的上下文（包含摘要 + 最近轮次 + 查询状态）。

        返回格式：
        - 历史摘要（如果有）
        - 最近几轮问答
        - 最近查询状态快照
        """
        parts = []

        # 1. 历史摘要
        if self.summary:
            parts.append(f"历史对话摘要：{self.summary}")

        # 2. 最近几轮问答
        recent = self.turns[-self.KEEP_RECENT_TURNS:]
        if recent:
            parts.append("最近对话：")
            for t in recent:
                parts.append(f"  - Q: {t.question}")
                if t.answer:
                    parts.append(f"    A: {t.answer[:100]}...")

        # 3. 查询状态快照
        if self.query_state:
            state_str = self.query_state.to_context_str()
            if state_str:
                parts.append(f"最近查询状态：{state_str}")

        return "\n".join(parts)

    def should_summarize(self) -> bool:
        """是否应该生成摘要（轮次超过阈值且尚未摘要）"""
        return len(self.turns) > self.MAX_TURNS_BEFORE_SUMMARY and not self.summary
