from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from services.structured_data.live_query_plan import LiveQueryPlan, compile_live_query_plan
from services.structured_data.multi_source import MultiSourceContractError, SourceBinding
from services.structured_data.snapshot_hub import MySQLSourceReader, PostgresSourceReader


class QueryParameter(BaseModel):
    type: Literal["string", "integer", "number", "boolean"]
    required: bool = True
    pii: bool = False


class SourceQueryTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(pattern=r"^QT-[A-Z0-9-]{6,80}$")
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,63}$")
    plan: LiveQueryPlan
    parameters: dict[str, QueryParameter] = Field(default_factory=dict)
    timeout_ms: int = Field(default=5000, ge=100, le=30000)
    max_rows: int = Field(default=100, ge=1, le=1000)
    pii_scope: str = Field(min_length=1, max_length=256)

class RealtimeQueryReceipt(BaseModel):
    template_id: str
    source_id: str
    status: Literal["COMPLETE", "UNKNOWN", "INPUT_INCOMPLETE"]
    observed_at: datetime
    row_count: int = Field(ge=0)
    truncated: bool = False
    template_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    parameters_sha256: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    readonly_verified: bool
    timeout_ms: int
    max_rows: int
    reason: str | None = None


class SourceQueryResult(BaseModel):
    rows: list[dict[str, Any]]
    receipt: RealtimeQueryReceipt


def _sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _validate_parameter(name: str, value: Any, schema: QueryParameter) -> None:
    if value is None:
        if schema.required:
            raise MultiSourceContractError(f"ERROR_PARAMETERS: missing parameter {name}")
        return
    expected = schema.type
    valid = (
        (expected == "string" and isinstance(value, str))
        or (expected == "integer" and isinstance(value, int) and not isinstance(value, bool))
        or (expected == "number" and isinstance(value, int | float) and not isinstance(value, bool))
        or (expected == "boolean" and isinstance(value, bool))
    )
    if not valid:
        raise MultiSourceContractError(f"ERROR_PARAMETERS: parameter {name} must be {expected}")


