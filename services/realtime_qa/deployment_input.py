"""Platform-managed Ontop deployment input for newly published releases.

S7 automation needs a per-project ``deployment-input.json`` (endpoint, JDBC
properties and a read-only attestation). Historically only an operator CLI
created it, so every new project stalled at WAITING_CONFIGURATION. This module
creates the same files from the managed read-only principal, fail-closed:
the principal must pass catalog read-back before anything is published, and an
existing project configuration is never replaced.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.engine import make_url

from services.realtime_qa.postgres_readonly import attest_postgres_read_only_principal

PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,79}$")
DEFAULT_PORT_RANGE = (18100, 18199)
AUTO_VERIFIED_BY = "ORION S7 自动部署（平台只读核验）"


class DeploymentInputUnavailable(RuntimeError):
    """Credential-free reason why the platform cannot prepare deployment input."""


def ontop_jdbc_properties(database_url: str, *, application_name: str) -> str:
    parsed = make_url(database_url)
    if not parsed.username or parsed.password is None or not parsed.database:
        raise DeploymentInputUnavailable("只读数据源连接缺少账号、密码或数据库名。")
    for value in (parsed.username, parsed.password, parsed.host or "", parsed.database):
        if any(ch in str(value) for ch in ("\n", "\r", "\\")):
            raise DeploymentInputUnavailable("只读数据源连接包含 properties 不支持的转义字符。")
    host = parsed.host or "postgres"
    if host in {"127.0.0.1", "localhost"}:
        host = "postgres"  # Ontop runs inside the compose network.
    return "\n".join([
        f"jdbc.url=jdbc:postgresql://{host}:{parsed.port or 5432}/{parsed.database}",
        f"jdbc.user={parsed.username}",
        f"jdbc.password={parsed.password}",
        "jdbc.driver=org.postgresql.Driver",
        "ontop.query.defaultTimeout=120",
        "ontop.allowRetrievingBlackBoxViewMetadataFromDB=true",
        f"ontop.applicationName={application_name}",
        "",
    ])


def _port_range() -> tuple[int, int]:
    raw = str(os.getenv("ORION_S7_ONTOP_PORT_RANGE") or "").strip()
    if not raw:
        return DEFAULT_PORT_RANGE
    try:
        low, high = (int(part) for part in raw.split("-", 1))
    except ValueError:
        raise DeploymentInputUnavailable("ORION_S7_ONTOP_PORT_RANGE 必须形如 18100-18199。") from None
    if not 1024 < low <= high < 65536:
        raise DeploymentInputUnavailable("ORION_S7_ONTOP_PORT_RANGE 超出允许范围。")
    return low, high


def _reserved_ports(config_root: Path) -> set[int]:
    ports: set[int] = set()
    for path in config_root.glob("*/deployment-input.json"):
        try:
            endpoint = str(json.loads(path.read_text(encoding="utf-8")).get("endpoint") or "")
            match = re.search(r":(\d+)/sparql", endpoint)
            if match:
                ports.add(int(match.group(1)))
        except (OSError, ValueError):
            continue
    return ports


def _port_is_free(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def choose_endpoint(config_root: Path, *, port_is_free: Callable[[int], bool] = _port_is_free) -> str:
    low, high = _port_range()
    reserved = _reserved_ports(config_root)
    for port in range(low, high + 1):
        if port not in reserved and port_is_free(port):
            return f"http://127.0.0.1:{port}/sparql"
    raise DeploymentInputUnavailable(f"端口范围 {low}-{high} 内没有可用的 Ontop 端口。")


def _checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _private_write(path: Path, contents: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def ensure_deployment_input(
    config_root: Path,
    project_id: str,
    *,
    attest: Callable[..., dict[str, Any]] = attest_postgres_read_only_principal,
    port_is_free: Callable[[int], bool] = _port_is_free,
) -> dict[str, Any]:
    """Return the project deployment input path, creating it when absent."""

    if not PROJECT_ID.fullmatch(project_id):
        raise DeploymentInputUnavailable("工程编号无效。")
    config_root = config_root.resolve()
    target = config_root / project_id
    config_path = target / "deployment-input.json"
    if config_path.is_file():
        return {"status": "EXISTING", "config_path": config_path}
    if str(os.getenv("ORION_S7_AUTO_PREPARE_INPUT", "true")).strip().lower() in {"0", "false", "no", "off"}:
        raise DeploymentInputUnavailable("平台已关闭自动准备部署输入（ORION_S7_AUTO_PREPARE_INPUT=false）。")
    reader_url = str(os.getenv("ORION_SOURCE_DATA_READER_URL") or "").strip()
    if not reader_url:
        raise DeploymentInputUnavailable("受管环境未提供 ORION_SOURCE_DATA_READER_URL，无法自动准备只读部署输入。")
    try:
        parsed = make_url(reader_url)
    except Exception:
        raise DeploymentInputUnavailable("受管只读数据源连接格式无效。") from None
    if not parsed.drivername.startswith("postgresql") or not parsed.username:
        raise DeploymentInputUnavailable("自动部署只支持受管 PostgreSQL 只读账号。")
    config_root.mkdir(parents=True, exist_ok=True)
    with (config_root / f".{project_id}.prepare.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if config_path.is_file():
            return {"status": "EXISTING", "config_path": config_path}
        if target.exists() or target.is_symlink():
            raise DeploymentInputUnavailable("工程已有不完整的部署配置目录，平台不会自动覆盖。")
        endpoint = choose_endpoint(config_root, port_is_free=port_is_free)
        staging = Path(tempfile.mkdtemp(prefix=f".{project_id}.prepare-", dir=config_root))
        try:
            properties_path = staging / "ontop.properties"
            _private_write(properties_path, ontop_jdbc_properties(reader_url, application_name="orion-s7-release"))
            before = _checksum(properties_path)
            attestation_path = staging / "read-only-attestation.json"
            attestation = attest(
                reader_url, principal=parsed.username, properties_path=properties_path,
                output_path=attestation_path, verified_by=AUTO_VERIFIED_BY,
            )
            attestation_path.chmod(0o600)
            if attestation.get("properties_sha256") != before or _checksum(properties_path) != before:
                raise DeploymentInputUnavailable("只读核验期间 properties 发生变化。")
            payload = {
                "endpoint": endpoint,
                "properties_path": "ontop.properties",
                "read_only_attestation_path": "read-only-attestation.json",
                "image": os.getenv("ORION_ONTOP_IMAGE", "ontology-workorder-agent/ontop:5.3.0"),
                "network_name": os.getenv("ORION_DOCKER_NETWORK", "ontology-workorder-agent_default"),
                "set_default": False,
            }
            _private_write(staging / "deployment-input.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return {"status": "PREPARED", "config_path": config_path, "endpoint": endpoint}

