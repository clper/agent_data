"""
Schema 元数据加载：从 YAML 读取表/列信息，构建结构化表示。

设计要点：
- 表名/列名统一小写，避免大小写不一致导致的安全绕过
- sensitive 标记的列会被权限层自动隐藏或脱敏
- FK 关系用于检索时的图扩展（找到关联表）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class Column:
    """列的元数据"""
    name: str                          # 列名（小写）
    type: str                          # SQL 类型
    cn: str = ""                       # 中文名
    pk: bool = False                   # 是否主键
    fk: str = ""                       # 外键引用，如 "department.dept_id"
    sensitive: bool = False            # 是否敏感列
    comment: str = ""                  # 注释
    enum_values: list[str] = field(default_factory=list)  # 枚举值


@dataclass
class Table:
    """表的元数据"""
    name: str                          # 表名（小写）
    cn_name: str = ""                  # 中文名
    comment: str = ""                  # 注释
    columns: list[Column] = field(default_factory=list)

    @property
    def pk_columns(self) -> list[Column]:
        return [c for c in self.columns if c.pk]

    @property
    def fk_map(self) -> dict[str, str]:
        """返回 {列名: 引用表.引用列} 映射"""
        return {c.name: c.fk for c in self.columns if c.fk}

    @property
    def visible_columns(self) -> list[str]:
        """返回非敏感列名列表（给模型的列白名单）"""
        return [c.name for c in self.columns if not c.sensitive]

    @property
    def all_column_names(self) -> list[str]:
        return [c.name for c in self.columns]

    def to_doc(self) -> str:
        """
        生成表的文档字符串（用于 BM25 索引和 LLM 上下文）。
        包含表名、中文名、列信息。
        """
        parts = [f"表 {self.name}（{self.cn_name}）: {self.comment}"]
        for c in self.columns:
            sens = " [敏感]" if c.sensitive else ""
            enum_str = f" 枚举:{c.enum_values}" if c.enum_values else ""
            parts.append(f"  - {c.name} ({c.type}): {c.cn}{sens}{enum_str}")
        return "\n".join(parts)


@dataclass
class SchemaMetadata:
    """完整的 Schema 元数据"""
    database: str
    tables: dict[str, Table]           # {表名: Table}
    metrics: dict[str, str] = field(default_factory=dict)  # 预定义指标

    def get_table(self, name: str) -> Table | None:
        return self.tables.get(name.lower())

    def get_all_table_names(self) -> list[str]:
        return list(self.tables.keys())

    def get_fk_neighbors(self, table_name: str) -> list[str]:
        """获取指定表的所有 FK 邻居（直接关联的表）"""
        table = self.get_table(table_name)
        if not table:
            return []
        neighbors = []
        for col in table.columns:
            if col.fk:
                ref_table = col.fk.split(".")[0]
                neighbors.append(ref_table)
        # 反向查找：其他表引用了当前表
        for t in self.tables.values():
            if t.name == table_name:
                continue
            for col in t.columns:
                if col.fk and col.fk.split(".")[0] == table_name:
                    neighbors.append(t.name)
        return list(set(neighbors))


def load_schema(path: str | Path) -> SchemaMetadata:
    """
    从 YAML 文件加载 Schema 元数据。

    为什么用 YAML 而不是 information_schema 自动抽取？
    → YAML 允许 DBA 补充中文注释、枚举值、敏感标记等"业务语义"
    → 自动抽取只能拿到类型信息，缺乏业务上下文
    → 生产环境可以两者结合：自动抽取 + YAML 覆盖
    """
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f)

    tables: dict[str, Table] = {}
    for t_raw in raw.get("tables", []):
        cols: list[Column] = []
        for c_raw in t_raw.get("columns", []):
            cols.append(Column(
                name=c_raw["name"].lower(),
                type=c_raw.get("type", ""),
                cn=c_raw.get("cn", ""),
                pk=c_raw.get("pk", False),
                fk=c_raw.get("fk", ""),
                sensitive=c_raw.get("sensitive", False),
                comment=c_raw.get("comment", ""),
                enum_values=c_raw.get("enum", []),
            ))
        table = Table(
            name=t_raw["name"].lower(),
            cn_name=t_raw.get("cn_name", ""),
            comment=t_raw.get("comment", ""),
            columns=cols,
        )
        tables[table.name] = table

    return SchemaMetadata(
        database=raw.get("database", ""),
        tables=tables,
        metrics=raw.get("metrics", {}),
    )
