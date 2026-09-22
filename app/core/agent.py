"""
Agent 主编排器：实现七层信任链。

数据流：
  会话 → 理解 → 澄清 → Schema RAG → 生成 → 安全校验 → 权限注入 → 执行 → 解释 → 审计

设计要点：
- 每个步骤独立可测
- PermissionError 短路修复循环
- 修复循环最多 N 轮
- 非数据查询（meta/out_of_scope）不进入 SQL 链路
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from app.config import Settings
from app.core.clarification import (
    INTENT_DATA_QUERY,
    INTENT_META,
    INTENT_NEED_CLARIFICATION,
    INTENT_OUT_OF_SCOPE,
    UnderstandResult,
    analyze_intent,
)
from app.core.conversation import ConversationStore
from app.core.llm import ChatMessage, LLMClient
from app.models.plan import AgentResponse, ExecResult, QueryPlan
from app.models.state import SessionState, Turn, UserContext
from app.schema_rag.indexer import SchemaIndex
from app.schema_rag.metadata import SchemaMetadata, load_schema
from app.schema_rag.retriever import SchemaRetriever
from app.security.audit import AuditLogger
from app.security.permissions import PermissionChecker
from app.sql.executor import SQLExecutor
from app.sql.generator import generate_plan
from app.sql.repair import attempt_repair
from app.sql.validator import SecurityValidator, validate_and_raise

logger = logging.getLogger(__name__)

# 解释层提示词
EXPLAIN_SYSTEM = """你是一个企业数据分析助手。请根据查询结果，用简洁的中文回答用户的问题。

