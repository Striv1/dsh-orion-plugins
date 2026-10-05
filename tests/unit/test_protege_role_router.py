from __future__ import annotations

import json
import os
import plistlib
from pathlib import Path

import pytest

from scripts import protege_role_router as router


def fake_application(tmp_path: Path) -> Path:
    application = tmp_path / "Protégé-构建版.app"
    contents = application / "Contents"
    plugins = contents / "plugins"
    plugins.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "edu.stanford.protege.zh.full",
                "CFBundleShortVersionString": "5.6.9",
            },
            handle,
        )
    (plugins / "protege-mcp-0.8.1.jar").write_bytes(b"verified-plugin")
    return application


def test_bind_and_resolve_construction_role(monkeypatch, tmp_path: Path) -> None:
    application = fake_application(tmp_path)
    routing_file = tmp_path / "routing.json"
    instances = [{"id": "window-full", "title": "Protege window 2"}]
    monkeypatch.setattr(
        router,
        "discover_broker",
        lambda broker, secret: (broker, {"instances": instances}),
    )

    route = router.bind_role(
        role="construction",
        instance_id="window-full",
        application=application,
        broker="http://127.0.0.1:8123",
        secret="secret",
        routing_file=routing_file,
    )

    assert route["bundle_id"] == "edu.stanford.protege.zh.full"
    assert route["application_name"] == "Protégé-构建版"
    assert route["mcp_url"].endswith("/instances/window-full/mcp?v=2")
    assert router.resolve_role(
        role="construction",
        broker="http://127.0.0.1:8123",
        secret="secret",
        routing_file=routing_file,
    ) == route


def test_resolve_rejects_closed_role_window(monkeypatch, tmp_path: Path) -> None:
    application = fake_application(tmp_path)
    routing_file = tmp_path / "routing.json"
    state = {"instances": [{"id": "window-full"}]}
    monkeypatch.setattr(
        router,
        "discover_broker",
        lambda broker, secret: (broker, state),
    )
    router.bind_role(
        role="construction",
        instance_id="window-full",
        application=application,
        broker="http://127.0.0.1:8123",
        secret="secret",
        routing_file=routing_file,
    )
    state["instances"] = []

    with pytest.raises(router.ProtegeRoutingError, match="已经关闭"):
        router.resolve_role(
            role="construction",
            broker="http://127.0.0.1:8123",
            secret="secret",
            routing_file=routing_file,
        )


def test_ensure_reuses_live_matching_route(monkeypatch, tmp_path: Path) -> None:
    application = fake_application(tmp_path)
    routing_file = tmp_path / "routing.json"
    monkeypatch.setattr(
        router,
        "discover_broker",
        lambda broker, secret: (broker, {"instances": [{"id": "window-full"}]}),
    )
    expected = router.bind_role(
        role="construction",
        instance_id="window-full",
        application=application,
        broker="http://127.0.0.1:8123",
        secret="secret",
        routing_file=routing_file,
    )
    monkeypatch.setattr(
        router.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("live route must not relaunch Protégé"),
    )

    actual = router.ensure_role(
        role="construction",
        application=application,
        broker="http://127.0.0.1:8123",
        secret="secret",
        routing_file=routing_file,
        timeout=1,
    )

    assert actual == expected


def test_discover_broker_uses_advertised_ephemeral_port_when_8123_is_standalone(
    monkeypatch, tmp_path: Path
) -> None:
    state_file = tmp_path / "broker.json"
    state_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "host": "127.0.0.1",
                "port": 57254,
                "version": "0.8.1",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(router, "DEFAULT_BROKER_STATE_FILE", state_file)

    def request_json(url: str, secret: str) -> dict[str, object]:
        assert secret == "secret"
        if url == "http://127.0.0.1:8123/instances":
            raise router.ProtegeRoutingError("HTTP Error 404: Not Found")
        assert url == "http://127.0.0.1:57254/instances"
        return {"instances": [{"id": "window-ephemeral"}]}

    monkeypatch.setattr(router, "_request_json", request_json)

    broker, payload = router.discover_broker("http://127.0.0.1:8123", "secret")

    assert broker == "http://127.0.0.1:57254"
    assert payload["instances"] == [{"id": "window-ephemeral"}]


