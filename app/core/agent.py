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
from app.models.plan import AgentResponse, ExecResult
from app.models.state import SessionState, Turn, UserContext
from app.schema_rag.indexer import SchemaIndex
from app.schema_rag.metadata import SchemaMetadata, load_schema
from app.schema_rag.retriever import SchemaRetriever
from app.security.audit import AuditLogger
from app.security.permissions import PermissionChecker
from app.sql import compiler
from app.sql.executor import SQLExecutor
from app.sql.generator import GenerationResult, generate_sql
from app.sql.repair import attempt_repair
from app.sql.validator import SecurityValidator

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
        5. 生成层：LLM → SQL → sqlglot AST
        6. 校验层：AST 白名单 + 行列权限
        7. 执行层：只读执行 + 修复循环
        """
        request_id = str(uuid.uuid4())[:8]
        start_time = time.perf_counter()
        session = self.sessions.get_or_create(session_id, user)

        try:
            # 多轮对话：如果上一轮是澄清追问，将用户回答与原始问题合并
            question = self._resolve_clarification_followup(question, session, request_id)

            # 工作记忆：获取会话上下文（摘要 + 最近问答 + 查询状态）
            session_context = session.get_context_for_understanding()

            # Router: 先判断是否为复合问题
            from app.core.decomposer import decompose_question
            
            understand = analyze_intent(
                question, session.recent_questions(), self.llm, user, session_context
            )
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

    def _resolve_clarification_followup(
        self, question: str, session: SessionState, request_id: str
    ) -> str:
        """
        多轮对话上下文合并：如果上一轮是澄清追问，将用户回答与原始问题合并。

        例如：
        - 上一轮用户问："缺勤过员工的绩效怎么样？"
        - 系统追问："请问哪个时间段？看哪个指标？"
        - 用户回答："本月、平均绩效"
        - 合并为："本月缺勤过员工的平均绩效是多少？"
        """
        if not session.turns:
            return question

        last_turn = session.turns[-1]
        if last_turn.intent != INTENT_NEED_CLARIFICATION:
            return question

        # 上一轮是澄清追问，用 LLM 合并原始问题 + 用户回答
        original_question = last_turn.question
        merge_prompt = f"""用户之前问了一个问题，系统追问了细节，用户现在回答了追问。
请将原始问题和用户的回答合并为一个完整、明确的查询问题。

原始问题：{original_question}
系统追问：{last_turn.answer}
用户回答：{question}

合并后的完整问题（只输出问题本身，不要其他内容）："""

        messages = [
            ChatMessage(role="system", content="你是一个问题合并助手。将多轮对话合并为一个完整的查询问题。只输出合并后的问题，不要任何解释。"),
            ChatMessage(role="user", content=merge_prompt),
        ]
        merged = self.llm.chat(messages, temperature=0.0).strip()

        if merged and len(merged) > 5:
            logger.info(
                "[%s] Clarification follow-up merged: '%s' + '%s' -> '%s'",
                request_id, original_question, question, merged,
            )
            return merged

        return question

    def _maybe_generate_summary(self, session: SessionState, request_id: str) -> None:
        """
        工作记忆：当轮次超过阈值时，生成旧轮对话摘要。

        摘要存入 session.summary，后续理解层会注入此摘要。
        """
        if not session.should_summarize():
            return

        # 取需要摘要的旧轮（保留最近 N 轮不摘要）
        turns_to_summarize = session.turns[:-session.KEEP_RECENT_TURNS]
        if not turns_to_summarize:
            return

        # 构建摘要输入
        qa_pairs = []
        for t in turns_to_summarize:
            qa_pairs.append(f"Q: {t.question}")
            if t.answer:
                qa_pairs.append(f"A: {t.answer[:150]}")

        summary_prompt = f"""请将以下对话历史压缩为一段简洁的摘要（不超过 100 字），保留关键信息：

{chr(10).join(qa_pairs)}

