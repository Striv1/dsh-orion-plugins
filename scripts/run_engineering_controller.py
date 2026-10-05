#!/usr/bin/env python3
"""Explicit project opt-in host scheduler for the existing S5/S6 runners only."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.managed_stage_bridge import (  # noqa: E402
    _atomic_receipt,
    opted_in_tasks,
    recover_managed_commit,
)
from services.ontology_engineering import OntologyWorkflowService  # noqa: E402
from services.ontology_engineering.engineering_tasks import TaskConflict  # noqa: E402
from services.ontology_engineering.workflow import WorkflowError  # noqa: E402

RUNNERS = {
    'S5': ('BUILD_ONTOLOGY_WITH_PROTEGE_AND_VERIFY', 'scripts.run_protege_build_stage'),
    'S6': ('RUN_FULL_QUALITY_AND_REASONING_VALIDATION', 'scripts.run_quality_validation_stage'),
}
UNRESOLVED = {'RUNNING', 'NEEDS_RECONCILIATION', 'PAUSED'}


def process_identity(pid: int) -> str | None:
    """PID plus OS creation time and fixed argv; never trust a bare persisted PID."""
    result = subprocess.run(['ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                            capture_output=True, text=True, timeout=5, check=False)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _terminate_owned(pid: int, identity: str | None) -> bool:
    if not identity or process_identity(pid) != identity:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def _control(tasks, project, epoch=None):
    control = tasks.project_status(project)
    if not control or not control.get('enabled'):
        raise TaskConflict('Project has no enabled controller opt-in')
    tasks._check_native_stop(control)
    if epoch is not None and control['epoch'] != epoch:
        raise TaskConflict('Project authorization changed during execution')
    return control


def _tasks(tasks, project):
    with tasks._transaction(write=False) as state:
        return [dict(task) for task in state['tasks'].values() if task['project'] == project]



def _task_versions(tasks, project):
    fields = ('status', 'generation', 'stop_epoch', 'stage_execution_id')
    return {task['task_id']: {key: task.get(key) for key in fields}
            for task in _tasks(tasks, project)}


def _runner_outcome_proven(tasks, project, stage, baseline, *, allow_reconciled_retry=False):
    if not isinstance(baseline, dict):
        return False
    for task in _tasks(tasks, project):
        if task['stage'] != stage:
            continue
        prior = baseline.get(task['task_id'], {})
        if (task['status'] == 'RECEIPT_RECORDED'
                and (task.get('generation') != prior.get('generation')
                     or task.get('stage_execution_id') != prior.get('stage_execution_id'))):
            return True
        if (allow_reconciled_retry and task['status'] == 'READY'
                and task.get('reconciliation_reference')
                and task.get('stop_epoch', 0) > prior.get('stop_epoch', 0)):
            return True
    return False

def _recover(service, tasks, project):
    for task in _tasks(tasks, project):
        if task['status'] not in UNRESOLVED:
            continue
        if task['status'] == 'RUNNING' and task.get('lease_until', 0) > time.time():
            raise TaskConflict('A managed runner still owns an unexpired lease')
        recover_managed_commit(service, tasks, task['task_id'], actor='ORION_HOST_CONTROLLER',
                               reason='Host restart verifies original formal commit; never repeats effects')


def _command(project, workflow_home, stage, semantica_url):
    command = [sys.executable, '-m', RUNNERS[stage][1], project, '--workflow-home', str(workflow_home)]
    if stage == 'S6' and semantica_url:
        command += ['--semantica-api-url', semantica_url]
    return command


def run_controller(*, workflow_home: Path, project: str, semantica_url: str | None = None,
                   watch: bool = False) -> dict:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', project):
        raise ValueError('Invalid project identity')
    workflow_home = Path(workflow_home).resolve()
    if semantica_url:
        url = urlsplit(semantica_url)
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('Semantica URL must be a trusted HTTP base URL')
    tasks = opted_in_tasks(workflow_home, project)
    if tasks is None:
        return {'status': 'NOT_OPTED_IN', 'project_id': project, 'executed': False}
    directory = tasks.directory
    path = directory / 'controller.json'
    if path.is_symlink() or (directory / 'controller.lock').is_symlink():
        raise ValueError('Controller state must not be a symlink')
    with (directory / 'controller.lock').open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'OWNER_ACTIVE', 'project_id': project, 'executed': False}
        previous = json.loads(path.read_text()) if path.exists() else {}
        record = {**{key: previous.get(key) for key in ('child_pid', 'child_identity', 'current_stage', 'task_baseline')},
                  'schema_version': 1, 'project_id': project, 'controller_pid': os.getpid(),
                  'status': 'STARTING', 'executed': False}

        def save(status, **details):
            record.update(status=status, **details)
            _atomic_receipt(path, record)
            return dict(record)

        service = OntologyWorkflowService(workflow_home)
        try:
            control = _control(tasks, project)
        except TaskConflict:
            pid = previous.get('child_pid')
            stopped = _terminate_owned(pid, previous.get('child_identity')) if isinstance(pid, int) else False
            return save('PAUSED', owned_child_sigterm=stopped)
        epoch = control['epoch']
        record.update(authorization_epoch=epoch, session_id=control['session_id'])
        # A crash may leave a child alive. Observe its verified identity, never
        # launch a second runner or infer completion from stage PASSED alone.
        pid, identity = previous.get('child_pid'), previous.get('child_identity')
        prior_runner_unresolved = isinstance(pid, int) and previous.get('status') in {
            'RUNNING', 'RUNNER_ACTIVE', 'OBSERVING_OWNED_RUNNER', 'INTERRUPTED_NEEDS_RECONCILIATION', 'NEEDS_RECONCILIATION'}
        if isinstance(pid, int) and identity and process_identity(pid) == identity:
            if previous.get('authorization_epoch') != epoch:
                return save('PAUSED', owned_child_sigterm=_terminate_owned(pid, identity))
            if not watch:
                return save('RUNNER_ACTIVE', child_pid=pid, child_identity=identity)
            save('OBSERVING_OWNED_RUNNER', child_pid=pid, child_identity=identity)
            try:
                while process_identity(pid) == identity:
                    try:
                        _control(tasks, project, epoch)
                    except TaskConflict:
                        return save('PAUSED', owned_child_sigterm=_terminate_owned(pid, identity))
                    time.sleep(0.5)
            except BaseException:
                _terminate_owned(pid, identity)
                save('INTERRUPTED_NEEDS_RECONCILIATION')
                raise
        while True:
            try:
                _control(tasks, project, epoch)
            except TaskConflict:
                return save('PAUSED', child_pid=None, child_identity=None)
            try:
                _recover(service, tasks, project)
            except (TaskConflict, WorkflowError, ValueError, OSError) as exc:
                return save('NEEDS_RECONCILIATION', reason=str(exc), child_pid=None, child_identity=None)
            if prior_runner_unresolved:
                if not _runner_outcome_proven(tasks, project, previous.get('current_stage'),
                                              previous.get('task_baseline'), allow_reconciled_retry=True):
                    return save('NEEDS_RECONCILIATION', reason='Previous runner exited without attributable task evidence; no automatic retry')
                prior_runner_unresolved = False
            try:
                directive = service.get_next_action(project)
            except (WorkflowError, ValueError, OSError) as exc:
                return save('NEEDS_RECONCILIATION', reason=str(exc))
            stage, action = directive.get('current_stage'), directive.get('action')
            record.update(current_stage=stage, revision=directive.get('revision'), action=action)
            if stage in {'S4', 'S7'}:
                return save('WAITING_HUMAN', child_pid=None, child_identity=None)
            if (stage not in RUNNERS or action != RUNNERS[stage][0]
                    or not directive.get('allowed_write_tools')
                    or directive.get('stage_status') != 'RUNNING'):
                return save('WAITING_WORKFLOW', child_pid=None, child_identity=None)
            if stage not in control['allowed_stages']:
                return save('OUTSIDE_AUTHORIZED_STAGES', child_pid=None, child_identity=None)
            # Bind the actual process identity before monitoring cancellation.
            log_path = directory / f'controller-{stage}.log'
            if log_path.is_symlink():
                raise ValueError('Controller log must not be a symlink')
            task_baseline = _task_versions(tasks, project)
            child = None
            identity = None
            try:
                with log_path.open('a') as log:
                    os.chmod(log_path, 0o600)
                    child = subprocess.Popen(_command(project, workflow_home, stage, semantica_url),
                                             cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                                             stdin=subprocess.DEVNULL, start_new_session=True)
                identity = process_identity(child.pid)
                save('RUNNING', executed=True, child_pid=child.pid, child_identity=identity,
                     log_path=str(log_path), task_baseline=task_baseline)
                while child.poll() is None:
                    try:
                        _control(tasks, project, epoch)
                    except TaskConflict:
                        stopped = _terminate_owned(child.pid, identity)
                        with suppress(subprocess.TimeoutExpired):
                            child.wait(timeout=10)
                        return save('PAUSED', owned_child_sigterm=stopped)
                    time.sleep(0.5)
            except BaseException:
                if child is not None:
                    if identity:
                        _terminate_owned(child.pid, identity)
                    elif child.poll() is None:
                        # This live Popen object was created in this exact try;
                        # no persisted/unverified PID is used for this cleanup.
                        child.terminate()
                    with suppress(subprocess.TimeoutExpired):
                        child.wait(timeout=5)
                with suppress(OSError):
                    save('INTERRUPTED_NEEDS_RECONCILIATION')
                raise
            save('RUNNER_EXITED', child_exit_code=child.returncode, child_pid=None, child_identity=None)
            try:
                _recover(service, tasks, project)
            except (TaskConflict, WorkflowError, ValueError, OSError) as exc:
                return save('NEEDS_RECONCILIATION', reason=str(exc))
            if child.returncode != 0:
                return save('RUNNER_FAILED')
            try:
                current = service.get_next_action(project)
            except (WorkflowError, ValueError, OSError) as exc:
                return save('NEEDS_RECONCILIATION', reason=str(exc))
            if current.get('revision') == directive.get('revision') or current.get('current_stage') == stage:
                return save('NEEDS_RECONCILIATION', reason='Runner exited without a verified formal stage transition')
            if not _runner_outcome_proven(tasks, project, stage, task_baseline):
                return save('NEEDS_RECONCILIATION', reason='No managed formal receipt proves runner completion')
            if not watch:
                return save('ACTION_COMPLETED', current_stage=current.get('current_stage'), revision=current.get('revision'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workflow-home', type=Path, required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--semantica-url')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--once', action='store_true')
    mode.add_argument('--watch', action='store_true')
    args = parser.parse_args()
    def interrupted(_signal, _frame):
        raise SystemExit('Controller interrupted; owned child cleanup follows')
    signal.signal(signal.SIGTERM, interrupted)
    result = run_controller(workflow_home=args.workflow_home, project=args.project,
                            semantica_url=args.semantica_url, watch=args.watch)
    print(json.dumps(result, ensure_ascii=False))
    return 1 if result['status'] in {'NEEDS_RECONCILIATION', 'RUNNER_FAILED'} else 0


if __name__ == '__main__':
    raise SystemExit(main())
