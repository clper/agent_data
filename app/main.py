"""
FastAPI 入口：提供 HTTP API。

端点：
- POST /api/query    主查询接口
- POST /api/sessions 创建会话
- GET  /api/health   健康检查
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from app.config import Settings, settings
from app.core.agent import DataAgent
from app.models.plan import AgentResponse
from app.models.state import UserContext

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

# 全局 Agent 实例
agent: DataAgent | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global agent
    settings.validate()
    agent = DataAgent(settings)
    logger.info("Agent started")
    yield
    if agent:
        agent.executor.close()
    logger.info("Agent stopped")


app = FastAPI(
    title="MySQL Agent v2",
    description="企业级 MySQL 数据问答 Agent",
    version="0.1.0",
    lifespan=lifespan,
)


# ═══════════════════════════════════════════
# 请求/响应模型
# ═══════════════════════════════════════════

class QueryRequest(BaseModel):
    """查询请求"""
    question: str = Field(..., description="用户问题", min_length=1, max_length=1000)
    session_id: str = Field(..., description="会话 ID")
    user_id: str = Field(..., description="用户 ID")
    role: str = Field(default="exec", description="角色: exec/employee/dept_lead/bu_head")
    department_id: int | None = Field(default=None, description="部门 ID")
    business_unit_id: int | None = Field(default=None, description="事业部 ID")


class QueryResponse(BaseModel):
    """查询响应"""
    answer: str
    sql: str = ""
    tables_used: list[str] = []
    row_count: int = 0
    execution_time_ms: float = 0.0
    error: str | None = None
    needs_clarification: bool = False
    clarification_question: str = ""


class SessionRequest(BaseModel):
    """创建会话请求"""
    session_id: str
    user_id: str
    role: str = "exec"
    department_id: int | None = None
    business_unit_id: int | None = None


class HealthResponse(BaseModel):
    """健康检查响应"""
    status: str
    tables_loaded: int
    metrics_loaded: int


# ═══════════════════════════════════════════
# 路由
# ═══════════════════════════════════════════

@app.post("/api/query", response_model=QueryResponse)
async def query(req: QueryRequest) -> QueryResponse:
    """主查询接口"""
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    user = UserContext(
        user_id=req.user_id,
        role=req.role,
        department_id=req.department_id,
        business_unit_id=req.business_unit_id,
    )

    result = agent.answer(req.question, req.session_id, user)
    return QueryResponse(
        answer=result.answer,
        sql=result.sql,
        tables_used=result.tables_used,
        row_count=result.row_count,
        execution_time_ms=result.execution_time_ms,
        error=result.error,
        needs_clarification=result.needs_clarification,
        clarification_question=result.clarification_question,
    )


@app.post("/api/sessions")
async def create_session(req: SessionRequest) -> dict[str, str]:
    """创建会话"""
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    user = UserContext(
        user_id=req.user_id,
        role=req.role,
        department_id=req.department_id,
        business_unit_id=req.business_unit_id,
    )
    agent.sessions.get_or_create(req.session_id, user)
    return {"status": "ok", "session_id": req.session_id}


@app.get("/api/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """健康检查"""
    if agent is None:
        return HealthResponse(status="starting", tables_loaded=0, metrics_loaded=0)
    return HealthResponse(
        status="ok",
        tables_loaded=len(agent.metadata.tables),
        metrics_loaded=len(agent.metadata.metrics),
    )


# ═══════════════════════════════════════════
# 启动
# ═══════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
