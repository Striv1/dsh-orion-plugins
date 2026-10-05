import copy
from contextlib import nullcontext

import pytest

from harness.orion_workflow_mcp import OrionWorkflowTools
from services.ontology_contracts.errors import WorkflowError
from services.structured_data import source_query_plan as module
from services.structured_data.pipeline import StructuredDataImportError
from tests.unit.test_source_evidence_query import (
    HASH,
    MANIFEST,
    PROJECT,
    SNAP_INVENTORY,
    SNAP_SCHEMA,
    ready_service,
)


def source_fixture():
    schema = copy.deepcopy(SNAP_SCHEMA)
    schema['tables'][0]['columns'] += ['opened_at']
    schema['tables'].append({'name': 'ms_src_line_abc', 'source_id': 'src', 'source_table': 'line',
                              'dataset_id': 'DS-SNAPSHOT', 'columns': ['dataset_id', 'plant_code', 'line_id', 'status', 'amount']})
    return schema, copy.deepcopy(SNAP_INVENTORY)


def plan_fixture():
    return {'sources': {'p': 'plant', 'l': 'line'}, 'from': 'p',
            'joins': [{'left': 'p.plant_code', 'right': 'l.plant_code'}],
            'select': {'city': {'field': 'p.city'}, 'n': {'field': 'l.line_id', 'aggregate': 'count_distinct'}},
            'filters': [{'field': 'l.status', 'normalize': 'upper', 'op': 'eq', 'value': 'FAIL'}],
            'group_by': ['city'], 'having': [{'field': 'n', 'op': 'gte', 'value': 2}],
            'order_by': [{'field': 'city', 'direction': 'ASC'}], 'limit': 1}


def compile_(plan=None, schema=None, inventory=None):
    s, i = source_fixture()
    return module.compile_source_query_plan(plan or plan_fixture(), project_id=PROJECT,
                   schema_snapshot=schema or s, datasource_inventory=inventory or i)


def test_compiler_scopes_every_source_and_uses_bound_values():
    statement, params, bindings, fields, limit = compile_()
    assert statement.count('WHERE dataset_id=%s') == 2
    assert 'JOIN "l" ON "p"."plant_code"="l"."plant_code"' in statement
    assert 'COUNT(DISTINCT "l"."line_id")' in statement
    assert 'HAVING COUNT(DISTINCT "l"."line_id") >= %s' in statement
    assert 'COUNT(*) OVER ()' in statement
    assert params == ['DS-SNAPSHOT', 'DS-SNAPSHOT', 'FAIL', 2, 2]
    assert fields == ['city', 'n'] and limit == 1 and len(bindings) == 2


@pytest.mark.parametrize('defect', ['sql', 'table', 'column', 'technical', 'unjoined', 'cycle', 'alias', 'limit', 'group', 'operation', 'cast', 'source_owner'])
def test_invalid_plans_fail_before_opening_connection(monkeypatch, defect):
    plan = plan_fixture()
    schema, inventory = source_fixture()
    if defect == 'sql':
        plan['sql'] = 'SELECT * FROM secret'
    elif defect == 'table':
        plan['sources']['l'] = 'other_project'
    elif defect == 'column':
        plan['select']['city']['field'] = 'p.secret'
    elif defect == 'technical':
        plan['filters'][0]['field'] = 'l.dataset_id'
    elif defect == 'unjoined':
        plan['joins'] = []
    elif defect == 'cycle':
        plan['joins'].append(plan['joins'][0])
    elif defect == 'alias':
        plan['select']['bad;DROP TABLE x'] = {'field': 'p.city'}
    elif defect == 'limit':
        plan['limit'] = True
    elif defect == 'group':
        plan['group_by'] = []
    elif defect == 'operation':
        plan['filters'][0]['op'] = 'eq;delete'
    elif defect == 'cast':
        plan['select']['city']['cast'] = 'text); DROP TABLE x;'
    else:
        inventory['datasets'][0]['project_id'] = 'other'
    monkeypatch.setattr(module.psycopg, 'connect', lambda *_: pytest.fail('must not connect'))
    with pytest.raises(StructuredDataImportError):
        module.query_source_plan(reader_url='postgresql://reader@localhost/test', project_id=PROJECT,
                                 schema_snapshot=schema, datasource_inventory=inventory, query_plan=plan)


def test_parameter_injection_is_data_and_time_and_number_casts_are_explicit():
    plan = plan_fixture()
    malicious = "x'; SELECT pg_sleep(999); --"
    plan['filters'][0]['value'] = malicious
    plan['filters'] += [{'field': 'p.opened_at', 'cast': 'datetime', 'op': 'gte', 'value': '2026-09-01T00:00:00+08:00'},
                        {'field': 'l.amount', 'cast': 'decimal', 'op': 'gt', 'value': 10}]
    statement, params, *_ = compile_(plan)
    assert malicious not in statement and malicious in params
    assert 'AS timestamptz' in statement and 'AS numeric' in statement


