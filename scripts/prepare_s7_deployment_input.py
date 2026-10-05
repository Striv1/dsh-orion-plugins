"""Prepare a new project's local Ontop configuration without deploying it."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.engine import make_url

from scripts.run_quality_validation_stage import _docker_jdbc_properties
from services.ontology_engineering.workflow import PROJECT_ID_PATTERN
from services.realtime_qa.deployment_automation import DeploymentAutomationConfig
from services.realtime_qa.postgres_readonly import (
    PostgresReadOnlyVerificationError,
    attest_postgres_read_only_principal,
)


class DeploymentPreparationError(RuntimeError):
    """Contains only safe, credential-free preparation diagnostics."""


def _checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _private_write(path: Path, contents: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_resources(endpoint: str, image: str, network: str) -> str:
    parsed = urlparse(endpoint)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1"
            or parsed.path != "/sparql" or not parsed.port
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise DeploymentPreparationError("endpoint must be a loopback HTTP /sparql URL with an explicit port")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", parsed.port))
        except OSError:
            raise DeploymentPreparationError("the requested Ontop port is already occupied") from None
    image_result = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        capture_output=True, text=True, timeout=15,
    )
    network_result = subprocess.run(
        ["docker", "network", "inspect", network, "--format", "{{.Name}}"],
        capture_output=True, text=True, timeout=15,
    )
    if image_result.returncode or network_result.returncode:
        raise DeploymentPreparationError("the configured Ontop image or Docker network is unavailable")
    return image_result.stdout.strip()


def prepare_deployment_input(
    *, workflow_root: Path, project_id: str, endpoint: str,
    verified_by: str, set_default: bool = False,
) -> dict[str, object]:
    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise DeploymentPreparationError("invalid ontology project id")
    workflow_root = workflow_root.resolve()
    project_dir = workflow_root / project_id
    state_path = project_dir / "workflow-state.json"
    if (project_dir.is_symlink() or project_dir.resolve().parent != workflow_root
            or state_path.is_symlink() or not state_path.is_file()):
        raise DeploymentPreparationError("the existing project state is required")
    state = json.loads(state_path.read_text())
    if state.get("project_id") != project_id:
        raise DeploymentPreparationError("project state identity does not match")
    reader_url = os.getenv("ORION_SOURCE_DATA_READER_URL", "").strip()
    if not reader_url:
        raise DeploymentPreparationError("ORION_SOURCE_DATA_READER_URL must be provided by the managed environment")
    try:
        parsed = make_url(reader_url)
    except Exception:
        raise DeploymentPreparationError("the managed PostgreSQL reader URL is invalid") from None
    if not parsed.drivername.startswith("postgresql") or not parsed.username or parsed.password is None:
        raise DeploymentPreparationError("a managed PostgreSQL reader principal is required")
    if any(character in str(value) for value in [parsed.username, parsed.password, parsed.host, parsed.database]
           for character in ("\n", "\r", "\\")):
        raise DeploymentPreparationError("managed connection fields contain unsupported properties escape characters")
    config = DeploymentAutomationConfig.from_env(workflow_root)
    config.config_root.mkdir(parents=True, exist_ok=True)
    target = config.config_root / project_id
    image = os.getenv("ORION_ONTOP_IMAGE", "ontology-workorder-agent/ontop:5.3.0")
    network = os.getenv("ORION_DOCKER_NETWORK", "ontology-workorder-agent_default")
    image_id = _verify_resources(endpoint, image, network)
    lock_path = config.config_root / f".{project_id}.prepare.lock"
    with lock_path.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.exists() or target.is_symlink():
            raise DeploymentPreparationError("project deployment configuration already exists; automatic replacement is refused")
        staging = Path(tempfile.mkdtemp(prefix=f".{project_id}.prepare-", dir=config.config_root))
        try:
            properties = _docker_jdbc_properties(reader_url).replace(
                "ontop.applicationName=orion-s6-pre-release", "ontop.applicationName=orion-s7-release",
            )
            if "ontop.allowRetrievingBlackBoxViewMetadataFromDB=true" not in properties.splitlines():
                raise DeploymentPreparationError("the platform Ontop builder must enable database metadata before attestation")
            properties_path = staging / "ontop.properties"
            _private_write(properties_path, properties)
            before = _checksum(properties_path)
            attestation_path = staging / "read-only-attestation.json"
            attestation = attest_postgres_read_only_principal(
                reader_url, principal=parsed.username, properties_path=properties_path,
                output_path=attestation_path, verified_by=verified_by,
            )
            attestation_path.chmod(0o600)
            if attestation["properties_sha256"] != before or _checksum(properties_path) != before:
                raise DeploymentPreparationError("properties changed during read-only attestation")
            deployment_input = {
                "endpoint": endpoint, "properties_path": "ontop.properties",
                "read_only_attestation_path": "read-only-attestation.json",
                "image": image, "network_name": network, "set_default": set_default,
                "semantica_url": os.getenv("SEMANTICA_API_URL", "http://127.0.0.1:8001").rstrip("/"),
            }
            _private_write(staging / "deployment-input.json", json.dumps(deployment_input, ensure_ascii=False, indent=2) + "\n")
            if target.exists() or target.is_symlink():
                raise DeploymentPreparationError("project deployment configuration appeared during preparation")
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return {
        "status": "PREPARED_NOT_DEPLOYED", "project_id": project_id,
        "configuration_directory": str(target), "endpoint": endpoint,
        "set_default": set_default, "database_metadata_enabled": True,
        "read_only_catalog_verified": True, "image_id": image_id, "network_name": network,
        "files": {name: {"sha256": _checksum(target / name), "mode": oct((target / name).stat().st_mode & 0o777)}
                  for name in ("ontop.properties", "read-only-attestation.json", "deployment-input.json")},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workflow-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--verified-by", required=True)
    parser.add_argument("--set-default", action="store_true")
    arguments = parser.parse_args()
    try:
        result = prepare_deployment_input(**vars(arguments))
    except (DeploymentPreparationError, PostgresReadOnlyVerificationError) as exc:
        parser.exit(1, str(exc) + "\n")
    except Exception as exc:
        parser.exit(1, f"deployment preparation failed safely: {type(exc).__name__}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
