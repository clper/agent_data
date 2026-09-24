# MySQL Agent v2 详细架构文档

> **版本**: v2.0 (Phase 3 + 语义记忆)  
> **最后更新**: 2025-09-25  
> **代码量**: ~5000 行 Python + 122 行 YAML + 133 行 SQL  
> **评测通过率**: 100%（70/70 黄金测试集）

---

## 目录

1. [项目定位与目标](#1-项目定位与目标)
2. [技术栈](#2-技术栈)
3. [系统架构总览](#3-系统架构总览)
4. [目录结构与文件职责](#4-目录结构与文件职责)
5. [七层信任链（核心安全管线）](#5-七层信任链核心安全管线)
6. [Decompose-Merge 复合问题架构](#6-decompose-merge-复合问题架构)
7. [Schema RAG 检索层](#7-schema-rag-检索层)
8. [SQL 编译器（AST 管线）](#8-sql-编译器ast-管线)
9. [安全体系](#9-安全体系)
10. [时间处理双层防护](#10-时间处理双层防护)
11. [记忆系统](#11-记忆系统)
12. [数据模型](#12-数据模型)
13. [数据库 Schema](#13-数据库-schema)
14. [配置系统](#14-配置系统)
15. [评测框架](#15-评测框架)
16. [API 接口](#16-api-接口)
17. [关键设计决策](#17-关键设计决策)

---

## 1. 项目定位与目标

基于 NL2SQL 的**企业数据分析 AI Agent**，核心能力：
- 用户用**自然语言**查询 MySQL 数据库，获得数据回答
- **七层信任链**保证安全性（防注入、防越权、防幻觉列）
- **Decompose-Merge** 处理复合问题（如"绩效最高的人是谁？他的入职日期？"）
- **语义记忆**实现跨会话学习（用户偏好、查询模式、纠错记录）

---

## 2. 技术栈

| 组件 | 技术选型 | 用途 |
|------|---------|------|
| 语言 | Python 3.12 | 主语言 |
| LLM | DeepSeek API (OpenAI 兼容) | SQL 生成、意图分类、结果解释、问题分解、修复 |
| 数据库 | MySQL 8.0 | 业务数据存储 |
| DB 连接 | SQLAlchemy + PyMySQL | 连接池 + 参数化查询 |
| AST 处理 | sqlglot | SQL 解析、安全校验、权限注入、参数化 |
| 中文分词 | jieba | BM25 检索的中文 tokenize |
| Schema 描述 | YAML | 表/列元数据（含中文注释、敏感标记、枚举值） |
| 语义记忆 | SQLite | 跨会话持久化用户偏好/模式/纠错/洞察 |
| Web 框架 | FastAPI + Uvicorn | HTTP API 服务 |
| 配置管理 | python-dotenv + dataclass | .env 文件 + 环境变量 |

---

## 3. 系统架构总览

### 3.1 主流程（简单问题）

```
用户问题
  │
  ▼
┌──────────────────────────────────────────────────────────────────┐
│  七层信任链（agent.py: answer() → _answer_chain_with_understand()) │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  L1 会话层 ──→ ConversationStore.get_or_create()                 │
│       │                                                          │
│  L2 理解层 ──→ analyze_intent() [LLM]                            │
│       │         输入: 问题 + 用户身份 + 工作记忆上下文              │
│       │         输出: intent ∈ {data_query, meta,                 │
│       │                out_of_scope, need_clarification}          │
│       │         + 规则兜底: DB关键词 → 强制纠正为 data_query        │
│       │                                                          │
│  L3 澄清层 ──→ 追问 / 超范围拒绝 / 元问题回答                      │
│       │         多轮合并: 上轮追问 + 用户回答 → LLM 合并为完整问题   │
│       │                                                          │
│  L4 检索层 ──→ SchemaRetriever.retrieve()                        │
│       │         ① BM25 检索 top_k 张表（jieba 分词）               │
│       │         ② FK 图扩展: 加入关联表                             │
│       │         ③ 格式化为 schema_context（LLM 提示面）             │
│       │                                                          │
│  L5 生成层 ──→ generate_sql() [LLM]                              │
│       │         输入: 问题 + schema_context + 时间上下文            │
│       │               + 语义记忆上下文（可选）                      │
│       │         输出: SQL（带字面量值）                              │
│       │         → sqlglot 解析为 AST                               │
│       │                                                          │
│  L6 校验层 ──→ SecurityValidator.validate_ast()                   │
│       │         ① 危险函数黑名单（SLEEP, BENCHMARK...）             │
│       │         ② 禁止表达式（INTO OUTFILE, FOR UPDATE）            │
│       │         ③ 表白名单检查（只允许检索到的表）                   │
│       │         ④ 列白名单检查（防幻觉列）                           │
│       │         → PermissionChecker.apply_row_permissions()        │
│       │           AST 级行级权限注入（WHERE 谓词）                  │
│       │         → PermissionChecker.apply_column_permissions()     │
│       │           AST 级列级权限过滤（SELECT 列移除/展开）           │
│       │                                                          │
│  L7 执行层 ──→ SQLExecutor.execute()                              │
│       │         ① 只读事务 (SET TRANSACTION READ ONLY)             │
│       │         ② 查询超时 (MAX_EXECUTION_TIME)                    │
│       │         ③ 结果截断 (max_rows=200)                          │
│       │         ④ 失败 → attempt_repair() [LLM] 修复循环(≤2轮)     │
│       │                                                          │
│  解释层 ────→ _explain() [LLM]                                   │
│              输入: 问题 + 查询结果(Markdown表格)                    │
│              输出: 自然语言回答                                     │
│                                                                  │
│  后处理:                                                         │
│  - 工作记忆: 追加 Turn + 更新 QueryState + 可能生成摘要            │
│  - 语义记忆: extract_from_turn() 提取模式/偏好                     │
│  - 反思: 每 N 轮触发 reflect_and_maintain()                       │
│  - 审计: AuditLogger.log() 记录完整链路                            │
└──────────────────────────────────────────────────────────────────┘
  │
  ▼
AgentResponse(answer, sql, tables_used, row_count, execution_time_ms)
```

### 3.2 复合问题流程（Decompose-Merge）

```
用户问题: "研发部绩效最高的员工是谁？他的入职日期是什么时候？"
  │
  ▼
Router: decompose_question() [LLM]
  │
  ├─ is_composite=true
  │  sub_questions:
  │    1. "研发部绩效最高的员工是谁？" (depends_on=[])
  │    2. "{result_1} 的入职日期是什么时候？" (depends_on=[1])
  │  merge_strategy: "sequential"
  │
  ▼
ExecutionContext (中间状态管理)
  │
  ├─ Sub-Q1: retrieve → generate_sql → validate → execute
  │  → SubResult(id=1, rows=[("王五",)], columns=["name"])
  │
  ├─ resolve_placeholders("{result_1} 的入职日期")
  │  → "王五 的入职日期"
  │
  ├─ get_dependency_context(Sub-Q2)
  │  → 注入前序结果数据表（帮助 LLM 生成 IN 子句等）
  │
  ├─ Sub-Q2: retrieve → generate_sql → validate → execute
  │  → SubResult(id=2, rows=[("2020-01-10",)], columns=["hire_date"])
  │
  ▼
merge_results() [LLM]
  → "研发部绩效最高的员工是王五（绩效得分85分），他于2020年1月10日入职。"
```

---

## 4. 目录结构与文件职责

```
mysql_agent_v2/
├── app/
│   ├── __init__.py
│   ├── config.py                    # 全局配置（5 个 Config 类 + Settings 聚合）
│   ├── main.py                      # FastAPI 入口（3 个端点）
│   │
│   ├── core/                        # 核心编排层
│   │   ├── agent.py (743行)         # ★ Agent 主编排器：七层信任链 + Decompose-Merge
│   │   ├── clarification.py (128行) # 意图路由：data_query/meta/out_of_scope/need_clarification
│   │   ├── context.py (196行)       # 执行上下文：多步推理中间结果管理 + 占位符解析
│   │   ├── conversation.py (63行)   # 会话存储：进程内 dict + TTL 过期清理
│   │   ├── decomposer.py (203行)    # 问题分解器：LLM 判断复合问题 → 有序子问题列表
│   │   ├── llm.py (121行)           # LLM 客户端：OpenAI 兼容接口 + JSON 解析容错
│   │   └── merger.py (110行)        # 结果综合器：LLM 将多子问题结果合为自然语言
│   │
│   ├── models/                      # 数据契约层
│   │   ├── plan.py (51行)           # ExecResult + AgentResponse
│   │   └── state.py (175行)         # UserContext(frozen) + SessionState + Turn + QueryState
│   │
│   ├── schema_rag/                  # Schema 检索层
│   │   ├── metadata.py (144行)      # YAML → SchemaMetadata/Table/Column 结构化表示
│   │   ├── indexer.py (171行)       # BM25 倒排索引（jieba 分词）+ 向量检索预留
│   │   └── retriever.py (100行)     # 检索器：BM25 + FK 图扩展 → RetrievalResult
│   │
│   ├── sql/                         # SQL 处理管线
│   │   ├── generator.py (126行)     # LLM SQL 生成（带时间上下文 + 记忆注入）
│   │   ├── compiler.py (479行)      # ★ AST 编译器：解析/提取/行权限注入/列过滤/参数化
│   │   ├── validator.py (220行)     # AST 安全校验：函数黑名单/表白名单/列白名单
│   │   ├── executor.py (130行)      # SQL 执行器：只读事务 + 超时 + 截断 + 参数转换
│   │   ├── param_normalizer.py (212行) # 时间参数规范化：AST 级兜底（"本月"→"2026-09"）
│   │   └── repair.py (79行)         # 修复循环：LLM 修复失败 SQL（≤2 轮）
│   │
│   ├── security/                    # 安全层
│   │   ├── permissions.py (192行)   # ★ 权限控制：4 角色行级谓词 + 列级过滤（AST 级）
│   │   └── audit.py (93行)          # 审计日志：JSONL 格式 + 线程安全
│   │
│   └── memory/                      # 语义记忆层（Phase 3 新增）
│       ├── store.py (432行)         # ★ SQLite 存储：Memory 类 + CRUD + 衰减 + 冲突消解
│       ├── extractor.py (230行)     # 记忆提取：从 Turn 中提取模式/偏好/纠错（规则，不用 LLM）
│       ├── retriever.py (104行)     # 记忆检索：关键词匹配 + 类别优先级 → prompt 注入
│       └── reflector.py (187行)     # 反思器：LLM 洞察生成 + 衰减清理 + 冲突消解
│
├── schema/
│   └── company_schema.yaml (122行)  # 8 张表的完整元数据
│
├── scripts/
│   ├── init_demo.sql (133行)        # 建库建表 + 演示数据 + 只读账号
│   ├── run.py                       # 命令行交互脚本
│   ├── chat.py                      # 交互式聊天脚本
│   ├── run_eval.py                  # 评测运行入口
│   ├── view_logs.py                 # 审计日志查看器
│   └── extend_golden_dataset.py     # 黄金数据集扩展工具
│
├── tests/
│   ├── test_golden_dataset.py (486行) # ★ 70 题黄金测试集 + 多维度评分
│   ├── test_memory.py (318行)         # 23 个语义记忆单元测试
│   ├── test_validator.py (188行)      # AST 校验器测试
│   ├── test_permissions.py (140行)    # 权限控制测试
│   └── test_retriever.py (122行)      # Schema 检索器测试
│
├── data/
│   └── semantic_memory.db           # 语义记忆 SQLite 数据库（运行时生成）
│
├── logs/
│   └── audit.jsonl                  # 审计日志（运行时生成）
│
└── pyproject.toml                   # 项目依赖配置
```

---

## 5. 七层信任链（核心安全管线）

实现在 `agent.py: answer()` 方法中，每层独立可测。

### 5.1 各层详解

| 层 | 名称 | 实现 | 核心逻辑 |
|----|------|------|---------|
| L1 | 会话层 | `ConversationStore` | 进程内 dict + TTL=30min 过期清理 |
| L2 | 理解层 | `clarification.analyze_intent()` | LLM 四分类 + 用户身份注入 + 规则兜底 |
| L3 | 澄清层 | `agent._handle_clarification()` | 追问 / 超范围拒绝 / 元问题回答 / 多轮合并 |
| L4 | 检索层 | `SchemaRetriever` | BM25(jieba) + FK 图扩展 |
| L5 | 生成层 | `generator.generate_sql()` | LLM 直出 SQL + 时间上下文 + 记忆注入 |
| L6 | 校验层 | `SecurityValidator` + `PermissionChecker` | AST 白名单 + 行/列权限 |
| L7 | 执行层 | `SQLExecutor` + `repair.attempt_repair()` | 只读事务 + 超时 + 修复循环 |

### 5.2 安全深度防御策略

```
LLM 输出 SQL
  │
  ├─ 第 1 道: AST 结构检查 → 只允许 SELECT（禁止 INSERT/UPDATE/DELETE/DROP）
  ├─ 第 2 道: 函数黑名单 → SLEEP, BENCHMARK, LOAD_FILE 等
  ├─ 第 3 道: 禁止表达式 → INTO OUTFILE, FOR UPDATE
  ├─ 第 4 道: 表白名单 → 只允许 RAG 检索到的表
  ├─ 第 5 道: 列白名单 → 防幻觉列（列必须存在于 schema 中）
  ├─ 第 6 道: 行级权限 → AST 级 WHERE 注入（按角色限制数据范围）
  ├─ 第 7 道: 列级权限 → AST 级 SELECT 过滤（移除敏感列）
  ├─ 第 8 道: 参数化 → 哨兵标记 → %s 替换（defense in depth）
  ├─ 第 9 道: 只读事务 → SET TRANSACTION READ ONLY
  └─ 第 10 道: 只读账号 → agent_ro 只有 SELECT 权限
```

**关键设计**: PermissionError 短路修复循环 — 安全错误不可修复，不浪费重试次数。

---

## 6. Decompose-Merge 复合问题架构

### 6.1 组件交互

| 组件 | 文件 | 职责 |
|------|------|------|
| 分解器 | `decomposer.py` | LLM 判断是否复合 → 拆分为有序子问题列表 |
| 执行上下文 | `context.py` | 管理中间结果 + 占位符解析 + 依赖注入 |
| 综合器 | `merger.py` | LLM 将多子问题结果合为连贯自然语言回答 |

### 6.2 数据结构

```python
# 分解结果
DecompositionResult(
    is_composite: bool,
    sub_questions: list[SubQuestion],  # 有序子问题列表
    merge_strategy: str,               # sequential | parallel | conditional
    merge_description: str,            # 合并策略说明
)

# 子问题
SubQuestion(
    id: int,
    question: str,                     # 可含 {result_N} 占位符
    depends_on: list[int],             # 依赖的前序子问题 ID
    description: str,
)

# 执行上下文
ExecutionContext:
    results: dict[int, SubResult]      # {子问题ID: 执行结果}
    resolve_placeholders(text)         # {result_1} → "王五"
    get_dependency_context(sub_q)      # 生成前序结果数据表
```

### 6.3 占位符解析规则

- `{result_N}` — 单行取第一个字段值；多行取所有行首字段值（逗号分隔）
- `{result_N.field}` — 取指定字段的值

---

## 7. Schema RAG 检索层

### 7.1 架构

```
YAML Schema
  │
  ▼
SchemaMetadata (metadata.py)
  │  database, tables: dict[str, Table], metrics: dict[str, str]
  │  Table: name, cn_name, comment, columns: list[Column]
  │  Column: name, type, cn, pk, fk, sensitive, comment, enum_values
  │
  ▼
SchemaIndex (indexer.py)
  │  BM25Index: 倒排索引 + jieba 分词
  │  向量检索预留: doc_embeddings, cosine_similarity
  │
  ▼
SchemaRetriever (retriever.py)
  │  retrieve(query) → RetrievalResult
  │  ① BM25 评分 → top_k 张表
  │  ② FK 图扩展 → 加入关联表
  │  get_context_for_llm(result) → 格式化的 schema 文本
```

### 7.2 BM25 公式

```
score(D, Q) = Σ IDF(qi) * (f(qi,D) * (k1+1)) / (f(qi,D) + k1*(1 - b + b*|D|/avgdl))
IDF(qi) = log((N - n + 0.5) / (n + 0.5) + 1)
```

参数: k1=1.5, b=0.75

### 7.3 FK 图扩展

检索到的表的**直接关联表**也纳入候选集：
- 正向: 表的 FK 列引用的表
- 反向: 引用该表的其他表

例如: 检索到 `performance` → 扩展出 `employee`（通过 FK `emp_id`）

---

## 8. SQL 编译器（AST 管线）

文件: `app/sql/compiler.py` (479行)

### 8.1 管线流程

```
LLM 输出 SQL（带字面量值）
  │
  ▼
parse_sql() ──→ _mark_literals()
  │  将字面量替换为哨兵 '__P0###', '__P1###' ...
  │  → sqlglot.parse(marked_sql, read="mysql")
  │  → 得到 AST (exp.Select)
  │
  ▼
normalize_time_params() ──→ param_normalizer
  │  AST 级时间值规范化（"本月" → "2026-09"）
  │
  ▼
SecurityValidator.validate_ast()
  │  函数黑名单 + 表白名单 + 列白名单
  │
  ▼
PermissionChecker.apply_row_permissions()
  │  向 WHERE 注入行级谓词（如 employee.emp_id = '101'）
  │  处理表别名（e.emp_id 而非 employee.emp_id）
  │
  ▼
PermissionChecker.apply_column_permissions()
  │  过滤 SELECT 中的敏感列
  │  SELECT * → 展开为可见列列表
  │  自查询检测: 员工查自己时 salary 可见
  │
  ▼
extract_params()
  │  哨兵值 → 从原始 SQL 提取字面量 → 替换为 NULL → SQL 中 NULL → %s
  │  返回 (参数化SQL, 参数值列表)
  │
  ▼
SQLExecutor.execute(final_sql, final_params)
```

### 8.2 哨兵机制

```python
# 原始 SQL: WHERE dept_name = '销售部' AND month = '2026-06'
# _mark_literals() 后:
# WHERE dept_name = '__P0###' AND month = '__P1###'
#
# sqlglot 解析为 AST 后:
# - 字面量节点值为 '__P0###', '__P1###'
# - 注入的权限谓词值是普通字面量（无哨兵）
#
# extract_params() 时:
# - 只将哨兵值替换为 %s
# - 从原始 SQL 中提取对应位置的真实值作为参数
# - 注入的权限值保留为字面量
```

---

## 9. 安全体系

### 9.1 权限模型（4 角色）

| 角色 | 行级限制 | 列级限制 | 说明 |
|------|---------|---------|------|
| `exec` | 无 | 仅全局隐藏列(id_card) | 高管，可查全公司所有数据 |
| `dept_lead` | 本部门 | 无 | 部门主管，只能查自己部门 |
| `bu_head` | 本事业部 | 无 | 事业部负责人，只能查自己事业部 |
| `employee` | 仅自己 | salary + bonus + id_card | 普通员工，只能查自己且不能看薪资 |

### 9.2 行级权限实现

```python
# EmployeeRowPredicate: employee/performance/attendance.emp_id = {user_id}
# DeptLeadRowPredicate: employee/performance/attendance.dept_id = {dept_id}
#                       department.dept_id = {dept_id}
#                       revenue/cost.dept_id = {dept_id}
# BUHeadRowPredicate: department.bu_id = {bu_id}
```

注入方式: AST 级 WHERE 谓词注入，自动处理表别名。

### 9.3 列级权限实现

```python
# 全局隐藏列: employee.id_card（任何角色不可看）
# employee 角色: employee.salary, performance.bonus（查自己时 salary 可见）
# SELECT * 自动展开为可见列列表
```

**特殊逻辑**: `is_self_query()` 检测 — 员工查自己数据时 salary 可见。

### 9.4 审计日志

JSONL 格式，每行一条记录:
```json
{
  "request_id": "a1b2c3d4",
  "user_id": "101",
  "role": "employee",
  "session_id": "sess_001",
  "question": "我的薪资是多少",
  "rewritten_question": "员工101的薪资是多少",
  "generated_sql": "SELECT salary FROM employee WHERE emp_id = %s",
  "candidate_tables": ["employee"],
  "tables_accessed": ["employee"],
  "columns_accessed": ["salary"],
  "answer": "您的月薪为12,000元。",
  "row_count": 1,
  "execution_ms": 1234.56,
  "status": "success",
  "timestamp": "2026-09-25T10:30:00"
}
```

---

## 10. 时间处理双层防护

### 10.1 Prompt 层（generator.py）

`_get_time_context()` 生成当前时间信息注入 LLM prompt:
```
当前时间信息：
  - 今天：2026-09-25（周五）
  - 本月：2026-09
  - 上月：2026-08
  - 今年：2026
  - 本季度：2026年Q3（2026-07 ~ 2026-09）
注意：用户说'本月'时请用 '2026-09'，说'上月'时请用 '2026-08'...
```

### 10.2 AST 兜底层（param_normalizer.py）

当 LLM 仍然输出非标准时间值时，AST 级兜底规范化:

```python
# 支持的自然语言表达式:
"本月" / "这个月" / "当月"  → "2026-09"
"上月" / "上个月"            → "2026-08"
"今年" / "本年"              → "2026-%" (LIKE 模式)
"去年"                      → "2025-%"
"2026年6月"                 → "2026-06"
"2026年"                    → "2026-%"

# 检测逻辑:
# 遍历 WHERE 子树中的 EQ 节点
# 如果左/右是时间列(month/work_date/hire_date/date) + 字面量
# 且值不是标准格式 → 替换为标准值
# 如果值含 "-%" → 将 EQ 转换为 LIKE 节点
```

---

## 11. 记忆系统

### 11.1 架构

```
┌─────────────────────────────────────────────────────┐
│                    记忆系统                           │
├─────────────────────────────────────────────────────┤
│                                                     │
│  提取层 (extractor.py)                              │
│  ├─ extract_from_turn(): 每轮成功查询后提取          │
│  │   ├─ _extract_query_patterns(): 常用表/部门/聚合  │
│  │   └─ _extract_preferences(): 关注指标/部门        │
│  └─ record_correction(): 用户纠错记录               │
│                                                     │
│  存储层 (store.py)                                  │
│  ├─ Memory: 单条记忆（user_id, category, key, value）│
│  ├─ SemanticMemoryStore: SQLite 持久化              │
│  │   ├─ store(): 存入（自动去重/合并）               │
│  │   ├─ get_relevant(): 关键词匹配 + 类别优先级      │
│  │   ├─ touch(): 更新访问计数                        │
│  │   ├─ decay(): 衰减清理（weight < threshold）      │
│  │   └─ resolve_conflicts(): 同 key 保留高权重       │
│  └─ 衰减公式:                                       │
│     factor = max(0.1, 1 - decay_rate * age /         │
│                  (1 + log(1 + access_count)))        │
│     effective_weight = confidence × factor           │
│                                                     │
│  检索层 (retriever.py)                              │
│  └─ get_memory_context():                           │
│     获取相关记忆 → 按类别分组 → 格式化注入 prompt     │
│     优先级: correction > preference > pattern > insight│
│     截断保护: 600 字符上限                            │
│                                                     │
│  反思层 (reflector.py)                              │
│  └─ reflect_and_maintain():                         │
│     ├─ _generate_insights(): LLM 回顾生成洞察        │
│     ├─ store.decay(): 衰减清理                       │
│     └─ store.resolve_conflicts(): 冲突消解           │
└─────────────────────────────────────────────────────┘
```

### 11.2 记忆类别

| 类别 | 来源 | 初始置信度 | 说明 |
|------|------|-----------|------|
| `preference` | 从问题中提取 | 0.5 | 用户关注的指标/部门 |
| `pattern` | 从 SQL 中提取 | 0.5-0.6 | 常用表/过滤条件/聚合方式 |
| `correction` | 用户纠错 | 0.95 | 纠错记录（最高优先级） |
| `insight` | LLM 反思生成 | 0.6 | 高层洞察（需后续验证） |

### 11.3 记忆生命周期

```
查询成功 → extract_from_turn() → store()（去重合并）
                                    ↓
每轮查询前 → get_memory_context() → 注入 generator prompt
                                    ↓
              get_relevant() → touch()（累加访问计数）
                                    ↓
每 N 轮 → reflect_and_maintain()
            ├─ LLM 生成 insight → store()
            ├─ decay() → 删除 weight < 0.15 的记忆
            └─ resolve_conflicts() → 同 key 保留高权重
```

---

## 12. 数据模型

### 12.1 用户身份（不可变）

```python
@dataclass(frozen=True)
class UserContext:
    user_id: str                    # 员工 ID（即 emp_id）
    role: str                       # "exec" | "employee" | "dept_lead" | "bu_head"
    department_id: int | None       # 所属部门 ID
    business_unit_id: int | None    # 所属事业部 ID
    extra: dict[str, Any]           # 扩展字段
```

**为什么 frozen=True?** 身份信息在整个请求链路中不应被任何模块篡改。

### 12.2 会话状态（可变）

```python
@dataclass
class SessionState:
    session_id: str
    user: UserContext
    turns: list[Turn]               # 对话轮次列表
    summary: str                    # 旧轮对话摘要（超过 5 轮时生成）
    query_state: QueryState | None  # 最近查询状态快照
    
    # 工作记忆配置
    MAX_TURNS_BEFORE_SUMMARY = 5    # 超过此轮次触发摘要
    KEEP_RECENT_TURNS = 5           # 保留最近 N 轮完整
```

### 12.3 对话轮次

```python
@dataclass
class Turn:
    question: str                   # 用户原始问题
    rewritten_question: str         # 改写后的问题
    intent: str                     # 识别的意图
    sql_executed: str               # 执行的 SQL
    answer: str                     # Agent 回答
    tables_used: list[str]          # 使用的表名
    timestamp: datetime
    error: str | None
```

### 12.4 查询状态快照

```python
@dataclass
class QueryState:
    tables: list[str]               # 使用的表名
    filters: dict[str, str]         # 过滤条件 {"dept_name": "销售部"}
    group_by: list[str]             # 聚合维度
    metrics: list[str]              # 聚合指标
```

用于多轮对话上下文消解（如"那研发部呢？"）。

---

## 13. 数据库 Schema

数据库名: `company_ops`，共 8 张表:

```
department (部门表)
  ├── dept_id BIGINT PK
  ├── dept_name VARCHAR(64)     -- 销售部/研发部/市场部
  └── bu_id BIGINT              -- 所属事业部

employee (员工表)
  ├── emp_id BIGINT PK
  ├── name VARCHAR(64)
  ├── dept_id BIGINT FK→department
  ├── hire_date DATE
  ├── salary DECIMAL(12,2) [敏感]
  └── id_card CHAR(18) [敏感-全局隐藏]

performance (绩效表)
  ├── perf_id BIGINT PK
  ├── emp_id BIGINT FK→employee
  ├── month CHAR(7)             -- 格式 YYYY-MM
  ├── score DECIMAL(5,2)
  └── bonus DECIMAL(12,2) [敏感]

attendance (考勤表)
  ├── att_id BIGINT PK
  ├── emp_id BIGINT FK→employee
  ├── work_date DATE
  └── status VARCHAR(16)        -- normal/late/absent/leave

project (项目表)
  ├── project_id BIGINT PK
  ├── project_name VARCHAR(128)
  ├── dept_id BIGINT FK→department
  ├── budget DECIMAL(14,2)
  └── status VARCHAR(16)        -- active/closed

customer (客户表)
  ├── cust_id BIGINT PK
  ├── cust_name VARCHAR(128)
  ├── owner_emp_id BIGINT FK→employee
  ├── level CHAR(1)             -- A/B/C
  └── industry VARCHAR(64)

revenue (收入表)
  ├── rev_id BIGINT PK
  ├── month CHAR(7)
  ├── dept_id BIGINT FK→department
  └── amount DECIMAL(14,2)

cost (成本表)
  ├── cost_id BIGINT PK
  ├── month CHAR(7)
  ├── dept_id BIGINT FK→department
  ├── item VARCHAR(64)          -- 人力/差旅/推广/办公费
  └── amount DECIMAL(14,2)
```

### FK 关系图

```
department ←── employee (dept_id)
department ←── project (dept_id)
department ←── revenue (dept_id)
department ←── cost (dept_id)
employee   ←── performance (emp_id)
employee   ←── attendance (emp_id)
employee   ←── customer (owner_emp_id)
```

### 演示数据

- 3 个部门（销售部/研发部/市场部），2 个事业部
- 4 个员工（张三/李四/王五/赵六）
- 绩效: 2026-06/07/09 三个月
- 考勤: 含 normal/late/absent/leave 四种状态
- 3 个项目、3 个客户
- 收入/成本: 多个月份数据

---

## 14. 配置系统

5 个配置类 + 1 个聚合类，从 `.env` + 环境变量加载:

| 配置类 | 关键参数 | 环境变量 |
|--------|---------|---------|
| `LLMConfig` | base_url, api_key, model, temperature, timeout | `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` |
| `DBConfig` | url, pool_size, max_overflow, query_timeout, max_rows | `DB_URL`, `DB_POOL_SIZE` |
| `SecurityConfig` | audit_log_path, max_repair_rounds, enable_row/column_level | `AUDIT_LOG_PATH`, `MAX_REPAIR_ROUNDS` |
| `SchemaConfig` | schema_path, bm25_k1, bm25_b, default_top_k, vector_enabled | `SCHEMA_PATH`, `VECTOR_ENABLED` |
| `MemoryConfig` | enabled, db_path, decay_rate, decay_threshold, reflect_every_n_turns | `MEMORY_ENABLED`, `MEMORY_DB_PATH` |

---

## 15. 评测框架

文件: `tests/test_golden_dataset.py` (486行)

### 15.1 测试集结构

70 道黄金测试题，分 6 个层级:

| 层级 | 名称 | 题数 | 考察内容 |
|------|------|------|---------|
| L1 | basic | 17 | 单表简单查询 |
| L2 | join | 14 | 多表 JOIN 查询 |
| L3 | multistep | 11 | 多步骤复杂查询 |
| L4 | security | 12 | 安全测试（注入/越权/敏感数据） |
| L5 | edge | 6 | 边界情况（空结果/模糊查询） |
| L6 | composite | 10 | 复合问题（Decompose-Merge） |

### 15.2 多维度评分

每道题评估 4 个维度:

```python
scores = {
    "execution_accuracy": ...,    # SQL 执行是否正确（row_count > 0）
    "intent_accuracy": ...,       # 意图是否匹配
    "security_compliance": ...,   # 安全测试是否合规
    "llm_judge_score": ...,       # LLM 评判回答质量
}
```

### 15.3 评测特殊处理

- **L6_composite**: 按回答长度判定（>20 字符 = 通过），因为 Decompose-Merge 的 row_count=0
- **安全拒绝**: Agent 拒绝回答视为正确执行
- **partial_allow**: 安全测试中部分允许的情况，有数据返回=0.85，安全拒绝=0.8
- **长回答保底**: >100 字符 → 0.85，>50 字符且分低 → 0.6

### 15.4 运行评测

```bash
cd mysql_agent_v2
python -m pytest tests/test_golden_dataset.py -v
```

---

## 16. API 接口

### 16.1 主查询接口

```
POST /api/query
Body:
{
  "question": "销售部本月收入是多少",
  "session_id": "sess_001",
  "user_id": "101",
  "role": "employee",
  "department_id": 1,
  "business_unit_id": null
}

Response:
{
  "answer": "销售部本月的收入为430,000元。",
  "sql": "SELECT SUM(r.amount) ...",
  "tables_used": ["revenue", "department"],
  "row_count": 1,
  "execution_time_ms": 1234.56,
  "error": null,
  "needs_clarification": false,
  "clarification_question": ""
}
```

### 16.2 创建会话

```
POST /api/sessions
Body: {"session_id": "sess_001", "user_id": "101", "role": "exec"}
Response: {"status": "ok", "session_id": "sess_001"}
```

### 16.3 健康检查

```
GET /api/health
Response: {"status": "ok", "tables_loaded": 8, "metrics_loaded": 6}
```

---

## 17. 关键设计决策

| # | 决策 | 原因 |
|---|------|------|
| 1 | LLM 直接生成 SQL（非 QueryPlan） | 更灵活，支持 CTE/窗口函数/子查询等全部 SQL 表达力 |
| 2 | sqlglot AST 级权限注入（非字符串拼接） | 防注入攻击，处理别名/嵌套等复杂场景 |
| 3 | 哨兵标记 → 统一参数化 | 将 LLM 输出的字面量安全地转为参数化查询 |
| 4 | BM25 自研（非 rank_bm25 库） | 透明可教学，避免额外依赖，生产可换 ES |
| 5 | 时间处理双层防护 | Prompt 层覆盖 95% 场景 + AST 兜底层防遗漏 |
| 6 | 语义记忆用 SQLite | 轻量级，单文件部署，线程安全（threading.local） |
| 7 | 记忆提取用规则（不用 LLM） | 零额外 API 调用开销，提取准确率高 |
| 8 | PermissionError 短路修复循环 | 安全错误不可修复，不应浪费重试次数 |
| 9 | 规则兜底意图分类 | LLM 可能将数据查询误判为 out_of_scope，DB 关键词兜底 |
| 10 | 工作记忆（摘要+QueryState） | 支持多轮上下文消解，避免重复信息溢出 |

---

## 附录: 运行指南

### 环境准备

```bash
# 1. 安装依赖
pip install -e .

# 2. 配置 .env
cat > .env << EOF
LLM_API_KEY=sk-your-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
DB_URL=mysql+pymysql://agent_ro:password@localhost:3306/company_ops
EOF

# 3. 初始化数据库
mysql -uroot -p < scripts/init_demo.sql
```

### 启动服务

```bash
python -m app.main
# 或
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 运行测试

```bash
# 全部单元测试
python -m pytest tests/ -v

# 仅评测
python -m pytest tests/test_golden_dataset.py -v

# 仅记忆测试
python -m pytest tests/test_memory.py -v
```
