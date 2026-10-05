import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness import realtime_qa_mcp
from services.realtime_qa.models import RealtimeSessionAnswer
from services.realtime_qa.result_pages import register_result_page_api
from tests.unit.test_realtime_evidence_receipts import stored_answer


def fixture(tmp_path, rows=58):
    store, _, answer = stored_answer(tmp_path)
    release = tmp_path/'core-api-project'/'07-release'
    package = release/'package-1.0.0'
    package.mkdir(parents=True)
    (package/'manifest.json').write_text('{"files":[]}')
    fingerprint = 'sha256:' + hashlib.sha256((package/'manifest.json').read_bytes()).hexdigest()
    (release/'publication.json').write_text(json.dumps({'approval_decision': 'APPROVED',
        'release_version': '1.0.0', 'integrity_verification_status': 'PASSED', 'package_path': '07-release/package-1.0.0'}))
    (release/'gate-results.json').write_text('{"stage":"S7","status":"PASSED"}')
    bundle = answer['answer']['evidence_bundle']
    bundle['release']['release_fingerprint'] = fingerprint
    bundle['release']['package_path'] = str(package)
    answer['runtime_binding']['release_fingerprint'] = fingerprint
    record = deepcopy(bundle['evidence'][0])
    record.update(source_kind='document', source_ref='document-fact:months', payload={
        'cq_results': [{'source_question_id': 'CQ02', 'capability_name': 'months',
            'rows': [{'candidate': f'C{i:04}', 'months': i+12} for i in range(rows)], 'row_count': rows}],
        'facts': ['SECRET_SOURCE_FACTS_DO_NOT_RETURN'],
    })
    bundle['evidence'] = [record]
    response = RealtimeSessionAnswer.model_validate(answer)
    reference = store.save(response)
    runtime = SimpleNamespace(binding=response.answer.evidence_bundle.release)
    app = FastAPI()
    register_result_page_api(app, store, lambda _: runtime)
    payload = {'session_id': reference['session_id'], 'query_id': reference['query_id'],
        'receipt_id': reference['receipt_id'], 'receipt_sha256': reference['sha256'],
        'expected_release': {key: reference[key] for key in ('project_id','release_version','release_fingerprint')},
        'source_question_id': 'CQ02', 'capability_name': 'months'}
    return TestClient(app), payload, release


def test_58_real_immutable_rows_paginate_without_rerun_or_raw_facts(tmp_path):
    client, payload, _ = fixture(tmp_path)
    all_rows = []
    offset = 0
    while offset is not None:
        response = client.post('/ontology/realtime/session-result-page', json={**payload, 'offset': offset, 'limit': 20})
        assert response.status_code == 200, response.text
        page = response.json()
        assert page['total'] == 58 and page['query_reexecuted'] is False
        assert 'SECRET_SOURCE' not in response.text and 'package_path' not in response.text
        all_rows.extend(page['rows'])
        offset = page['next_offset']
    assert all_rows == [{'candidate': f'C{i:04}', 'months': i+12} for i in range(58)]


@pytest.mark.parametrize('change', ['session', 'project', 'version', 'fingerprint', 'hash', 'query', 'path', 'withdrawn', 'limit'])
def test_result_page_rejects_identity_scope_and_withdrawal(tmp_path, change):
    client, payload, release = fixture(tmp_path)
    if change == 'session':
        payload['session_id'] = payload['session_id'].replace('1','2')
    elif change == 'project':
        payload['expected_release']['project_id'] = 'other-project'
    elif change == 'fingerprint':
        payload['expected_release']['release_fingerprint'] = 'sha256:' + 'b'*64
    elif change == 'version':
        payload['expected_release']['release_version'] = '2.0.0'
    elif change == 'hash':
        payload['receipt_sha256'] = 'sha256:' + 'f'*64
    elif change == 'query':
        payload['query_id'] = 'other'
    elif change == 'path':
        payload['fact_artifact'] = '/etc/passwd'
    elif change == 'limit':
        payload['limit'] = 51
    else:
        (release/'release-revocation.json').write_text('{"status":"REVOKED"}')
    assert client.post('/ontology/realtime/session-result-page', json=payload).status_code in {404,409,422}


def test_empty_executed_result_is_not_missing_receipt(tmp_path):
    client, payload, _ = fixture(tmp_path, rows=0)
    page = client.post('/ontology/realtime/session-result-page', json=payload).json()
    assert page['rows'] == [] and page['total'] == 0 and page['next_offset'] is None


def test_mcp_receipt_tool_validates_and_calls_read_endpoint(tmp_path, monkeypatch):
    client, payload, _ = fixture(tmp_path)
    args = {**payload.pop('expected_release'), **payload, 'offset': 40, 'limit': 20}
    calls = []
    def post(path, request):
        calls.append(path)
        response = client.post(path, json=request)
        assert response.status_code == 200
        return response.json()
    monkeypatch.setattr(realtime_qa_mcp, '_request_json', post)
    result = realtime_qa_mcp.RealtimeQaMcpServer().handle({'jsonrpc':'2.0', 'id':1, 'method':'tools/call',
        'params': {'name':'read_ontology_query_results','arguments':args}})['result']
    assert result['structuredContent']['returned'] == 18
    assert calls == ['/ontology/realtime/session-result-page']
    assert not result['isError']


def test_preview_preserves_associated_scalar_rows_and_marks_omission():
    rows = [{'candidate': f'C{i}', 'project': '项目'+str(i), 'long': 'x'*400} for i in range(58)]
    summary = realtime_qa_mcp._executed_result_summary({'evidence':[{'source_kind':'structured_db','payload':{'rows':rows}}]}, None)
    result = summary['results'][0]
    assert result['row_count'] == 58 and result['preview_omitted_rows'] == 48
    assert result['rows_preview'][1]['candidate'] == 'C1' and result['rows_preview'][1]['project'] == '项目1'
    assert len(result['rows_preview']) == 10 and len(result['rows_preview'][0]['long']) == 301
