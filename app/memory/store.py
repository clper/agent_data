"""
语义记忆存储层：SQLite 持久化。

设计要点：
- 每个 user_id 有独立的记忆集合
- 记忆按类别组织：preference / pattern / correction / insight
- 支持衰减因子（随时间降低权重）
- 支持访问计数（高频记忆衰减更慢）
- 线程安全（每线程一个 SQLite 连接）

记忆类型：
- preference: 用户偏好（如"总是查销售部"、"关注利润指标"）
- pattern: 查询模式（如"经常按月汇总"、"喜欢按部门分组"）
- correction: 纠错记录（如"人力成本只算基本工资，不含社保"）
- insight: 反思洞察（如"该用户关注成本结构，优先展示明细"）
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 记忆类别
CATEGORY_PREFERENCE = "preference"
CATEGORY_PATTERN = "pattern"
CATEGORY_CORRECTION = "correction"
CATEGORY_INSIGHT = "insight"

ALL_CATEGORIES = [
    CATEGORY_PREFERENCE, CATEGORY_PATTERN,
    CATEGORY_CORRECTION, CATEGORY_INSIGHT,
]

# 默认衰减参数
DEFAULT_DECAY_RATE = 0.02       # 每天衰减 2%
DEFAULT_HALF_LIFE_DAYS = 30     # 半衰期 30 天


class Memory:
    """单条语义记忆"""

    __slots__ = (
        "id", "user_id", "category", "key", "value",
        "confidence", "access_count", "created_at", "last_accessed_at",
        "metadata",
    )

    def __init__(
        self,
        user_id: str,
        category: str,
        key: str,
        value: str,
        confidence: float = 0.8,
        access_count: int = 0,
        created_at: datetime | None = None,
        last_accessed_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
        id: int | None = None,
    ):
        self.id = id
        self.user_id = user_id
        self.category = category
        self.key = key
        self.value = value
        self.confidence = confidence
        self.access_count = access_count
        self.created_at = created_at or datetime.now()
        self.last_accessed_at = last_accessed_at or datetime.now()
        self.metadata = metadata or {}

    @property
    def age_days(self) -> float:
        """记忆年龄（天）"""
        return (datetime.now() - self.created_at).total_seconds() / 86400

    @property
    def effective_weight(self) -> float:
        """
        有效权重 = confidence × decay_factor

        decay_factor = max(0.1, 1 - decay_rate * age / (1 + log(1 + access_count)))
        高频访问的记忆衰减更慢。
        """
        import math
        decay = DEFAULT_DECAY_RATE
        age = self.age_days
        access_boost = 1 + math.log(1 + self.access_count)
        factor = max(0.1, 1 - (decay * age / access_boost))
        return self.confidence * factor

    def to_prompt_str(self) -> str:
        """格式化为可注入 prompt 的字符串"""
        return f"[{self.category}] {self.key}: {self.value} (置信度: {self.confidence:.0%})"

    def __repr__(self) -> str:
        return (
            f"Memory(id={self.id}, user={self.user_id}, cat={self.category}, "
            f"key={self.key!r}, weight={self.effective_weight:.2f})"
        )


class SemanticMemoryStore:
    """
    语义记忆存储（SQLite 实现）。

    接口设计：
    - store(): 存入一条记忆（自动去重/合并）
    - retrieve(): 按用户 + 类别检索
    - get_relevant(): 获取与当前问题相关的记忆
    - touch(): 更新访问计数
    - decay(): 执行记忆衰减（清理低权重记忆）
    - reflect(): 反思入口（由 reflector.py 调用）
    """

    def __init__(self, db_path: str = "data/semantic_memory.db"):
        self._db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """获取当前线程的 SQLite 连接"""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
            self._local.conn = sqlite3.connect(self._db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self) -> None:
        """初始化表结构"""
        conn = self._get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL DEFAULT 0.8,
                access_count INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                last_accessed_at TEXT NOT NULL,
                metadata TEXT DEFAULT '{}'
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_user_cat
            ON memories(user_id, category)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_user_key
            ON memories(user_id, key)
        """)
        conn.commit()
        logger.info("Semantic memory store initialized: %s", self._db_path)

    # ── 写入 ──────────────────────────────────────────────

    def store(self, memory: Memory) -> int:
        """
        存入一条记忆。

        去重策略：
        - 同一 user_id + category + key → 更新 value + confidence
        - 否则 → 新增
        """
        conn = self._get_conn()
        now = datetime.now().isoformat()

        # 查找是否已存在
        row = conn.execute(
            "SELECT id, confidence, access_count FROM memories "
            "WHERE user_id = ? AND category = ? AND key = ?",
            (memory.user_id, memory.category, memory.key),
        ).fetchone()

        if row:
            # 已存在：合并（取较高置信度，累加访问次数）
            new_confidence = max(row["confidence"], memory.confidence)
            new_count = row["access_count"] + 1
            conn.execute(
                "UPDATE memories SET value = ?, confidence = ?, "
                "access_count = ?, last_accessed_at = ?, metadata = ? "
                "WHERE id = ?",
                (memory.value, new_confidence, new_count, now,
                 json.dumps(memory.metadata), row["id"]),
            )
            conn.commit()
            logger.debug("Updated memory %d: %s", row["id"], memory.key)
            return row["id"]
        else:
            # 新增
            cursor = conn.execute(
                "INSERT INTO memories "
                "(user_id, category, key, value, confidence, access_count, "
                "created_at, last_accessed_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (memory.user_id, memory.category, memory.key, memory.value,
                 memory.confidence, memory.access_count,
                 memory.created_at.isoformat(), now,
                 json.dumps(memory.metadata)),
            )
            conn.commit()
            logger.debug("Created memory %d: %s", cursor.lastrowid, memory.key)
            return cursor.lastrowid

    # ── 读取 ──────────────────────────────────────────────

    def retrieve(
        self,
        user_id: str,
        category: str | None = None,
        limit: int = 20,
    ) -> list[Memory]:
        """按用户 + 类别检索记忆（按有效权重降序）"""
        conn = self._get_conn()

        if category:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? AND category = ? "
                "ORDER BY confidence DESC, access_count DESC LIMIT ?",
                (user_id, category, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? "
                "ORDER BY confidence DESC, access_count DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()

        return [self._row_to_memory(r) for r in rows]

    def get_relevant(
        self,
        user_id: str,
        question: str,
        limit: int = 5,
    ) -> list[Memory]:
        """
        获取与当前问题相关的记忆。

        匹配策略（轻量级，无 Embedding）：
        1. 关键词匹配：问题中包含记忆的 key 或 value 关键词
        2. 类别优先级：correction > preference > pattern > insight
        3. 按有效权重排序
        """
        all_memories = self.retrieve(user_id, limit=50)
        if not all_memories:
            return []

        # 关键词匹配打分
        question_lower = question.lower()
        scored: list[tuple[float, Memory]] = []

        for mem in all_memories:
            score = 0.0
            # key 匹配
            key_words = mem.key.lower().split()
            for kw in key_words:
                if kw in question_lower:
                    score += 2.0
            # value 关键词匹配
            value_words = mem.value.lower().split()
            for vw in value_words:
                if len(vw) >= 2 and vw in question_lower:
                    score += 1.0
            # 类别优先级
            category_boost = {
                CATEGORY_CORRECTION: 3.0,
                CATEGORY_PREFERENCE: 2.0,
                CATEGORY_PATTERN: 1.0,
                CATEGORY_INSIGHT: 0.5,
            }.get(mem.category, 0.0)

            total = score + category_boost + mem.effective_weight
            if score > 0 or mem.category == CATEGORY_CORRECTION:
                scored.append((total, mem))

        # 排序取 top-N
        scored.sort(key=lambda x: x[0], reverse=True)
        result = [mem for _, mem in scored[:limit]]

        # 更新访问计数
        for mem in result:
            self.touch(mem.id)

        return result

    def touch(self, memory_id: int) -> None:
        """更新访问计数和最后访问时间"""
        conn = self._get_conn()
        now = datetime.now().isoformat()
        conn.execute(
            "UPDATE memories SET access_count = access_count + 1, "
            "last_accessed_at = ? WHERE id = ?",
            (now, memory_id),
        )
        conn.commit()

    # ── 衰减 + 清理 ──────────────────────────────────────

    def decay(self, threshold: float = 0.15) -> int:
        """
        执行记忆衰减：删除有效权重低于阈值的记忆。

        Returns: 删除的记忆数量
        """
        conn = self._get_conn()
        rows = conn.execute("SELECT * FROM memories").fetchall()
        to_delete = []

        for row in rows:
            mem = self._row_to_memory(row)
            if mem.effective_weight < threshold:
                to_delete.append(mem.id)

        if to_delete:
            placeholders = ",".join("?" for _ in to_delete)
            conn.execute(
                f"DELETE FROM memories WHERE id IN ({placeholders})",
                to_delete,
            )
            conn.commit()
            logger.info("Decayed %d memories (threshold=%.2f)", len(to_delete), threshold)

        return len(to_delete)

    def resolve_conflicts(self, user_id: str) -> int:
        """
        冲突消解：同一 key 不同 category 的记忆，保留高权重的。

        Returns: 删除的冲突记忆数量
        """
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT key, GROUP_CONCAT(id) as ids, GROUP_CONCAT(category) as cats "
            "FROM memories WHERE user_id = ? "
            "GROUP BY user_id, key HAVING COUNT(*) > 1",
            (user_id,),
        ).fetchall()

        removed = 0
        for row in rows:
            ids = [int(x) for x in row["ids"].split(",")]
            # 获取所有冲突记忆，保留 effective_weight 最高的
            memories = []
            for mid in ids:
                mrow = conn.execute(
                    "SELECT * FROM memories WHERE id = ?", (mid,)
                ).fetchone()
                if mrow:
                    memories.append(self._row_to_memory(mrow))

            if len(memories) < 2:
                continue

            # 按权重排序，保留第一个
            memories.sort(key=lambda m: m.effective_weight, reverse=True)
            to_remove = [m.id for m in memories[1:] if m.id is not None]
            if to_remove:
                placeholders = ",".join("?" for _ in to_remove)
                conn.execute(
                    f"DELETE FROM memories WHERE id IN ({placeholders})",
                    to_remove,
                )
                removed += len(to_remove)

        if removed:
            conn.commit()
            logger.info("Resolved %d conflicting memories for user %s", removed, user_id)

        return removed

    # ── 统计 ──────────────────────────────────────────────

    def count(self, user_id: str | None = None) -> int:
        """记忆总数"""
        conn = self._get_conn()
        if user_id:
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM memories WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        else:
            row = conn.execute("SELECT COUNT(*) as cnt FROM memories").fetchone()
        return row["cnt"]

    def count_by_category(self, user_id: str) -> dict[str, int]:
        """按类别统计记忆数量"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT category, COUNT(*) as cnt FROM memories "
            "WHERE user_id = ? GROUP BY category",
            (user_id,),
        ).fetchall()
        return {r["category"]: r["cnt"] for r in rows}

    # ── 内部 ──────────────────────────────────────────────

    def _row_to_memory(self, row: sqlite3.Row) -> Memory:
        """将 SQLite 行转为 Memory 对象"""
        metadata = {}
        try:
            metadata = json.loads(row["metadata"]) if row["metadata"] else {}
        except json.JSONDecodeError:
            pass

        return Memory(
            id=row["id"],
            user_id=row["user_id"],
            category=row["category"],
            key=row["key"],
            value=row["value"],
            confidence=row["confidence"],
            access_count=row["access_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            last_accessed_at=datetime.fromisoformat(row["last_accessed_at"]),
            metadata=metadata,
        )

    def close(self) -> None:
        """关闭当前线程的连接"""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
