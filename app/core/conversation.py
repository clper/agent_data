"""
会话存储：管理多轮对话的上下文。

设计要点：
- MVP 使用进程内存储（dict），重启丢失
- 生产环境可替换为 Redis / 数据库
- 每个 session_id 对应一个 SessionState
- 支持会话过期清理
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.models.state import SessionState, UserContext

logger = logging.getLogger(__name__)


class ConversationStore:
    """
    会话存储（进程内实现）。

    为什么用 dict 而不是数据库？
    → MVP 阶段简单优先
    → 接口已抽象，后续替换为 Redis 只需实现同样的方法
    """

    def __init__(self, ttl_minutes: int = 30):
        self._sessions: dict[str, SessionState] = {}
        self._ttl = timedelta(minutes=ttl_minutes)

    def get_or_create(
        self, session_id: str, user: UserContext
    ) -> SessionState:
        """获取或创建会话"""
        self._cleanup_expired()
        if session_id not in self._sessions:
            self._sessions[session_id] = SessionState(
                session_id=session_id, user=user
            )
            logger.info("Created session: %s for user: %s", session_id, user.user_id)
        return self._sessions[session_id]

    def get(self, session_id: str) -> SessionState | None:
        """获取会话（不存在返回 None）"""
        return self._sessions.get(session_id)

    def delete(self, session_id: str) -> None:
        """删除会话"""
        self._sessions.pop(session_id, None)

    def _cleanup_expired(self) -> None:
        """清理过期会话"""
        now = datetime.now()
        expired = [
            sid for sid, s in self._sessions.items()
            if now - s.last_active > self._ttl
        ]
        for sid in expired:
            del self._sessions[sid]
            logger.info("Expired session: %s", sid)