class Cursor:
    def __init__(self, owner=PROJECT, rows=None):
        self.owner = owner
        self.calls = []
        self.rows = [('北京', 5, 2), ('上海', 4, 2)] if rows is None else rows

    def execute(self, statement, params=None):
        self.calls.append((statement, params))

    def fetchone(self):
        if 'snapshot_sets' in self.calls[-1][0]:
            return (self.owner, True, MANIFEST)
        return (self.owner, HASH, True, {'tables': [{'table': 'plant', 'target_table': 'ms_src_plant_abc'},
                                                 {'table': 'line', 'target_table': 'ms_src_line_abc'}]})

    def fetchall(self):
        return self.rows


def install(monkeypatch, cursor):
    connection = type('Connection', (), {'cursor': lambda _: nullcontext(cursor)})()
    monkeypatch.setattr(module.psycopg, 'connect', lambda *_: nullcontext(connection))


def test_catalog_verification_and_query_share_one_read_only_transaction(monkeypatch):
    cursor = Cursor()
    install(monkeypatch, cursor)
    schema, inventory = source_fixture()
    result = module.query_source_plan(reader_url='postgresql://reader@localhost/test', project_id=PROJECT,
                          schema_snapshot=schema, datasource_inventory=inventory, query_plan=plan_fixture())
    assert 'REPEATABLE READ READ ONLY' in cursor.calls[0][0]
    assert 'statement_timeout = 15000' in cursor.calls[1][0]
    assert len([c for c in cursor.calls if 'FROM orion_catalog.' in c[0]]) == 4
    assert result['rows'] == [{'city': '北京', 'n': 5}]
    assert result['truncated'] and result['total_row_count'] == 2
    assert result['ontology_runtime_verified'] is False
    assert len(result['sources']) == 2


def test_catalog_drift_stops_before_reading_rows(monkeypatch):
    cursor = Cursor(owner='other-project')
    install(monkeypatch, cursor)
    schema, inventory = source_fixture()
    with pytest.raises(StructuredDataImportError, match='实时快照'):
        module.query_source_plan(reader_url='postgresql://reader@localhost/test', project_id=PROJECT,
                      schema_snapshot=schema, datasource_inventory=inventory, query_plan=plan_fixture())
    assert not any('evidence_rows' in call[0] for call in cursor.calls)


def test_workflow_plan_uses_existing_s1_guards_and_does_not_write(tmp_path, monkeypatch):
    service, pid, root = ready_service(tmp_path, stage='S3')
    state = service._read_state(root)
    before = {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    monkeypatch.setattr(module, 'query_source_plan', lambda **kwargs: {
        'validation_scope': 'SOURCE_EVIDENCE_ONLY', 'total_row_count': 3, 'returned_row_count': 3,
        'truncated': False, 'project_id': kwargs['project_id']})
    summary, result = OrionWorkflowTools(service).call('query_source_evidence', {
        'project_id': pid, 'expected_revision': state['revision'], 'query_plan': plan_fixture()})
    assert '完整结果 3 行' in summary and result['revision'] == state['revision']
    assert before == {p: p.read_bytes() for p in root.rglob('*') if p.is_file()}
    with pytest.raises(WorkflowError):
        service.query_source_evidence(project_id=pid, expected_revision=state['revision'] - 1, query_plan=plan_fixture())
    with pytest.raises(WorkflowError, match='不能混用'):
        service.query_source_evidence(project_id=pid, expected_revision=state['revision'], query_plan=plan_fixture(), table='plant')


def test_generated_relational_query_counts_full_distinct_population_in_memory():
    # Execute the common SQL subset, not a mocked result: duplicate inspection
    # rows must not inflate distinct business objects, and other versions must
    # stay excluded before joins/grouping. PostgreSQL-specific casts are also
    # exercised in the 3081 source read-back, separately from this test.
    import sqlite3
    statement, params, *_ = compile_()
    with sqlite3.connect(':memory:') as database:
        database.execute("ATTACH DATABASE ':memory:' AS orion_data")
        database.execute('CREATE TABLE orion_data.ms_src_plant_abc (dataset_id TEXT, plant_code TEXT, city TEXT, opened_at TEXT)')
        database.execute('CREATE TABLE orion_data.ms_src_line_abc (dataset_id TEXT, plant_code TEXT, line_id TEXT, status TEXT, amount TEXT)')
        database.executemany('INSERT INTO orion_data.ms_src_plant_abc VALUES (?,?,?,?)', [
            ('DS-SNAPSHOT', 'P1', 'A', '2026-01-01'), ('DS-SNAPSHOT', 'P2', 'B', '2026-01-01'),
            ('DS-OTHER', 'P1', 'wrong', '2026-01-01')])
        database.executemany('INSERT INTO orion_data.ms_src_line_abc VALUES (?,?,?,?,?)', [
            ('DS-SNAPSHOT', 'P1', 'L1', 'FAIL', '1'), ('DS-SNAPSHOT', 'P1', 'L1', 'fail', '1'),
            ('DS-SNAPSHOT', 'P1', 'L2', 'fail', '2'), ('DS-SNAPSHOT', 'P1', 'L3', 'PASS', '3'),
            ('DS-SNAPSHOT', 'P2', 'L4', 'FAIL', '4'), ('DS-SNAPSHOT', 'P2', 'L5', 'fail', '5'),
            ('DS-OTHER', 'P1', 'L999', 'FAIL', '999')])
        assert database.execute(statement.replace('%s', '?'), params).fetchall() == [('A', 2, 2), ('B', 2, 2)]
