from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BROKER = "http://127.0.0.1:8123"
DEFAULT_BROKER_STATE_FILE = Path("~/.protege-mcp/broker.json")
DEFAULT_ROUTING_FILE = Path(".orion-mcp-control/protege-routing.json")
DEFAULT_CONSTRUCTION_APP = Path("/Applications/Protégé-5.6.9-全面汉化版.app")
MANAGED_JAVA_ROOT = Path(__file__).resolve().parents[1] / ".orion-runtime/java"
MANAGED_JAVA_MEMBER = "jdk-17.0.20+8/Contents/Home/lib/modules"
MANAGED_JAVA_ARCHIVE_SHA256 = "524850138c742324fb21fca4ff6ef68ea25f25bf59366a864e45b4a0c45ed0df"


class ProtegeRoutingError(RuntimeError):
    pass


class ProtegeAuthenticationError(ProtegeRoutingError):
    pass


def _restore_managed_java_modules(java_home: Path) -> None:
    """Repair only our pinned JVM, offline, from its verified vendor archive."""
    expected = MANAGED_JAVA_ROOT / "jdk-17.0.20+8/Contents/Home"
    if java_home.resolve() != expected.resolve() or (java_home / "lib/modules").exists():
        return
    archive = MANAGED_JAVA_ROOT / "downloads/jdk17.tar.gz"
    with (MANAGED_JAVA_ROOT / "repair.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        target = java_home / "lib/modules"
        if target.exists():
            return
        try:
            with archive.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != MANAGED_JAVA_ARCHIVE_SHA256:
                    raise ProtegeRoutingError("本地 Java 恢复包校验失败，未恢复任何文件")
                stream.seek(0)
                with tarfile.open(fileobj=stream) as package:
                    source = package.extractfile(MANAGED_JAVA_MEMBER)
                    if source is None:
                        raise ProtegeRoutingError("本地 Java 恢复包缺少 lib/modules")
                    with source, tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as output:
                        temporary = Path(output.name)
                        try:
                            shutil.copyfileobj(source, output)
                            output.flush()
                            os.fsync(output.fileno())
                            temporary.chmod(0o644)
                            os.replace(temporary, target)
                        finally:
                            temporary.unlink(missing_ok=True)
        except (OSError, KeyError, tarfile.TarError) as exc:
            raise ProtegeRoutingError("本地 Java 恢复包不可用，请修复受管 Java 运行时") from exc
        print("✓ 已从校验通过的本地安装包恢复 Java 核心文件", flush=True)


def validate_java_runtime(application: Path) -> None:
    """Check the configured desktop JVM before waiting for an impossible launch."""
    java_home = application.expanduser() / "Contents/jre"
    for config in (application.expanduser() / "Contents/conf/jvm.conf",
                   Path.home() / ".Protege/conf/jvm.conf"):
        if config.is_file():
            for line in config.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.strip().partition("=")
                if separator and key.strip() == "java_home":
                    java_home = Path(value.strip()).expanduser()
    _restore_managed_java_modules(java_home)
    java = java_home / "bin/java"
    if not java.is_file() or not (java_home / "lib/modules").is_file():
        raise ProtegeRoutingError(
            f"Protégé Java 运行时不完整：{java_home}；请修复 bin/java 或 lib/modules 后重试"
        )
    try:
        subprocess.run([str(java), "-version"], check=True, timeout=10,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProtegeRoutingError(f"Protégé Java 自检失败：{java_home}") from exc


def _read_secret(secret_path: Path) -> str:
    path = secret_path.expanduser()
    # The broker directory secret authenticates /internal, not public MCP calls.
    # Preserve explicit token files; resolve the historical default to the desktop token.
    if path == Path.home() / ".protege-mcp/secret":
        preferences = Path.home() / "Library/Preferences/protege_preferences.io.github.hakjuoh.protege_mcp.server.plist"
        if preferences.is_file():
            try:
                with preferences.open("rb") as handle:
                    settings = plistlib.load(handle)
                token = settings["/PROTEGE_PREFERENCES/io.github.hakjuoh.protege_mcp/server/"]["bearerToken"]
                if isinstance(token, str) and token.strip():
                    return token.strip()
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ProtegeRoutingError("无法读取 Protégé 桌面访问令牌，请检查 MCP 配置") from exc
            raise ProtegeRoutingError("Protégé 桌面访问令牌为空，请检查 MCP 配置")
    secret = path.read_text(encoding="utf-8").strip()
    if not secret:
        raise ProtegeRoutingError(f"Protégé MCP secret 为空：{secret_path}")
    return secret


def _request_json(url: str, secret: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {secret}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise ProtegeAuthenticationError(
                "Protégé MCP 认证失败：请检查桌面访问令牌；内部通信 secret 不能用作访问令牌。"
            ) from exc
        raise ProtegeRoutingError(f"无法读取 Protégé MCP Broker：HTTP {exc.code}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise ProtegeRoutingError(f"无法读取 Protégé MCP Broker：{exc}") from exc


def _state_broker_url(state_file: Path | None = None) -> str | None:
    """Return the live broker URL advertised by the Protégé plugin.

    The broker normally binds to 8123. If that port is occupied by a stale
    standalone window, the broker deliberately chooses an ephemeral port and
    publishes it in ``broker.json``. Treating 8123 as immutable makes callers
    wait for a window that has already registered successfully elsewhere.
    """
    state_file = DEFAULT_BROKER_STATE_FILE if state_file is None else state_file
    try:
        payload = json.loads(state_file.expanduser().read_text(encoding="utf-8"))
        pid = int(payload["pid"])
        host = str(payload["host"])
        port = int(payload["port"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535:
        return None
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return None
    display_host = f"[{host}]" if ":" in host else host
    return f"http://{display_host}:{port}"


def discover_broker(broker: str, secret: str) -> tuple[str, dict[str, Any]]:
    candidates = [broker.rstrip("/")]
    advertised = _state_broker_url()
    if advertised and advertised not in candidates:
        candidates.append(advertised)

    failures: list[str] = []
    auth_error = None
    for candidate in candidates:
        try:
            payload = _request_json(f"{candidate}/instances", secret)
        except ProtegeRoutingError as exc:
            if isinstance(exc, ProtegeAuthenticationError):
                auth_error = exc
            failures.append(f"{candidate}: {exc}")
            continue
        if isinstance(payload.get("instances"), list):
            return candidate, payload
        failures.append(f"{candidate}: 未返回合法的 instances 列表")
    if auth_error is not None:
        raise auth_error
    detail = "; ".join(failures) if failures else "没有可用候选地址"
    raise ProtegeRoutingError(f"无法发现 Protégé MCP Broker（{detail}）")


def list_instances(broker: str, secret: str) -> list[dict[str, Any]]:
    _, payload = discover_broker(broker, secret)
    instances = payload.get("instances")
    assert isinstance(instances, list)
    return [item for item in instances if isinstance(item, dict) and item.get("id")]


def instance_mcp_url(broker: str, instance_id: str) -> str:
    return f"{broker.rstrip('/')}/instances/{instance_id}/mcp?v=2"


def _load_routes(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "roles": {}}
    except json.JSONDecodeError as exc:
        raise ProtegeRoutingError(f"Protégé 角色路由文件损坏：{path}") from exc
    if payload.get("version") != 1 or not isinstance(payload.get("roles"), dict):
        raise ProtegeRoutingError(f"Protégé 角色路由文件版本无效：{path}")
    return payload


def _write_routes(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _bundle_metadata(application: Path) -> dict[str, Any]:
    info_path = application / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (FileNotFoundError, plistlib.InvalidFileException) as exc:
        raise ProtegeRoutingError(f"无法读取 Protégé 应用信息：{info_path}") from exc

    plugin_candidates = sorted((application / "Contents").glob("**/protege-mcp-*.jar"))
    if not plugin_candidates:
        plugin_candidates = sorted(
            Path.home().joinpath(".Protege/plugins").glob("protege-mcp-*.jar")
        )
    plugin = plugin_candidates[-1] if plugin_candidates else None
    return {
        "application": str(application.resolve()),
        "application_name": application.stem,
        "bundle_id": info.get("CFBundleIdentifier"),
        "application_version": info.get("CFBundleShortVersionString"),
        "plugin": str(plugin) if plugin else None,
        "plugin_sha256": hashlib.sha256(plugin.read_bytes()).hexdigest() if plugin else None,
    }


def bind_role(
    *,
    role: str,
    instance_id: str,
    application: Path,
    broker: str,
    secret: str,
    routing_file: Path,
) -> dict[str, Any]:
    actual_broker, payload = discover_broker(broker, secret)
    instances = {
        item["id"]: item
        for item in payload["instances"]
        if isinstance(item, dict) and item.get("id")
    }
    if instance_id not in instances:
        raise ProtegeRoutingError(f"Protégé 窗口不存在或已经关闭：{instance_id}")
    if not application.is_dir():
        raise ProtegeRoutingError(f"Protégé 应用不存在：{application}")

    payload = _load_routes(routing_file)
    route = {
        "role": role,
        "instance_id": instance_id,
        "mcp_url": instance_mcp_url(actual_broker, instance_id),
        "broker_url": actual_broker,
        "bound_at": int(time.time() * 1000),
        "broker_instance": instances[instance_id],
        **_bundle_metadata(application),
    }
    payload["roles"][role] = route
    _write_routes(routing_file, payload)
    return route


def resolve_role(
    *, role: str, broker: str, secret: str, routing_file: Path
) -> dict[str, Any]:
    payload = _load_routes(routing_file)
    route = payload["roles"].get(role)
    if not isinstance(route, dict) or not route.get("instance_id"):
        raise ProtegeRoutingError(f"尚未绑定 Protégé {role} 窗口")
    actual_broker, instances_payload = discover_broker(broker, secret)
    live_ids = {
        item["id"]
        for item in instances_payload["instances"]
        if isinstance(item, dict) and item.get("id")
    }
    if route["instance_id"] not in live_ids:
        raise ProtegeRoutingError(
            f"Protégé {role} 窗口已经关闭：{route['instance_id']}"
        )
    expected_mcp_url = instance_mcp_url(actual_broker, str(route["instance_id"]))
    if route.get("mcp_url") != expected_mcp_url or route.get("broker_url") != actual_broker:
        route["mcp_url"] = expected_mcp_url
        route["broker_url"] = actual_broker
        route["broker_instance"] = next(
            item
            for item in instances_payload["instances"]
            if isinstance(item, dict) and item.get("id") == route["instance_id"]
        )
        _write_routes(routing_file, payload)
    return route


def _application_running(application: Path) -> bool:
    """Fail closed if process inspection fails; MCP absence is not process exit."""
    executable = str(application.resolve() / "Contents/MacOS/protege")
    try:
        result = subprocess.run(
            ["/bin/ps", "-axo", "comm="], check=True, capture_output=True,
            text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProtegeRoutingError("无法核验 Protégé 进程；暂停自动启动") from exc
    return any(line.strip() == executable for line in result.stdout.splitlines())


def ensure_role(
    *, role: str, application: Path, broker: str, secret: str,
    routing_file: Path, timeout: float,
) -> dict[str, Any]:
    # Serialize launches across callers, including callers using different routes.
    application = application.expanduser().resolve()
    key = hashlib.sha256(str(application).encode()).hexdigest()[:20]
    lock_dir = Path.home() / ".protege-mcp" / "launch-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    with (lock_dir / f"{key}.lock").open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ProtegeRoutingError("Protégé 正在恢复；等待现有启动完成") from exc
        return _ensure_role_locked(
            role=role, application=application, broker=broker, secret=secret,
            routing_file=routing_file, timeout=timeout, launch_state=handle,
        )


def _ensure_role_locked(
    *,
    role: str,
    application: Path,
    broker: str,
    secret: str,
    routing_file: Path,
    timeout: float,
    launch_state: Any,
) -> dict[str, Any]:
    try:
        route = resolve_role(
            role=role, broker=broker, secret=secret, routing_file=routing_file
        )
        if Path(route.get("application", "")) == application.resolve():
            return route
    except ProtegeAuthenticationError:
        raise
    except ProtegeRoutingError:
        pass

    if not application.is_dir():
        raise ProtegeRoutingError(f"Protégé 应用不存在：{application}")
    # Keep the registered role intact: never adopt an unrelated review window.
    if _application_running(application):
        raise ProtegeRoutingError(
            "Protégé 进程仍存活，但施工窗口暂未连接；等待重连，不重复启动"
        )
    launch_state.seek(0)
    previous = launch_state.read().strip()
    if previous and time.time() - float(previous) < 120:
        raise ProtegeRoutingError("Protégé 启动冷却中；等待 120 秒后再检查")
    try:
        before_ids = {item["id"] for item in list_instances(broker, secret)}
    except ProtegeAuthenticationError:
        raise
    except ProtegeRoutingError:
        # The broker belongs to the desktop application lifecycle. If every
        # Protégé window is gone, launching the requested application is also
        # the supported way to recreate the broker.
        before_ids = set()
    # Persist before launching so timeout/restarts cannot create a launch storm.
    launch_state.seek(0)
    launch_state.truncate()
    launch_state.write(str(time.time()))
    launch_state.flush()
    os.fsync(launch_state.fileno())
    subprocess.run(
        ["/usr/bin/open", "-a", str(application)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + timeout
    new_instances: list[dict[str, Any]] = []
    registration_auth_error = None
    while time.monotonic() < deadline:
        time.sleep(0.5)
        try:
            current = list_instances(broker, secret)
        except ProtegeAuthenticationError as exc:
            # A freshly launched broker knows no desktop token until the first
            # window registers. Only this bounded registration phase may retry 401.
            registration_auth_error = exc
            continue
        except ProtegeRoutingError:
            continue
        new_instances = [item for item in current if item["id"] not in before_ids]
        if new_instances:
            break
    if not new_instances:
        if registration_auth_error is not None:
            raise registration_auth_error
        raise ProtegeRoutingError(
            f"{application.name} 已启动，但 {timeout:g} 秒内没有注册新的 MCP 窗口"
        )
    selected = max(new_instances, key=lambda item: int(item.get("registered_at", 0)))
    return bind_role(
        role=role,
        instance_id=str(selected["id"]),
        application=application,
        broker=broker,
        secret=secret,
        routing_file=routing_file,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="管理 ORION 的 Protégé 角色窗口路由。")
    parser.add_argument("command", choices=("ensure", "resolve", "bind", "status"))
    parser.add_argument("--role", default="construction")
    parser.add_argument("--application", type=Path, default=DEFAULT_CONSTRUCTION_APP)
    parser.add_argument("--instance-id")
    parser.add_argument("--broker", default=DEFAULT_BROKER)
    parser.add_argument("--secret", type=Path, default=Path("~/.protege-mcp/secret"))
    parser.add_argument("--routing-file", type=Path, default=DEFAULT_ROUTING_FILE)
    parser.add_argument("--timeout", type=float, default=45)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    secret = _read_secret(args.secret)
    try:
        if args.command == "ensure":
            route = ensure_role(
                role=args.role,
                application=args.application,
                broker=args.broker,
                secret=secret,
                routing_file=args.routing_file,
                timeout=args.timeout,
            )
        elif args.command == "resolve":
            route = resolve_role(
                role=args.role,
                broker=args.broker,
                secret=secret,
                routing_file=args.routing_file,
            )
        elif args.command == "bind":
            if not args.instance_id:
                raise ProtegeRoutingError("bind 必须提供 --instance-id")
            route = bind_role(
                role=args.role,
                instance_id=args.instance_id,
                application=args.application,
                broker=args.broker,
                secret=secret,
                routing_file=args.routing_file,
            )
        else:
            payload = _load_routes(args.routing_file)
            live_ids = {item["id"] for item in list_instances(args.broker, secret)}
            for value in payload["roles"].values():
                if isinstance(value, dict):
                    value["live"] = value.get("instance_id") in live_ids
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return
    except (ProtegeRoutingError, subprocess.CalledProcessError) as exc:
        print(f"Protégé 角色路由失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    print(route["mcp_url"])


if __name__ == "__main__":
    main()