摘要（只输出摘要内容，不要其他）："""

        messages = [
            ChatMessage(
                role="system",
                content="你是一个对话摘要助手。将多轮对话压缩为简洁的摘要，保留查询主题、关键实体和时间范围。",
            ),
            ChatMessage(role="user", content=summary_prompt),
        ]

        try:
            summary = self.llm.chat(messages, temperature=0.0).strip()
            if summary and len(summary) > 5:
                session.summary = summary
                logger.info("[%s] Generated session summary: %s", request_id, summary[:100])
        except Exception as e:
            logger.warning("[%s] Failed to generate summary: %s", request_id, e)

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
        
        新管线：LLM → SQL → sqlglot AST → 校验 → 权限注入 → 参数化 → 执行
        """
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

        # 第 3 层：检索层（Schema RAG）
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

        # 第 4 层：生成层（LLM 直接生成 SQL）
        gen_result = generate_sql(
            rewritten, retrieval_result, schema_context, self.metadata, self.llm
        )

        if not gen_result.sql:
            return self._handle_out_of_scope(
                question, session, user, request_id, start_time
            )

        # 第 5-6 层：校验 + 权限 + 执行（含修复循环）
        allowed_tables = set(retrieval_result.table_names)
        exec_result, final_sql, tables_used, columns_used = self._validate_and_execute(
            gen_result.sql, allowed_tables, user, rewritten, schema_context, request_id
        )

        if exec_result is None:
            return AgentResponse(
                answer="抱歉，查询执行失败，请稍后重试。",
                error="execution_failed",
            )

        # 第 7 层：解释层
        answer_text = self._explain(rewritten, exec_result, schema_context)

        turn = Turn(
            question=question,
            rewritten_question=rewritten,
            intent=understand.intent,
            sql_executed=final_sql,
            answer=answer_text,
            tables_used=tables_used,
        )
        session.add_turn(turn)

        # 工作记忆：检查是否需要生成摘要
        self._maybe_generate_summary(session, request_id)

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        self.audit.log(
            request_id=request_id,
            user_id=user.user_id,
            role=user.role,
            session_id=session.session_id,
            question=question,
            rewritten_question=rewritten,
            generated_sql=final_sql,
            candidate_tables=retrieval_result.table_names,
            tables_accessed=tables_used,
            columns_accessed=columns_used,
            answer=answer_text,
            row_count=exec_result.row_count,
            execution_ms=elapsed_ms,
        )

        return AgentResponse(
            answer=answer_text,
            sql=final_sql,
            tables_used=tables_used,
            row_count=exec_result.row_count,
            execution_time_ms=elapsed_ms,
        )

    def _validate_and_execute(
        self,
        sql: str,
        allowed_tables: set[str],
        user: UserContext,
        question: str,
        schema_context: str,
        request_id: str,
    ) -> tuple[ExecResult | None, str, list[str], list[str]]:
        """
        校验 + 权限注入 + 执行（含修复循环）。

        返回: (exec_result, final_sql, tables_used, columns_used)
        """
        exec_result = None
        last_error = None
        current_sql = sql
        tables_used: list[str] = []
        columns_used: list[str] = []

        for round_idx in range(self.s.security.max_repair_rounds + 1):
            try:
                # 解析 SQL 为 AST
                ast = compiler.parse_sql(current_sql)

                # 时间参数规范化（AST 级兜底）
                from app.sql.param_normalizer import normalize_time_params
                normalize_time_params(ast)

                # AST 级安全校验
                errors = self.validator.validate_ast(ast, allowed_tables)
                if errors:
                    msg = "; ".join(e.message for e in errors)
                    raise PermissionError(f"SQL 校验失败: {msg}")

                # 行级权限先注入（AST 级）
                ast = self.perm_checker.apply_row_permissions(ast, user)
                # 列级权限（AST 级，依赖行级注入结果判断 salary 可见性）
                ast = self.perm_checker.apply_column_permissions(ast, user, current_sql)

                # 提取参数化 SQL（从原始 SQL 提取字面量值）
                final_sql, final_params = compiler.extract_params(ast, current_sql)
                tables_used = compiler.extract_tables(ast)
                columns_used = compiler.extract_columns(ast)

                logger.info("[%s] SQL: %s | params: %s", request_id, final_sql[:200], final_params)

                # 执行
                exec_result = self.executor.execute(final_sql, final_params)
                last_error = None
                current_sql = final_sql
                break

            except PermissionError:
                raise  # 安全错误不修复
            except Exception as e:
                last_error = str(e)
                logger.warning("[%s] Failed (round %d): %s", request_id, round_idx, e)

                if round_idx < self.s.security.max_repair_rounds:
                    repaired = attempt_repair(
                        current_sql, str(e), schema_context, question,
                        self.metadata, self.llm,
                    )
                    if repaired and repaired.sql:
                        current_sql = repaired.sql
                    else:
                        break
                else:
                    break

        return exec_result, current_sql, tables_used, columns_used

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
            # 1. 解析占位符（多行结果现在会包含所有值）
            resolved_question = ctx.resolve_placeholders(sub_q.question)

            # 2. 为有依赖的子问题注入前序结果数据
            dep_context = ctx.get_dependency_context(sub_q)
            if dep_context:
                resolved_question = resolved_question + "\n\n" + dep_context

            logger.info(
                "[%s] Executing sub-question %d: %s",
                request_id,
                sub_q.id,
                resolved_question[:200],
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

        # 工作记忆：检查是否需要生成摘要
        self._maybe_generate_summary(session, request_id)

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
        """执行单个子问题（新管线：LLM → SQL → AST → 权限 → 执行）"""
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

            # LLM 直接生成 SQL
            gen_result = generate_sql(
                question, retrieval_result, schema_context, self.metadata, self.llm
            )
            if not gen_result.sql:
                return SubResult(
                    id=sub_id,
                    question=question,
                    answer="抱歉，无法生成查询。",
                    error="Empty SQL",
                )

            # 校验 + 权限 + 执行
            allowed_tables = set(retrieval_result.table_names)
            exec_result, final_sql, _, _ = self._validate_and_execute(
                gen_result.sql, allowed_tables, user, question, schema_context, request_id
            )

            if exec_result is None:
                return SubResult(
                    id=sub_id,
                    question=question,
                    answer="查询执行失败。",
                    error="execution_failed",
                )

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
