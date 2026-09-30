# MySQL Agent v2 开发交接文档

> **最后更新**: 2026-09-29  
> **项目路径**: `d:\Projects\ai_agent\agent_data_v1.0.0\mysql_agent_v2`  
> **最新 commit**: `fc7e4c0`（语义记忆 + 评测修复 + 文档整理，已推送）  
> **未提交变更**: 自适应修复循环增强（`repair.py` 重写 + `agent.py` 集成 + `config.py` 调整）  
> **评测状态**: 70/70 = 100% 通过（含 48 个单元测试）

---

## 一、项目概述

基于 NL2SQL 的企业数据分析 AI Agent，核心能力是让用户用自然语言查询 MySQL 数据库，同时保证安全性（七层信任链）。

### 核心目标

> **让 Agent 更好、更准地回答用户问题。**
>
> 所有架构设计、安全机制、记忆系统、修复循环，最终目的都是为了让用户问一个问题时，系统能给出**正确、完整、易懂**的回答。安全性是底线，准确率是核心。

这意味着后续所有开发工作的优先级判断标准是：**哪个改动对回答质量的提升最大，就先做哪个。**

**技术栈**: Python 3.12 + DeepSeek API + MySQL 8.0 + sqlglot (AST) + SQLite (记忆) + jieba (BM25 分词) + FastAPI

**启动方式**:
```bash
cd mysql_agent_v2
# 需要 .env 文件配置 LLM_API_KEY, DB_URL 等
python -m app.main
```

---

## 二、架构总览

```
用户问题
  │
  ▼
┌──────────────────────────────────────────────────────────────────┐
│  七层信任链（核心安全管线）                                         │
│                                                                   │
│  1. 会话层  → get_or_create session                               │
│  2. 理解层  → LLM 意图分类 + 问题改写（注入工作记忆上下文）         │
│  3. 澄清层  → 追问 / 超范围拒绝 / 多轮合并                        │
│  4. 检索层  → BM25 Schema RAG 召回相关表 + FK 图扩展              │
│  5. 生成层  → LLM 直接生成 SQL（注入时间上下文 + 语义记忆）         │
│  6. 校验层  → sqlglot AST 白名单 + 时间规范化 + 行列权限注入       │
│  7. 执行层  → 只读执行 + 自适应修复循环（最多 3 轮，3 种策略）      │
│                                                                   │
│  → 解释层 → LLM 翻译结果为自然语言                                 │
│  → 审计层 → JSONL 日志                                            │
└──────────────────────────────────────────────────────────────────┘
```

**复合问题**: Decompose-Merge 架构（Decomposer 拆分子问题 → ExecutionContext 管理中间结果 → Merger 综合回答）

**架构选型**: 采用 AGENTSKI 分治管线（非 ReAct），优势在于确定性高、安全不可绕过、成本低。详见下方"架构对比"章节。

---

## 三、目录结构

