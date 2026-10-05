from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from services.realtime_qa.wren_project import MAPPING, SCHEMA, build_project, digest

ROOT = Path(__file__).resolve().parents[2]
WREN = Path(os.environ["ORION_WREN_PYTHON"]) if os.environ.get("ORION_WREN_PYTHON") else None
WORKER = ROOT / "services/realtime_qa/wren_project_worker.py"


def binding_fixture(tmp_path, *, count=4, extra_mapping=None):
    package = tmp_path / "package"
    mappings = []
    tables = []
    definitions = [("Customer", "customers", "customer_id", [("name", "customerName", "string")]),
                   ("Sale", "sales", "sale_id", [("amount", "amount", "decimal"), ("sold_at", "soldAt", "date")])]
    for name, table, primary, fields in definitions:
        columns = [primary, *[f[0] for f in fields], *(["customer_id"] if name == "Sale" else [])]
        tables.append({"name": "ms_" + table, "source_table": table, "source_id": "source1",
                       "dataset_id": "DS-TEST123", "columns": ["dataset_id", *columns],
                       "column_types": dict.fromkeys(columns, "text"), "row_count": 2 if name == "Customer" else count})
        derivation = {"from_snapshot_table": table, "source_id": "source1"}
        mappings.append({"id": "class_" + name, "mapping_type": "TABLE_TO_CLASS", "target": name,
                         "derivation": {**derivation, "identity_columns": [primary]}})
        for column, target, kind in [(primary, primary, "string"), *fields]:
            mappings.append({"id": name + "_" + target, "mapping_type": "COLUMN_TO_DATA_PROPERTY",
                             "target": target, "domain": name, "datatype": "xsd:" + kind,
                             "derivation": {**derivation, "from_snapshot_column": column}})
    mappings.append({"id": "relation", "mapping_type": "CANDIDATE_JOIN_TO_OBJECT_PROPERTY", "target": "customer",
                     "domain": "Sale", "range": "Customer", "derivation": {"from_snapshot_table": "sales",
                     "source_id": "source1", "from_snapshot_column": "customer_id", "join_target": "customers.customer_id"}})
    if extra_mapping:
        mappings.append(extra_mapping)
    content = {MAPPING: yaml.safe_dump({"mappings": mappings}), SCHEMA: json.dumps({"tables": tables})}
    files = []
    for relative, value in content.items():
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        files.append({"path": relative, "sha256": "sha256:" + hashlib.sha256(value.encode()).hexdigest()})
    manifest = json.dumps({"files": files}).encode()
    (package / "manifest.json").write_bytes(manifest)
    return SimpleNamespace(package_path=str(package), project_id="test-project", release_version="1.0",
                           release_fingerprint="sha256:" + hashlib.sha256(manifest).hexdigest(), database_access_mode="READ_ONLY")


def rows_fixture(count=4):
    sales = [{"sale_id": "s1", "amount": "10.10", "sold_at": "2026-09-01", "customer_id": "c1"},
             {"sale_id": "s2", "amount": "20.20", "sold_at": "2026-09-02", "customer_id": "c1"},
             {"sale_id": "s3", "amount": None, "sold_at": "2026-09-02", "customer_id": "c2"},
             {"sale_id": "s4", "amount": "30.30", "sold_at": "2026-10-01", "customer_id": "c2"}]
    sales.extend({"sale_id": f"s{i}", "amount": "0.10", "sold_at": "2026-09-03", "customer_id": "c1"} for i in range(5, count + 1))
    return {"st_0": [{"customer_id": "c1", "name": "甲客户"}, {"customer_id": "c2", "name": "乙客户"}], "st_1": sales}