def test_resolve_refreshes_route_when_broker_moves_to_ephemeral_port(
    monkeypatch, tmp_path: Path
) -> None:
    application = fake_application(tmp_path)
    routing_file = tmp_path / "routing.json"
    routing_file.write_text(
        json.dumps(
            {
                "version": 1,
                "roles": {
                    "construction": {
                        "role": "construction",
                        "instance_id": "window-full",
                        "application": str(application.resolve()),
                        "mcp_url": (
                            "http://127.0.0.1:8123/instances/window-full/mcp?v=2"
                        ),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        router,
        "discover_broker",
        lambda broker, secret: (
            "http://127.0.0.1:57254",
            {"instances": [{"id": "window-full", "title": "window"}]},
        ),
    )

    route = router.resolve_role(
        role="construction",
        broker="http://127.0.0.1:8123",
        secret="secret",
        routing_file=routing_file,
    )

    assert route["broker_url"] == "http://127.0.0.1:57254"
    assert route["mcp_url"].startswith("http://127.0.0.1:57254/instances/")
    persisted = json.loads(routing_file.read_text(encoding="utf-8"))
    assert persisted["roles"]["construction"]["broker_url"].endswith(":57254")

@pytest.mark.parametrize('failure', ['broker timeout', 'window missing'])
def test_live_process_never_relaunched_after_mcp_failure(monkeypatch, tmp_path, failure):
    application = fake_application(tmp_path)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    def unavailable(**kwargs):
        raise router.ProtegeRoutingError(failure)
    monkeypatch.setattr(router, 'resolve_role', unavailable)
    monkeypatch.setattr(router, '_application_running', lambda app: True)
    monkeypatch.setattr(router.subprocess, 'run', lambda *a, **k: pytest.fail('must not launch'))
    for _ in range(3):
        with pytest.raises(router.ProtegeRoutingError, match='不重复启动'):
            router.ensure_role(role='construction', application=application,
                broker='http://localhost', secret='secret', routing_file=tmp_path/'route.json', timeout=0)


def test_launch_timeout_has_persistent_cooldown(monkeypatch, tmp_path):
    application = fake_application(tmp_path)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(router, '_application_running', lambda app: False)
    monkeypatch.setattr(router, 'list_instances', lambda *a: [])
    calls = []
    monkeypatch.setattr(router.subprocess, 'run', lambda args, **k: calls.append(args))
    args = dict(role='construction', application=application, broker='http://localhost',
                secret='secret', routing_file=tmp_path/'route.json', timeout=0)
    with pytest.raises(router.ProtegeRoutingError, match='没有注册'):
        router.ensure_role(**args)
    with pytest.raises(router.ProtegeRoutingError, match='冷却'):
        router.ensure_role(**args)
    assert calls == [['/usr/bin/open', '-a', str(application)]]


def test_process_probe_matches_exact_bundle(monkeypatch, tmp_path):
    from types import SimpleNamespace
    application = fake_application(tmp_path)
    executable = str(application / 'Contents/MacOS/protege')
    monkeypatch.setattr(router.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=executable+'\n'))
    assert router._application_running(application)
    monkeypatch.setattr(router.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=executable+'-other\n'))
    assert not router._application_running(application)


def test_cold_start_registers_and_binds(monkeypatch, tmp_path):
    application = fake_application(tmp_path)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(router, '_application_running', lambda app: False)
    replies = iter([[], [{'id': 'new-window'}]])
    monkeypatch.setattr(router, 'list_instances', lambda *a: next(replies))
    monkeypatch.setattr(router.subprocess, 'run', lambda *a, **k: None)
    monkeypatch.setattr(router.time, 'sleep', lambda *a: None)
    monkeypatch.setattr(router, 'bind_role', lambda **k: {'instance_id': k['instance_id']})
    result = router.ensure_role(role='construction', application=application,
        broker='http://localhost', secret='secret', routing_file=tmp_path/'route.json', timeout=1)
    assert result['instance_id'] == 'new-window'


def test_concurrent_launch_is_rejected(monkeypatch, tmp_path):
    application = fake_application(tmp_path)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    key = router.hashlib.sha256(str(application.resolve()).encode()).hexdigest()[:20]
    directory = tmp_path/'.protege-mcp'/'launch-locks'
    directory.mkdir(parents=True)
    with (directory/f'{key}.lock').open('a+') as handle:
        router.fcntl.flock(handle.fileno(), router.fcntl.LOCK_EX | router.fcntl.LOCK_NB)
        with pytest.raises(router.ProtegeRoutingError, match='正在恢复'):
            router.ensure_role(role='construction', application=application,
                broker='http://localhost', secret='secret', routing_file=tmp_path/'route.json', timeout=0)


def test_process_inspection_failure_does_not_mean_exit(monkeypatch, tmp_path):
    def failed(*a, **k):
        raise router.subprocess.TimeoutExpired('ps', 5)
    monkeypatch.setattr(router.subprocess, 'run', failed)
    with pytest.raises(router.ProtegeRoutingError, match='暂停自动启动'):
        router._application_running(fake_application(tmp_path))


def test_java_preflight_rejects_missing_modules_before_launch(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    app = fake_application(tmp_path)
    jdk = tmp_path / 'jdk'
    (jdk / 'bin').mkdir(parents=True)
    (jdk / 'bin/java').write_text('#!/bin/sh\nexit 0\n')
    config = tmp_path / '.Protege/conf/jvm.conf'
    config.parent.mkdir(parents=True)
    config.write_text(f'java_home={jdk}\n')
    with pytest.raises(router.ProtegeRoutingError, match='lib/modules'):
        router.validate_java_runtime(app)


def test_java_preflight_executes_configured_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    app = fake_application(tmp_path)
    jdk = tmp_path / 'jdk with spaces'
    (jdk / 'bin').mkdir(parents=True)
    (jdk / 'lib').mkdir()
    java = jdk / 'bin/java'
    java.write_text('#!/bin/sh\n[ "$1" = "-version" ]\n')
    java.chmod(0o755)
    (jdk / 'lib/modules').write_bytes(b'test')
    config = tmp_path / '.Protege/conf/jvm.conf'
    config.parent.mkdir(parents=True)
    config.write_text(f'java_home={jdk}\n')
    router.validate_java_runtime(app)
    java.write_text('#!/bin/sh\nexit 1\n')
    with pytest.raises(router.ProtegeRoutingError, match='自检失败'):
        router.validate_java_runtime(app)


def test_managed_java_recovers_missing_modules_from_verified_archive(monkeypatch, tmp_path):
    import hashlib
    import io
    import tarfile
    root = tmp_path / 'managed'
    home = root / 'jdk-17.0.20+8/Contents/Home'
    (home / 'lib').mkdir(parents=True)
    (root / 'downloads').mkdir()
    archive = root / 'downloads/jdk17.tar.gz'
    content = b'pinned vendor runtime modules'
    with tarfile.open(archive, 'w:gz') as package:
        member = tarfile.TarInfo(router.MANAGED_JAVA_MEMBER)
        member.size = len(content)
        package.addfile(member, io.BytesIO(content))
    monkeypatch.setattr(router, 'MANAGED_JAVA_ROOT', root)
    monkeypatch.setattr(router, 'MANAGED_JAVA_ARCHIVE_SHA256', hashlib.sha256(archive.read_bytes()).hexdigest())
    router._restore_managed_java_modules(home)
    assert (home / 'lib/modules').read_bytes() == content
    archive.write_bytes(b'corrupt')
    router._restore_managed_java_modules(home)  # Healthy JVM does not need archive.
    (home / 'lib/modules').unlink()
    with pytest.raises(router.ProtegeRoutingError, match='校验失败'):
        router._restore_managed_java_modules(home)
    assert not (home / 'lib/modules').exists()


def test_java_repair_never_changes_external_runtime(monkeypatch, tmp_path):
    monkeypatch.setattr(router, 'MANAGED_JAVA_ROOT', tmp_path / 'managed')
    external = tmp_path / 'user-jdk'
    router._restore_managed_java_modules(external)
    assert not external.exists()


def test_desktop_token_is_distinct_from_internal_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    internal = tmp_path / ".protege-mcp/secret"
    internal.parent.mkdir()
    internal.write_text("internal-only")
    prefs = tmp_path / "Library/Preferences/protege_preferences.io.github.hakjuoh.protege_mcp.server.plist"
    prefs.parent.mkdir(parents=True)
    prefs.write_bytes(plistlib.dumps({"/PROTEGE_PREFERENCES/io.github.hakjuoh.protege_mcp/server/": {"bearerToken": "public-token"}}))
    assert router._read_secret(internal) == "public-token"
    assert internal.read_text() == "internal-only"
    explicit = tmp_path / "explicit-token"
    explicit.write_text("explicit")
    assert router._read_secret(explicit) == "explicit"
    prefs.write_bytes(plistlib.dumps({}))
    with pytest.raises(router.ProtegeRoutingError):
        router._read_secret(internal)


def test_authentication_failure_is_not_misreported_as_missing_window(monkeypatch, tmp_path):
    import urllib.error

    monkeypatch.setattr(router, "_state_broker_url", lambda: None)

    def unauthorized(*args, **kwargs):
        raise urllib.error.HTTPError("http://localhost/instances", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(router.urllib.request, "urlopen", unauthorized)
    with pytest.raises(router.ProtegeAuthenticationError, match="认证失败"):
        router.discover_broker("http://localhost", "wrong")
    monkeypatch.setattr(router, "resolve_role", lambda **kwargs: router.discover_broker("http://localhost", "wrong"))
    with pytest.raises(router.ProtegeAuthenticationError, match="认证失败"):
        router.ensure_role(role="construction", application=tmp_path, broker="http://localhost",
                           secret="wrong", routing_file=tmp_path / "routing.json", timeout=1)


def test_new_broker_retries_auth_until_window_registers(monkeypatch, tmp_path):
    application = fake_application(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(router, "_application_running", lambda app: False)
    replies = iter([[], router.ProtegeAuthenticationError("not registered yet"), [{"id": "new-window"}]])

    def instances(*args):
        reply = next(replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(router, "list_instances", instances)
    monkeypatch.setattr(router.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(router.time, "sleep", lambda *a: None)
    monkeypatch.setattr(router, "bind_role", lambda **k: {"instance_id": k["instance_id"]})
    result = router.ensure_role(role="construction", application=application,
        broker="http://localhost", secret="secret", routing_file=tmp_path / "route.json", timeout=1)
    assert result["instance_id"] == "new-window"
