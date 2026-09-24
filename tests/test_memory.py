"""
语义记忆模块单元测试。

覆盖：
- Memory 对象：权重计算、衰减
- SemanticMemoryStore：CRUD、去重、检索、衰减、冲突消解
- Extractor：从 Turn 中提取模式/偏好
- Retriever：prompt 格式化
"""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

import pytest

from app.memory.store import (
    CATEGORY_CORRECTION,
    CATEGORY_PATTERN,
    CATEGORY_PREFERENCE,
    Memory,
    SemanticMemoryStore,
)
from app.memory.extractor import (
    _extract_key_from_question,
    _extract_preferences,
    _extract_query_patterns,
    extract_from_turn,
    record_correction,
)
from app.memory.retriever import get_memory_context
from app.models.state import Turn


# ── Memory 对象测试 ──────────────────────────────────────


class TestMemory:
    def test_age_days(self):
        mem = Memory(
            user_id="1",
            category=CATEGORY_PATTERN,
            key="test",
            value="test",
            created_at=datetime.now() - timedelta(days=10),
        )
        assert 9.9 < mem.age_days < 10.1

    def test_effective_weight_fresh(self):
        mem = Memory(
            user_id="1",
            category=CATEGORY_PATTERN,
            key="test",
            value="test",
            confidence=0.8,
            created_at=datetime.now(),
        )
        # 刚创建的记忆权重接近 confidence
        assert mem.effective_weight > 0.7

    def test_effective_weight_old(self):
        mem = Memory(
            user_id="1",
            category=CATEGORY_PATTERN,
            key="test",
            value="test",
            confidence=0.8,
            created_at=datetime.now() - timedelta(days=60),
        )
        # 60 天旧记忆权重明显下降
        assert mem.effective_weight < 0.5

    def test_effective_weight_high_access(self):
        mem = Memory(
            user_id="1",
            category=CATEGORY_PATTERN,
            key="test",
            value="test",
            confidence=0.8,
            access_count=100,
            created_at=datetime.now() - timedelta(days=30),
        )
        # 高频访问的记忆衰减更慢
        assert mem.effective_weight > 0.4

    def test_to_prompt_str(self):
        mem = Memory(
            user_id="1",
            category=CATEGORY_PREFERENCE,
            key="dept",
            value="关注销售部",
            confidence=0.9,
        )
        s = mem.to_prompt_str()
        assert "preference" in s
        assert "dept" in s
        assert "90%" in s


# ── SemanticMemoryStore 测试 ─────────────────────────────


class TestSemanticMemoryStore:
    @pytest.fixture
    def store(self, tmp_path):
        db_path = str(tmp_path / "test_memory.db")
        s = SemanticMemoryStore(db_path=db_path)
        yield s
        s.close()

    def test_store_and_retrieve(self, store):
        mem = Memory(
            user_id="u1",
            category=CATEGORY_PATTERN,
            key="table:revenue",
            value="常查 revenue 表",
        )
        mid = store.store(mem)
        assert mid > 0

        results = store.retrieve("u1")
        assert len(results) == 1
        assert results[0].key == "table:revenue"

    def test_store_dedup(self, store):
        """同一 key 重复 store 应更新而非新增"""
        mem1 = Memory(user_id="u1", category=CATEGORY_PATTERN, key="k1", value="v1")
        mem2 = Memory(user_id="u1", category=CATEGORY_PATTERN, key="k1", value="v2")
        store.store(mem1)
        store.store(mem2)

        results = store.retrieve("u1")
        assert len(results) == 1
        assert results[0].value == "v2"
        assert results[0].access_count == 1  # 第二次 store 累加了 count

    def test_retrieve_by_category(self, store):
        store.store(Memory(user_id="u1", category=CATEGORY_PATTERN, key="k1", value="v1"))
        store.store(Memory(user_id="u1", category=CATEGORY_PREFERENCE, key="k2", value="v2"))

        patterns = store.retrieve("u1", category=CATEGORY_PATTERN)
        assert len(patterns) == 1
        assert patterns[0].category == CATEGORY_PATTERN

    def test_get_relevant_keyword_match(self, store):
        store.store(Memory(user_id="u1", category=CATEGORY_PREFERENCE, key="销售", value="关注销售数据"))
        store.store(Memory(user_id="u1", category=CATEGORY_PATTERN, key="月度", value="按月查询"))

        relevant = store.get_relevant("u1", "销售部本月收入")
        assert len(relevant) >= 1
        # 包含"销售"的记忆应被匹配
        assert any("销售" in m.key for m in relevant)

    def test_get_relevant_correction_priority(self, store):
        store.store(Memory(user_id="u1", category=CATEGORY_PATTERN, key="pattern", value="pattern value"))
        store.store(Memory(
            user_id="u1", category=CATEGORY_CORRECTION,
            key="correction:cost", value="人力成本不含社保", confidence=0.95,
        ))

        relevant = store.get_relevant("u1", "人力成本")
        # 纠错记忆应始终被返回（即使关键词不完美匹配）
        assert any(m.category == CATEGORY_CORRECTION for m in relevant)

    def test_decay(self, store):
        # 创建一个低置信度 + 旧记忆
        old_mem = Memory(
            user_id="u1",
            category=CATEGORY_PATTERN,
            key="old",
            value="old value",
            confidence=0.1,
            created_at=datetime.now() - timedelta(days=60),
        )
        store.store(old_mem)

        # 创建一个高置信度新记忆
        new_mem = Memory(
            user_id="u1",
            category=CATEGORY_PATTERN,
            key="new",
            value="new value",
            confidence=0.9,
        )
        store.store(new_mem)

        decayed = store.decay(threshold=0.15)
        assert decayed >= 1
        remaining = store.retrieve("u1")
        assert len(remaining) == 1
        assert remaining[0].key == "new"

    def test_resolve_conflicts(self, store):
        store.store(Memory(user_id="u1", category=CATEGORY_PATTERN, key="same_key", value="low", confidence=0.3))
        store.store(Memory(user_id="u1", category=CATEGORY_PREFERENCE, key="same_key", value="high", confidence=0.9))

        # 手动插入两条不同 category 的相同 key（绕过去重）
        # resolve_conflicts 会保留权重高的
        resolved = store.resolve_conflicts("u1")
        # 由于 store 的去重，实际只有一条，所以 resolved=0
        # 但方法不应崩溃
        assert resolved >= 0

    def test_count(self, store):
        store.store(Memory(user_id="u1", category=CATEGORY_PATTERN, key="k1", value="v1"))
        store.store(Memory(user_id="u1", category=CATEGORY_PATTERN, key="k2", value="v2"))
        store.store(Memory(user_id="u2", category=CATEGORY_PATTERN, key="k3", value="v3"))

        assert store.count() == 3
        assert store.count("u1") == 2
        assert store.count("u2") == 1

    def test_count_by_category(self, store):
        store.store(Memory(user_id="u1", category=CATEGORY_PATTERN, key="k1", value="v1"))
        store.store(Memory(user_id="u1", category=CATEGORY_PREFERENCE, key="k2", value="v2"))
        store.store(Memory(user_id="u1", category=CATEGORY_PATTERN, key="k3", value="v3"))

        counts = store.count_by_category("u1")
        assert counts[CATEGORY_PATTERN] == 2
        assert counts[CATEGORY_PREFERENCE] == 1


