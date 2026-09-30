"""
自适应修复循环：借鉴 ReAct 的 Thought-Action-Observation 思路。

设计要点：
- 错误分类：根据错误信息自动识别错误类型
- 策略切换：不同轮次/错误类型使用不同修复策略
  - Round 1: 直接修复（修 SQL 本身）
  - Round 2: Schema 感知修复（如果 Round 1 失败，注入更多 schema 信息）
  - Round 3: 简化重写（如果修不好，从用户问题重新生成简化版 SQL）
- 修复历史：每轮修复的 SQL + 错误都传给 LLM，避免重复犯错
- PermissionError 不修复（安全错误不可修复）
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.llm import ChatMessage, LLMClient
from app.sql.generator import GenerationResult
from app.schema_rag.metadata import SchemaMetadata

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# 1. 错误分类
# ═══════════════════════════════════════════════════════

@dataclass
class ClassifiedError:
    """分类后的错误"""
    error_type: str          # "column_not_found" | "table_not_found" | "syntax" | "type_mismatch" | "empty_result" | "other"
    detail: str              # 错误详情（如缺失的列名、表名）
    original_error: str      # 原始错误信息
    suggestion: str = ""     # 修复建议


def classify_error(error_message: str, metadata: SchemaMetadata) -> ClassifiedError:
    """
    根据错误信息分类，并提取关键细节。

    支持的错误类型：
    - column_not_found: 列不存在（最常见，LLM 幻觉列名）
    - table_not_found: 表不存在
    - syntax: SQL 语法错误
    - type_mismatch: 数据类型不匹配
    - other: 其他错误
    """
    err = error_message.lower()

    # 列不存在（MySQL 错误码 1054）
    if "unknown column" in err or "1054" in err:
        # 提取列名: Unknown column 'xxx' in ...
        match = re.search(r"[Uu]nknown column '(\w+)'", error_message)
        col_name = match.group(1) if match else ""
        # 尝试在 schema 中找相似列名
        suggestion = _find_similar_column(col_name, metadata)
        return ClassifiedError(
            error_type="column_not_found",
            detail=col_name,
            original_error=error_message,
            suggestion=suggestion,
        )

    # 表不存在（MySQL 错误码 1146）
    if "doesn't exist" in err or "1146" in err:
        match = re.search(r"Table '(\w+\.(\w+))' doesn't exist", error_message)
        table_name = match.group(2) if match else ""
        return ClassifiedError(
            error_type="table_not_found",
            detail=table_name,
            original_error=error_message,
        )

    # 语法错误（MySQL 错误码 1064）
    if "syntax" in err or "1064" in err:
        return ClassifiedError(
            error_type="syntax",
            detail="",
            original_error=error_message,
        )

    # 类型不匹配（MySQL 错误码 1267, 1265, 1366）
    if "truncated" in err or "incorrect" in err or "data too long" in err:
        return ClassifiedError(
            error_type="type_mismatch",
            detail="",
            original_error=error_message,
        )

    return ClassifiedError(
        error_type="other",
        detail="",
        original_error=error_message,
    )


def _find_similar_column(wrong_name: str, metadata: SchemaMetadata) -> str:
    """
    在 schema 中查找与错误列名相似的列名。

    用于给 LLM 提供修复建议（如 "sallary" → "salary"）。
    """
    if not wrong_name:
        return ""

    wrong_lower = wrong_name.lower()
    candidates: list[str] = []

    for table in metadata.tables.values():
        for col in table.columns:
            candidates.append(col.name)

    # 简单相似度：编辑距离太复杂，用子串匹配
    best_match = ""
    best_score = 0
    for col_name in candidates:
        # 计算重叠字符数
        common = sum(1 for c in wrong_lower if c in col_name)
        score = common / max(len(wrong_lower), len(col_name), 1)
        if score > best_score:
            best_score = score
            best_match = col_name

    if best_score > 0.5 and best_match != wrong_lower:
        return f"你是不是想用 '{best_match}'？"
    return ""


# ═══════════════════════════════════════════════════════
# 2. 修复历史
# ═══════════════════════════════════════════════════════

@dataclass
class RepairAttempt:
    """单轮修复尝试的记录"""
    round_idx: int                     # 第几轮（从 0 开始）
    sql: str                           # 尝试的 SQL
    error: str                         # 产生的错误
    error_type: str = ""               # 错误分类
    strategy: str = ""                 # 使用的修复策略


def format_repair_history(history: list[RepairAttempt]) -> str:
    """
    将修复历史格式化为 LLM 可理解的文本。

    关键：让 LLM 知道之前试过什么、为什么失败，避免重复犯错。
    """
    if not history:
        return ""

    lines = ["【修复历史】以下是之前尝试过的修复，请不要重复相同的错误："]
    for attempt in history:
        lines.append(f"\n--- 第 {attempt.round_idx + 1} 次尝试（策略: {attempt.strategy}）---")
        lines.append(f"SQL: {attempt.sql}")
        lines.append(f"错误类型: {attempt.error_type}")
        lines.append(f"错误信息: {attempt.error}")

    return "\n".join(lines)


# ═══════════════════════════════════════════════════════
# 3. 分策略修复
# ═══════════════════════════════════════════════════════

# 策略 1: 直接修复（最常用）
REPAIR_DIRECT = """你是一个 SQL 修复专家。之前的查询执行失败了，请根据错误信息修复 SQL。

