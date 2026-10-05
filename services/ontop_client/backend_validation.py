"""Static execution diagnostics and live candidate checks for formal runtime queries."""
from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pyparsing import ParseBaseException
from rdflib.plugins.sparql import prepareQuery

from services.ontop_client.query_execution import POLICY as ADAPTER_POLICY
from services.ontop_client.query_execution import _walk, compile_ontop_select

POLICY = "formal-target-backend-validation-v1"


def fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def text_checksum(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def diagnose_select(query: str) -> dict[str, Any]:
    """Static evidence is never a claim that a particular backend executed SQL."""
    executed, execution = compile_ontop_select(query)
    analysis = "AVAILABLE"
    try:
        nodes = list(_walk(prepareQuery(executed).algebra))
        unresolved = sorted({node.name for node in nodes if node.name in {"Builtin_EXISTS", "Builtin_NOTEXISTS"}})
    except (ValueError, TypeError, KeyError, AttributeError, ParseBaseException):
        # The existing syntax gate remains authoritative. An analyzer limitation
        # must neither invent backend compatibility nor reject otherwise valid SPARQL.
        analysis, unresolved = "UNAVAILABLE", []
    return {
        "policy": POLICY, "adapter_policy": ADAPTER_POLICY,
        "status": "STATIC_ONLY", "runtime_verified": False, "analysis_status": analysis,
        "compatibility": "EQUIVALENT_COMPILATION" if execution["strategy"] != "ORIGINAL" else "UNVERIFIED",
        "requires_live_backend_check": True, "unresolved_operators": unresolved,
        "query_execution": {**execution, "executed_query_sha256": text_checksum(executed)},
    }


def candidate_backend_identity(container_name: str) -> dict[str, Any]:
    """Read immutable local image identity; never inspect environment credentials."""
    result: dict[str, Any] = {
        "container_name": container_name, "image_id": None, "image_digest": None,
        "version": None, "identity_status": "UNAVAILABLE", "version_status": "UNAVAILABLE",
    }
    try:
        inspected = subprocess.run(
            ["docker", "inspect", "--format", '{{json .Image}}', container_name],
            check=True, capture_output=True, text=True, timeout=5,
        )
        image_id = json.loads(inspected.stdout)
        if not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            return result
        result.update(image_id=image_id, identity_status="VERIFIED_LOCAL_IMAGE_ID")
        image = subprocess.run(
            ["docker", "image", "inspect", "--format", '{{json .RepoDigests}}', image_id],
            check=True, capture_output=True, text=True, timeout=5,
        )
        digests = json.loads(image.stdout) or []
        result["image_digest"] = sorted(digests)[0] if digests else None
        # The mutable configured image tag does not prove the application version.
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        pass
    return result


def failure_category(exc: Exception) -> str:
    # Aggregated validation failures wrap the first per-case error as __cause__;
    # classify by the underlying transport/backend error when one exists.
    cause = exc.__cause__
    for candidate in (exc, cause):
        if isinstance(candidate, httpx.TimeoutException):
            return "TIMEOUT"
    for candidate in (exc, cause):
        if isinstance(candidate, httpx.HTTPStatusError):
            return "BACKEND_REJECTED" if candidate.response.status_code < 500 else "BACKEND_EXECUTION_ERROR"
        if isinstance(candidate, httpx.TransportError):
            return "CONNECTION_FAILURE"
    if isinstance(exc, ValueError):
        return "CONTRACT_OR_ARTIFACT_INVALID"
    return "QUERY_CONTRACT_FAILED"


def validate_candidate_queries(
    *, project_dir: Path, endpoint: str, candidate_id: str,
    backend_identity: dict[str, Any] | None = None,
    source_fingerprint: str | None = None, timeout_seconds: float = 120.0,
    progress_callback=None, receipt_path: Path | None = None,
    purpose: str = "BEFORE_MATERIALIZATION",
) -> dict[str, Any]:
    """Fail before materialization, using exactly S7's queries, parameters and assertions.

    No release binding is synthesized; this is explicitly candidate evidence and
    is never reused across invocations or backend/input changes.
    """
    from services.ontop_client.client import OntopClient
    from services.realtime_qa.deployment_automation import verify_query_validation_cases

    runtime_dir = project_dir / "03-mapping-review/runtime"
    source_path = runtime_dir / "runtime-source.json"
    runtime = json.loads(source_path.read_text())
    capabilities = runtime.get("query_capabilities") or {}
    queries = runtime.get("ontop_queries") or {}
    paths = {}
    for name, entry in queries.items():
        path = (runtime_dir / entry["path"]).resolve()
        if not path.is_relative_to(runtime_dir.resolve()):
            raise ValueError("candidate query artifact escapes runtime directory")
        paths[name] = path
    missing_cases = sorted(name for name in queries if not (capabilities.get(name) or {}).get("validation_cases"))
    if missing_cases:
        raise ValueError("formal target queries lack validation_cases: " + ", ".join(missing_cases))
    if set(capabilities) != set(queries) or not queries:
        raise ValueError("formal target query/capability coverage mismatch")
    mapping = runtime_dir / "mapping.obda"
    mapping_hash = "sha256:" + hashlib.sha256(mapping.read_bytes()).hexdigest()
    if mapping_hash != runtime.get("mapping_sha256"):
        raise ValueError("candidate mapping checksum mismatch")
    client = OntopClient(
        endpoint, timeout=timeout_seconds, allowed_queries=set(queries), query_files=paths,
        query_checksums={name: entry["sha256"] for name, entry in queries.items()},
        query_capabilities=capabilities,
    )
    receipt: dict[str, Any] = {
        "policy": POLICY, "stage": "S6", "scope": "CANDIDATE_ONLY",
        "purpose": purpose, "execution_reused": False,
        "started_at": datetime.now().astimezone().isoformat(),
        "candidate_id": candidate_id, "database_access_mode": "READ_ONLY",
        "adapter_policy": ADAPTER_POLICY,
        "backend": backend_identity or {"identity_status": "UNAVAILABLE", "version_status": "UNAVAILABLE"},
        "source_fingerprint": source_fingerprint,
        "runtime_source_sha256": "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "mapping_sha256": mapping_hash,
        "ontology_sha256": "sha256:" + hashlib.sha256((project_dir / "05-ontology-build/ontology.ttl").read_bytes()).hexdigest(),
        "capabilities_sha256": fingerprint(capabilities), "queries": [],
        "status": "RUNNING", "runtime_verified": False,
    }
    started = time.monotonic()
    def progress(details):
        safe_details = {key: value for key, value in details.items() if key != "error"}
        receipt["progress"] = safe_details
        if progress_callback is not None:
            progress_callback(safe_details)

    def persist():
        if receipt_path is not None:
            from services.ontology_engineering.stage_execution import _write_json
            _write_json(receipt_path, receipt)
    persist()
    try:
        for name, capability in sorted(capabilities.items()):
            for case in capability["validation_cases"]:
                parameters = dict(case.get("parameters") or {})
                rendered = client.render(name, **parameters)
                receipt["queries"].append({
                    "query_name": name, "case_id": case["id"],
                    "template_sha256": queries[name]["sha256"],
                    "parameters_sha256": fingerprint(parameters),
                    **diagnose_select(rendered),
                })
        receipt["validation"] = verify_query_validation_cases(
            client, capabilities, timeout_seconds=timeout_seconds, progress_callback=progress,
        )
        bound_files = {
            source_path: receipt["runtime_source_sha256"], mapping: receipt["mapping_sha256"],
            project_dir / "05-ontology-build/ontology.ttl": receipt["ontology_sha256"],
            **{paths[name]: entry["sha256"] for name, entry in queries.items()},
        }
        if any("sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != expected
               for path, expected in bound_files.items()):
            raise ValueError("candidate validation inputs changed during execution")
        if backend_identity and backend_identity.get("image_id") and backend_identity.get("container_name"):
            refreshed = candidate_backend_identity(backend_identity["container_name"])
            if refreshed.get("image_id") != backend_identity["image_id"]:
                raise ValueError("candidate backend identity changed during execution")
        receipt.update(status="PASSED", runtime_verified=True)
    except Exception as exc:
        # Exception text can contain backend SQL or connection details; store only type/category.
        receipt.update(status="FAILED", error_category=failure_category(exc), error_type=type(exc).__name__)
        raise
    finally:
        receipt["elapsed_seconds"] = time.monotonic() - started
        receipt["finished_at"] = datetime.now().astimezone().isoformat()
        receipt["input_fingerprint"] = fingerprint({key: receipt[key] for key in (
            "candidate_id", "adapter_policy", "backend", "source_fingerprint", "runtime_source_sha256",
            "mapping_sha256", "ontology_sha256", "capabilities_sha256", "queries",
        )})
        persist()
    return receipt
