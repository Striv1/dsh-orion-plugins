from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from services.realtime_qa.binding import ReleaseBindingLoader
from services.realtime_qa.deployment import verify_ontop_deployment_binding
from services.realtime_qa.models import (
    OntologyReleaseBinding,
    RealtimeRuntimeRegistration,
    RealtimeRuntimeRegistryConfig,
)


def register_runtime(
    *,
    registry_path: Path,
    project_dir: Path,
    deployment_binding_path: Path | None = None,
    allow_pre_runtime: bool = False,
    set_default: bool = False,
) -> dict[str, Any]:
    """Atomically register one verified S7 deployment without storing credentials."""

    project_dir = project_dir.resolve()
    binding = ReleaseBindingLoader().load(
        project_dir,
        allow_pre_runtime=allow_pre_runtime,
    )
    resolved_deployment_path: Path | None = None
    if binding.structured_query_enabled:
        if deployment_binding_path is None:
            raise ValueError("structured runtime requires deployment_binding_path")
        resolved_deployment_path = deployment_binding_path.resolve()
        verify_ontop_deployment_binding(binding, resolved_deployment_path)
    registration = RealtimeRuntimeRegistration(
        project_id=binding.project_id,
        project_dir=str(project_dir),
        deployment_binding_path=(
            str(resolved_deployment_path) if resolved_deployment_path else None
        ),
    )
    activation = _verify_runtime_activation(
        binding=binding,
        project_dir=project_dir,
        deployment_binding_path=resolved_deployment_path,
        allow_pre_runtime=allow_pre_runtime,
    )

    registry_path = registry_path.resolve()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        config = _read_registry(registry_path)
        entries = {
            entry.project_id: entry
            for entry in (config.runtimes if config is not None else [])
        }
        entries[registration.project_id] = registration
        default_project_id = (
            registration.project_id
            if set_default
            else (config.default_project_id if config is not None else None)
        )
        updated = RealtimeRuntimeRegistryConfig(
            default_project_id=default_project_id,
            runtimes=[entries[key] for key in sorted(entries)],
        )
        _atomic_write_json(registry_path, updated.model_dump(mode="json"))
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    return {
        "project_id": binding.project_id,
        "release_version": binding.release_version,
        "release_fingerprint": binding.release_fingerprint,
        "ontop_deployment_id": binding.ontop_deployment_id,
        "registry_path": str(registry_path),
        "artifact_verified": True,
        "deployment_contract_verified": binding.structured_query_enabled,
        "runtime_endpoint_verification": activation["runtime_endpoint_verification"],
        "runtime_loader_verification": activation["runtime_loader_verification"],
        "default_project_id": updated.default_project_id,
        "document_cq_validation": activation.get("document_cq_validation"),
        **({"reasoning_validation": activation["reasoning_validation"]}
           if "reasoning_validation" in activation else {}),
    }


def _verify_runtime_activation(
    *,
    binding: OntologyReleaseBinding,
    project_dir: Path,
    deployment_binding_path: Path | None,
    allow_pre_runtime: bool = False,
) -> dict[str, Any]:
    """Use the same loader as 8091 before the registry's enabled bit is committed."""

    from services.config import Settings
    from services.realtime_qa.runtime import build_realtime_evidence_runtime

    settings = Settings.from_env()
    runtime = build_realtime_evidence_runtime(
        project_dir=project_dir,
        deployment_binding_path=deployment_binding_path,
        database_url=settings.database_url,
        fuseki_url=settings.fuseki_url,
        semantica_url=settings.semantica_url,
        timeout=5.0,
        allow_pre_runtime=allow_pre_runtime,
    )
    if (
        runtime.binding.project_id != binding.project_id
        or runtime.binding.release_fingerprint != binding.release_fingerprint
        or not runtime.runtime_verified
    ):
        raise ValueError("runtime activation loader did not verify the candidate release")
    if binding.reasoning_capabilities and not runtime.reasoning_runtime_verified:
        raise ValueError("runtime activation reasoning endpoint is not verified")
    from services.realtime_qa.deployment_automation import S7DeploymentAutomation

    document_cq_validation = S7DeploymentAutomation._verify_document_cq_validation_cases(runtime.binding)
    reasoning_validation = None
    if not binding.structured_query_enabled and binding.reasoning_capabilities:
        # Health only proves availability. Execute the manifest-bound document
        # rules and reviewed CQ cases before making this runtime discoverable.
        reasoning_validation = S7DeploymentAutomation._verify_reasoning_validation_cases(
            runtime.binding, "", settings.semantica_url,
        )
        if reasoning_validation.get("status") != "PASSED":
            raise ValueError("document runtime reasoning/CQ validation did not pass")
    return {
        **({"reasoning_validation": reasoning_validation} if reasoning_validation else {}),
        "document_cq_validation": document_cq_validation,
        "runtime_endpoint_verification": (
            "VERIFIED" if binding.structured_query_enabled else "NOT_APPLICABLE"
        ),
        "runtime_loader_verification": "VERIFIED",
    }


def _read_registry(path: Path) -> RealtimeRuntimeRegistryConfig | None:
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return RealtimeRuntimeRegistryConfig.model_validate(payload)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