规则：
1. 只修复导致错误的部分，不要改变查询的语义
2. WHERE 条件中的值直接写入 SQL（系统会自动参数化）
3. 只生成 SELECT 语句
4. 如果无法修复，返回空 SQL

{error_hint}

输出 JSON：
{{"sql": "修复后的 SQL", "explanation": "修复说明"}}"""

# 策略 2: Schema 感知修复（列名/表名错误时使用）
REPAIR_SCHEMA_AWARE = """你是一个 SQL 修复专家。查询因为使用了不存在的列名/表名而失败。

重要：请严格根据以下可用表结构来修复 SQL，不要使用结构中不存在的列名或表名。

规则：
1. 只使用下面列出的表和列
2. 如果原始查询的列名不存在，找最接近的正确列名替换
3. WHERE 条件中的值直接写入 SQL
4. 只生成 SELECT 语句

{error_hint}

输出 JSON：
{{"sql": "修复后的 SQL", "explanation": "修复说明"}}"""

# 策略 3: 简化重写（最后一招，从用户问题重新生成）
REPAIR_SIMPLIFY = """你是一个 SQL 修复专家。之前的修复尝试都失败了，请换一个思路。

策略：放弃修复原有 SQL，根据用户问题和可用表结构，重新生成一个更简单但等价的查询。

要求：
1. 新 SQL 要尽量简单（减少 JOIN、子查询等复杂结构）
2. 能回答用户的核心问题即可
3. WHERE 条件中的值直接写入 SQL
4. 只生成 SELECT 语句

{error_hint}