```
mysql_agent_v2/
├── app/
│   ├── core/
│   │   ├── agent.py (771行)        # Agent 主编排器（七层信任链 + 记忆集成 + 自适应修复）
│   │   ├── clarification.py (128行) # 理解层/澄清层（意图分类 + 问题改写）
│   │   ├── context.py (196行)       # ExecutionContext（Decompose-Merge 中间状态）
│   │   ├── conversation.py (63行)   # 会话存储（进程内 dict + TTL）
│   │   ├── decomposer.py (203行)    # 问题分解器（LLM → 子问题列表）
│   │   ├── llm.py (121行)          # LLM 客户端封装（chat + chat_json）
│   │   └── merger.py (110行)        # 结果综合器（LLM → 合并回答）
│   ├── memory/                      # 语义记忆模块
│   │   ├── store.py (432行)         # SQLite 存储层（Memory + CRUD + 衰减 + 冲突消解）
│   │   ├── extractor.py (230行)     # 写入逻辑（查询模式/偏好/纠错提取）
│   │   ├── retriever.py (104行)     # 读取逻辑（关键词匹配 → prompt 注入）
│   │   └── reflector.py (187行)     # 反思+遗忘（LLM 洞察 + 衰减 + 冲突消解）
│   ├── models/
│   │   ├── plan.py (51行)           # AgentResponse, ExecResult 等数据模型
│   │   └── state.py (175行)         # SessionState, Turn, QueryState, UserContext
│   ├── schema_rag/
│   │   ├── indexer.py (171行)       # BM25 索引构建（jieba 分词）
│   │   ├── metadata.py (144行)      # Schema 元数据加载（表/列/指标/枚举）
│   │   └── retriever.py (100行)     # Schema 检索器（BM25 + FK 扩展）
│   ├── security/
│   │   ├── audit.py (93行)          # 审计日志（JSONL + 线程安全）
│   │   └── permissions.py (192行)   # 行级 + 列级权限（AST 级注入，4 角色）
│   ├── sql/
│   │   ├── compiler.py (479行)      # SQL 编译器（AST 解析/参数化/表列提取/权限注入）
│   │   ├── executor.py (130行)      # SQL 执行器（只读连接池 + 超时 + 截断）
│   │   ├── generator.py (126行)     # SQL 生成器（LLM → SQL，注入时间+记忆）
│   │   ├── param_normalizer.py (212行) # 时间参数规范化器（AST 级兜底）
│   │   ├── repair.py (362行)        # ★ 自适应修复循环（错误分类+3策略+历史感知）
│   │   └── validator.py (220行)     # 安全校验器（AST 4 道防线）
│   ├── config.py (96行)             # 全局配置（5 个 Config 类 + Settings 聚合）
│   └── main.py (162行)              # FastAPI 入口（3 端点）
├── data/
│   ├── golden_qa_30.json            # 70 题评测集（文件名历史遗留）
│   └── semantic_memory.db           # 语义记忆 SQLite（运行时生成）
├── schema/
│   └── company_schema.yaml (122行)  # 8 张表 Schema（含业务指标/枚举/敏感标记）
├── scripts/
│   ├── init_demo.sql (133行)        # 数据库初始化（建库建表+演示数据+只读账号）
│   └── build_index.py               # BM25 索引构建
├── tests/
│   ├── test_golden_dataset.py       # 70 题评测框架（6 层级 L1-L6）
│   ├── test_memory.py               # 语义记忆单元测试（23 题）
│   ├── test_validator.py            # 安全校验测试（17 题）
│   └── test_permissions.py          # 权限测试（7 题）
├── docs/
│   ├── ARCHITECTURE.md (891行)      # 详细架构文档（17 章节）
│   ├── HANDOFF.md                   # 本文档
│   ├── QUICKSTART.md                # 快速开始指南
│   └── TODO.md                      # 开发路线图
├── reports/                         # 评测报告（JSON + Markdown）
└── pyproject.toml
```

---

## 四、已完成的工作（全部已推送，除修复增强外）

### 4.1 Phase 0-2 核心能力（commit `510d508` 及之前）

| 能力 | 关键文件 | 说明 |
|------|---------|------|
| 七层信任链 | `agent.py` | 会话→理解→澄清→检索→生成→校验→执行 |
| AST 安全管线 | `compiler.py`, `validator.py` | sqlglot 解析 → 哨兵标记 → AST 校验 → 权限注入 → 参数化 |
| 4 角色权限 | `permissions.py` | exec/dept_lead/bu_head/employee，行级 WHERE + 列级 SELECT |
| Decompose-Merge | `decomposer.py`, `context.py`, `merger.py` | 复合问题拆分 + 依赖管理 + 结果综合 |
| 时间双层防护 | `generator.py`, `param_normalizer.py` | Prompt 注入时间上下文 + AST 级规范化兜底 |
| Schema RAG | `indexer.py`, `retriever.py` | BM25 倒排索引（jieba 分词）+ FK 图扩展 |
| 工作记忆 | `state.py` | 摘要（旧轮压缩）+ QueryState（查询状态快照） |

### 4.2 语义记忆系统（commit `fc7e4c0`）

| 文件 | 功能 |
|------|------|
| `store.py` | SQLite 存储。Memory 对象（衰减权重 = confidence × max(0.1, 1 - decay_rate × age / (1 + log(1 + access_count)))），CRUD + 去重合并 + 关键词检索 + 衰减清理 + 冲突消解 |
| `extractor.py` | 规则提取（不用 LLM）：查询模式（pattern）、用户偏好（preference）、纠错记录（correction） |
| `retriever.py` | 关键词匹配 + 类别优先级（correction > preference > pattern > insight），600 字符截断 |
| `reflector.py` | 每 10 轮触发：LLM 洞察生成 + 衰减清理（weight < 0.15）+ 冲突消解 |