def run_worker(descriptor, plan, rows=None, limit=100):
    if WREN is None or not WREN.is_absolute() or not WREN.is_file():
        pytest.fail("Install the separate pinned Wren environment and set ORION_WREN_PYTHON; real worker safety tests are required")
    result = subprocess.run([str(WREN), "-I", str(WORKER)], input=json.dumps({
        "descriptor": descriptor, "table_rows": rows or rows_fixture(), "plan": plan, "limit": limit}),
        capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def test_build_freezes_sources_and_persists_complete_mdl(tmp_path):
    binding = binding_fixture(tmp_path)
    before = {str(p): p.read_bytes() for p in Path(binding.package_path).rglob("*") if p.is_file()}
    descriptor = build_project(binding, cache_root=tmp_path / "cache")
    assert descriptor["relationships"][0]["joinType"] == "MANY_TO_ONE"
    assert descriptor["source_tables"][1]["columns"] == ["sale_id", "amount", "sold_at", "customer_id"]
    assert descriptor["source_tables"][1]["semantic_types"]["amount"] == ["decimal"]
    assert "orion_data." in descriptor["mdl_postgres"]["models"][0]["refSql"]
    assert "DS-TEST123" in descriptor["mdl_postgres"]["models"][0]["refSql"]
    assert "receipt.main." in descriptor["mdl"]["models"][0]["refSql"]
    assert descriptor["mdl_postgres_sha256"] == digest(descriptor["mdl_postgres"])
    project = Path(descriptor["project_path"])
    assert (project / "cubes/Sale__statistics/metadata.yml").is_file()
    assert (project / "views/Sale__records/metadata.yml").is_file()
    assert json.loads((project / "target/mdl.json").read_text()) == descriptor["mdl_postgres"]
    assert build_project(binding, cache_root=tmp_path / "cache")["key"] == descriptor["key"]
    assert {str(p): p.read_bytes() for p in Path(binding.package_path).rglob("*") if p.is_file()} == before


def test_package_tamper_rejected(tmp_path):
    binding = binding_fixture(tmp_path)
    with (Path(binding.package_path) / MAPPING).open("a") as stream:
        stream.write("\n# changed\n")
    with pytest.raises(ValueError, match="校验"):
        build_project(binding, cache_root=tmp_path / "cache")


def test_unsupported_rule_explicitly_omitted(tmp_path):
    binding = binding_fixture(tmp_path, extra_mapping={"id": "rule", "mapping_type": "RULE_TO_CLASS", "target": "RiskySale"})
    descriptor = build_project(binding, cache_root=tmp_path / "cache")
    assert descriptor["omitted"][0]["target"] == "RiskySale"
    assert "规则" in descriptor["omitted"][0]["reason"]
    assert all(m["name"] != "RiskySale" for m in descriptor["models"])


def test_native_cube_relationship_decimal_and_unknown(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    result = run_worker(descriptor, {"cube_query": {"cube": "Sale__statistics", "measures": ["sum_amount", "count_rows", "count_known_amount"],
                                                    "dimensions": ["customer__customerName"], "timeDimensions": [{"dimension": "soldAt", "granularity": "month"}]}})
    assert "error" not in result, result
    assert len(result["rows"]) == 3
    september_a = next(r for r in result["rows"] if r["customer__customerName"] == "甲客户")
    assert float(september_a["sum_amount"]) == 30.30
    september_b = next(r for r in result["rows"] if r["customer__customerName"] == "乙客户" and "09-01" in r["soldAt__month"])
    assert september_b["sum_amount"] is None
    assert september_b["count_rows"] == 1 and september_b["count_known_amount"] == 0
    assert result["engine_version"] == "0.15.0" and result["strict_mode"]


def test_aggregate_over_1000_rows_not_sampled(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path, count=1504), cache_root=tmp_path / "cache")
    result = run_worker(descriptor, {"cube_query": {"cube": "Sale__statistics", "measures": ["count_rows", "sum_amount"]}}, rows_fixture(1504))
    assert "error" not in result, result
    assert result["rows"][0]["count_rows"] == 1504
    assert float(result["rows"][0]["sum_amount"]) == 210.60
    assert result["source_row_counts"]["st_1"] == 1504


@pytest.mark.parametrize("sql", [
    "SELECT * FROM receipt.main.Sale", "SELECT * FROM pg_catalog.Sale",
    "SELECT * FROM read_csv('/tmp/x')", "SELECT pg_read_file('/tmp/x') FROM Sale",
    "SELECT * FROM Sale; DELETE FROM Sale", "SELECT * FROM Sale JOIN Customer ON true",
    "SELECT * FROM missing", "SELECT random() FROM Sale", "SELECT * FROM Sale TABLESAMPLE BERNOULLI (10)",
])
def test_sql_boundary_blocks_physical_and_unreviewed_operations(tmp_path, sql):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    result = run_worker(descriptor, {"sql": sql})
    assert "error" in result
    assert "/tmp/x" not in result["error"]


def test_read_only_view_and_n_plus_one(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    result = run_worker(descriptor, {"sql": 'SELECT * FROM "Sale__records" ORDER BY "_key"'}, limit=2)
    assert "error" not in result, result
    assert len(result["rows"]) == 2 and result["truncated"]
    cube = run_worker(descriptor, {"cube_query": {"cube": "Sale__statistics", "measures": ["count_rows"], "dimensions": ["sale_id"]}}, limit=2)
    assert cube["truncated"] and "LIMIT 3" in cube["sql"]


def test_identity_conflict_blocks_join_fanout(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    rows = rows_fixture()
    rows["st_0"][1]["customer_id"] = "c1"
    result = run_worker(descriptor, {"cube_query": {"cube": "Sale__statistics", "measures": ["sum_amount"]}}, rows)
    assert "业务标识存在冲突" in result["error"]


def test_incomplete_rows_and_changed_manifest_fail_closed(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    rows = rows_fixture()
    rows["st_1"].pop()
    assert "完整来源数量" in run_worker(descriptor, {"sql": "SELECT * FROM Sale"}, rows)["error"]
    changed = copy.deepcopy(descriptor)
    changed["mdl"]["models"][0]["refSql"] = "SELECT 1"
    assert "指纹" in run_worker(changed, {"sql": "SELECT * FROM Sale"})["error"]


def test_decimal_overprecision_rejected_without_rounding(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    rows = rows_fixture()
    rows["st_1"][0]["amount"] = "0.1234567890123456789"
    assert "精确" in run_worker(descriptor, {"sql": "SELECT * FROM Sale"}, rows)["error"]


def test_cache_is_rebuilt_and_symlink_cannot_redirect_write(tmp_path):
    binding = binding_fixture(tmp_path)
    descriptor = build_project(binding, cache_root=tmp_path / "cache")
    target = Path(descriptor["project_path"]) / "target/mdl.json"
    target.write_text('{"models": []}')
    rebuilt = build_project(binding, cache_root=tmp_path / "cache")
    assert json.loads(target.read_text()) == rebuilt["mdl_postgres"]
    outside = tmp_path / "outside.json"
    outside.write_text("keep")
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="链接"):
        build_project(binding, cache_root=tmp_path / "cache")
    assert outside.read_text() == "keep"


@pytest.mark.parametrize("sql", [
    'SELECT * FROM Sale LIMIT 1',
    'SELECT * FROM Sale OFFSET 1',
    'WITH sample AS (SELECT * FROM Sale LIMIT 1) SELECT SUM(amount) FROM sample',
    'SELECT SUM(amount) FROM (SELECT * FROM Sale OFFSET 1) sample',
])
def test_no_hidden_input_sampling(tmp_path, sql):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    assert "LIMIT/OFFSET" in run_worker(descriptor, {"sql": sql})["error"]


def test_declared_many_to_one_and_preaggregated_cte_join(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    direct = run_worker(descriptor, {"sql": '''SELECT c."customerName", SUM(s.amount) AS amount
        FROM Sale s JOIN Customer c ON s._fk_customer = c.customer_id GROUP BY c."customerName"'''})
    assert "error" not in direct, direct
    assert {row["customerName"]: float(row["amount"]) for row in direct["rows"]} == {"甲客户": 30.3, "乙客户": 30.3}
    grouped = run_worker(descriptor, {"sql": '''WITH totals AS (
        SELECT _fk_customer AS customer_id, SUM(amount) AS amount FROM Sale GROUP BY _fk_customer
        ), counts AS (SELECT _fk_customer AS customer_id, COUNT(*) AS n FROM Sale GROUP BY _fk_customer)
        SELECT c."customerName", t.amount, n.n FROM Customer c
        LEFT JOIN totals t ON c.customer_id = t.customer_id
        LEFT JOIN counts n ON c.customer_id = n.customer_id ORDER BY c.customer_id'''})
    assert "error" not in grouped, grouped
    assert [row["n"] for row in grouped["rows"]] == [2, 2]
    assert [float(row["amount"]) for row in grouped["rows"]] == [30.3, 30.3]
    unsafe = run_worker(descriptor, {"sql": '''SELECT c.customer_id, SUM(s.amount) FROM Customer c
        JOIN Sale s ON s._fk_customer = c.customer_id GROUP BY c.customer_id'''})
    assert "重复计数" in unsafe["error"]
    wrong_grain = run_worker(descriptor, {"sql": '''WITH totals AS (
        SELECT _fk_customer AS customer_id, "soldAt", SUM(amount) AS amount FROM Sale GROUP BY _fk_customer, "soldAt")
        SELECT c.customer_id, SUM(t.amount) FROM Customer c LEFT JOIN totals t ON c.customer_id = t.customer_id
        GROUP BY c.customer_id'''})
    assert "重复计数" in wrong_grain["error"]


def test_parameters_bind_scalar_and_escape_pattern_literals(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    rows = rows_fixture()
    rows["st_0"][0]["name"] = "甲%_客户"
    rows["st_0"][1]["name"] = "甲普通客户"
    matched = run_worker(descriptor, {"sql": 'SELECT customer_id FROM Customer WHERE "customerName" LIKE :name',
        "parameters": {"name": {"value": "%_", "match": "contains"}}}, rows)
    assert "error" not in matched, matched
    assert matched["rows"] == [{"customer_id": "c1"}]
    attack = run_worker(descriptor, {"sql": "SELECT customer_id FROM Customer WHERE customer_id = :id",
        "parameters": {"id": "c1' OR 1=1 --"}}, rows)
    assert attack["rows"] == []
    assert "error" in run_worker(descriptor, {"sql": "SELECT * FROM Sale", "parameters": {"unused": 1}})
    assert "error" in run_worker(descriptor, {"sql": "SELECT * FROM Sale; DELETE FROM Sale", "parameters": {}})


def live_registration_fixture(binding, descriptor):
    from services.structured_data.source_registration import schema_contract_signature

    contracts = []
    for source in descriptor["source_tables"]:
        key = "customer_id" if source["source_table"] == "customers" else "sale_id"
        foreign = [] if source["source_table"] == "customers" else [{"name": "sales_customer_fk", "columns": ["customer_id"],
            "referenced_schema": "business", "referenced_table": "customers", "referenced_columns": ["customer_id"],
            "validated": True, "deferrable": False}]
        table = {"source_id": "source1", "source_table": source["source_table"], "qualified_source_table": "business." + source["source_table"],
                 "physical_schema": "business", "physical_table": source["source_table"], "columns": source["columns"],
                 "types": {name: {"amount": "numeric", "sold_at": "date"}.get(name, "text") for name in source["columns"]},
                 "nullable": {name: name != key for name in source["columns"]}, "primary_key": [key], "foreign_keys": foreign}
        table["schema_signature"] = schema_contract_signature(table)
        contracts.append(table)
    scope = {table["qualified_source_table"]: table["columns"] for table in contracts}
    binding.source_bindings = {"source1": {"access_mode": "READ_ONLY", "status": "ACTIVE", "database": "business_db",
        "authorized_tables": list(scope), "authorized_columns": scope}}
    return {"source1": {"state": "ACTIVE", "database": "business_db", "source_tables": contracts,
        "authorized_tables": list(scope), "authorized_columns": scope}}


def test_live_compilation_uses_catalog_grain_and_dynamic_rows(tmp_path):
    from services.realtime_qa.wren_project import build_live_project

    binding = binding_fixture(tmp_path)
    descriptor = build_project(binding, cache_root=tmp_path / "cache")
    registrations = live_registration_fixture(binding, descriptor)
    live = build_live_project(binding, descriptor, registrations, cache_root=tmp_path / "cache")
    assert live["execution_scope"] == "LIVE_SOURCE_DATABASE"
    assert live["native_validation"]["status"] == "PASSED"
    assert all(source["expected_count"] is None and source["dataset_column"] is None for source in live["source_tables"])
    sale = next(model for model in live["models"] if model["name"] == "Sale")
    assert 'FROM "business"."sales"' in sale["refSql"]
    assert "DISTINCT" not in sale["refSql"] and "DS-TEST" not in sale["refSql"]
    assert '"amount" AS "amount"' in sale["refSql"]
    assert descriptor["source_tables"][1]["expected_count"] == 4
    missing_constraint = copy.deepcopy(registrations)
    missing_constraint["source1"]["source_tables"][1]["foreign_keys"] = []
    with pytest.raises(ValueError, match="外键"):
        build_live_project(binding, descriptor, missing_constraint, cache_root=tmp_path / "cache")
    binding.source_bindings["source1"]["authorized_columns"]["business.sales"] = ["sale_id"]
    with pytest.raises(ValueError, match="交集"):
        build_live_project(binding, descriptor, registrations, cache_root=tmp_path / "cache")


@pytest.mark.parametrize("value", ["9223372036854775808", "1.25", True])
def test_fixture_integer_cannot_round_or_overflow(tmp_path, value):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    descriptor["source_tables"][1]["semantic_types"]["amount"] = ["bigint"]
    rows = rows_fixture()
    rows["st_1"][0]["amount"] = value
    assert "64位整数" in run_worker(descriptor, {"sql": "SELECT * FROM Sale"}, rows)["error"]


def test_invalid_fixture_date_fails_without_echoing_data(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    rows = rows_fixture()
    rows["st_1"][0]["sold_at"] = "2026-02-31"
    error = run_worker(descriptor, {"sql": "SELECT * FROM Sale"}, rows)["error"]
    assert "无效日历" in error and "2026-02-31" not in error


class ExportCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.description = [SimpleNamespace(name="=1+1"), SimpleNamespace(name="amount")]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def execute(self, sql):
        assert sql == "SELECT governed_projection"

    def fetchmany(self, count):
        chunk, self.rows = self.rows[:count], self.rows[count:]
        return chunk


def test_export_streams_exact_decimal_escapes_formulas_and_cleans_partial(tmp_path):
    import csv
    from decimal import Decimal

    from services.realtime_qa.wren_project_worker import _stream_export

    def connection():
        return SimpleNamespace(cursor=lambda **kwargs: ExportCursor([("=2+2", Decimal("100.01")), ("normal", Decimal("-1.20"))]))

    path = tmp_path / "result.csv"
    result = _stream_export(connection(), "SELECT governed_projection", {"path": str(path), "max_rows": 2})
    assert result["row_count"] == 2 and result["complete"]
    assert result["byte_count"] == path.stat().st_size
    with path.open(encoding="utf-8-sig", newline="") as stream:
        assert list(csv.reader(stream)) == [["'=1+1", "amount"], ["'=2+2", "100.01"], ["normal", "-1.20"]]
    rejected = tmp_path / "rejected.csv"
    with pytest.raises(ValueError, match="行数预算"):
        _stream_export(connection(), "SELECT governed_projection", {"path": str(rejected), "max_rows": 1})
    assert not rejected.exists() and not Path(str(rejected) + ".partial").exists()
    with pytest.raises(ValueError, match="字节预算"):
        _stream_export(connection(), "SELECT governed_projection", {"path": str(rejected), "max_bytes": 2})
    assert not rejected.exists() and not Path(str(rejected) + ".partial").exists()


def test_freshness_rechecks_role_barrier_and_observation_age():
    from datetime import UTC, datetime, timedelta

    from services.realtime_qa.wren_project_worker import _freshness_guard

    class Cursor:
        def __init__(self, answers):
            self.answers = iter(answers)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, sql, parameters=None):
            assert sql.startswith("SELECT pg_")
        def fetchone(self):
            return (next(self.answers),)

    def connection(answers):
        return SimpleNamespace(cursor=lambda: Cursor(answers))

    freshness = {"observed_at": datetime.now(UTC).isoformat(), "query_node": "REPLICA", "threshold_ms": 10000,
                 "primary_wal_barrier": "0/123ABC"}
    payload = {"descriptor": {"execution_scope": "LIVE_SOURCE_DATABASE"}, "execution": {"data_freshness": freshness}}
    checked = _freshness_guard(connection([True, True]), payload)
    assert "verified_at" in checked and checked["observation_age_ms"] >= 0
    with pytest.raises(ValueError, match="追上"):
        _freshness_guard(connection([True, False]), payload)
    with pytest.raises(ValueError, match="角色"):
        _freshness_guard(connection([False]), payload)
    freshness["observed_at"] = (datetime.now(UTC) - timedelta(seconds=20)).isoformat()
    with pytest.raises(ValueError, match="过期"):
        _freshness_guard(connection([]), payload)


def test_oversized_json_result_rejected_before_any_stdout(capsys):
    from services.realtime_qa.wren_project_worker import MAX_RESPONSE_BYTES, _encode_response

    with pytest.raises(ValueError, match="4 MiB"):
        _encode_response({"rows": [{"long_text": "汉" * (MAX_RESPONSE_BYTES // 3)}]})
    assert capsys.readouterr().out == ""
    assert json.loads(_encode_response({"rows": [{"value": "正常"}]}))["rows"] == [{"value": "正常"}]


def test_duplicate_alias_scopes_are_normalized_with_boolean_filters(tmp_path):
    descriptor = build_project(binding_fixture(tmp_path), cache_root=tmp_path / "cache")
    sql = '''WITH totals AS (
        SELECT p._fk_customer AS customer_id, SUM(p.amount) AS amount FROM Sale p
        WHERE (p.amount > :minimum AND p.amount <= :maximum) OR p.amount IS NULL
        GROUP BY p._fk_customer)
        SELECT c.customer_id, p.amount FROM Customer c JOIN totals p ON c.customer_id=p.customer_id
        WHERE c.customer_id = :customer_id'''
    result = run_worker(descriptor, {"sql": sql, "parameters": {"minimum": 0, "maximum": 100, "customer_id": "c1"}})
    assert "error" not in result, result
    assert len(result["rows"]) == 1 and float(result["rows"][0]["amount"]) == 30.3
    assert result["requested_sql"] == sql
    assert "orion_scope_1" in result["sql"] and "orion_scope_2" in result["sql"]
    assert "orion_scope_1" in result["expanded_sql"] or "orion_scope_2" in result["expanded_sql"]


def test_json_numbers_preserve_unsafe_integer_identity():
    from services.realtime_qa.wren_project_worker import _json_value

    assert _json_value(2**53 - 1) == 2**53 - 1
    assert _json_value(-(2**53 - 1)) == -(2**53 - 1)
    assert _json_value(2**53) == "9007199254740992"
    assert _json_value(-(2**63)) == "-9223372036854775808"
    assert _json_value(2**63 - 1) == "9223372036854775807"
    assert _json_value(1000) == 1000 and _json_value(True) is True


def test_cube_quotes_reserved_model_identifiers_before_native_planning(tmp_path):
    binding = binding_fixture(tmp_path)
    path = Path(binding.package_path) / MAPPING
    changed = path.read_text().replace("Sale", "Order")
    path.write_text(changed)
    manifest_path = Path(binding.package_path) / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    for item in manifest["files"]:
        if item["path"] == MAPPING:
            item["sha256"] = "sha256:" + hashlib.sha256(changed.encode()).hexdigest()
    raw = json.dumps(manifest).encode()
    manifest_path.write_bytes(raw)
    binding.release_fingerprint = "sha256:" + hashlib.sha256(raw).hexdigest()
    descriptor = build_project(binding, cache_root=tmp_path / "cache")
    result = run_worker(descriptor, {"cube_query": {"cube": "Order__statistics", "measures": ["count_rows"]}})
    assert "error" not in result, result
    assert result["rows"] == [{"count_rows": 4}]
    assert 'FROM "Order"' in result["sql"]


def test_primary_rechecks_current_source_but_never_refreshes_old_snapshot():
    from datetime import UTC, datetime, timedelta

    from services.realtime_qa.wren_project_worker import _freshness_guard

    class Cursor:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def execute(self, statement):
            assert statement == "SELECT pg_is_in_recovery()"
        def fetchone(self):
            return (False,)

    connection = SimpleNamespace(cursor=Cursor)
    stale = (datetime.now(UTC) - timedelta(seconds=20)).isoformat()
    payload = {"descriptor": {"execution_scope": "LIVE_SOURCE_DATABASE"}, "execution": {"data_freshness": {
        "observed_at": stale, "query_node": "PRIMARY", "threshold_ms": 10000}}}
    fresh = _freshness_guard(connection, payload)
    assert fresh["observed_at"] != stale and fresh["upstream_observed_at"] == stale
    assert fresh["observation_age_ms"] == 0 and fresh["upstream_observation_age_ms"] > 10000
    with pytest.raises(ValueError, match="事务快照"):
        _freshness_guard(connection, payload, transaction={"transaction_started_at": stale})
    snapshot = datetime.now(UTC).isoformat()
    checked = _freshness_guard(connection, payload, transaction={"transaction_started_at": snapshot})
    assert checked["observed_at"] == snapshot
