"""Bounded analysis plans over a single immutable, complete result set."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.realtime_qa.checked_query_api import QueryIdentity


class AnalysisSource(QueryIdentity):
    receipt_id: str = Field(pattern=r"^EVD-[a-f0-9]{32}$")
    receipt_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    source_query_id: str = Field(min_length=1, max_length=128)
    capability_name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")
    source_question_id: str | None = Field(default=None, min_length=1, max_length=160)


class Dimension(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=1, max_length=128)
    period: Literal["value", "day", "month", "year"] = "value"


class Metric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^m_[a-z][a-z0-9_]{0,39}$")
    operation: Literal["count_rows", "count_non_null", "count_distinct", "sum", "avg", "min", "max"]
    field: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def field_contract(self):
        if (self.operation == "count_rows") != (self.field is None):
            raise ValueError("count_rows 不指定字段；其他指标必须指定字段。")
        return self


class AnalysisFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str = Field(min_length=1, max_length=128)
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "is_null", "not_null"]
    value: str | int | float | bool | None = None

    @model_validator(mode="after")
    def null_contract(self):
        if (self.operator in {"is_null", "not_null"}) != (self.value is None):
            raise ValueError("空值使用 is_null/not_null 且不提供 value；其他过滤必须提供 value。")
        return self


class AnalysisRequest(AnalysisSource):
    question: str = Field(min_length=2, max_length=1000)
    dimensions: list[Dimension] = Field(default_factory=list, max_length=2)
    metrics: list[Metric] = Field(min_length=1, max_length=4)
    filters: list[AnalysisFilter] = Field(default_factory=list, max_length=8)
    limit: int = Field(default=50, ge=1, le=100, strict=True)

    @model_validator(mode="after")
    def unique_metrics(self):
        if len({metric.name for metric in self.metrics}) != len(self.metrics):
            raise ValueError("指标名称不能重复。")
        if len({item.field for item in self.dimensions}) != len(self.dimensions):
            raise ValueError("分组字段不能重复。")
        return self
