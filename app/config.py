"""
全局配置：从 .env 文件 + 环境变量读取，不硬编码任何密钥。

设计原则：
- 优先从项目根目录的 .env 文件加载（方便本地开发）
- 也支持系统环境变量覆盖（生产环境用环境变量）
- 配置项按功能分组，一目了然
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# 自动加载项目根目录的 .env 文件
_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")


@dataclass
class LLMConfig:
    """LLM 调用配置（OpenAI 兼容接口）"""
    base_url: str = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
    api_key: str = os.getenv("LLM_API_KEY", "")
    model: str = os.getenv("LLM_MODEL", "deepseek-chat")
    temperature: float = float(os.getenv("LLM_TEMPERATURE", "0.1"))
    timeout: int = int(os.getenv("LLM_TIMEOUT", "30"))
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "2048"))


@dataclass
class DBConfig:
    """数据库连接配置"""
    url: str = os.getenv(
        "DB_URL",
        "mysql+pymysql://readonly_user:readonly_pass@localhost:3306/company",
    )
    pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    query_timeout: int = int(os.getenv("DB_QUERY_TIMEOUT", "10"))  # 秒
    max_rows: int = int(os.getenv("DB_MAX_ROWS", "200"))  # 结果截断行数


@dataclass
class SecurityConfig:
    """安全策略配置"""
    audit_log_path: str = os.getenv("AUDIT_LOG_PATH", "logs/audit.jsonl")
    max_repair_rounds: int = int(os.getenv("MAX_REPAIR_ROUNDS", "2"))
    enable_row_level: bool = os.getenv("ENABLE_ROW_LEVEL", "1") == "1"
    enable_column_level: bool = os.getenv("ENABLE_COLUMN_LEVEL", "1") == "1"


@dataclass
class SchemaConfig:
    """Schema RAG 配置"""
    schema_path: str = os.getenv("SCHEMA_PATH", "schema/company_schema.yaml")
    index_path: str = os.getenv("INDEX_PATH", "data/schema_index.pkl")
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    default_top_k: int = 6
    max_top_k: int = 12
    # 向量检索（Phase 2 启用）
    vector_enabled: bool = os.getenv("VECTOR_ENABLED", "0") == "1"
    vector_weight: float = 0.35  # RRF 中向量权重


@dataclass
class Settings:
    """聚合所有配置"""
    llm: LLMConfig = field(default_factory=LLMConfig)
    db: DBConfig = field(default_factory=DBConfig)
    security: SecurityConfig = field(default_factory=SecurityConfig)
    schema: SchemaConfig = field(default_factory=SchemaConfig)

    def validate(self) -> None:
        """启动前校验必填项"""
        if not self.llm.api_key:
            raise ValueError("LLM_API_KEY 环境变量未设置")


# 全局单例
settings = Settings()
