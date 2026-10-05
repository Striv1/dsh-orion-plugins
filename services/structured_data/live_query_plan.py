"""Compile bounded live-source queries from an allowlisted relational plan.

No caller-supplied SQL reaches the source connection. Every table and column
must be declared in the SourceBinding before a query can be registered.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .multi_source import MultiSourceContractError, SourceBinding
from .snapshot_hub import _split_table

ALIAS = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
FIELD = re.compile(r"^[a-z][a-z0-9_]{0,47}\.[A-Za-z_][A-Za-z0-9_$-]{0,127}$")
COMPARE = {"eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">="}


class LiveQueryJoin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: str = Field(pattern=FIELD.pattern)
    right: str = Field(pattern=FIELD.pattern)


class LiveQueryFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(pattern=FIELD.pattern)
    op: Literal["eq", "ne", "lt", "lte", "gt", "gte", "is_null", "is_not_null"]
    parameter: str | None = Field(default=None, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @model_validator(mode="after")
    def validate_parameter(self) -> LiveQueryFilter:
        if (self.op in COMPARE) != (self.parameter is not None):
            raise ValueError("comparison requires a parameter; null checks do not")
        return self


class LiveQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: dict[str, str] = Field(min_length=1, max_length=4)
    from_alias: str = Field(pattern=ALIAS.pattern)
    joins: list[LiveQueryJoin] = Field(default_factory=list, max_length=3)
    select: dict[str, str] = Field(min_length=1, max_length=12)
    filters: list[LiveQueryFilter] = Field(default_factory=list, max_length=12)
    order_by: list[str] = Field(default_factory=list, max_length=12)


def compile_live_query_plan(
    plan: LiveQueryPlan, binding: SourceBinding, *, parameters: set[str], max_rows: int,
) -> str:
    """Return generated parameterized SQL or reject the plan before connecting."""

    if not set(plan.tables.values()).issubset(binding.authorized_tables):
        raise MultiSourceContractError("G-S1-SOURCE-SCOPE: live query references unauthorized table")
    if not all(ALIAS.fullmatch(alias) for alias in plan.tables) or plan.from_alias not in plan.tables:
        raise MultiSourceContractError("G-S1-SOURCE-SCOPE: invalid live query table alias")
    if not all(ALIAS.fullmatch(name) for name in plan.select):
        raise MultiSourceContractError("G-S1-SOURCE-SCOPE: invalid live query output name")
    if not set(plan.order_by).issubset(plan.select):
        raise MultiSourceContractError("G-S1-SOURCE-SCOPE: order_by must use selected outputs")
    used_parameters = {item.parameter for item in plan.filters if item.parameter is not None}
    if used_parameters != parameters:
        raise MultiSourceContractError("ERROR_PARAMETERS: plan filters and parameter schema differ")

    quote = '"' if binding.engine == "POSTGRESQL" else "`"

    def identifier(name: str) -> str:
        # Bound identifiers are validated by SourceBinding, ALIAS or FIELD.
        return f"{quote}{name}{quote}"

    def table(alias: str) -> str:
        name = plan.tables[alias]
        if not binding.authorized_columns.get(name):
            raise MultiSourceContractError(
                "G-S1-SOURCE-SCOPE: live query requires explicit authorized columns"
            )
        schema, table_name = _split_table(binding, name)
        return f"{identifier(schema)}.{identifier(table_name)} AS {identifier(alias)}"

    def field(reference: str) -> str:
        if not FIELD.fullmatch(reference):
            raise MultiSourceContractError("G-S1-SOURCE-SCOPE: invalid live query field")
        alias, column = reference.split(".", 1)
        if alias not in plan.tables or column not in binding.authorized_columns.get(plan.tables[alias], ()):
            raise MultiSourceContractError("G-S1-SOURCE-SCOPE: live query references unauthorized column")
        return f"{identifier(alias)}.{identifier(column)}"

    base_table = table(plan.from_alias)
    selected = [f"{field(ref)} AS {identifier(name)}" for name, ref in plan.select.items()]
    statement = "SELECT " + ", ".join(selected) + " FROM " + base_table
    joined = {plan.from_alias}
    for join in plan.joins:
        left_alias, right_alias = join.left.split(".", 1)[0], join.right.split(".", 1)[0]
        if (left_alias in joined) == (right_alias in joined):
            raise MultiSourceContractError("G-S1-SOURCE-SCOPE: joins must connect one new table")
        new_alias = right_alias if left_alias in joined else left_alias
        if new_alias not in plan.tables:
            raise MultiSourceContractError("G-S1-SOURCE-SCOPE: join references unknown table")
        statement += f" JOIN {table(new_alias)} ON {field(join.left)} = {field(join.right)}"
        joined.add(new_alias)
    if joined != set(plan.tables):
        raise MultiSourceContractError("G-S1-SOURCE-SCOPE: unjoined live query table")

    conditions = []
    for item in plan.filters:
        left = field(item.field)
        if item.parameter is None:
            conditions.append(f"{left} IS {'NOT ' if item.op == 'is_not_null' else ''}NULL")
        else:
            conditions.append(f"{left} {COMPARE[item.op]} %({item.parameter})s")
    if conditions:
        statement += " WHERE " + " AND ".join(conditions)
    order = plan.order_by or list(plan.select)
    statement += " ORDER BY " + ", ".join(identifier(name) for name in order)
    # The extra row proves truncation without reading an unbounded result set.
    return statement + f" LIMIT {max_rows + 1}"
