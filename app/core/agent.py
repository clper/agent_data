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
from app.core.context import ExecutionContext, SubResult
from app.core.conversation import ConversationStore
from app.core.decomposer import DecompositionResult, decompose_question
from app.core.llm import ChatMessage, LLMClient
from app.core.merger import merge_results
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


# 数据库相关关键词（用于规则兆底，防止 LLM 将数据查询误分类为 out_of_scope）
_DB_KEYWORDS = [
    "薪资", "工资", "薪水", "薪酬",
    "绩效", "考核", "得分", "奖金",
    "考勤", "迟到", "缺勤", "请假", "出勤",
    "收入", "营收", "成本", "利润",
    "员工", "部门", "项目", "客户",
    "入职", "离职", "转正",
    "预算", "报销",
]


def _contains_db_keywords(question: str) -> bool:
    """检查问题是否包含数据库相关关键词"""
    return any(kw in question for kw in _DB_KEYWORDS)


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
            # Router: 先判断是否为复合问题
            from app.core.decomposer import decompose_question
            
            understand = analyze_intent(question, session.recent_questions(), self.llm, user)
            rewritten = understand.rewritten_question or question

            # 规则兆底：LLM 分类为 out_of_scope 但问题包含数据库关键词时，强制纠正
            if understand.intent == INTENT_OUT_OF_SCOPE and _contains_db_keywords(question):
                logger.info(
                    "[%s] Rule-based override: out_of_scope -> data_query (question contains DB keywords)",
                    request_id,
                )
                understand.intent = INTENT_DATA_QUERY
                understand.rewritten_question = question
            
            if understand.intent == INTENT_DATA_QUERY:
                decomposition = decompose_question(rewritten, self.llm)
                if decomposition.is_composite:
                    logger.info(
                        "[%s] Complex question detected, entering Decompose-Merge flow",
                        request_id,
                    )
                    return self._answer_composite(
                        question, rewritten, decomposition, session, user, request_id, start_time
                    )
            
            # 简单问题或元问题：走原有流程（传入 understand 避免重复调用 LLM）
            return self._answer_chain_with_understand(question, understand, session, user, request_id, start_time)
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
            question, session.recent_questions(), self.llm, user
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
        # 第 2.5 层：Router（简单问题 vs 复合问题）
        # ═══════════════════════════════════════════
        rewritten = understand.rewritten_question or question
        decomposition = decompose_question(rewritten, self.llm)

        if decomposition.is_composite:
            logger.info(
                "[%s] Complex question detected, entering Decompose-Merge flow (%d sub-questions)",
                request_id,
                len(decomposition.sub_questions),
            )
            return self._answer_composite(
                question, rewritten, decomposition, session, user, request_id, start_time
            )

        # 简单问题：走原有单轮流程
        logger.info("[%s] Simple question, entering single-turn flow", request_id)
        return self._answer_simple(
            question, rewritten, session, user, request_id, start_time
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
        # 行级权限先注入（为列级权限提供"自身查询"上下文）
        plan = self.perm_checker.apply_row_permissions(plan, user)
        # 列级权限（依赖行级注入结果判断 salary 可见性）
        plan = self.perm_checker.apply_column_permissions(plan, user)

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
                        # 重新应用权限（行级先于列级）
                        plan = self.perm_checker.apply_row_permissions(plan, user)
                        plan = self.perm_checker.apply_column_permissions(plan, user)
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

    def _answer_chain_with_understand(
        self,
        question: str,
        understand: UnderstandResult,
        session: SessionState,
        user: UserContext,
        request_id: str,
        start_time: float,
    ) -> AgentResponse:
        """
        简单问题流程（复用 Router 层的 understand 结果，避免重复调用 LLM）。
        
        与 _answer_chain 的唯一区别：跳过理解层，直接使用传入的 understand。
        """
        # 从第 2 层（澄清层）开始执行
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

        # 第 3 层及以后：走正常流程
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

        # 第 4 层：生成层
        plan = generate_plan(
            rewritten, retrieval_result, schema_context, self.metadata, self.llm
        )
        logger.info("[%s] Plan: tables=%s, cols=%s", request_id, plan.target_tables, plan.select_columns)

        # 注意：不再用 plan.intent 覆盖 Router 的意图分类
        # Router 已确认为 data_query，即使 generate_plan 的 LLM 返回 out_of_scope 也继续执行
        # 这避免了两次 LLM 调用意图不一致的问题

        # 第 5 层：校验层（行级先于列级）
        plan = self.perm_checker.apply_row_permissions(plan, user)
        plan = self.perm_checker.apply_column_permissions(plan, user)

        sql, params = plan.to_sql()
        logger.info("[%s] SQL: %s | params: %s", request_id, sql, params)

        allowed_tables = set(retrieval_result.table_names)
        validate_and_raise(self.validator, plan, sql, allowed_tables)

        # 第 6 层：执行层
        exec_result = None
        last_error = None

        for round_idx in range(self.s.security.max_repair_rounds + 1):
            try:
                exec_result = self.executor.execute(sql, params)
                last_error = None
                break
            except PermissionError:
                raise
            except Exception as e:
                last_error = str(e)
                logger.warning("[%s] Execution failed (round %d): %s", request_id, round_idx, e)

                if round_idx < self.s.security.max_repair_rounds:
                    repaired = attempt_repair(
                        plan, str(e), schema_context, rewritten, self.metadata, self.llm
                    )
                    if repaired:
                        plan = repaired
                        plan = self.perm_checker.apply_row_permissions(plan, user)
                        plan = self.perm_checker.apply_column_permissions(plan, user)
                        sql, params = plan.to_sql()
                        validate_and_raise(self.validator, plan, sql, allowed_tables)
                    else:
                        break

        if exec_result is None:
            return AgentResponse(
                answer=f"抱歉，查询执行失败：{last_error}",
                error=last_error,
            )

        # 第 7 层：解释层
        answer_text = self._explain(rewritten, exec_result, schema_context)

        turn = Turn(
            question=question,
            rewritten_question=rewritten,
            intent=understand.intent,
            sql_executed=exec_result.sql,
            answer=answer_text,
            tables_used=plan.target_tables,
        )
        session.add_turn(turn)

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

    def _answer_composite(
        self,
        original_question: str,
        rewritten: str,
        decomposition: "DecompositionResult",
        session: SessionState,
        user: UserContext,
        request_id: str,
        start_time: float,
    ) -> AgentResponse:
        """复合问题：Decompose-Merge 流程"""
        from app.core.context import ExecutionContext
        from app.core.merger import merge_results
        from app.core.decomposer import DecompositionResult

        ctx = ExecutionContext()

        for sub_q in decomposition.sub_questions:
            resolved_question = ctx.resolve_placeholders(sub_q.question)
            logger.info(
                "[%s] Executing sub-question %d: %s",
                request_id,
                sub_q.id,
                resolved_question,
            )

            sub_result = self._answer_sub_question(
                resolved_question, session, user, request_id, sub_q.id
            )

            ctx.add_result(sub_q.id, sub_result)

            if sub_result.error:
                logger.warning(
                    "[%s] Sub-question %d failed: %s", request_id, sub_q.id, sub_result.error
                )
                break

        final_answer = merge_results(
            original_question,
            ctx,
            decomposition.merge_strategy,
            decomposition.merge_description,
            self.llm,
        )

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        turn = Turn(
            question=original_question,
            rewritten_question=rewritten,
            intent="composite_query",
            answer=final_answer,
        )
        session.add_turn(turn)

        self.audit.log(
            request_id=request_id,
            user_id=user.user_id,
            role=user.role,
            session_id=session.session_id,
            question=original_question,
            rewritten_question=rewritten,
            generated_sql="",
            candidate_tables=[],
            tables_accessed=[],
            columns_accessed=[],
            answer=final_answer,
            row_count=0,
            execution_ms=elapsed_ms,
        )

        return AgentResponse(
            answer=final_answer,
            execution_time_ms=elapsed_ms,
        )

    def _answer_sub_question(
        self,
        question: str,
        session: SessionState,
        user: UserContext,
        request_id: str,
        sub_id: int,
    ) -> "SubResult":
        """执行单个子问题"""
        from app.core.context import SubResult

        try:
            retrieval_result = self.retriever.retrieve(question)
            schema_context = self.retriever.get_context_for_llm(retrieval_result)

            if not retrieval_result.table_names:
                return SubResult(
                    id=sub_id,
                    question=question,
                    answer="抱歉，没有找到相关数据表。",
                    error="No relevant tables found",
                )

            plan = generate_plan(
                question, retrieval_result, schema_context, self.metadata, self.llm
            )

            plan = self.perm_checker.apply_row_permissions(plan, user)
            plan = self.perm_checker.apply_column_permissions(plan, user)

            sql, params = plan.to_sql()

            allowed_tables = set(retrieval_result.table_names)
            validate_and_raise(self.validator, plan, sql, allowed_tables)

            exec_result = self.executor.execute(sql, params)

            answer_text = self._explain(question, exec_result, schema_context)

            return SubResult(
                id=sub_id,
                question=question,
                answer=answer_text,
                sql=exec_result.sql,
                columns=exec_result.columns,
                rows=exec_result.rows,
            )

        except Exception as e:
            logger.exception("Sub-question %d failed: %s", sub_id, e)
            return SubResult(
                id=sub_id,
                question=question,
                answer=f"查询失败：{e}",
                error=str(e),
            )
