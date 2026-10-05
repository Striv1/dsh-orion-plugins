from __future__ import annotations

import sqlite3
from contextlib import contextmanager, nullcontext

import pytest

from services.structured_data.live_query_plan import LiveQueryPlan, compile_live_query_plan
from services.structured_data.multi_source import MultiSourceContractError, SourceBinding
from services.structured_data.source_query import (
    QueryParameter,
    SourceQueryGateway,
    SourceQueryTemplate,
)


def _binding(status: str = "ACTIVE") -> SourceBinding:
    return SourceBinding(
        project_id="ontology-project-test",
        source_id="application_mysql",
        engine="MYSQL",
        connection_ref="env://MYSQL_SOURCE_URL",
        database="applications",
        schemas=["applications"],
        authorized_tables=["applications"],
        authorized_columns={"applications": ["applicant_id", "status"]},
        readonly_attested=True,
        pii_scope="STABLE_HASHED_IDENTIFIER_ONLY",
        owner="platform-test",
        status=status,
    )


def _template() -> SourceQueryTemplate:
    return SourceQueryTemplate(
        template_id="QT-APPLICATION-BY-ID",
        source_id="application_mysql",
        plan={
            "tables": {"a": "applications"},
            "from_alias": "a",
            "select": {"applicant_id": "a.applicant_id", "status": "a.status"},
            "filters": [{"field": "a.applicant_id", "op": "eq", "parameter": "applicant_id"}],
        },
        parameters={"applicant_id": QueryParameter(type="string", pii=True)},
        timeout_ms=1000,
        max_rows=10,
        pii_scope="STABLE_HASHED_IDENTIFIER_ONLY",
    )


def test_template_rejects_raw_sql_and_generates_bounded_query() -> None:
    with pytest.raises(ValueError, match="sql"):
        SourceQueryTemplate.model_validate({**_template().model_dump(), "sql": "SELECT * FROM secret"})
    statement = compile_live_query_plan(
        _template().plan, _binding(), parameters={"applicant_id"}, max_rows=10
    )
    assert statement == (
        "SELECT `a`.`applicant_id` AS `applicant_id`, `a`.`status` AS `status` "
        "FROM `applications`.`applications` AS `a` "
        "WHERE `a`.`applicant_id` = %(applicant_id)s "
        "ORDER BY `applicant_id`, `status` LIMIT 11"
    )


@pytest.mark.parametrize("change", [
    {"tables": {"a": "secret"}},
    {"select": {"private": "a.private"}},
    {"select": {"private": "a.*"}},
    {"select": {"private": "a.applicant_id; DROP TABLE secret"}},
    {"tables": {"a": "applications", "b": "applications"}},
])
def test_scope_is_enforced_before_source_connection(change: dict) -> None:
    plan = _template().plan.model_dump()
    plan.update(change)
    with pytest.raises((MultiSourceContractError, ValueError)):
        SourceQueryGateway(bindings=[_binding()], templates=[_template().model_copy(
            update={"plan": LiveQueryPlan.model_validate(plan)}
        )])


def test_join_and_filter_only_use_bound_columns() -> None:
    binding = _binding().model_copy(update={
        "authorized_tables": ["applications", "reviews"],
        "authorized_columns": {
            "applications": ["applicant_id", "status"],
            "reviews": ["applicant_id", "score"],
        },
    })
    plan = LiveQueryPlan.model_validate({
        "tables": {"a": "applications", "r": "reviews"}, "from_alias": "a",
        "joins": [{"left": "a.applicant_id", "right": "r.applicant_id"}],
        "select": {"id": "a.applicant_id", "score": "r.score"},
        "filters": [{"field": "r.score", "op": "gte", "parameter": "min_score"}],
    })
    statement = compile_live_query_plan(plan, binding, parameters={"min_score"}, max_rows=5)
    assert "JOIN `applications`.`reviews` AS `r` ON `a`.`applicant_id` = `r`.`applicant_id`" in statement
    assert "WHERE `r`.`score` >= %(min_score)s" in statement
    assert statement.endswith("LIMIT 6")

    with sqlite3.connect(":memory:") as database:
        database.execute("ATTACH DATABASE ':memory:' AS applications")
        database.execute("CREATE TABLE applications.applications (applicant_id TEXT, status TEXT)")
        database.execute("CREATE TABLE applications.reviews (applicant_id TEXT, score REAL)")
        database.executemany("INSERT INTO applications.applications VALUES (?, ?)", [
            ("A1", "OPEN"), ("A2", "OPEN"), ("A3", "OPEN")
        ])
        database.executemany("INSERT INTO applications.reviews VALUES (?, ?)", [
            ("A1", 4.5), ("A2", 3.0), ("A3", None)
        ])
        assert database.execute(statement.replace("%(min_score)s", ":min_score"),
                                {"min_score": 4.5}).fetchall() == [("A1", 4.5)]


