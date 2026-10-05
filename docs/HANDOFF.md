# MySQL Agent v2 开发交接文档

> **最后更新**: 2026-10-06  
> **项目路径**: `d:\Projects\ProjectVir\agent_data`  
> **最新 commit**: `9b98752`（答案自验证机制，已推送）  
> **未提交变更**: 无  
> **评测状态**: 65/70 = 92.9%（含 55 个单元测试 100%）

---

## 一、项目概述

基于 NL2SQL 的企业数据分析 AI Agent，核心能力是让用户用自然语言查询 MySQL 数据库，同时保证安全性（七层信任链）。

### 核心目标

> **让 Agent 更好、更准地回答用户问题。**
>
> 所有架构设计、安全机制、记忆系统、修复循环，最终目的都是为了让用户问一个问题时，系统能给出**正确、完整、易懂**的回答。安全性是底线，准确率是核心。

这意味着后续所有开发工作的优先级判断标准是：**哪个改动对回答质量的提升最大，就先做哪个。**

**技术栈**: Python 3.12 + DeepSeek API + MySQL 8.0 (远程 146.56.250.254:3306) + sqlglot (AST) + SQLite (记忆) + jieba (BM25 分词) + FastAPI

**启动方式**:
```bash
cd d:\Projects\ProjectVir\agent_data
# 需要 .env 文件配置 LLM_API_KEY, DB_URL 等
python scripts/run.py
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
│  3. 澄清层  → 追问 / 超范围拒绝 / 多轮合并 / 日常对话快速通道    │
│  4. 检索层  → BM25 Schema RAG 召回相关表 + FK 图扩展              │
│  5. 生成层  → LLM 直接生成 SQL（注入时间上下文 + 语义记忆 + Few-shot）│
│  6. 校验层  → sqlglot AST 白名单 + 时间规范化 + 行列权限注入       │
│  7. 执行层  → 只读执行 + 自适应修复循环（最多 3 轮，3 种策略）      │
│                                                                   │
│  → 验证层 → LLM 后置验证（复杂查询）+ MISMATCH 自动重试         │
│  → 解释层 → LLM 翻译结果为自然语言                                 │
│  → 审计层 → JSONL 日志                                            │
└──────────────────────────────────────────────────────────────────┘
```

**复合问题**: Decompose-Merge 架构（Decomposer 拆分子问题 → ExecutionContext 管理中间结果 → Merger 综合回答）

**架构选型**: 采用 AGENTSKI 分治管线（非 ReAct），优势在于确定性高、安全不可绕过、成本低。详见下方"架构对比"章节。

---

## 三、目录结构

```
agent_data/
├── app/
│   ├── core/
│   │   ├── agent.py (~900行)      # Agent 主编排器（七层信任链 + 记忆集成 + 自适应修复 + 答案自验证）
│   │   ├── clarification.py (128行) # 理解层/澄清层（意图分类 + 问题改写）
│   │   ├── context.py (196行)       # ExecutionContext（Decompose-Merge 中间状态）
│   │   ├── conversation.py (63行)   # 会话存储（进程内 dict + TTL）
│   │   ├── decomposer.py (203行)    # 问题分解器（LLM → 子问题列表）
│   │   ├── llm.py (121行)          # LLM 客户端封装（chat + chat_json）
│   │   └── merger.py (110行)        # 结果综合器（LLM → 合并回答）
│   ├── memory/                      # 语义记忆模块
│   │   ├── store.py (432行)         # SQLite 存储层
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
│   │   ├── few_shot.py (~100行)     # ★ Few-shot 示例检索器（表名+标签+关键词三层过滤）
│   │   ├── generator.py (~150行)    # SQL 生成器（LLM → SQL，注入时间+记忆+Few-shot）
│   │   ├── param_normalizer.py (212行) # 时间参数规范化器（AST 级兗底）
│   │   ├── repair.py (362行)        # ★ 自适应修复循环（错误分类+3策略+历史感知）
│   │   ├── validator.py (220行)     # 安全校验器（AST 4 道防线）
│   │   └── verifier.py (169行)      # ★ 答案自验证器（LLM 后置验证 + MISMATCH 重试）
│   ├── config.py (96行)             # 全局配置（5 个 Config 类 + Settings 聚合）
│   └── main.py (162行)              # FastAPI 入口（3 端点）
├── data/
│   ├── golden_qa_30.json            # 70 题评测集（覆盖 12 张表，文件名历史遗留）
│   ├── few_shot_examples.json       # 20 条 Few-shot 示例（覆盖 12 张表）
│   └── semantic_memory.db           # 语义记忆 SQLite（运行时生成）
├── schema/
│   └── company_schema.yaml (~180行) # 12 张表 Schema（含业务指标/枚举/敏感标记/FK）
├── scripts/
│   ├── init_demo.sql (151KB)        # 数据库初始化（12 张表 + 3610 行数据 + 只读账号）
│   ├── generate_demo_data.py (500行) # 数据生成脚本（random.seed(42) 可重复）
│   ├── generate_golden.py           # Golden Dataset 生成脚本
│   ├── run.py                       # 交互式终端入口
│   └── build_index.py               # BM25 索引构建
├── tests/
│   ├── test_golden_dataset.py       # 70 题评测框架（6 层级 L1-L6）
│   ├── test_memory.py               # 语义记忆单元测试（23 题）
│   ├── test_validator.py            # 安全校验测试（17 题）
│   ├── test_permissions.py          # 权限测试（7 题）
│   └── test_retriever.py            # Schema 检索测试（8 题）
├── docs/
│   ├── ARCHITECTURE.md (891行)      # 详细架构文档（17 章节）
│   ├── HANDOFF.md                   # 本文档
│   ├── QUICKSTART.md                # 快速开始指南
│   └── TODO.md                      # 开发路线图
├── reports/                         # 评测报告（JSON + Markdown）
└── pyproject.toml
```