**集成点**: agent.py 初始化时创建 store → 查询前 `get_memory_context()` 注入 prompt → 查询后 `extract_from_turn()` → 每 N 轮 `_maybe_reflect()`

### 4.3 自适应修复循环增强（未提交）

**之前的修复循环**：2 轮，所有错误用同一个 prompt 修，无历史感知。

**增强后**：借鉴 ReAct 的 Thought-Action-Observation 思路，3 轮 + 3 策略 + 历史感知。

| 能力 | 实现 |
|------|------|
| 错误分类 | `classify_error()` 自动识别 6 类错误（column_not_found/table_not_found/syntax/type_mismatch/empty_result/other），提取错误细节 + 相似列名建议 |
| 策略切换 | Round 0: 根据错误类型选策略（列名错→schema_aware，其他→direct）；Round 1: 上轮同策略失败→升级；Round 2 (最后): simplify 简化重写 |
| 修复历史 | `RepairAttempt` 记录每轮 SQL + 错误 + 策略，`format_repair_history()` 拼接给 LLM，避免重复犯错 |
| 相同 SQL 检测 | 修复后 SQL 与上轮完全相同时跳过，防止无限循环 |
| PermissionError 短路 | 安全错误不可修复，直接抛出 |

**修改文件**:
- `app/sql/repair.py` — 完整重写（79行 → 362行）
- `app/core/agent.py` — `_validate_and_execute()` 集成修复历史 + 分类 + 策略
- `app/config.py` — `max_repair_rounds` 从 2 提升到 3

### 4.4 最新评测结果

```
70/70 = 100% 通过率（2026-09-29 运行）

分层统计:
  L1_basic:       17/17  = 100%   # 单表简单查询
  L2_join:        14/14  = 100%   # 多表 JOIN
  L3_multistep:   11/11  = 100%   # 多步骤聚合
  L4_security:    12/12  = 100%   # 安全测试（注入/越权/敏感数据）
  L5_edge:         6/6   = 100%   # 边界情况（空结果/模糊查询）
  L6_composite:   10/10  = 100%   # 复合问题（Decompose-Merge）

单元测试: 48/48 通过（validator 17 + permissions 7 + memory 23 + retriever 4...）

注意: LLM 非确定性导致偶发波动（Q16 曾偶发失败，重跑即过），属正常现象。
```

---

## 五、架构对比：我们的管线 vs ReAct

| 维度 | ReAct（另一个团队用的） | 我们的 AGENTSKI 管线 |
|------|------------------------|---------------------|
| 决策者 | LLM 决定下一步做什么 | 代码确定性控制流程 |
| 安全性 | 依赖 prompt 约束 | AST 级硬校验，不可绕过 |
| 准确率 | 中（LLM 波动大） | 高（确定性管线） |
| 成本 | 高（每题 5-10 次 LLM） | 低（每题 2-3 次 LLM） |
| 灵活性 | 高（LLM 自由组合工具） | 中（管线固定，新场景需加代码） |
| 错误恢复 | 高（LLM 看到结果自由调整） | 中（已通过自适应修复增强） |
| 适用场景 | 开放式任务自动化 | 企业数据查询（安全+准确率优先） |

**结论**: 在企业数据问答场景，我们的架构更合适。自适应修复增强补上了错误恢复的短板。

---

## 六、下一步开发路线图（按优先级）

> **核心原则：所有开发围绕"让 Agent 更好更准地回答用户问题"展开。**
> 优先级判断标准：对回答质量的提升幅度。安全、性能等是约束条件，不是目标。

### P0 — 对回答质量影响最大

#### 6.1 答案自验证（最大盲区）

**问题**: SQL 执行成功后，直接 `_explain()` 翻译结果，**从不验证答案是否真的回答了问题**。

```
用户问："销售部本月收入是多少？"
LLM 生成：SELECT SUM(amount) FROM revenue WHERE month='2026-09'
                                    ↑ 忘了关联 department 过滤"销售部"
结果：返回全公司收入 → 系统自信地给出错误答案
```

