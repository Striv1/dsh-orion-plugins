"""Local trusted operator/native-host bridge. Never registered as an auto-approval tool."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from services.ontology_engineering.engineering_tasks import EngineeringTasks, TaskConflict

CONTROLLER_WAIT_SECONDS = 8


def pause_session(home: Path, session_id: str, event_time_ms: float) -> dict:
    directory = home / '.engineering-control'
    if not directory.is_dir():
        return {'status': 'NOT_OPTED_IN', 'projects': []}
    if not math.isfinite(event_time_ms) or event_time_ms <= 0:
        raise ValueError('Invalid native event time')
    paused = []
    for path in sorted(directory.glob('*/tasks.json')):
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError('Unsafe task authority path')
        tasks = EngineeringTasks(path.parent)
        project = path.parent.name
        control = tasks.project_status(project)
        if (not control or control['session_id'] != session_id or not control['enabled']
                or event_time_ms / 1000 < control['authorized_at']):
            continue
        tasks.pause_project(project, session_id=session_id, actor='HARNESS_NATIVE_USER_STOP',
                            reason='Native user cancellation; explicit resume required',
                            event_time=event_time_ms / 1000)
        paused.append(project)
    return {'status': 'PAUSED' if paused else 'NO_MATCHING_ACTIVE_CONTROL', 'projects': paused}




def stable_task_control_summary(state: dict, project_id: str) -> list[dict]:
    """Only durable coordination decisions, excluding volatile lease heartbeats."""
    fields = ('task_id', 'project', 'stage', 'input_fingerprint', 'status',
              'generation', 'stop_epoch', 'project_epoch', 'stage_execution_id',
              'receipt_reference', 'reconciliation_reference')
    return [{key: task.get(key) for key in fields}
            for _, task in sorted(state.get('tasks', {}).items())
            if task.get('project') == project_id]

def _start_controller_once(home: Path, project_id: str, session_id: str,
                     semantica_url: str | None = None) -> dict:
    """Start a fixed host controller only for the already-authorized session."""
    from scripts.managed_stage_bridge import _atomic_receipt
    from scripts.run_engineering_controller import ROOT, process_identity

    home = Path(home).resolve()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', project_id or ''):
        raise ValueError('Invalid project identity')
    directory = home / '.engineering-control' / project_id
    if not (directory / 'tasks.json').is_file():
        return {'status': 'NOT_OPTED_IN', 'started': False}
    if (directory.is_symlink() or (directory / 'tasks.json').is_symlink()
            or not directory.resolve().is_relative_to(home)):
        raise ValueError('Unsafe task authority path')
    tasks = EngineeringTasks(directory)
    lock_path, receipt_path = directory / 'controller-start.lock', directory / 'controller-start.json'
    if lock_path.is_symlink() or receipt_path.is_symlink():
        raise ValueError('Unsafe controller launch state')
    with lock_path.open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tasks._transaction(write=False, blocking=False) as state:
            control = state.get('controls', {}).get(project_id)
            if not control or not control.get('enabled'):
                return {'status': 'PAUSED', 'started': False}
            if control.get('session_id') != session_id:
                return {'status': 'SESSION_MISMATCH', 'started': False}
            try:
                tasks._check_native_stop(control)
            except TaskConflict:
                return {'status': 'NATIVE_STOP_PENDING', 'started': False}
            workflow_path = home / project_id / 'workflow-state.json'
            if (workflow_path.is_symlink() or not workflow_path.is_file()
                    or not workflow_path.resolve().is_relative_to(home)):
                raise ValueError('Existing project required')
            fingerprint = hashlib.sha256(workflow_path.read_bytes() + json.dumps(
                {'epoch': control['epoch'], 'session': session_id, 'semantica_url': semantica_url,
                 'task_control': stable_task_control_summary(state, project_id)},
                sort_keys=True).encode()).hexdigest()
            previous = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
            pid, identity = previous.get('pid'), previous.get('identity')
            if isinstance(pid, int) and identity and process_identity(pid) == identity:
                if (previous.get('authorization_epoch') != control['epoch']
                        or previous.get('session_id') != session_id):
                    return {'status': 'STALE_CONTROLLER_ACTIVE', 'started': False, 'pid': pid}
                return {'status': 'ALREADY_RUNNING', 'started': False, 'pid': pid}
            if previous.get('launch_fingerprint') == fingerprint:
                controller_path = directory / 'controller.json'
                if controller_path.is_symlink():
                    raise ValueError('Unsafe controller state')
                controller_state = json.loads(controller_path.read_text()) if controller_path.exists() else {}
                terminal = {'WAITING_HUMAN', 'WAITING_WORKFLOW', 'OUTSIDE_AUTHORIZED_STAGES',
                            'NEEDS_RECONCILIATION', 'RUNNER_FAILED', 'ACTION_COMPLETED', 'PAUSED'}
                if (controller_state.get('controller_pid') == pid
                        and controller_state.get('status') in terminal):
                    return {'status': 'OBSERVATION_UNCHANGED', 'started': False}
                # The old controller vanished before a durable terminal result.
                # Restart the controller, not the stage: it must reconcile the
                # original task/commit receipt before any runner may launch.
            command = [sys.executable, '-m', 'scripts.run_engineering_controller',
                       '--workflow-home', str(home), '--project', project_id, '--watch']
            if semantica_url:
                from urllib.parse import urlsplit
                url = urlsplit(semantica_url)
                if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
                    raise ValueError('Invalid trusted Semantica URL')
                command += ['--semantica-url', semantica_url]
            log_path = directory / 'controller-host.log'
            if log_path.is_symlink():
                raise ValueError('Unsafe controller launch log')
            with log_path.open('a') as log:
                os.chmod(log_path, 0o600)
                process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            identity = process_identity(process.pid)
            record = {'status': 'STARTED', 'started': True, 'pid': process.pid, 'identity': identity,
                      'project_id': project_id, 'session_id': session_id,
                      'authorization_epoch': control['epoch'], 'launch_fingerprint': fingerprint}
            _atomic_receipt(receipt_path, record)
            return record



def start_controller(home: Path, project_id: str, session_id: str,
                     semantica_url: str | None = None) -> dict:
    # Each attempt releases both launcher and task locks before waiting. The
    # previous controller must be able to observe its invalid epoch and stop.
    # Re-read authorization/session/native cancellation on every new attempt.
    deadline = time.monotonic() + CONTROLLER_WAIT_SECONDS
    while True:
        try:
            result = _start_controller_once(home, project_id, session_id, semantica_url)
        except BlockingIOError:
            result = {'status': 'CONTROL_STATE_BUSY', 'started': False}
        if result['status'] not in {'STALE_CONTROLLER_ACTIVE', 'CONTROL_STATE_BUSY'} or time.monotonic() >= deadline:
            return result
        time.sleep(0.1)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['pause-session', 'status', 'authorize', 'start-controller'])
    parser.add_argument('--workflow-home', type=Path, required=True)
    parser.add_argument('--project-id')
    parser.add_argument('--session-id')
    parser.add_argument('--event-time-ms', type=float)
    parser.add_argument('--stages', nargs='+')
    parser.add_argument('--authorization-ref')
    parser.add_argument('--actor')
    parser.add_argument('--reason')
    parser.add_argument('--semantica-url')
    args = parser.parse_args()
    home = args.workflow_home.resolve()
    if args.action == 'pause-session':
        if not args.session_id or args.event_time_ms is None:
            parser.error('native session and event time required')
        result = pause_session(home, args.session_id, args.event_time_ms)
    elif args.action == 'start-controller':
        if not args.project_id or not args.session_id:
            parser.error('existing project and authorized session required')
        result = start_controller(home, args.project_id, args.session_id, args.semantica_url)
    else:
        project = home / (args.project_id or '')
        if (not args.project_id or project.parent != home
                or not (project / 'workflow-state.json').is_file()):
            parser.error('existing project required')
        directory = home / '.engineering-control' / args.project_id
        if args.action == 'status' and not (directory / 'tasks.json').is_file():
            result = {'status': 'NOT_OPTED_IN'}
        else:
            tasks = EngineeringTasks(directory)
            result = tasks.project_status(args.project_id) if args.action == 'status' else tasks.authorize_project(
                args.project_id, allowed_stages=args.stages, session_id=args.session_id,
                authorization_ref=args.authorization_ref, actor=args.actor, reason=args.reason)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