class SourceQueryGateway:
    """Executes only registered, parameterized templates on verified read-only sources."""

    def __init__(
        self,
        *,
        bindings: list[SourceBinding],
        templates: list[SourceQueryTemplate],
        circuit_breaker_threshold: int = 3,
        circuit_breaker_cooldown_seconds: float = 30.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # Registration is an immutable execution contract. A caller editing a
        # model after registration must not change validation while SQL stays cached.
        self.bindings = {item.source_id: item.model_copy(deep=True) for item in bindings}
        self.templates = {item.template_id: item.model_copy(deep=True) for item in templates}
        if circuit_breaker_threshold < 1 or circuit_breaker_cooldown_seconds <= 0:
            raise ValueError("circuit breaker threshold/cooldown must be positive")
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_cooldown_seconds = circuit_breaker_cooldown_seconds
        self.clock = clock or time.monotonic
        self._failures: dict[str, int] = {}
        self._open_until: dict[str, float] = {}
        self._compiled: dict[str, str] = {}
        self._template_hashes: dict[str, str] = {}
        if len(self.bindings) != len(bindings) or len(self.templates) != len(templates):
            raise MultiSourceContractError(
                "G-S6-REALTIME-READONLY: duplicate source or query template"
            )
        for template in self.templates.values():
            if template.source_id not in self.bindings:
                raise MultiSourceContractError(
                    "SOURCE_NOT_BOUND: query template source is not registered"
                )
            binding = self.bindings[template.source_id]
            if template.pii_scope != binding.pii_scope:
                raise MultiSourceContractError("G-S1-SOURCE-SCOPE: live query PII scope differs")
            self._compiled[template.template_id] = compile_live_query_plan(
                template.plan, binding, parameters=set(template.parameters), max_rows=template.max_rows
            )
            self._template_hashes[template.template_id] = _sha256({
                "template": template.model_dump(mode="json"),
                "source_scope": binding.model_dump(
                    include={
                        "project_id", "source_id", "engine", "connection_ref", "database",
                        "schemas", "authorized_tables", "authorized_columns",
                        "access_mode", "readonly_attested", "pii_scope",
                    }
                ),
            })

    def template_sha256(self, template_id: str) -> str:
        """Return the immutable reviewed identity, including the source connection reference."""
        try:
            return self._template_hashes[template_id]
        except KeyError as exc:
            raise MultiSourceContractError("ERROR_PARAMETERS: query template is not allowed") from exc

    def execute(self, template_id: str, parameters: dict[str, Any]) -> SourceQueryResult:
        template = self.templates.get(template_id)
        if template is None:
            raise MultiSourceContractError("ERROR_PARAMETERS: query template is not allowed")
        if set(parameters) != set(template.parameters):
            raise MultiSourceContractError(
                "ERROR_PARAMETERS: provided parameters do not match the template schema"
            )
        for name, schema in template.parameters.items():
            _validate_parameter(name, parameters.get(name), schema)
        if any(value is None for value in parameters.values()):
            return self._unknown(
                template, parameters, "INPUT_INCOMPLETE: live query parameter is unknown",
                status="INPUT_INCOMPLETE",
            )
        binding = self.bindings[template.source_id]
        if binding.status != "ACTIVE":
            return self._unknown(template, parameters, "source is not ACTIVE")
        now = self.clock()
        if self._open_until.get(template.source_id, 0) > now:
            return self._unknown(
                template,
                parameters,
                "CIRCUIT_OPEN: source temporarily suppressed after repeated failures",
            )
        reader = PostgresSourceReader() if binding.engine == "POSTGRESQL" else MySQLSourceReader()
        try:
            with reader._connection(binding) as connection, connection.cursor() as cursor:
                if binding.engine == "POSTGRESQL":
                    cursor.execute(f"SET LOCAL statement_timeout = {template.timeout_ms}")
                else:
                    cursor.execute(f"SET SESSION MAX_EXECUTION_TIME = {template.timeout_ms}")
                cursor.execute(self._compiled[template_id], parameters)
                names = [str(item[0]) for item in cursor.description or []]
                raw_rows = cursor.fetchmany(template.max_rows + 1)
                truncated = len(raw_rows) > template.max_rows
                raw_rows = raw_rows[: template.max_rows]
                rows = [
                    {name: value for name, value in zip(names, row, strict=True)}
                    for row in raw_rows
                ]
        except MultiSourceContractError:
            raise
        except Exception as exc:
            failures = self._failures.get(template.source_id, 0) + 1
            self._failures[template.source_id] = failures
            if failures >= self.circuit_breaker_threshold:
                self._open_until[template.source_id] = now + self.circuit_breaker_cooldown_seconds
            return self._unknown(template, parameters, f"{type(exc).__name__}: {exc}")
        self._failures.pop(template.source_id, None)
        self._open_until.pop(template.source_id, None)
        return SourceQueryResult(
            rows=rows,
            receipt=RealtimeQueryReceipt(
                template_id=template.template_id,
                source_id=template.source_id,
                status="COMPLETE",
                observed_at=datetime.now(UTC),
                row_count=len(rows),
                truncated=truncated,
                template_sha256=self._template_hashes[template_id],
                parameters_sha256=_sha256(parameters),
                readonly_verified=True,
                timeout_ms=template.timeout_ms,
                max_rows=template.max_rows,
            ),
        )

    def _unknown(
        self, template: SourceQueryTemplate, parameters: dict[str, Any], reason: str,
        *, status: Literal["UNKNOWN", "INPUT_INCOMPLETE"] = "UNKNOWN",
    ) -> SourceQueryResult:
        return SourceQueryResult(
            rows=[],
            receipt=RealtimeQueryReceipt(
                template_id=template.template_id,
                source_id=template.source_id,
                status=status,
                observed_at=datetime.now(UTC),
                row_count=0,
                template_sha256=self._template_hashes[template.template_id],
                parameters_sha256=_sha256(parameters),
                readonly_verified=False,
                timeout_ms=template.timeout_ms,
                max_rows=template.max_rows,
                reason=reason,
            ),
        )