输出 JSON：
{{"sql": "重新生成的简化 SQL", "explanation": "为什么简化以及新 SQL 的思路"}}"""


def _build_repair_messages(
    strategy: str,
    classified: ClassifiedError,
    schema_context: str,
    question: str,
    history: list[RepairAttempt],
) -> list[ChatMessage]:
    """
    根据策略构建修复 prompt。

    关键差异：
    - 直接修复：只给错误信息
    - Schema 感知：额外注入完整 schema + 修复建议
    - 简化重写：强调"换思路" + 注入修复历史
    """
    # 构建错误提示
    error_hint_parts = []
    error_hint_parts.append(f"错误类型: {classified.error_type}")
    error_hint_parts.append(f"错误信息: {classified.original_error}")
    if classified.suggestion:
        error_hint_parts.append(f"修复建议: {classified.suggestion}")
    error_hint = "\n".join(error_hint_parts)

    # 追加修复历史（让 LLM 知道之前试过什么）
    history_text = format_repair_history(history)
    if history_text:
        error_hint += "\n\n" + history_text

    # 根据策略选择 system prompt
    if strategy == "schema_aware":
        system_prompt = REPAIR_SCHEMA_AWARE.format(error_hint=error_hint)
        user_msg = (
            f"可用表结构（严格使用这些表和列）：\n{schema_context}\n\n"
            f"用户问题：{question}\n\n"
            f"失败的 SQL：\n{history[-1].sql if history else ''}\n\n"
            f"请根据可用表结构修复 SQL。"
        )
    elif strategy == "simplify":
        system_prompt = REPAIR_SIMPLIFY.format(error_hint=error_hint)
        user_msg = (
            f"可用表结构：\n{schema_context}\n\n"
            f"用户问题：{question}\n\n"
            f"之前多次修复都失败了，请换一个思路，重新生成一个更简单但等价的查询。"
        )
    else:  # direct
        system_prompt = REPAIR_DIRECT.format(error_hint=error_hint)
        last_sql = history[-1].sql if history else ""
        user_msg = (
            f"可用表结构：\n{schema_context}\n\n"
            f"用户问题：{question}\n\n"
            f"原 SQL：\n{last_sql}\n\n"
            f"请修复 SQL。"
        )

    return [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=user_msg),
    ]


# ═══════════════════════════════════════════════════════
# 4. 主入口
# ═══════════════════════════════════════════════════════

def attempt_repair(
    error_message: str,
    schema_context: str,
    question: str,
    metadata: SchemaMetadata,
    llm: LLMClient,
    history: list[RepairAttempt] | None = None,
    round_idx: int = 0,
    max_rounds: int = 3,
) -> GenerationResult | None:
    """
    自适应修复 SQL。

    策略选择逻辑：
    - Round 0: 根据错误类型选择
      - column_not_found / table_not_found → schema_aware
      - 其他 → direct
    - Round 1: 如果上一轮也是同类错误 → schema_aware（带历史），否则 → direct
    - Round 2 (最后一轮): simplify（换思路重写）

    Args:
        error_message: 当前错误信息
        schema_context: Schema 上下文
        question: 用户问题
        metadata: Schema 元数据
        llm: LLM 客户端
        history: 之前的修复尝试记录
        round_idx: 当前轮次（0-based）
        max_rounds: 最大轮次

    Returns:
        修复后的 GenerationResult，如果无法修复返回 None
    """
    history = history or []

    # 1. 错误分类
    classified = classify_error(error_message, metadata)
    logger.info(
        "Repair round %d: error_type=%s, detail=%s",
        round_idx, classified.error_type, classified.detail,
    )

    # 2. 策略选择
    remaining_rounds = max_rounds - round_idx
    if remaining_rounds <= 1:
        # 最后一轮：简化重写
        strategy = "simplify"
    elif classified.error_type in ("column_not_found", "table_not_found"):
        strategy = "schema_aware"
    else:
        strategy = "direct"

    # 如果上一轮用了同样策略且失败了，切换到下一个策略
    if history and history[-1].strategy == strategy:
        if strategy == "direct":
            strategy = "schema_aware"
        elif strategy == "schema_aware" and remaining_rounds > 1:
            strategy = "simplify"

    logger.info("Repair strategy: %s (round %d/%d)", strategy, round_idx + 1, max_rounds)

    # 3. 构建 prompt 并调用 LLM
    messages = _build_repair_messages(
        strategy, classified, schema_context, question, history,
    )

    result_dict = llm.chat_json(messages, temperature=0.0)

    sql = result_dict.get("sql", "").strip()
    explanation = result_dict.get("explanation", "")

    if not sql:
        logger.warning("Repair failed: LLM returned empty SQL (strategy=%s)", strategy)
        return None

    # 检测修复后的 SQL 是否与之前完全相同（避免无限循环）
    if history and sql.strip().lower() == history[-1].sql.strip().lower():
        logger.warning("Repair produced identical SQL, skipping")
        return None

    logger.info("Repair SQL (%s): %s", strategy, sql[:200])
    return GenerationResult(sql=sql, explanation=explanation)