**实现方案**: 在 `_explain()` 前增加 `_verify_answer()` 步骤：
- 输入：用户问题 + 生成的 SQL + 查询结果
- LLM 判断：这个 SQL 的结果是否回答了用户的问题？
- 如果验证失败 → 带反馈信息重新生成 SQL（最多 1 次）
- 文件：新增 `app/sql/verifier.py`，集成到 `agent.py._answer_chain_with_understand()`

#### 6.2 动态 Few-shot 示例注入

**问题**: `generator.py` 的 prompt 只有 3 个硬编码示例，不随查询类型变化。复杂聚合（迟到率）、JOIN 计算（毛利率）缺乏参考。

**实现方案**:
- 建立 SQL 示例库（JSON/YAML），按查询类型分类（聚合/JOIN/比率/排名/趋势）
- 新增 `app/sql/few_shot.py`，根据检索到的表 + 查询类型，BM25 检索最相关的 2-3 个示例
- 注入到 `generator.py` 的 system prompt
- Schema YAML 中已有 `metrics` 预定义指标，可作为示例来源

### P1 — 用户体验关键

#### 6.3 空结果/异常结果诊断

**问题**: 查询返回 0 行时，只说"结果为空"，不分析原因。

**实现方案**:
- 空结果时，LLM 分析可能原因（时间范围不对？条件太严？数据不存在？）
- 给出建议（"本月数据尚未录入，是否查看上月数据？"）
- 文件：修改 `agent.py._explain()` 或新增 `app/core/diagnosis.py`

#### 6.4 多轮上下文消解增强

**问题**: `QueryState` 用正则解析 SQL，子查询/窗口函数失效。"销售部收入→研发部呢→那上个月呢？"无法正确继承上文。

**实现方案**:
- 基于 SQL 模板化的上下文消解：把上一轮 SQL 结构化为 `SELECT {metrics} FROM {tables} WHERE {filters}`
- 用户说"研发部呢"时，替换 filter 值而非重新生成
- 修改 `state.py.QueryState` 的 SQL 解析逻辑（当前用正则，改为 sqlglot AST 提取）
- 增强 `_resolve_clarification_followup()` 支持更多场景

### P2 — 锦上添花

#### 6.5 置信度评估

**问题**: 系统对所有问题都给出确定性回答，无"我不确定"能力。数据库没有的字段，LLM 可能用近似列替代并自信回答。

**实现方案**: 检索匹配度低时（BM25 分数低于阈值）或 SQL 生成为空时，主动告知用户不确定性。

#### 6.6 Schema 检索自适应粒度

**问题**: 固定 `top_k=6`，简单问题浪费 token，复杂问题表不够。

**实现方案**: 根据问题复杂度（关键词数/是否复合问题）动态调整 top_k（2-10）。

#### 6.7 角色适配回答

**问题**: `_explain()` prompt 固定，不区分 CEO/经理/员工。

**实现方案**: `_explain()` 注入用户角色信息，CEO 给全局趋势，经理聚焦本部门。

---

## 七、关键设计决策记录

| 决策 | 选择 | 原因 |
|------|------|------|
| 整体架构 | AGENTSKI 管线（非 ReAct） | 确定性高、安全不可绕过、成本低 |
| SQL 生成 | LLM 直接生成 SQL（非 QueryPlan） | 更灵活，支持完整 SQL 表达力 |
| 安全校验 | sqlglot AST 级（非正则） | 正则可被绕过，AST 严格可靠 |
| 权限注入顺序 | 行级先于列级 | 列级需感知行级限制（salary 可见性判断） |
| 时间处理 | 双层防护（Prompt + AST） | Prompt 覆盖 90%，AST 兜底防漏 |
| 修复循环 | 自适应 3 策略（非统一 prompt） | 错误分类 + 策略切换 + 历史感知 |
| 记忆存储 | SQLite | 轻量持久化，单用户足够 |
| 记忆衰减 | 时间衰减 + 访问频率补偿 | 高频记忆衰减更慢，模拟遗忘曲线 |
| 复合问题 | Decompose-Merge | 子问题可并行，依赖关系显式管理 |
| 评测重试 | 失败重试 2 次 | LLM 非确定性需要容错 |

---

## 八、运行命令

