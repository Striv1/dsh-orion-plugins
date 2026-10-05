"""Run legacy S6 candidate imports in an ephemeral copy of the active runtime."""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

from scripts.semantica_runtime_config import DEFAULT_CONFIG, load_config


def active_validation_commands(command: str | None = None) -> tuple[str, str]:
    values = load_config(Path(os.environ.get('SEMANTICA_ACTIVE_RUNTIME_CONFIG', DEFAULT_CONFIG)))
    python = values.get('SEMANTICA_PYTHON', '')
    mcp = values.get('SEMANTICA_MCP_COMMAND', '')
    if not python or not mcp or not Path(python).is_file() or not Path(mcp).is_file():
        raise RuntimeError('S6 requires the configured active Semantica Python and MCP; no legacy fallback is allowed.')
    if command and Path(command).absolute() != Path(mcp).absolute():
        raise ValueError('S6 MCP override must match the active runtime; switching to legacy code is not allowed.')
    return python, mcp


@contextmanager
def isolated_validation_runtime(command: str | None = None):
    python, mcp = active_validation_commands(command)
    with tempfile.TemporaryDirectory(prefix='orion-s6-semantica-') as directory:
        root = Path(directory)
        seed = root / 'empty.json'
        seed.write_text('{"nodes": [], "edges": []}', encoding='utf-8')
        with socket.socket() as socket_handle:
            socket_handle.bind(('127.0.0.1', 0))
            port = socket_handle.getsockname()[1]
        base_url = f'http://127.0.0.1:{port}'
        key = secrets.token_urlsafe(32)
        # Inherit OS basics only: no persistent production graph, Python path,
        # provider credentials, registrations or shared-state configuration.
        environment = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG', 'TMPDIR') if k in os.environ}
        environment.update(SEMANTICA_API_KEY=key, SEMANTICA_API_URL=base_url)
        with (root / 'server.log').open('w+') as log:
            process = subprocess.Popen([python, '-m', 'semantica.explorer', '--graph', str(seed),
                                        '--port', str(port), '--host', '127.0.0.1', '--no-browser'],
                                       env=environment, cwd=root, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 90
                with httpx.Client(timeout=2, trust_env=False, headers={'X-API-Key': key}) as client:
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError('Isolated Semantica validation runtime exited before readiness.')
                        try:
                            response = client.get(base_url + '/api/graph/stats')
                            response.raise_for_status()
                            stats = response.json()
                            if stats.get('node_count') != 0 or stats.get('edge_count') != 0:
                                raise RuntimeError('Isolated validation runtime did not start with an empty graph.')
                            break
                        except httpx.HTTPError:
                            time.sleep(.2)
                    else:
                        raise RuntimeError('Isolated Semantica validation runtime readiness timed out.')
                yield {'base_url': base_url, 'api_key': key, 'command': mcp, 'environment': environment}
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