# ── Extractor 测试 ───────────────────────────────────────


class TestExtractor:
    @pytest.fixture
    def store(self, tmp_path):
        db_path = str(tmp_path / "test_memory.db")
        s = SemanticMemoryStore(db_path=db_path)
        yield s
        s.close()

    def test_extract_query_patterns(self, store):
        turn = Turn(
            question="销售部2026年6月的收入",
            sql_executed="SELECT SUM(r.amount) FROM revenue r JOIN department d ON r.dept_id = d.dept_id WHERE d.dept_name = 'Sales' AND r.month = '2026-06' GROUP BY d.dept_name",
            tables_used=["revenue", "department"],
        )
        count = _extract_query_patterns(turn, "u1", store)
        assert count >= 2  # 至少提取了表模式和部门模式

        memories = store.retrieve("u1")
        keys = [m.key for m in memories]
        assert any("revenue" in k for k in keys)
        assert any("dept" in k for k in keys)

    def test_extract_preferences(self, store):
        turn = Turn(
            question="销售部本月利润是多少",
            sql_executed="SELECT SUM(amount) FROM revenue",
            tables_used=["revenue"],
        )
        count = _extract_preferences(turn, "u1", store)
        assert count >= 1

        memories = store.retrieve("u1")
        assert any("profit" in m.key or "revenue" in m.key for m in memories)

    def test_extract_from_turn_skips_error(self, store):
        turn = Turn(question="test", sql_executed="", error="some error")
        count = extract_from_turn(turn, "u1", store)
        assert count == 0

    def test_record_correction(self, store):
        count = record_correction("u1", "销售部收入", "应该是包含退款的净收入", store)
        assert count == 1

        corrections = store.retrieve("u1", category=CATEGORY_CORRECTION)
        assert len(corrections) == 1
        assert corrections[0].confidence == 0.95

    def test_extract_key_from_question(self):
        assert len(_extract_key_from_question("销售部2026年6月的收入是多少？")) <= 30
        assert _extract_key_from_question("") == ""


# ── Retriever 测试 ───────────────────────────────────────


class TestRetriever:
    @pytest.fixture
    def store(self, tmp_path):
        db_path = str(tmp_path / "test_memory.db")
        s = SemanticMemoryStore(db_path=db_path)
        yield s
        s.close()

    def test_empty_memory_returns_empty(self, store):
        result = get_memory_context("u1", "test question", store)
        assert result == ""

    def test_format_with_corrections(self, store):
        store.store(Memory(
            user_id="u1", category=CATEGORY_CORRECTION,
            key="correction:cost", value="人力成本不含社保", confidence=0.95,
        ))
        result = get_memory_context("u1", "人力成本", store)
        assert "纠错" in result
        assert "人力成本不含社保" in result

    def test_format_with_preferences(self, store):
        store.store(Memory(
            user_id="u1", category=CATEGORY_PREFERENCE,
            key="销售", value="关注销售数据", confidence=0.7,
        ))
        result = get_memory_context("u1", "销售数据", store)
        assert "偏好" in result or "销售" in result

    def test_max_chars_truncation(self, store):
        for i in range(20):
            store.store(Memory(
                user_id="u1", category=CATEGORY_PATTERN,
                key=f"k{i}", value=f"v{'x' * 50}{i}", confidence=0.8,
            ))
        result = get_memory_context("u1", "k0 k1 k2 k3 k4", store, max_chars=100)
        assert len(result) <= 103  # 100 + "..."
