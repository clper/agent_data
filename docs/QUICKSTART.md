# MySQL Agent v2 - 快速开始指南

企业级 MySQL 数据问答 Agent，采用七层信任链架构，支持自然语言查询、多表 JOIN、权限控制、审计日志等功能。

---

## 📋 前置要求

- Python 3.10+
- MySQL 8.0+（本地或远程）
- DeepSeek API Key（或其他 OpenAI 兼容接口）

---

## 🚀 一键启动（推荐）

```powershell
cd d:\Projects\ai_agent\agent_data_v1.0.0\mysql_agent_v2
python scripts\run.py
```

**效果**：后台启动 FastAPI 服务 + 进入交互式问答终端，一个窗口搞定。

---

## 🔧 分步启动（调试用）

### 1. 安装依赖

```powershell
pip install -e .
```

### 2. 配置环境变量

编辑 `.env` 文件：

```ini
LLM_API_KEY=sk-your-api-key
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat
DB_URL=mysql+pymysql://root:123456@localhost:3306/company_ops
SCHEMA_PATH=schema/company_schema.yaml
```

### 3. 初始化数据库

```powershell
mysql --default-character-set=utf8mb4 -u root -p < scripts\init_demo.sql
```

### 4. 构建 Schema 索引

```powershell
python scripts\build_index.py
```

### 5. 启动服务

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

### 6. 交互式问答

```powershell
python scripts\chat.py
```

---

## 🧪 运行测试

```powershell
# 全部测试
python -m pytest tests/ -v

# 指定模块测试
python -m pytest tests/test_validator.py -v

# 带覆盖率报告
python -m pytest tests/ --cov=app --cov-report=html
```

---

## 📊 查看审计日志

```powershell
# 摘要模式
python scripts\view_logs.py

# 详情模式（显示完整 SQL）
python scripts\view_logs.py --detail

# JSON 原始输出
python scripts\view_logs.py --json

# 清空日志
Clear-Content logs\audit.jsonl
```

---

## 🌐 API 端点

服务启动后访问：

- **健康检查**：http://127.0.0.1:8000/health
- **Swagger 文档**：http://127.0.0.1:8000/docs
- **ReDoc 文档**：http://127.0.0.1:8000/redoc

**示例请求**：

```bash
curl -X POST "http://127.0.0.1:8000/ask" \
  -H "Content-Type: application/json" \
  -d '{"question": "张三的入职日期是什么时候？", "user_id": "101", "role": "employee"}'
```

---

## 📁 项目结构

```
mysql_agent_v2/
├── app/
│   ├── core/              # 核心编排（agent.py, decomposer.py, merger.py）
│   ├── sql/               # SQL 生成与执行（generator, validator, executor）
│   ├── schema_rag/        # Schema 检索（metadata, indexer, retriever）
│   ├── security/          # 安全层（permissions, audit）
│   ├── models/            # 数据契约（plan.py, state.py）
│   └── main.py            # FastAPI 入口
├── schema/                # 元数据 YAML
├── scripts/               # 工具脚本
│   ├── run.py             # 一键启动
│   ├── chat.py            # 交互式问答
│   ├── view_logs.py       # 日志查看器
│   ├── build_index.py     # Schema 索引构建
│   └── init_demo.sql      # 数据库初始化
├── tests/                 # 单元测试
├── logs/                  # 审计日志（JSONL）
├── data/                  # 运行时数据（索引缓存等）
├── .env                   # 配置文件
├── pyproject.toml         # 依赖管理
└── TODO.md                # 开发路线图
```

---

## 🎯 功能特性

### ✅ 已实现（MVP + Phase 1-2）

- **七层信任链**：会话→理解→澄清→检索→生成→校验→执行
- **结构化 QueryPlan**：LLM 生成 JSON 计划 → Python 编译为 SQL
- **BM25 Schema RAG**：jieba 分词 + FK 图扩展
- **AST 白名单校验**：sqlglot 解析，禁止 DDL/DML/危险函数
- **行列权限控制**：列级隐藏敏感列 + 行级 WHERE 谓词注入
- **修复循环**：执行失败后模型自动修复（最多 N 轮）
- **审计日志**：记录每次问答的详细信息（问题、SQL、结果、耗时）
- **动态时间上下文**：自动识别"今年/上月/本周"等相对时间

### 🚧 开发中（Phase 3）

- **问题分解（Decompose-Merge）**：复杂问题拆分为有序子问题列表
- **执行上下文**：中间结果存储，供后续子问题引用
- **结果综合器**：汇总所有子问题结果，生成最终回答

### 📅 规划中（Phase 4-5）

- **评估体系**：Golden Dataset + 指标计算 + 运行器
- **记忆系统**：跨会话记忆用户偏好、常用查询模式
- **ReAct 模式**：可选的自主推理模式（需验证后启用）

---

## 🔍 常见问题

### Q1: 如何修改数据库连接？

编辑 `.env` 文件的 `DB_URL` 字段：

```ini
DB_URL=mysql+pymysql://用户名:密码@主机:端口/数据库名
```

### Q2: 如何添加新表？

1. 编辑 `schema/company_schema.yaml`，添加表定义
2. 运行 `python scripts\build_index.py` 重建索引
3. 重启服务

### Q3: 如何查看 LLM 生成的原始 SQL？

使用日志查看器的详情模式：

```powershell
python scripts\view_logs.py --detail
```

### Q4: 如何切换 LLM 提供商？

编辑 `.env`：

```ini
# DeepSeek
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_MODEL=deepseek-chat

# OpenAI
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4

# 本地模型（Ollama）
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=llama3.1
```

### Q5: 如何禁用权限控制（仅测试用）？

⚠️ **警告**：生产环境严禁禁用！

编辑 `app/config.py`，设置：

```python
SKIP_PERMISSIONS = True
```

---

## 📝 更新日志

### v2.0.0 (2026-09-23)
- ✅ MVP 完成：七层信任链完整实现
- ✅ 32 个单元测试全部通过
- ✅ 实际运行验证通过（5 个场景）
- ✅ .env 配置管理 + 一键启动脚本
- ✅ 审计日志 + 日志查看器

---

## 🤝 贡献指南

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/my-feature`
3. 提交更改：`git commit -am 'Add my feature'`
4. 推送分支：`git push origin feature/my-feature`
5. 提交 Pull Request

---

## 📄 许可证

MIT License

---

## 📧 联系方式

- GitHub Issues: https://github.com/clper/agent_data/issues
- Email: clper-agent-data@github.com
