#!/usr/bin/env python3
"""Read a whitelisted runtime selection; never evaluate config as shell code."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / '.orion-semantica' / 'active-runtime.json'
PATH_FIELDS = {'SEMANTICA_CLI','SEMANTICA_MCP_COMMAND','SEMANTICA_PYTHON','SEMANTICA_GRAPH_PATH','SEMANTICA_STATE_DIR',
               'SEMANTICA_EXPLORER_STATE_DIR','SEMANTICA_EXPLORER_LEGACY_GRAPH','ORION_EXPLORATION_INDEX_ROOT'}
FIELDS = PATH_FIELDS | {'SEMANTICA_RUNTIME_MODE','SEMANTICA_RUNTIME_OWNER','SEMANTICA_PORT','SEMANTICA_AUTO_SYNC'}


def load_config(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding='utf-8'))
    if set(raw) != {'schema_version','environment'} or raw['schema_version'] != 1:
        raise ValueError('runtime config must contain schema_version=1 and environment')
    values = raw['environment']
    if not isinstance(values, dict) or set(values) - FIELDS:
        raise ValueError('runtime config contains unsupported environment fields')
    for key, value in values.items():
        if not isinstance(value, str) or not value or any(c in value for c in '\n\r\x00`$"\\'):
            raise ValueError('invalid runtime config value for ' + key)
        if key in PATH_FIELDS and not Path(value).is_absolute():
            raise ValueError(key + ' must be an absolute path')
    if values.get('SEMANTICA_RUNTIME_MODE','shared') not in {'shared','managed'}:
        raise ValueError('invalid runtime mode')
    if values.get('SEMANTICA_AUTO_SYNC','false') not in {'false','true'}:
        raise ValueError('invalid auto-sync value')
    if 'SEMANTICA_PORT' in values and not (values['SEMANTICA_PORT'].isdigit() and 1 <= int(values['SEMANTICA_PORT']) <= 65535):
        raise ValueError('invalid port')
    if 'SEMANTICA_RUNTIME_OWNER' in values and not re.fullmatch(r'[A-Za-z0-9_.-]+', values['SEMANTICA_RUNTIME_OWNER']):
        raise ValueError('invalid runtime owner')
    if 'SEMANTICA_EXPLORER_STATE_DIR' in values:
        directory = Path(values['SEMANTICA_EXPLORER_STATE_DIR']).resolve()
        for key in ('SEMANTICA_GRAPH_PATH','SEMANTICA_EXPLORER_LEGACY_GRAPH'):
            if key not in values or not Path(values[key]).resolve().is_relative_to(directory):
                raise ValueError('persistent runtime graph must be a copy inside its state directory')
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path(os.environ.get('SEMANTICA_ACTIVE_RUNTIME_CONFIG', DEFAULT_CONFIG)))
    sub = parser.add_subparsers(dest='action', required=True)
    get = sub.add_parser('get')
    get.add_argument('field', choices=sorted(FIELDS))
    get.add_argument('--default', default='')
    sub.add_parser('check')
    execute = sub.add_parser('exec')
    execute.add_argument('script',type=Path)
    execute.add_argument('arguments',nargs=argparse.REMAINDER)
    args = parser.parse_args()
    values = load_config(args.config)
    if args.action == 'get':
        print(values.get(args.field,args.default))
        return 0
    if args.action == 'check':
        print(json.dumps({'configured':bool(values),'config_path':str(args.config),'fields':sorted(values)},ensure_ascii=False))
        return 0
    expected = ROOT / 'scripts/ensure_semantica_runtime.sh'
    if args.script.resolve() != expected:
        raise ValueError('runtime config can only execute the managed Semantica launcher')
    environment = dict(os.environ, **values)
    environment['SEMANTICA_RUNTIME_CONFIG_LOADED'] = '1'
    lock_path = args.config.parent / 'runtime-transition.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+') as lock:
        # Supervisor health canaries and stage jobs share this lock; wait a bounded
        # time instead of failing immediately on a routine overlap.
        # Read-only checks stay short so supervisor canaries never stall.
        default_wait = '5' if args.arguments[:1] == ['check'] else '90'
        wait_seconds = float(os.environ.get('SEMANTICA_RUNTIME_LOCK_WAIT_SECONDS', default_wait))
        deadline = time.monotonic() + max(wait_seconds, 0.0)
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print('Semantica runtime transition is in progress; no process was started.', file=sys.stderr)
                    return 75
                time.sleep(0.5)
        return subprocess.run([str(expected),*args.arguments], env=environment, check=False).returncode


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError,OSError,json.JSONDecodeError) as exc:
        print('Semantica runtime config rejected: '+str(exc),file=sys.stderr)
        raise SystemExit(2) from exc
