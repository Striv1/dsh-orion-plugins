"""Faults at real workflow commit boundaries, using only a temporary workspace."""

import hashlib
import json
import subprocess
import sys

import pytest

from services.ontology_engineering import OntologyWorkflowService
from services.ontology_engineering.workflow import WorkflowError, WorkflowGateError
from tests.unit.test_cq_semantic_workflow import setup_s2


def prepared(tmp_path):
    service, pid, payload = setup_s2(tmp_path)
    preview = service.preflight_stage_submission(project_id=pid, stage='S2', payload=payload)
    assert preview['status'] == 'PASSED'
    args = dict(project_id=pid, stage='S2', preflight_token=preview['preflight_token'],
                expected_revision=service.get_status(pid)['revision'])
    return service, pid, payload, args


def test_authentic_retry_returns_original_result_without_second_execution(tmp_path, monkeypatch):
    service, pid, _, args = prepared(tmp_path)
    result = service.commit_preflight_stage_submission(**args)
    state = (tmp_path / pid / 'workflow-state.json').read_bytes()
    events = (tmp_path / pid / 'events/agent-trace.jsonl').read_bytes()
    fresh = OntologyWorkflowService(tmp_path)
    monkeypatch.setattr(fresh, '_execute_preflight_payload', lambda *a: pytest.fail('re-execution'))
    assert fresh.commit_preflight_stage_submission(**args) == result
    assert (tmp_path / pid / 'workflow-state.json').read_bytes() == state
    assert (tmp_path / pid / 'events/agent-trace.jsonl').read_bytes() == events
    with pytest.raises(WorkflowError, match='校验失败'):
        fresh.commit_preflight_stage_submission(**{**args, 'preflight_token': args['preflight_token']+'bad'})


def test_result_survives_token_consumption_write_failure(tmp_path, monkeypatch):
    service, pid, _, args = prepared(tmp_path)
    write = service._write_json

    def fail_consumption(path, value):
        if path.parent.name == '.preflight-submissions' and value.get('used_at'):
            raise OSError('lost receipt write')
        write(path, value)

    monkeypatch.setattr(service, '_write_json', fail_consumption)
    with pytest.raises(OSError, match='lost receipt'):
        service.commit_preflight_stage_submission(**args)
    revision = service.get_status(pid)['revision']
    fresh = OntologyWorkflowService(tmp_path)
    monkeypatch.setattr(fresh, '_execute_preflight_payload', lambda *a: pytest.fail('re-execution'))
    recovered = fresh.commit_preflight_stage_submission(**args)
    assert recovered['revision'] == revision
    assert recovered['stage_statuses']['S2'] == 'PASSED'


def test_process_exit_after_formal_commit_recovers_checkpoint(tmp_path, monkeypatch):
    service, pid, _, args = prepared(tmp_path)
    execute = service._execute_preflight_payload

    def crash(*a):
        execute(*a)
        raise SystemExit('process exited before caller saved result')

    monkeypatch.setattr(service, '_execute_preflight_payload', crash)
    with pytest.raises(SystemExit):
        service.commit_preflight_stage_submission(**args)
    revision = service.get_status(pid)['revision']
    fresh = OntologyWorkflowService(tmp_path)
    monkeypatch.setattr(fresh, '_execute_preflight_payload', lambda *a: pytest.fail('re-execution'))
    result = fresh.commit_preflight_stage_submission(**args)
    assert result['revision'] == revision == args['expected_revision'] + 1
    assert result['preflight_consumed'] is True


def test_partial_write_without_checkpoint_blocks_blind_retry_and_other_mutation(tmp_path, monkeypatch):
    service, pid, payload, args = prepared(tmp_path)

    def partial(*a):
        (tmp_path / pid / '02-semantic-recognition/partial.yaml').write_text('partial: true')
        raise SystemExit()

    monkeypatch.setattr(service, '_execute_preflight_payload', partial)
    with pytest.raises(SystemExit):
        service.commit_preflight_stage_submission(**args)
    fresh = OntologyWorkflowService(tmp_path)
    with pytest.raises(WorkflowError, match='部分写入'):
        fresh.commit_preflight_stage_submission(**args)
    with pytest.raises(WorkflowError, match='未完成预检操作'):
        fresh.record_semantic_candidates(project_id=pid, **payload)


