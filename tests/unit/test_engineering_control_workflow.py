import json
import time

import pytest

from scripts.engineering_control import pause_session
from services.ontology_engineering.engineering_tasks import EngineeringTasks, TaskConflict
from tests.unit.test_preflight_operation_recovery import prepared


def control(service, pid):
    tasks = EngineeringTasks(service.root / '.engineering-control' / pid)
    tasks.authorize_project(pid, allowed_stages=['S2', 'S3'], session_id='test-session',
                            authorization_ref='explicit-test-grant', actor='test', reason='isolated test')
    return tasks


def test_native_session_pause_blocks_formal_commit_and_survives_new_service(tmp_path):
    service, pid, _, args = prepared(tmp_path)
    control(service, pid)
    state = (tmp_path / pid / 'workflow-state.json').read_bytes()
    assert pause_session(tmp_path, 'another-session', time.time()*1000)['projects'] == []
    assert pause_session(tmp_path, 'test-session', time.time()*1000)['projects'] == [pid]
    from services.ontology_engineering import OntologyWorkflowService
    fresh = OntologyWorkflowService(tmp_path)
    with pytest.raises(TaskConflict, match='paused'):
        fresh.commit_preflight_stage_submission(**args)
    assert (tmp_path / pid / 'workflow-state.json').read_bytes() == state


def test_direct_mcp_cannot_bypass_managed_owner(tmp_path):
    service, pid, _, args = prepared(tmp_path)
    tasks = control(service, pid)
    task = tasks.ensure(pid, 'S2', 'test-input')
    token = tasks.claim(task['task_id'], 'worker', session_id='test-session',
                        authorization_ref='explicit-test-grant')
    with pytest.raises(TaskConflict, match='owner token'):
        service.commit_preflight_stage_submission(**args)
    with service.managed_execution(tasks, token), tasks.guard(token):
        result = service.commit_preflight_stage_submission(**args)
    assert result['stage_statuses']['S2'] == 'PASSED'


def test_durable_cancel_journal_blocks_before_async_control_command(tmp_path):
    service, pid, _, args = prepared(tmp_path)
    tasks = control(service, pid)
    journal = tmp_path / '.engineering-control/cancellations/test-session'
    journal.mkdir(parents=True)
    event_time = time.time()*1000
    (journal / f'{event_time}.json').write_text(json.dumps({
        'session_id': 'test-session', 'event_time_ms': event_time,
        'source': 'HARNESS_NATIVE_USER_STOP'}))
    assert tasks.project_status(pid)['enabled'] is True
    with pytest.raises(TaskConflict):
        service.commit_preflight_stage_submission(**args)