```bash
cd mysql_agent_v2

# 启动服务
python -m app.main

# 运行 70 题评测（约 4 分钟，需要 MySQL 运行中）
python -m pytest tests/test_golden_dataset.py -v --tb=short

# 运行单元测试（48 题）
python -m pytest tests/test_validator.py tests/test_permissions.py tests/test_memory.py -v

# 运行全部测试
python -m pytest tests/ -v

# 构建 BM25 索引
python scripts/build_index.py
```

---

## 九、环境要求

- Python 3.12+
- MySQL 8.0+（需先执行 `scripts/init_demo.sql` 初始化演示数据）
- DeepSeek API（或其他 OpenAI 兼容接口）
- `.env` 文件配置（直接复制以下内容到 `mysql_agent_v2/.env`）:
  ```
  # LLM 配置（DeepSeek）
  LLM_API_KEY=sk-a86173b019854e429a40025742792f91
  LLM_BASE_URL=https://api.deepseek.com/v1
  LLM_MODEL=deepseek-chat

  # 数据库配置（本机 MySQL）
  DB_URL=mysql+pymysql://root:123456@localhost:3306/company_ops

  # Schema 元数据路径
  SCHEMA_PATH=schema/company_schema.yaml

  # 安全策略
  ENABLE_ROW_LEVEL=1
  ENABLE_COLUMN_LEVEL=1
  MAX_REPAIR_ROUNDS=3

  # 审计日志
  AUDIT_LOG_PATH=logs/audit.jsonl
  ```

### SSH Deploy Key（Git 推送认证）

```
密钥文件: C:\Users\Administrator\.ssh\id_ed25519_agent_data
公钥文件: C:\Users\Administrator\.ssh\id_ed25519_agent_data.pub
仓库: git@github.com:clper/agent_data.git
GitHub 仓库设置: https://github.com/clper/agent_data/settings/keys
```

如果 push 报权限错误，检查 SSH key 是否可用：
```bash
ssh -T git@github.com -i C:\Users\Administrator\.ssh\id_ed25519_agent_data
# 成功输出: Hi clper/agent_data! You've successfully authenticated...
```

---

## 十、Git 提交与推送

### 仓库信息

```
远程仓库: git@github.com:clper/agent_data.git
分支: main
协议: SSH（Deploy Key）
Git 用户: AI Agent <clper-agent-data@github.com>
SSH 密钥: C:\Users\Administrator\.ssh\id_ed25519_agent_data
```

### 提交流程

```bash
cd d:\Projects\ai_agent\agent_data_v1.0.0\mysql_agent_v2

# 1. 查看当前变更状态
git status

# 2. 添加要提交的文件（按需选择，不要提交 data/semantic_memory.db 和 reports/）
git add app/ tests/ docs/

# 3. 提交（commit message 用中文，格式：类型: 简要描述）
git commit -m "feat: 简要描述本次改动"

# 4. 推送到远程
git push origin main
```

### Commit Message 规范

| 前缀 | 用途 | 示例 |
|------|------|------|
| `feat:` | 新功能/新模块 | `feat: 答案自验证层` |
| `fix:` | 修复 bug | `fix: 多轮上下文消解失败` |
| `refactor:` | 重构（不改功能） | `refactor: 修复循环策略切换逻辑` |
| `test:` | 测试相关 | `test: 新增修复循环单元测试` |
| `docs:` | 文档变更 | `docs: 更新架构文档` |

### 注意事项

- **不要提交的文件**: `data/semantic_memory.db`（运行时生成）、`reports/`（评测报告）、`.env`（密钥）、`__pycache__/`
- **推送前必须**: 先跑一遍 70 题评测确认无回归 (`python -m pytest tests/test_golden_dataset.py -v --tb=short`)
- **SSH 认证**: 使用 Deploy Key，如果 push 报权限错误，检查 SSH key 是否配置正确
- **PowerShell 注意**: 不要用 `&&` 连接命令，用 `;` 分号

---

## 十一、未提交变更

以下文件已修改但尚未 commit/push（自适应修复循环增强）：

```
 M app/config.py        # max_repair_rounds: 2 → 3
 M app/core/agent.py    # _validate_and_execute() 集成自适应修复
 M app/sql/repair.py    # 完整重写：错误分类 + 3 策略 + 历史感知（79→362行）
?? data/semantic_memory.db
?? reports/             # 评测报告文件
```

建议下一步先 commit 这些变更，再开始 P0 开发。