def test_live_query_requires_explicit_columns_and_copies_registration() -> None:
    binding = _binding()
    template = _template()
    gateway = SourceQueryGateway(bindings=[binding], templates=[template])
    template.parameters.clear()
    binding.authorized_columns.clear()
    assert set(gateway.templates[template.template_id].parameters) == {"applicant_id"}
    assert gateway.bindings[binding.source_id].authorized_columns["applications"] == [
        "applicant_id", "status"
    ]
    with pytest.raises(MultiSourceContractError, match="explicit authorized columns"):
        SourceQueryGateway(bindings=[binding], templates=[_template()])


def test_gateway_executes_only_compiled_scope_and_reports_truncation(monkeypatch) -> None:
    executed = []

    class Cursor:
        description = [("applicant_id",), ("status",)]

        def execute(self, statement, params=None):
            executed.append((statement, params))

        def fetchmany(self, count):
            assert count == 2
            return [("A1", "OPEN"), ("A2", "CLOSED")]

    class Connection:
        def cursor(self):
            return nullcontext(Cursor())

    @contextmanager
    def connection(_reader, _binding):
        yield Connection()

    monkeypatch.setattr("services.structured_data.source_query.MySQLSourceReader._connection", connection)
    template = _template().model_copy(update={"max_rows": 1})
    result = SourceQueryGateway(bindings=[_binding()], templates=[template]).execute(
        template.template_id, {"applicant_id": "A1"}
    )
    assert executed[0][0] == "SET SESSION MAX_EXECUTION_TIME = 1000"
    assert executed[1][0].endswith("LIMIT 2")
    assert executed[1][1] == {"applicant_id": "A1"}
    assert result.rows == [{"applicant_id": "A1", "status": "OPEN"}]
    assert result.receipt.status == "COMPLETE"
    assert result.receipt.truncated and result.receipt.readonly_verified


def test_gateway_rejects_unregistered_template_and_wrong_parameters() -> None:
    gateway = SourceQueryGateway(bindings=[_binding()], templates=[_template()])
    with pytest.raises(MultiSourceContractError, match="query template is not allowed"):
        gateway.execute("QT-NOT-REGISTERED", {})
    with pytest.raises(MultiSourceContractError, match="provided parameters"):
        gateway.execute("QT-APPLICATION-BY-ID", {})
    with pytest.raises(MultiSourceContractError, match="must be string"):
        gateway.execute("QT-APPLICATION-BY-ID", {"applicant_id": 123})


def test_degraded_source_returns_unknown_without_querying() -> None:
    gateway = SourceQueryGateway(bindings=[_binding("DEGRADED")], templates=[_template()])
    result = gateway.execute("QT-APPLICATION-BY-ID", {"applicant_id": "HASH-A001"})
    assert result.rows == []
    assert result.receipt.status == "UNKNOWN"
    assert result.receipt.readonly_verified is False


def test_unknown_optional_parameter_does_not_become_an_empty_answer() -> None:
    template = _template().model_copy(update={
        "parameters": {"applicant_id": QueryParameter(type="string", required=False)}
    })
    gateway = SourceQueryGateway(bindings=[_binding()], templates=[template])
    result = gateway.execute(template.template_id, {"applicant_id": None})
    assert result.rows == []
    assert result.receipt.status == "INPUT_INCOMPLETE"
    assert result.receipt.readonly_verified is False


def test_repeated_source_failures_open_and_cool_down_circuit(monkeypatch) -> None:
    attempts = 0
    now = [10.0]

    @contextmanager
    def unavailable(_reader, _binding):
        nonlocal attempts
        attempts += 1
        raise OSError("source unavailable")
        yield

    monkeypatch.setattr(
        "services.structured_data.source_query.MySQLSourceReader._connection",
        unavailable,
    )
    gateway = SourceQueryGateway(
        bindings=[_binding()],
        templates=[_template()],
        circuit_breaker_threshold=2,
        circuit_breaker_cooldown_seconds=30,
        clock=lambda: now[0],
    )
    params = {"applicant_id": "HASH-A001"}
    assert gateway.execute("QT-APPLICATION-BY-ID", params).receipt.status == "UNKNOWN"
    assert gateway.execute("QT-APPLICATION-BY-ID", params).receipt.status == "UNKNOWN"
    circuit_result = gateway.execute("QT-APPLICATION-BY-ID", params)
    assert "CIRCUIT_OPEN" in str(circuit_result.receipt.reason)
    assert attempts == 2
    now[0] = 41.0
    gateway.execute("QT-APPLICATION-BY-ID", params)
    assert attempts == 3
