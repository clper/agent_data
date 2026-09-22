# Agent 开发待办事项（TODO）

本文档记录后续需要实施的功能和优化项，按优先级排序。

---

## P0 - 高优先级（Phase 3 核心功能）

### 1. 问题分解 + 多步推理（Decompose-Merge）
**状态**：待实施  
**描述**：实现复杂问题的分解执行流程，支持条件依赖和多步推理。  
**架构**：
- `app/core/decomposer.py`：问题分解器（LLM 判断是否复合问题，拆分为有序子问题列表）
- `app/core/context.py`：执行上下文（存储中间结果，供后续子问题引用）
- `app/core/merger.py`：结果综合器（汇总所有子问题结果，生成最终回答）
- 修改 `app/core/agent.py`：在理解层后插入 Router，路由到单轮或分解流程

**关键设计**：
- Decomposer 输出格式：`{is_composite: bool, sub_questions: list[str], merge_strategy: str}`
- ExecutionContext 数据结构：`{sub_results: list[dict], dependencies: dict}`
- Merger 使用 LLM 综合，传入所有子问题结果作为上下文

**验收标准**：
- ✅ 能处理"找出绩效最高的员工，然后查他负责的客户"这类两步骤问题
- ✅ 能处理"销售部收入超过 50 万的月份，这些月份研发部的绩效平均分是多少"这类条件依赖问题
- ✅ 每个子问题独立走完整七层信任链（安全不变）
- ✅ 最大子问题数可配置（默认 5）

---

### 2. 动态时间上下文（方案 C：混合方案）
**状态**：待实施  
**描述**：在 SYSTEM 提示词里注入当前时间，并加参数规范化兜底层。  

**实施内容**：
- 修改 `app/sql/generator.py`：在 GENERATE_SYSTEM 里调用 `_get_time_context()`
- 新增 `app/sql/param_normalizer.py`：参数规范化函数（将"今年"→"2026%"等）
- 修改 `app/core/agent.py`：在执行前调用 `normalize_params(sql, params)`

**_get_time_context() 输出示例**：
```
【时间上下文】
- 当前日期：2026年09月23日
- 今年：2026 年 → WHERE month LIKE '2026%'
- 上月：2026-08 月 → WHERE month = '2026-08'
- 本月：2026-09 月 → WHERE month = '2026-09'
- 去年：2025 年 → WHERE month LIKE '2025%'
```

**验收标准**：
- ✅ "今年的营业收入"能正确转换为 `WHERE month LIKE '2026%'`
- ✅ "上月的绩效"能正确转换为 `WHERE month = '2026-08'`
- ✅ 即使 LLM 犯错，参数规范化层能兜底修正

---

## P1 - 中优先级（生产级增强）

### 3. 长期记忆（Redis）
**状态**：规划中  
**描述**：会话重启后保留用户偏好和历史对话摘要。  

**实施内容**：
- 新增 `app/memory/long_term.py`：长期记忆模块（Redis 存储）
- 存储内容：用户角色偏好、高频查询表、历史对话摘要
- 检索时机：每次对话开始时加载相关记忆到上下文

**验收标准**：
- ✅ 会话重启后仍能记住用户上次说的"我是销售部的"
- ✅ 能基于历史对话做个性化推荐

---

### 4. 语义缓存
**状态**：规划中  
**描述**：相似问题直接返回缓存结果，减少 LLM 调用和 DB 查询。  

**实施内容**：
- 新增 `app/cache/semantic_cache.py`：基于 embedding 相似度匹配
- 缓存键：问题 embedding + 用户角色 + 权限上下文
- 缓存策略：LRU + TTL（默认 1 小时）

**验收标准**：
- ✅ 相同问题第二次问时命中缓存，耗时 < 100ms
- ✅ 相似度阈值可配置（默认 0.95）

---

### 5. 流式输出（SSE）
**状态**：规划中  
**描述**：逐步返回回答，提升用户体验。  

**实施内容**：
- 修改 `app/main.py`：新增 `/api/query/stream` SSE 端点
- 修改 `app/core/agent.py`：新增 `answer_stream()` 方法
- 流式阶段：理解 → 检索 → 生成 → 执行 → 解释，每步完成后推送进度

**验收标准**：
- ✅ 前端能看到实时进度条
- ✅ 大查询（> 3s）有明显体验提升

---

## P2 - 低优先级（优化与扩展）

### 6. ReAct 自适应纠错（可选）
**状态**：**待验证后决定** ⚠️  
**描述**：在子问题层引入轻量 ReAct 循环，自动纠错失败的 SQL。  

**重要说明**：
> **必须先验证纯 Decompose-Merge 的效果，再决定是否加 ReAct。**  
> 如果纯方案准确率 > 90%，则不需要 ReAct；如果 < 85%，再考虑引入。

**实施内容（如果需要）**：
- 修改 `app/core/decomposer.py`：子问题执行失败时触发 ReAct 反思
- 限制最大迭代次数为 2（防止死循环）
- 记录每次反思的 Thought/Action/Observation 到日志

**验收标准**：
- ✅ 能在 2 次内修复常见 SQL 错误（如列名拼写错、表名不存在）
- ✅ 不会陷入死循环

---

### 7. 工具扩展机制
**状态**：规划中  
**描述**：支持除 DB 查询外的其他工具（计算器、搜索引擎、代码执行器）。  

**实施内容**：
- 新增 `app/tools/base.py`：工具基类
- 新增 `app/tools/calculator.py`：计算器工具
- 修改 Orchestrator：根据问题类型路由到不同工具

---

### 8. Skill 模板库
**状态**：规划中  
**描述**：预定义的复杂任务模板（如"生成月度报告"、"对比两个部门业绩"）。  

**实施内容**：
- 新增 `app/skills/` 目录
- 每个 skill 是一个 Python 类，包含 prompt 模板和执行逻辑
- 用户可通过 `/skill monthly_report` 调用

---

## 已完成项

### ✅ Phase 0: MVP 基线
- [x] 七层信任链（会话 → 理解 → 澄清 → 检索 → 生成 → 校验 → 执行）
- [x] BM25 Schema RAG + FK 图扩展
- [x] AST 白名单安全校验
- [x] 行列权限控制
- [x] 只读执行 + 修复循环
- [x] FastAPI 入口 + 交互式终端
- [x] 审计日志（JSONL 格式）
- [x] 32 个单元测试（全通过）

### ✅ 基础设施
- [x] `.env` 配置文件管理
- [x] 一键启动脚本（`scripts/run.py`）
- [x] 日志查看器（`scripts/view_logs.py`）
- [x] GitHub 仓库初始化并推送

---

## 数据方案

### 演示数据规模（固定，不动）
| 表 | 行数 | 说明 |
|----|------|------|
| employee | 20 | 覆盖 5 个部门，不同入职时间 |
| department | 5 | 含事业部层级 |
| performance | 60 | 20 人 × 3 个月 |
| attendance | 180 | 20 人 × 9 天 |
| project | 15 | 跨部门项目 |
| customer | 30 | 不同等级客户 |
| revenue | 45 | 5 部门 × 9 个月 |
| cost | 45 | 5 部门 × 9 个月 |
| business_unit | 3 | 新增，事业部表 |
| salary_history | 40 | 新增，薪资历史（支持时间序列） |
| promotion | 10 | 新增，晋升记录 |
| meeting | 50 | 新增，会议记录 |

**注意**：表间关系仅记录在 YAML 元数据中，不使用数据库级别 FOREIGN KEY 约束。

---

## 最后更新
2026-09-23