---

## 四、已完成的工作（全部已推送）

### 4.1 Phase 0-2 核心能力（commit `510d508` 及之前）

| 能力 | 关键文件 | 说明 |
|------|---------|------|
| 七层信任链 | `agent.py` | 会话→理解→澄清→检索→生成→校验→执行 |
| AST 安全管线 | `compiler.py`, `validator.py` | sqlglot 解析 → 哨兵标记 → AST 校验 → 权限注入 → 参数化 |
| 4 角色权限 | `permissions.py` | exec/dept_lead/bu_head/employee，行级 WHERE + 列级 SELECT |
| Decompose-Merge | `decomposer.py`, `context.py`, `merger.py` | 复合问题拆分 + 依赖管理 + 结果综合 |
| 时间双层防护 | `generator.py`, `param_normalizer.py` | Prompt 注入时间上下文 + AST 级规范化兗底 |
| Schema RAG | `indexer.py`, `retriever.py` | BM25 倒排索引（jieba 分词）+ FK 图扩展 |
| 工作记忆 | `state.py` | 摘要（旧轮压缩）+ QueryState（查询状态快照） |

### 4.2 语义记忆系统（commit `fc7e4c0`）

| 文件 | 功能 |
|------|------|
| `store.py` | SQLite 存储。Memory 对象（衰减权重），CRUD + 去重合并 + 关键词检索 + 衰减清理 + 冲突消解 |
| `extractor.py` | 规则提取（不用 LLM）：查询模式（pattern）、用户偏好（preference）、纠错记录（correction） |
| `retriever.py` | 关键词匹配 + 类别优先级（correction > preference > pattern > insight），600 字符截断 |
| `reflector.py` | 每 10 轮触发：LLM 洞察生成 + 衰减清理（weight < 0.15）+ 冲突消解 |

### 4.3 自适应修复循环增强

借鉴 ReAct 的 Thought-Action-Observation 思路，3 轮 + 3 策略 + 历史感知。

| 能力 | 实现 |
|------|------|
| 错误分类 | `classify_error()` 自动识别 6 类错误，提取错误细节 + 相似列名建议 |
| 策略切换 | Round 0: 根据错误类型选策略；Round 1: 升级；Round 2: simplify 简化重写 |
| 修复历史 | `RepairAttempt` 记录每轮 SQL + 错误 + 策略，避免重复犯错 |
| 相同 SQL 检测 | 修复后 SQL 与上轮完全相同时跳过，防止无限循环 |

### 4.4 P0 核心能力增强（commit `88df3d1` ~ `9b98752`）

| 能力 | 关键文件 | 说明 |
|------|---------|------|
| 动态 Few-shot 示例注入 | `few_shot.py`, `few_shot_examples.json` | 20 条示例，三层过滤（表名+标签+关键词），注入到 SQL 生成 prompt |
| 答案自验证 | `verifier.py` | LLM 后置验证，仅复杂查询触发，MISMATCH 自动重试（最多 1 次） |
| 数据大幅扩充 | `generate_demo_data.py`, `init_demo.sql` | 8 表 63 行 → 12 表 3610 行，新增 4 张业务表 |
| 日常对话友好回复 | `agent.py` | 问候/感谢/告别快速通道 + LLM 驱动的超范围回复 + SQL 攻击严格拒绝 |
| Golden Dataset 更新 | `golden_qa_30.json` | 70 题覆盖 12 张新表，参考答案基于真实数据 |
| 评测器增强 | `test_golden_dataset.py` | 支持 casual_chat 意图、模糊问题双路径、安全测试边界 |