规则：
1. 直接回答问题，不要复述 SQL
2. 数据用表格或列表呈现
3. 如果结果为空，说明可能的原因
4. 保持客观，不要编造数据中没有的信息"""


class DataAgent:
    """
    数据问答 Agent。

    核心方法：answer(question, session_id, user) → AgentResponse
    """

    def __init__(self, settings: Settings):
        self.s = settings

        # LLM 客户端
        self.llm = LLMClient(settings.llm)

        # Schema 加载 + 索引构建
        self.metadata: SchemaMetadata = load_schema(settings.schema.schema_path)
        self.index = SchemaIndex()
        self.index.build(
            self.metadata,
            k1=settings.schema.bm25_k1,
            b=settings.schema.bm25_b,
        )
        self.retriever = SchemaRetriever(self.index, top_k=settings.schema.default_top_k)

        # 安全组件
        self.validator = SecurityValidator(self.metadata)
        self.perm_checker = PermissionChecker(self.metadata)
        self.audit = AuditLogger(settings.security.audit_log_path)

        # 执行器
        self.executor = SQLExecutor(settings.db)

        # 会话存储
        self.sessions = ConversationStore()

        logger.info(
            "DataAgent initialized: %d tables, %d metrics",
            len(self.metadata.tables),
            len(self.metadata.metrics),
        )

    def answer(
        self,
        question: str,
        session_id: str,
        user: UserContext,
    ) -> AgentResponse:
        """
        回答用户问题（主入口）。

        七层信任链：
        1. 会话层：获取/创建会话
        2. 理解层：意图分类 + 问题改写
        3. 澄清层：追问 / 超范围拒绝
        4. 检索层：Schema RAG → 候选表
        5. 生成层：LLM → QueryPlan
        6. 校验层：AST 白名单 + 行列权限
        7. 执行层：只读执行 + 修复循环
        """
        request_id = str(uuid.uuid4())[:8]
        start_time = time.perf_counter()
        session = self.sessions.get_or_create(session_id, user)

        try:
            return self._answer_chain(question, session, user, request_id, start_time)
        except Exception as e:
            logger.exception("Agent error: %s", e)
            return AgentResponse(
                answer=f"抱歉，处理您的问题时出错：{e}",
                error=str(e),
            )

    def _answer_chain(
        self,
        question: str,
        session: SessionState,
        user: UserContext,
        request_id: str,
        start_time: float,
    ) -> AgentResponse:
        """七层信任链主流程"""

        # ═══════════════════════════════════════════
        # 第 1 层：理解层（意图分类 + 问题改写）
        # ═══════════════════════════════════════════
        understand = analyze_intent(
            question, session.recent_questions(), self.llm
        )
        logger.info("[%s] Intent: %s", request_id, understand.intent)

        # ═══════════════════════════════════════════
        # 第 2 层：澄清层（路由决策）
        # ═══════════════════════════════════════════
        if understand.intent == INTENT_NEED_CLARIFICATION:
            return self._handle_clarification(
                question, understand, session, user, request_id, start_time
            )

        if understand.intent == INTENT_OUT_OF_SCOPE:
            return self._handle_out_of_scope(
                question, session, user, request_id, start_time
            )

        if understand.intent == INTENT_META:
            return self._handle_meta(
                question, session, user, request_id, start_time
            )

        # ═══════════════════════════════════════════
        # 第 3 层：检索层（Schema RAG）
        # ═══════════════════════════════════════════
        rewritten = understand.rewritten_question or question
        retrieval_result = self.retriever.retrieve(rewritten)
        schema_context = self.retriever.get_context_for_llm(retrieval_result)
        logger.info(
            "[%s] Retrieved tables: %s (expanded: %s)",
            request_id, retrieval_result.table_names, retrieval_result.expanded_tables,
        )

        if not retrieval_result.table_names:
            return AgentResponse(
                answer="抱歉，没有找到与您问题相关的数据库表。请尝试换一种描述方式。",
            )

        # ═══════════════════════════════════════════
        # 第 4 层：生成层（LLM → QueryPlan）
        # ═══════════════════════════════════════════
        plan = generate_plan(
            rewritten, retrieval_result, schema_context, self.metadata, self.llm
        )
        logger.info("[%s] Plan: tables=%s, cols=%s", request_id, plan.target_tables, plan.select_columns)

        if plan.intent != INTENT_DATA_QUERY:
            # 模型判断为非数据查询，降级处理
            if plan.intent == INTENT_OUT_OF_SCOPE:
                return self._handle_out_of_scope(
                    question, session, user, request_id, start_time
                )

        # ═══════════════════════════════════════════
        # 第 5 层：校验层（AST 白名单 + 行列权限）
        # ═══════════════════════════════════════════
        # 列级权限
        plan = self.perm_checker.apply_column_permissions(plan, user)
        # 行级权限
        plan = self.perm_checker.apply_row_permissions(plan, user)

        # 编译 SQL
        sql, params = plan.to_sql()
        logger.info("[%s] SQL: %s | params: %s", request_id, sql, params)

        # AST 校验
        allowed_tables = set(retrieval_result.table_names)
        validate_and_raise(self.validator, plan, sql, allowed_tables)

        # ═══════════════════════════════════════════
        # 第 6 层：执行层（只读执行 + 修复循环）
        # ═══════════════════════════════════════════
        exec_result = None
        last_error = None

        for round_idx in range(self.s.security.max_repair_rounds + 1):
            try:
                exec_result = self.executor.execute(sql, params)
                last_error = None
                break  # 成功，跳出循环
            except PermissionError:
                raise  # 安全错误不修复
            except Exception as e:
                last_error = str(e)
                logger.warning("[%s] Execution failed (round %d): %s", request_id, round_idx, e)

                if round_idx < self.s.security.max_repair_rounds:
                    # 尝试修复
                    repaired = attempt_repair(
                        plan, str(e), schema_context, rewritten, self.metadata, self.llm
                    )
                    if repaired:
                        plan = repaired
                        # 重新应用权限
                        plan = self.perm_checker.apply_column_permissions(plan, user)
                        plan = self.perm_checker.apply_row_permissions(plan, user)
                        sql, params = plan.to_sql()
                        # 重新校验
                        validate_and_raise(self.validator, plan, sql, allowed_tables)
                    else:
                        break

        if exec_result is None:
            return AgentResponse(
                answer=f"抱歉，查询执行失败：{last_error}",
                error=last_error,
            )

        # ═══════════════════════════════════════════
        # 第 7 层：解释层（LLM 把数据翻译为中文回答）
        # ═══════════════════════════════════════════
        answer_text = self._explain(rewritten, exec_result, schema_context)

        # 记录会话
        turn = Turn(
            question=question,
            rewritten_question=rewritten,
            intent=understand.intent,
            sql_executed=exec_result.sql,
            answer=answer_text,
            tables_used=plan.target_tables,
        )
        session.add_turn(turn)

        # 审计日志
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        self.audit.log(
            request_id=request_id,
            user_id=user.user_id,
            role=user.role,
            session_id=session.session_id,
            question=question,
            rewritten_question=rewritten,
            generated_sql=exec_result.sql,
            candidate_tables=retrieval_result.table_names,
            tables_accessed=plan.target_tables,
            columns_accessed=plan.select_columns,
            answer=answer_text,
            row_count=exec_result.row_count,
            execution_ms=elapsed_ms,
        )

        return AgentResponse(
            answer=answer_text,
            sql=exec_result.sql,
            tables_used=plan.target_tables,
            row_count=exec_result.row_count,
            execution_time_ms=elapsed_ms,
        )

    def _explain(
        self, question: str, result: ExecResult, schema_context: str
    ) -> str:
        """解释层：把查询结果翻译为自然语言"""
        data_table = result.to_markdown_table()
        user_msg = (
            f"用户问题：{question}\n\n"
            f"查询结果（{result.row_count} 行）：\n{data_table}\n\n"
            f"请用简洁的中文回答用户的问题。"
        )
        messages = [
            ChatMessage(role="system", content=EXPLAIN_SYSTEM),
            ChatMessage(role="user", content=user_msg),
        ]
        return self.llm.chat(messages, temperature=0.3)

    def _handle_clarification(
        self,
        question: str,
        understand: UnderstandResult,
        session: SessionState,
        user: UserContext,
        request_id: str,
        start_time: float,
    ) -> AgentResponse:
        """处理需要追问的情况"""
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        turn = Turn(
            question=question,
            intent=INTENT_NEED_CLARIFICATION,
            answer=understand.clarification_question,
        )
        session.add_turn(turn)
        return AgentResponse(
            answer=understand.clarification_question,
            needs_clarification=True,
            clarification_question=understand.clarification_question,
            execution_time_ms=elapsed_ms,
        )

    def _handle_out_of_scope(
        self,
        question: str,
        session: SessionState,
        user: UserContext,
        request_id: str,
        start_time: float,
    ) -> AgentResponse:
        """处理超范围问题"""
        answer = "抱歉，这个问题超出了我的能力范围。我只能帮您查询公司运营数据相关的问题。"
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        turn = Turn(question=question, intent=INTENT_OUT_OF_SCOPE, answer=answer)
        session.add_turn(turn)
        return AgentResponse(answer=answer, execution_time_ms=elapsed_ms)

    def _handle_meta(
        self,
        question: str,
        session: SessionState,
        user: UserContext,
        request_id: str,
        start_time: float,
    ) -> AgentResponse:
        """处理元问题（系统能力/数据库结构）"""
        tables_info = []
        for table in self.metadata.tables.values():
            tables_info.append(f"- {table.name}（{table.cn_name}）: {table.comment}")
        tables_str = "\n".join(tables_info)
        answer = f"我可以查询以下数据表：\n{tables_str}\n\n请问您想了解什么数据？"
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        turn = Turn(question=question, intent=INTENT_META, answer=answer)
        session.add_turn(turn)
        return AgentResponse(answer=answer, execution_time_ms=elapsed_ms)