def test_exit_before_any_formal_write_can_retry_same_operation(tmp_path, monkeypatch):
    service, pid, _, args = prepared(tmp_path)
    monkeypatch.setattr(service, '_execute_preflight_payload', lambda *a: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        service.commit_preflight_stage_submission(**args)
    fresh = OntologyWorkflowService(tmp_path)
    result = fresh.commit_preflight_stage_submission(**args)
    assert result['revision'] == args['expected_revision'] + 1


def test_changed_checkpoint_is_not_recognized_by_passed_stage_alone(tmp_path, monkeypatch):
    service, pid, _, args = prepared(tmp_path)
    execute = service._execute_preflight_payload

    def crash(*a):
        execute(*a)
        raise SystemExit()

    monkeypatch.setattr(service, '_execute_preflight_payload', crash)
    with pytest.raises(SystemExit):
        service.commit_preflight_stage_submission(**args)
    (tmp_path / pid / '02-semantic-recognition/ontology-candidates.yaml').write_text('tampered: true')
    with pytest.raises(WorkflowError, match='检查点不一致'):
        OntologyWorkflowService(tmp_path).commit_preflight_stage_submission(**args)


def test_expired_success_is_readable_but_wrong_original_revision_is_not(tmp_path):
    service, pid, _, args = prepared(tmp_path)
    result = service.commit_preflight_stage_submission(**args)
    path = next((tmp_path / pid / '.preflight-submissions').glob('*.json'))
    receipt = json.loads(path.read_text())
    receipt['expires_at'] = '2000-01-01T00:00:00+00:00'
    service._write_json(path, receipt)
    assert service.commit_preflight_stage_submission(**args) == result
    with pytest.raises(WorkflowError, match='原提交参数'):
        service.commit_preflight_stage_submission(**{**args, 'expected_revision': 999})


def test_corrupt_operation_blocks_writes(tmp_path):
    service, pid, payload, args = prepared(tmp_path)
    service.commit_preflight_stage_submission(**args)
    path = next((tmp_path / pid / '.preflight-operations').glob('*.json'))
    record = json.loads(path.read_text())
    record['result']['revision'] = 999
    path.write_text(json.dumps(record))
    with pytest.raises(WorkflowError, match='记录损坏'):
        service.record_semantic_candidates(project_id=pid, **payload)


def test_preflight_id_cannot_escape_receipt_directory(tmp_path):
    service, _, _, args = prepared(tmp_path)
    with pytest.raises(WorkflowError, match='格式无效'):
        service.commit_preflight_stage_submission(**{**args, 'preflight_token': '../outside.secret'})


def test_committed_manifest_does_not_include_mutable_operation_receipts(tmp_path):
    service, pid, _, args = prepared(tmp_path)
    service.commit_preflight_stage_submission(**args)
    manifest = json.loads((tmp_path / pid / 'artifact-manifest.json').read_text())
    for item in manifest['files']:
        assert not item['path'].startswith(('.preflight-submissions/', '.preflight-operations/'))
        actual = 'sha256:' + hashlib.sha256((tmp_path / pid / item['path']).read_bytes()).hexdigest()
        assert item['sha256'] == actual, item['path']


def test_real_child_process_exit_after_commit_recovers_without_second_revision(tmp_path):
    _, pid, _, args = prepared(tmp_path)
    code = '''
import json, os, sys
from services.ontology_engineering import OntologyWorkflowService
service = OntologyWorkflowService(sys.argv[1])
execute = service._execute_preflight_payload
def interrupted(*args):
    execute(*args)
    os._exit(43)
service._execute_preflight_payload = interrupted
service.commit_preflight_stage_submission(**json.loads(sys.argv[2]))
'''
    process = subprocess.run([sys.executable, '-c', code, str(tmp_path), json.dumps(args)],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 43, process.stderr
    fresh = OntologyWorkflowService(tmp_path)
    result = fresh.commit_preflight_stage_submission(**args)
    assert result['revision'] == args['expected_revision'] + 1
    assert fresh.get_status(pid)['revision'] == result['revision']


def test_pending_operation_directive_preserves_scope_and_user_pause(tmp_path, monkeypatch):
    service, pid, _, args = prepared(tmp_path)
    monkeypatch.setattr(service, '_execute_preflight_payload', lambda *a: (_ for _ in ()).throw(SystemExit()))
    with pytest.raises(SystemExit):
        service.commit_preflight_stage_submission(**args)
    directive = service.get_next_action(pid)
    assert directive['action'] == 'RECOVER_PREFLIGHT_OPERATION'
    assert directive['allowed_write_tools'] == ['commit_preflight_stage_submission']
    assert directive['execution_policy']['must_check_user_pause'] is True
    assert directive['execution_policy']['stop_on_reconciliation_required'] is True
    state_path = tmp_path / pid / 'workflow-state.json'
    state = json.loads(state_path.read_text())
    state['paused'] = True
    service._write_json(state_path, state)
    directive = service.get_next_action(pid)
    assert directive['allowed_write_tools'] == []
    assert directive['execution_policy']['mode'] == 'READ_ONLY_NO_ACTION'


def test_real_gate_failure_is_replayed_as_failure_and_allows_formal_repair(tmp_path, monkeypatch):
    service, pid, _, args = prepared(tmp_path)

    def reject(*a, **kw):
        raise WorkflowGateError('G-ISOLATED-FAILURE', 'isolated validation failure')

    monkeypatch.setattr(service, '_validate_s2', reject)
    with pytest.raises(WorkflowGateError, match='isolated validation failure'):
        service.commit_preflight_stage_submission(**args)
    fresh = OntologyWorkflowService(tmp_path)
    with pytest.raises(WorkflowGateError, match='isolated validation failure'):
        fresh.commit_preflight_stage_submission(**args)
    assert fresh.get_status(pid)['stage_statuses']['S2'] == 'FAILED'
    repaired = fresh.retry_failed_stage(project_id=pid)
    assert repaired['stage_statuses']['S2'] == 'RUNNING'
