"""Public analysis contracts. Connections and physical SQL are server owned."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.realtime_qa.checked_query_api import QueryIdentity


class AnalyticsCatalogRequest(QueryIdentity):
    search: str = Field(default="", max_length=200)
    data_mode: Literal["AUTO", "LIVE", "SNAPSHOT"] = "AUTO"


class ChartSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["table", "bar", "line", "kpi"] = "table"
    x: str | None = Field(default=None, max_length=128)
    y: list[str] = Field(default_factory=list, max_length=4)


class AnalyticsQueryRequest(QueryIdentity):
    question: str = Field(min_length=2, max_length=1000)
    data_mode: Literal["AUTO", "LIVE", "SNAPSHOT"] = "AUTO"
    sql: str | None = Field(default=None, min_length=1, max_length=16000)
    cube_query: dict[str, Any] | None = None
    parameters: dict[str, Any] = Field(default_factory=dict, max_length=30)
    limit: int = Field(default=100, ge=1, le=500, strict=True)
    chart: ChartSpec = Field(default_factory=ChartSpec)

    @model_validator(mode="after")
    def query_choice(self):
        if (self.sql is None) == (self.cube_query is None):
            raise ValueError("sql 与 cube_query 必须且只能提供一个。先读取本发布版本的分析目录。")
        if self.cube_query is not None and self.parameters:
            raise ValueError("parameters 仅用于 SQL 的命名参数。")
        return self


class AnalyticsReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_id: str = Field(pattern=r"^EVD-[a-f0-9]{32}$")
    sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    query_id: str = Field(min_length=1, max_length=128)


class AnalyticsAssetRequest(QueryIdentity):
    action: Literal["list", "search", "get", "remember", "report", "knowledge", "evaluation", "evaluate"]
    asset_id: str | None = Field(default=None, max_length=100)
    question: str = Field(default="", max_length=1000)
    title: str = Field(default="", max_length=200)
    definition: str = Field(default="", max_length=8000)
    receipts: list[AnalyticsReceipt] = Field(default_factory=list, max_length=50)
    confirmed: bool = Field(default=False, strict=True)
    layout: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def action_contract(self):
        if self.action in {"get", "evaluate"} and not self.asset_id:
            raise ValueError("此操作需要 asset_id。")
        if self.action in {"remember", "report", "knowledge", "evaluation"} and not self.receipts:
            raise ValueError("保存分析资产须关联真实执行回执。")
        if self.action in {"remember", "knowledge", "evaluation"} and not self.confirmed:
            raise ValueError("保存查询经验、口径说明或回归基线须由用户明确确认。")
        if self.action in {"remember", "evaluation"} and len(self.receipts) != 1:
            raise ValueError("查询经验和回归基线须恰好关联一个完整分析回执。")
        return self


class AnalyticsExportRequest(QueryIdentity):
    action: Literal["create", "status", "cancel", "download"]
    job_id: str | None = Field(default=None, pattern=r"^EXP-[a-f0-9]{32}$")
    receipt: AnalyticsReceipt | None = None
    max_rows: int = Field(default=100000, ge=1, le=1000000, strict=True)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "create" and self.receipt is None:
            raise ValueError("导出需要已经执行的查询回执；导出重新查询当前数据并保留独立时间范围。")
        if self.action != "create" and self.job_id is None:
            raise ValueError("需要导出任务标识。")
        return self