### 4.5 最新评测结果

```
65/70 = 92.9% 通过率（2026-10-06 运行）

单元测试: 55/55 通过（validator 17 + permissions 7 + memory 23 + retriever 8）

5 个失败用例:
  Q07: jieba 分词限制（“薪”无法匹配“薪水/薪资”）
  Q25: SQL 参数化 bug（复杂子查询）
  Q35: 权限过滤边界（评测器调整）
  Q46: LLM 非确定性（模糊问题直接回答而非澄清）
  Q47: 不存在数据查询边界

注意: LLM 非确定性导致偶发波动，属正常现象。5 个失败均为已知技术限制。
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

> **核心原则：所有开发围绕“让 Agent 更好更准地回答用户问题”展开。**
> 优先级判断标准：对回答质量的提升幅度。

### P1 — 对回答质量有明确提升

#### 6.1 多轮上下文消解增强

**问题**: `QueryState` 用正则解析 SQL，子查询/窗口函数失效。“销售部收入→研发部呢→那上个月呢？”无法正确继承上文。

**方案**:
- 基于 SQL 模板化的上下文消解：把上一轮 SQL 结构化为 `SELECT {metrics} FROM {tables} WHERE {filters}`
- 用户说“研发部呢”时，替换 filter 值而非重新生成
- 修改 `state.py.QueryState` 的 SQL 解析逻辑（当前用正则，改为 sqlglot AST 提取）

#### 6.2 SQL 参数化 bug 修复

**问题**: Q25 失败根因——sqlglot 参数化时将 `__PARAM_X###` 插入到 `c.amount` 中间，SQL 解析失败。

**方案**: 修复 `compiler.py` 中参数化逻辑，确保不在标识符中间插入占位符。

#### 6.3 BM25 检索增强（同义词映射）

**问题**: Q07 失败根因——jieba 分词“薪”无法匹配“薪水/薪资”，BM25 要求精确 token 匹配。

**方案**: 在检索器中加同义词映射表，检索时自动扩展查询词。

### P2 — 锦上添花

#### 6.4 查询结果缓存

相同问题 5 分钟内重复问，直接返回缓存结果，减少 LLM 调用延迟。

#### 6.5 Golden Dataset 扩充到 100 题

当前 70 题覆盖不够，复合问题（L6）只有 5 题。补充更多跨表复杂查询场景。

#### 6.6 解释层差异化

当前 `_explain()` 对所有查询用同一套 prompt。可按查询类型差异化：趋势类用“上升/下降”描述，排名类用“第1名是...”，比率类用百分比。

#### 6.7 角色适配回答

`_explain()` 注入用户角色信息，CEO 给全局趋势，经理聚焦本部门。

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
- MySQL 8.0+（远程数据库：146.56.250.254:3306，用户 clp:123456，数据库 company_ops）
- DeepSeek API（或其他 OpenAI 兼容接口）
- `.env` 文件配置（直接复制以下内容到项目根目录 `.env`）:
  ```
  # LLM 配置（DeepSeek）
  LLM_API_KEY=sk-a86173b019854e429a40025742792f91
  LLM_BASE_URL=https://api.deepseek.com/v1
  LLM_MODEL=deepseek-chat

  # 数据库配置（远程 MySQL）
  DB_URL=mysql+pymysql://clp:123456@146.56.250.254:3306/company_ops

  # Schema 元数据路径
  SCHEMA_PATH=schema/company_schema.yaml

  # 安全策略
  ENABLE_ROW_LEVEL=1
  ENABLE_COLUMN_LEVEL=1
  MAX_REPAIR_ROUNDS=3

  # 审计日志
  AUDIT_LOG_PATH=logs/audit.jsonl
  ```

### SSH Deploy Key（已废弃，当前使用 HTTPS）

仓库当前使用 HTTPS 协议推送，无需 SSH key 配置。

---

## 十、Git 提交与推送

### 仓库信息

```
远程仓库: https://github.com/clper/agent_data.git
分支: master
协议: HTTPS
```

### 提交流程

```bash
cd d:\Projects\ProjectVir\agent_data

# 1. 查看当前变更状态
git status

# 2. 添加要提交的文件（不要提交 data/semantic_memory.db 和 reports/）
git add app/ tests/ docs/

# 3. 提交（commit message 用中文，格式：类型: 简要描述）
git commit -m "feat: 简要描述本次改动"

# 4. 推送到远程
git push
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
- **推送前必须**: 先跑一遍 70 题评测确认无回归
- **PowerShell 注意**: 不要用 `&&` 连接命令，用 `;` 分号

---

## 十一、未提交变更

无。所有变更已提交并推送。
