from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from services.realtime_qa.models import OntologyReleaseBinding


class OntopDeploymentContractError(ValueError):
    pass


def render_ontop_release_deployment(
    binding: OntologyReleaseBinding,
    *,
    output_dir: Path,
    properties_path: Path,
    read_only_attestation_path: Path,
    endpoint: str,
    image: str = "ontology-workorder-agent/ontop:5.3.0",
    network_name: str | None = None,
) -> dict[str, Any]:
    """Render a secret-free deployment binding around an approved S7 package."""

    package_dir = Path(binding.package_path).resolve()
    ontology_path = _safe_package_artifact(
        package_dir,
        binding.ontology_artifact,
        binding.artifact_checksums[binding.ontology_artifact],
    )
    mapping_path = _safe_package_artifact(
        package_dir,
        binding.mapping_artifact,
        binding.artifact_checksums[binding.mapping_artifact],
    )
    properties_path = properties_path.resolve()
    attestation_path = read_only_attestation_path.resolve()
    if not properties_path.is_file() or not attestation_path.is_file():
        raise OntopDeploymentContractError(
            "Ontop properties and read-only attestation must both exist"
        )
    attestation = _read_json(attestation_path)
    properties_sha256 = _checksum(properties_path)
    properties = _read_properties(properties_path)
    _validate_read_only_attestation(
        attestation,
        properties_sha256=properties_sha256,
        jdbc_user=properties.get("jdbc.user", ""),
    )
    parsed_endpoint = urlparse(endpoint)
    try:
        endpoint_port = parsed_endpoint.port
    except ValueError as exc:
        raise OntopDeploymentContractError("endpoint port is invalid") from exc
    if (
        parsed_endpoint.scheme not in {"http", "https"}
        or not parsed_endpoint.hostname
        or not endpoint_port
        or parsed_endpoint.path.rstrip("/") != "/sparql"
        or parsed_endpoint.username is not None
        or parsed_endpoint.password is not None
        or parsed_endpoint.query
        or parsed_endpoint.fragment
    ):
        raise OntopDeploymentContractError(
            "endpoint must be an absolute Ontop /sparql URL with an explicit port"
        )

    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    service_name = compose_project_name(binding)
    compose: dict[str, Any] = {
        "name": service_name,
        "services": {
            "ontop": {
                "image": image,
                "restart": "unless-stopped",
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "3"},
                },
                "read_only": True,
                "security_opt": ["no-new-privileges:true"],
                "environment": {
                    "ONTOP_ONTOLOGY_FILE": "/opt/ontop/input/ontology.ttl",
                    "ONTOP_MAPPING_FILE": "/opt/ontop/input/mapping.obda",
                    "ONTOP_PROPERTIES_FILE": "/opt/ontop/input/ontop.properties",
                },
                "ports": [f"127.0.0.1:{endpoint_port}:8080"],
                "volumes": [
                    f"{ontology_path}:/opt/ontop/input/ontology.ttl:ro",
                    f"{mapping_path}:/opt/ontop/input/mapping.obda:ro",
                    f"{properties_path}:/opt/ontop/input/ontop.properties:ro",
                ],
            }
        },
    }
    if network_name:
        compose["services"]["ontop"]["networks"] = ["database"]
        compose["networks"] = {"database": {"external": True, "name": network_name}}
    compose_path = output_dir / "compose.yaml"
    compose_path.write_text(
        yaml.safe_dump(compose, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    deployment_binding = {
        "schema_version": 1,
        "status": "ARTIFACT_VERIFIED_NOT_RUNTIME_VERIFIED",
        "project_id": binding.project_id,
        "release_version": binding.release_version,
        "release_fingerprint": binding.release_fingerprint,
        "ontop_deployment_id": binding.ontop_deployment_id,
        "compose_project": service_name,
        "endpoint": endpoint,
        "mapping_path": str(mapping_path),
        "mapping_sha256": _checksum(mapping_path),
        "ontology_path": str(ontology_path),
        "ontology_sha256": _checksum(ontology_path),
        "properties_path": str(properties_path),
        "properties_sha256": properties_sha256,
        "read_only_attestation_path": str(attestation_path),
        "read_only_attestation_sha256": _checksum(attestation_path),
        "database_principal": attestation["database_principal"],
        "database_access_mode": "READ_ONLY",
        "rendered_at": datetime.now().astimezone().isoformat(),
        "compose_path": str(compose_path),
    }
    binding_path = output_dir / "deployment-binding.json"
    _write_json(binding_path, deployment_binding)
    return {**deployment_binding, "deployment_binding_path": str(binding_path)}


def verify_ontop_deployment_binding(
    binding: OntologyReleaseBinding,
    deployment_binding_path: Path,
) -> dict[str, Any]:
    deployment = _read_json(deployment_binding_path.resolve())
    expected = {
        "project_id": binding.project_id,
        "release_version": binding.release_version,
        "release_fingerprint": binding.release_fingerprint,
        "ontop_deployment_id": binding.ontop_deployment_id,
        "mapping_sha256": binding.artifact_checksums[binding.mapping_artifact],
        "ontology_sha256": binding.artifact_checksums[binding.ontology_artifact],
        "database_access_mode": "READ_ONLY",
    }
    if any(deployment.get(key) != value for key, value in expected.items()):
        raise OntopDeploymentContractError(
            "deployment binding does not match the approved S7 release"
        )
    for path_key, hash_key in (
        ("mapping_path", "mapping_sha256"),
        ("ontology_path", "ontology_sha256"),
        ("properties_path", "properties_sha256"),
        ("read_only_attestation_path", "read_only_attestation_sha256"),
    ):
        path = Path(str(deployment.get(path_key) or ""))
        if not path.is_file() or _checksum(path) != deployment.get(hash_key):
            raise OntopDeploymentContractError(
                f"deployment artifact changed after render: {path_key}"
            )
    attestation = _read_json(Path(deployment["read_only_attestation_path"]))
    properties = _read_properties(Path(deployment["properties_path"]))
    _validate_read_only_attestation(
        attestation,
        properties_sha256=str(deployment["properties_sha256"]),
        jdbc_user=properties.get("jdbc.user", ""),
    )
    return deployment


def _validate_read_only_attestation(
    attestation: dict[str, Any],
    *,
    properties_sha256: str,
    jdbc_user: str,
) -> None:
    required = {
        "database_access_mode": "READ_ONLY",
        "write_privileges": False,
        "verification_method": "DATABASE_CATALOG_READBACK",
        "properties_sha256": properties_sha256,
        "database_principal": jdbc_user,
    }
    if any(attestation.get(key) != value for key, value in required.items()):
        raise OntopDeploymentContractError(
            "database read-only attestation does not match Ontop properties"
        )
    if not str(attestation.get("verified_by") or "").strip():
        raise OntopDeploymentContractError("read-only attestation verified_by is required")
    try:
        datetime.fromisoformat(str(attestation.get("verified_at") or ""))
    except ValueError as exc:
        raise OntopDeploymentContractError("read-only attestation verified_at is invalid") from exc


def _safe_package_artifact(root: Path, relative: str, expected_sha256: str) -> Path:
    path = (root / relative).resolve()
    if root not in path.parents or not path.is_file():
        raise OntopDeploymentContractError("release deployment artifact is missing")
    if _checksum(path) != expected_sha256:
        raise OntopDeploymentContractError("release deployment artifact checksum mismatch")
    return path


def _read_properties(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip()
    if not values.get("jdbc.user"):
        raise OntopDeploymentContractError("Ontop properties have no jdbc.user")
    return values


def _service_name(deployment_id: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", deployment_id.lower()).strip("-")
    return f"orion-{value}"[:63]


def compose_project_name(binding: OntologyReleaseBinding) -> str:
    """Resolve an operator-managed display alias without changing release identity."""
    configured_path = os.environ.get("ORION_DOCKER_SERVICE_ALIASES_PATH")
    path = (
        Path(configured_path).expanduser()
        if configured_path
        else Path(__file__).resolve().parents[2]
        / "harness/runtime/docker-service-aliases.json"
    )
    if not path.is_file() and not configured_path:
        return _service_name(binding.ontop_deployment_id)
    config = _read_json(path)
    aliases = config.get("aliases")
    if config.get("schema_version") != 1 or not isinstance(aliases, dict):
        raise OntopDeploymentContractError("Docker service aliases require schema_version 1 and aliases object")
    seen: set[str] = set()
    for iri, slug in aliases.items():
        if not isinstance(iri, str) or not re.fullmatch(r"(?:https?://|urn:)[^\s]+", iri):
            raise OntopDeploymentContractError("Docker service alias key must be an absolute ontology IRI")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", slug) or len(slug) > 40:
            raise OntopDeploymentContractError("Docker service alias must be a lowercase slug of at most 40 characters")
        if slug in seen:
            raise OntopDeploymentContractError("Docker service aliases must be unique across ontology IRIs")
        seen.add(slug)
    slug = aliases.get(binding.ontology_iri)
    if slug is None:
        return _service_name(binding.ontop_deployment_id)
    # Reject unsupported characters instead of collapsing distinct versions to one name.
    version = binding.release_version
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)*(?:-[a-z0-9]+(?:[.-][a-z0-9]+)*)?", version):
        raise OntopDeploymentContractError("Docker service alias requires a numeric release version with optional lowercase prerelease")
    encoded_version = version.replace("-", "--").replace(".", "-")
    name = f"ahs-{slug}-v{encoded_version}"
    if len(name) > 63:
        raise OntopDeploymentContractError("Docker Compose project alias exceeds 63 characters")
    return name


def _checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise OntopDeploymentContractError(
            f"deployment contract is missing or invalid: {path.name}"
        ) from exc
    if not isinstance(payload, dict):
        raise OntopDeploymentContractError("deployment contract must be an object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
