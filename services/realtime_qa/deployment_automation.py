from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import math
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from services.ontology_contracts.cq_answers import (
    validate_cq_expected_rows,
    validate_cq_required_bindings,
)
from services.ontology_contracts.errors import WorkflowGateError
from services.ontology_engineering.formal_facts import FormalFactContractError
from services.realtime_qa.binding import (
    OntopDeploymentVerifier,
    OntopRuntimeVerificationError,
    ReleaseBindingLoader,
)
from services.realtime_qa.deployment import render_ontop_release_deployment
from services.realtime_qa.deployment_input import (
    DeploymentInputUnavailable,
    ensure_deployment_input,
)
from services.realtime_qa.json_values import query_json_scalar
from services.realtime_qa.postgres_readonly import PostgresReadOnlyVerificationError
from services.realtime_qa.rdf_results import typed_result_rows
from services.realtime_qa.reasoning import (
    SemanticaReasoningClient,
    document_evidence_facts,
    facts_from_rows,
    result_facts,
    semantic_facts,
)
from services.realtime_qa.reasoning_cq import execute_reasoning_cqs
from services.realtime_qa.registration import register_runtime


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _expected_value_matches(actual: Any, expected: Any, term: Any = None) -> bool:
    """Compare JSON booleans by RDF datatype; retain legacy nonboolean policy."""
    if not isinstance(expected, bool):
        return str(actual) == str(expected)
    if (
        not isinstance(term, dict)
        or term.get("type") not in {"literal", "typed-literal"}
        or term.get("datatype") != "http://www.w3.org/2001/XMLSchema#boolean"
        or "xml:lang" in term
    ):
        return False
    lexical = term.get("value")
    if not isinstance(lexical, str) or actual != lexical:
        return False
    return lexical in ({"true", "1"} if expected else {"false", "0"})


def _validation_timeout(value: float) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or not 0 < seconds <= 600:
        raise ValueError("S7 runtime validation timeout must be finite and within (0, 600] seconds")
    return seconds


def verify_query_validation_cases(
    client: Any, capabilities: dict[str, Any], *, timeout_seconds: float = 120.0,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run unchanged formal cases against a checksum-bound candidate or release client."""
    from services.realtime_qa.runtime_release import query_validation_code_fingerprint

    code_fingerprint = query_validation_code_fingerprint()
    cases = [
        (query_name, case)
        for query_name, capability in sorted(capabilities.items())
        for case in capability.get("validation_cases") or []
    ]
    if not cases:
        return {"status": "NOT_DECLARED", "case_count": 0, "query_execution_count": 0, "results": [],
                "validation_code_fingerprint": code_fingerprint}
    timeout = _validation_timeout(timeout_seconds)
    results: list[dict[str, Any]] = []
    # Invocation-local only: every case still checks its own frozen contract.
    executions: dict[tuple[str, str], dict[str, Any]] = {}
    validation_started = time.monotonic()
    # Contract/value/backend-rejection failures are independent per case, so
    # collect them all in one pass: each S6 run then exposes every broken CQ
    # instead of one per full S3→S6 rebuild. Timeouts and connection failures
    # still abort at once because later cases would only repeat them slowly.
    failures: list[tuple[str, Exception]] = []

    def report(status: str, *, error: str | None = None) -> None:
        if progress_callback is not None:
            progress_callback({
                "phase": "QUERY_VALIDATION",
                "status": status,
                "query_name": query_name,
                "case_id": case.get("id"),
                "completed": len(results),
                "total": len(cases),
                "case_started_at": case_started_at,
                "case_elapsed_seconds": time.monotonic() - case_started,
                "elapsed_seconds": time.monotonic() - validation_started,
                "results": list(results),
                **({"error": error} if error is not None else {}),
            })

    for query_name, case in cases:
        case_started = time.monotonic()
        case_started_at = _now()
        report("RUNNING")
        try:
            parameters = dict(case.get("parameters") or {})
            execution_key = (
                query_name,
                json.dumps(parameters, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
            )
            execution_reused = execution_key in executions
            if not execution_reused:
                # A freshly started Ontop endpoint compiles mappings lazily, so the
                # first query can exceed the per-request timeout. Retry exactly once
                # with the same frozen case; a second timeout is a real failure.
                for attempt in (1, 2):
                    try:
                        executions[execution_key] = client.select_with_metadata(query_name, **parameters)
                        break
                    except httpx.TimeoutException as exc:
                        if attempt == 2:
                            raise OntopRuntimeVerificationError(
                                f"query validation timed out twice after {timeout:g}s: "
                                f"{query_name}.{case.get('id')}"
                            ) from exc
                        report("RETRYING_AFTER_TIMEOUT")
            result = executions[execution_key]
            rows = result["rows"]
            variables = result["variables"]
            expected_fields = list(case.get("expected_fields") or [])
            # expected_fields describes the SELECT projection. SPARQL represents
            # a legitimate NULL by omitting its binding from an individual row.
            missing_fields = sorted(set(expected_fields) - set(variables))
            min_rows = int(case.get("min_rows") or 0)
            if len(rows) < min_rows or missing_fields:
                raise OntopRuntimeVerificationError(
                    f"query validation failed: {query_name}.{case.get('id')} "
                    f"(row_count={len(rows)}, min_rows={min_rows}, missing_fields={missing_fields})"
                )
            exact_result = None
            if "expected_rows" in case or "expected_row_fields" in case:
                try:
                    if set(case.get("expected_row_fields") or []) - set(variables):
                        raise ValueError("SELECT header omits complete-result fields")
                    exact_result = validate_cq_expected_rows(
                        question_id=f"{query_name}.{case['id']}", contract=case,
                        rows=typed_result_rows(result),
                    )
                except (WorkflowGateError, FormalFactContractError, ValueError, TypeError, KeyError) as exc:
                    raise OntopRuntimeVerificationError(
                        f"query complete result mismatch: {query_name}.{case.get('id')}: {exc}"
                    ) from exc
            conditional_nulls: dict[str, dict[str, int]] = {}
            for question_id, cq_binding in (capabilities[query_name].get("cq_bindings") or {}).items():
                if cq_binding["validation_case_id"] != case["id"]:
                    continue
                # Reuse S6's strict required-value check for this exact frozen
                # CQ case. Other diagnostic cases retain their own projection
                # and value assertions, with no invented non-null policy.
                try:
                    conditional_nulls[question_id] = validate_cq_required_bindings(
                        question_id=question_id,
                        contract={
                            "required_bindings": expected_fields,
                            "min_rows": min_rows,
                            "nullable_bindings": cq_binding.get("nullable_bindings"),
                        },
                        variables=variables,
                        rows=rows,
                    )
                except WorkflowGateError as exc:
                    raise OntopRuntimeVerificationError(
                        f"query required binding validation failed: {query_name}.{case.get('id')}: {exc}"
                    ) from exc
            expected_first_row = dict(case.get("expected_first_row") or {})
            row_terms = result.get("row_terms") or []
            first_terms = row_terms[0] if row_terms else {}
            if expected_first_row and (
                not rows
                or any(
                    not _expected_value_matches(
                        rows[0].get(field), expected, first_terms.get(field),
                    )
                    for field, expected in expected_first_row.items()
                )
            ):
                raise OntopRuntimeVerificationError(
                    f"query validation value mismatch: {query_name}.{case.get('id')}"
                )
            results.append(
                {
                    "query_name": query_name,
                    "case_id": case.get("id"),
                    "execution_reused": execution_reused,
                    "elapsed_seconds": time.monotonic() - case_started,
                    "row_count": len(rows),
                    "returned_fields": variables,
                    "unbound_field_counts": {
                        field: sum(row.get(field) is None for row in rows)
                        for field in expected_fields if any(row.get(field) is None for row in rows)
                    },
                    "conditional_null_counts": conditional_nulls,
                    **({"expected_rows_validation": exact_result} if exact_result is not None else {}),
                    **({"query_execution": result["query_execution"]} if result.get("query_execution") else {}),
                    "status": "PASSED",
                }
            )
        except Exception as exc:
            report("FAILED", error=f"{type(exc).__name__}: {exc}")
            if isinstance(exc, httpx.TimeoutException | httpx.TransportError) or isinstance(
                exc.__cause__, httpx.TimeoutException | httpx.TransportError,
            ) or not isinstance(exc, OntopRuntimeVerificationError | httpx.HTTPStatusError):
                raise
            failures.append((f"{query_name}.{case.get('id')}", exc))
            continue
        report("PASSED")
    if len(failures) == 1:
        raise failures[0][1]
    if failures:
        first = failures[0][1]
        others = "；".join(f"{label}: {type(exc).__name__}: {str(exc)[:600]}" for label, exc in failures[1:])
        raise OntopRuntimeVerificationError(
            f"{first}\n另有 {len(failures) - 1} 个验证用例失败（共 {len(failures)}/{len(cases)}，"
            f"通过 {len(results)}）：{others}"
        ) from first
    return {
        "status": "PASSED", "case_count": len(results),
        "validation_code_fingerprint": code_fingerprint,
        "query_execution_count": len(executions), "results": results,
    }


@dataclass(frozen=True)
class DeploymentAutomationConfig:
    workflow_root: Path
    config_root: Path
    deployment_root: Path
    registry_path: Path
    timeout_seconds: float = 120.0
    verify_interval_seconds: float = 2.0
    require_semantica_sync: bool = False

    @classmethod
    def from_env(cls, workflow_root: Path) -> DeploymentAutomationConfig:
        runtime_root = workflow_root.parent / ".orion-runtime"

        def env_path(name: str, default: Path) -> Path:
            value = str(os.getenv(name) or "").strip() or str(default)
            return Path(value).expanduser().resolve()

        return cls(
            workflow_root=workflow_root.resolve(),
            config_root=env_path(
                "ORION_S7_AUTO_DEPLOY_CONFIG_ROOT",
                runtime_root / "ontop-config",
            ),
            deployment_root=env_path(
                "ORION_S7_AUTO_DEPLOY_ROOT",
                runtime_root / "realtime-business",
            ),
            registry_path=env_path(
                "ORION_REALTIME_RUNTIME_REGISTRY",
                runtime_root / "realtime-runtime-registry.json",
            ),
            timeout_seconds=_validation_timeout(
                float(str(os.getenv("ORION_S7_AUTO_DEPLOY_TIMEOUT") or "").strip() or "120")
            ),
            verify_interval_seconds=float(
                str(os.getenv("ORION_S7_AUTO_DEPLOY_VERIFY_INTERVAL") or "").strip()
                or "2"
            ),
            require_semantica_sync=_enabled(
                os.getenv("ORION_S7_REQUIRE_SEMANTICA_SYNC", "true")
            ),
        )


class SemanticaSyncVerificationError(RuntimeError):
    """Preserve the failed read-back receipt in the durable deployment status."""

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result
        detail = str(result.get("detail") or result.get("status") or "unknown")
        super().__init__(f"Semantica ontology sync was not verified: {detail}")


class S7DeploymentAutomation:
    """Durable post-S7 Ontop deployment with fail-closed runtime promotion."""

    ACTIVE_STATES = {"DEPLOYMENT_QUEUED", "DEPLOYING", "RUNTIME_VERIFYING"}
    # NOT_APPLICABLE completes deployment without claiming a query runtime is
    # ready. Keep the original receipt (including its verification flags).
    COMPLETED_STATES = {"ONTOP_READY", "DOCUMENT_RUNTIME_READY", "NOT_APPLICABLE"}

    @classmethod
    def _replayable_completion(cls, receipt: dict[str, Any]) -> bool:
        if receipt.get("state") not in cls.COMPLETED_STATES:
            return False
        if receipt["state"] != "ONTOP_READY":
            return True
        from services.realtime_qa.runtime_release import query_validation_code_fingerprint

        # Only an explicit deployment request reaches this check. A code reload
        # does not redeploy historical projects or rewrite their receipts.
        return (receipt.get("query_validation") or {}).get("validation_code_fingerprint") == query_validation_code_fingerprint()

    def __init__(
        self,
        config: DeploymentAutomationConfig,
        *,
        command_runner: Callable[[list[str], float], None] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        status_listener: Callable[[Path, str, dict[str, Any]], None] | None = None,
        ontology_synchronizer: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self._command_runner = command_runner or self._run_command
        self._sleeper = sleeper
        self._status_listener = status_listener
        self._ontology_synchronizer = ontology_synchronizer
        self._semaphore = threading.BoundedSemaphore(
            max(1, int(os.getenv("ORION_S7_AUTO_DEPLOY_WORKERS", "1")))
        )

    def set_status_listener(
        self,
        listener: Callable[[Path, str, dict[str, Any]], None] | None,
    ) -> None:
        self._status_listener = listener

    def set_ontology_synchronizer(
        self,
        synchronizer: Callable[..., dict[str, Any]] | None,
    ) -> None:
        self._ontology_synchronizer = synchronizer

    def enqueue(self, *, project_dir: Path, release_version: str) -> dict[str, Any]:
        project_dir = project_dir.resolve()
        status_path = self._status_path(project_dir.name, release_version)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = status_path.with_suffix(status_path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            # The worker holds this same cross-process lock for the whole deployment.
            # Therefore an ACTIVE state observed after acquiring it is stale and safe
            # to recover; a concurrent completed worker is observed as terminal.
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {
                    **self.status(project_dir=project_dir, release_version=release_version),
                    "idempotent_replay": True,
                }
            existing = self.status(project_dir=project_dir, release_version=release_version)
            if existing.get("failure_category") == "RELEASE_CONTRACT_INVALID":
                return {**existing, "retry_blocked": True}
            if self._replayable_completion(existing):
                return {**existing, "idempotent_replay": True}
            queued_at = existing.get("queued_at") or _now()
            queued = {
                "schema_version": 1,
                "job_id": self._job_id(project_dir.name, release_version),
                "project_id": project_dir.name,
                "release_version": release_version,
                "state": "DEPLOYMENT_QUEUED",
                "artifact_verified": False,
                "runtime_verified": False,
                "queued_at": queued_at,
                "updated_at": _now(),
                "attempt": int(existing.get("attempt") or 0),
                "degraded_reason": None,
                "history": [
                    *list(existing.get("history") or []),
                    {"state": "DEPLOYMENT_QUEUED", "at": queued_at},
                ],
            }
            self._write_status(status_path, queued)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        thread = threading.Thread(
            target=self._run_in_background,
            kwargs={"project_dir": project_dir, "release_version": release_version},
            name=f"orion-s7-deploy-{project_dir.name}-{release_version}",
            daemon=True,
        )
        thread.start()
        return queued

    def run_now(self, *, project_dir: Path, release_version: str) -> dict[str, Any]:
        project_dir = project_dir.resolve()
        status_path = self._status_path(project_dir.name, release_version)
        status_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = status_path.with_suffix(status_path.suffix + ".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = self.status(project_dir=project_dir, release_version=release_version)
            if current.get("failure_category") == "RELEASE_CONTRACT_INVALID":
                return {**current, "retry_blocked": True}
            if self._replayable_completion(current):
                return {**current, "idempotent_replay": True}
            self._run_locked(project_dir, release_version, status_path, current)
            # Return the same durable JSON contract projected to workflow listeners.
            return self.status(project_dir=project_dir, release_version=release_version)

    def status(self, *, project_dir: Path, release_version: str) -> dict[str, Any]:
        path = self._status_path(project_dir.resolve().name, release_version)
        if not path.exists():
            return {
                "schema_version": 1,
                "project_id": project_dir.resolve().name,
                "release_version": release_version,
                "state": "NOT_QUEUED",
                "artifact_verified": False,
                "runtime_verified": False,
                "degraded_reason": "S7 deployment automation has not queued this release",
            }
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("deployment status must be a JSON object")
        return payload

    def recover_pending(self) -> None:
        jobs_root = self.config.deployment_root / "jobs"
        if not jobs_root.exists():
            return
        for status_path in sorted(jobs_root.glob("*/*.json")):
            try:
                payload = json.loads(status_path.read_text(encoding="utf-8"))
                state = str(payload.get("state") or "")
                project_id = str(payload.get("project_id") or "")
                release_version = str(payload.get("release_version") or "")
                project_dir = self.config.workflow_root / project_id
                if state in self.ACTIVE_STATES and project_dir.is_dir() and release_version:
                    self.enqueue(project_dir=project_dir, release_version=release_version)
            except (OSError, ValueError, json.JSONDecodeError):
                continue

    def _run_in_background(self, *, project_dir: Path, release_version: str) -> None:
        with self._semaphore:
            try:
                self.run_now(project_dir=project_dir, release_version=release_version)
            except Exception as exc:
                # Unexpected failures can occur after registry activation but before
                # the terminal receipt is written. Never leave a dead worker ACTIVE.
                status_path = self._status_path(project_dir.name, release_version)
                try:
                    status_path.parent.mkdir(parents=True, exist_ok=True)
                    with status_path.with_suffix(status_path.suffix + ".lock").open("a+") as lock:
                        try:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            return  # A recovery worker now owns the operation.
                        current = self.status(project_dir=project_dir, release_version=release_version)
                        if current.get("state") not in self.COMPLETED_STATES:
                            self._fail(
                                status_path, current, "DEPLOYMENT_FAILED",
                                f"Unhandled deployment worker error: {type(exc).__name__}",
                                worker_error_type=type(exc).__name__,
                                **self._semantica_failure_fields(exc),
                            )
                except Exception as receipt_error:
                    logging.getLogger(__name__).error(
                        "S7 worker failed (%s); failure receipt unavailable (%s)",
                        type(exc).__name__, type(receipt_error).__name__,
                    )
                return

    def _run_locked(
        self,
        project_dir: Path,
        release_version: str,
        status_path: Path,
        current: dict[str, Any],
    ) -> dict[str, Any]:
        publication = self._read_json(project_dir / "07-release/publication.json")
        if str(publication.get("release_version") or "") != release_version:
            return self._fail(
                status_path,
                current,
                "DEPLOYMENT_FAILED",
                "published release version does not match deployment job",
            )
        # Every source mode performs external work. Persist an active receipt
        # before that work so restart recovery can find document-only jobs too.
        current = self._transition(
            current,
            "DEPLOYING",
            attempt=int(current.get("attempt") or 0) + 1,
            started_at=_now(),
            artifact_verified=False,
            runtime_verified=False,
            degraded_reason=None,
        )
        self._write_status(status_path, current)
        if publication.get("realtime_query_capability") != "PACKAGED_ARTIFACT_VERIFIED":
            if (
                publication.get("document_runtime_capability")
                == "CURRENT_VERSION_SEARCH_PACKAGED"
            ):
                try:
                    binding = ReleaseBindingLoader().load(
                        project_dir,
                        allow_pre_runtime=True,
                    )
                    semantica_sync = self._sync_ontology_release(
                        project_dir=project_dir,
                        release_version=release_version,
                        semantica_url=os.getenv(
                            "SEMANTICA_API_URL", "http://127.0.0.1:8001"
                        ),
                    )
                    document_count = self._publish_documents(project_dir, binding)
                    registration = register_runtime(
                        registry_path=self.config.registry_path,
                        project_dir=project_dir,
                        allow_pre_runtime=True,
                        set_default=False,
                    )
                    result = self._transition(
                        current,
                        "DOCUMENT_RUNTIME_READY",
                        artifact_verified=True,
                        runtime_verified=True,
                        release_fingerprint=binding.release_fingerprint,
                        current_document_count=document_count,
                        semantica_sync=semantica_sync,
                        reasoning_validation=registration.get("reasoning_validation"),
                        document_cq_validation=registration.get("document_cq_validation"),
                        default_project_id=registration["default_project_id"],
                        degraded_reason=None,
                    )
                except Exception as exc:
                    result = self._transition(
                        current,
                        "DOCUMENT_RUNTIME_FAILED",
                        artifact_verified=False,
                        runtime_verified=False,
                        **self._semantica_failure_fields(exc),
                        degraded_reason=f"{type(exc).__name__}: {exc}",
                    )
                self._write_status(status_path, result)
                return result
            try:
                semantica_sync = self._sync_ontology_release(
                    project_dir=project_dir,
                    release_version=release_version,
                    semantica_url=os.getenv(
                        "ORION_SEMANTICA_URL", "http://127.0.0.1:8001"
                    ),
                )
                result = self._transition(
                    current,
                    "NOT_APPLICABLE",
                    artifact_verified=True,
                    runtime_verified=False,
                    semantica_sync=semantica_sync,
                    degraded_reason=None,
                    message="This release has no Ontop runtime contract.",
                )
            except Exception as exc:
                result = self._transition(
                    current,
                    "DEPLOYMENT_FAILED",
                    artifact_verified=True,
                    runtime_verified=False,
                    **self._semantica_failure_fields(exc),
                    degraded_reason=f"{type(exc).__name__}: {exc}",
                )
            self._write_status(status_path, result)
            return result

        running = current

        try:
            prepared = ensure_deployment_input(self.config.config_root, project_dir.name)
        except (DeploymentInputUnavailable, PostgresReadOnlyVerificationError) as exc:
            return self._fail(
                status_path,
                running,
                "WAITING_CONFIGURATION",
                f"missing deployment input for project {project_dir.name}: {exc}",
            )
        config_path = prepared["config_path"]
        if prepared["status"] == "PREPARED":
            running.update(deployment_input_prepared_by_platform=True)

        try:
            deployment_input = self._deployment_input(config_path)
            binding = ReleaseBindingLoader().load(
                project_dir,
                allow_pre_runtime=True,
            )
            output_dir = self.config.deployment_root / project_dir.name / release_version
            deployment = render_ontop_release_deployment(
                binding,
                output_dir=output_dir,
                properties_path=deployment_input["properties_path"],
                read_only_attestation_path=deployment_input[
                    "read_only_attestation_path"
                ],
                endpoint=deployment_input["endpoint"],
                image=deployment_input["image"],
                network_name=deployment_input["network_name"],
            )
            running.update(
                {
                    "artifact_verified": True,
                    "ontop_deployment_id": binding.ontop_deployment_id,
                    "endpoint": deployment_input["endpoint"],
                    "updated_at": _now(),
                }
            )
            self._write_status(status_path, running)
            self._command_runner(
                [
                    "docker",
                    "compose",
                    "-f",
                    str(deployment["compose_path"]),
                    "up",
                    "-d",
                ],
                self.config.timeout_seconds,
            )
            verifying = self._transition(running, "RUNTIME_VERIFYING")
            # Keep the latest phase/history and durable validation progress on failure.
            running = verifying
            self._write_status(status_path, verifying)
            identity = self._verify_until_ready(
                binding=binding,
                deployment_binding_path=Path(deployment["deployment_binding_path"]),
                endpoint=deployment_input["endpoint"],
            )
            def record_query_progress(progress: dict[str, Any]) -> None:
                verifying.update(validation_progress=progress, updated_at=_now())
                # Per-case progress is diagnostic. Avoid the expensive workflow
                # projection until the next actual deployment state transition.
                self._write_status_file(status_path, verifying)

            def record_reasoning_progress(progress: dict[str, Any]) -> None:
                verifying.update(
                    validation_progress=progress,
                    reasoning_validation_progress=progress,
                    reasoning_validation={
                        "status": progress["status"],
                        "case_count": progress["completed"],
                        "results": progress["results"],
                    },
                    updated_at=_now(),
                )
                self._write_status_file(status_path, verifying)

            query_validation = self._verify_query_validation_cases(
                binding,
                deployment_input["endpoint"],
                timeout_seconds=self.config.timeout_seconds,
                progress_callback=record_query_progress,
            )
            verifying["query_validation"] = query_validation
            verifying["document_cq_validation"] = self._verify_document_cq_validation_cases(binding)
            reasoning_validation = self._verify_reasoning_validation_cases(
                binding,
                deployment_input["endpoint"],
                deployment_input["semantica_url"],
                timeout_seconds=self.config.timeout_seconds,
                progress_callback=record_reasoning_progress,
            )
            verifying["reasoning_validation"] = reasoning_validation
            semantica_sync = self._sync_ontology_release(
                project_dir=project_dir,
                release_version=release_version,
                semantica_url=deployment_input["semantica_url"],
            )
            document_count = self._publish_documents(project_dir, binding)
            registration = register_runtime(
                registry_path=self.config.registry_path,
                project_dir=project_dir,
                deployment_binding_path=Path(deployment["deployment_binding_path"]),
                allow_pre_runtime=True,
                set_default=deployment_input["set_default"],
            )
            ready = self._transition(
                verifying,
                "ONTOP_READY",
                artifact_verified=True,
                runtime_verified=True,
                release_fingerprint=binding.release_fingerprint,
                verified_at=identity.verified_at.isoformat(),
                query_validation=query_validation,
                reasoning_validation=reasoning_validation,
                semantica_sync=semantica_sync,
                registered_at=_now(),
                current_document_count=document_count,
                default_project_id=registration["default_project_id"],
                degraded_reason=None,
            )
            self._write_status(status_path, ready)
            return ready
        except Exception as exc:
            failed = self._fail(
                status_path,
                running,
                "DEPLOYMENT_FAILED",
                f"{type(exc).__name__}: {exc}",
                **self._semantica_failure_fields(exc),
            )
            return failed

    def _sync_ontology_release(
        self,
        *,
        project_dir: Path,
        release_version: str,
        semantica_url: str,
    ) -> dict[str, Any]:
        if not self.config.require_semantica_sync:
            return {
                "status": "NOT_REQUIRED",
                "project_id": project_dir.name,
                "release_version": release_version,
            }
        if self._ontology_synchronizer is None:
            raise RuntimeError("S7 requires a Semantica ontology synchronizer")
        result = self._ontology_synchronizer(
            project_dir=project_dir,
            release_version=release_version,
            semantica_url=semantica_url,
        )
        if result.get("status") != "SYNCED" or result.get("registry_verified") is not True:
            raise SemanticaSyncVerificationError(result)
        return result

    @staticmethod
    def _semantica_failure_fields(exc: Exception) -> dict[str, Any]:
        fields: dict[str, Any] = {}
        if isinstance(exc, FormalFactContractError):
            fields.update(failure_category="RELEASE_CONTRACT_INVALID", contract_error=exc.to_dict())
        progress = getattr(exc, "reasoning_validation_progress", None)
        if isinstance(progress, dict):
            fields.update(
                validation_progress=progress,
                reasoning_validation_progress=progress,
                reasoning_validation={"status": "FAILED", "case_count": progress["completed"],
                                      "results": progress["results"]},
            )
        if isinstance(exc, SemanticaSyncVerificationError):
            fields["semantica_sync"] = exc.result
        return fields

    @staticmethod
    def _publish_documents(project_dir: Path, binding: Any) -> int:
        if not getattr(binding, "document_query_capabilities", None):
            return 0
        database_url = str(os.getenv("ORION_WORKFLOW_DATABASE_URL") or "").strip()
        if not database_url:
            raise RuntimeError(
                "ORION_WORKFLOW_DATABASE_URL is required for document runtime publication"
            )
        from services.fuseki_client.client import FusekiClient
        from services.ontology_engineering.storage import (
            MinioArtifactStore,
            PostgresWorkflowMetadataStore,
        )
        from services.realtime_qa.document_index import S0IncrementalDocumentIndexer
        from services.realtime_qa.incremental_pipeline import (
            MinioOriginalSnapshotStore,
            S0IncrementalPipeline,
        )
        from services.realtime_qa.postgres_registry import PostgresDocumentCurrentRegistry
        from services.realtime_qa.s0_adapter import S0RealtimePublisher

        PostgresWorkflowMetadataStore(database_url).ensure_schema()
        registry = PostgresDocumentCurrentRegistry(database_url)
        pipeline = S0IncrementalPipeline(
            MinioOriginalSnapshotStore(MinioArtifactStore.from_env()),
            S0IncrementalDocumentIndexer(
                FusekiClient(os.getenv("FUSEKI_URL", "http://127.0.0.1:3030/supply"))
            ),
            registry,
        )
        results = S0RealtimePublisher(pipeline).publish(
            project_dir=project_dir,
            input_root=project_dir,
            binding=binding,
        )
        if not results:
            raise RuntimeError("release declares document search but S0 has no documents")
        return registry.count_current(binding.project_id)

    def _verify_until_ready(self, *, binding, deployment_binding_path: Path, endpoint: str):
        deadline = time.monotonic() + self.config.timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            client = ReleaseBindingLoader().build_ontop_client(binding, endpoint, timeout=3.0)
            try:
                return OntopDeploymentVerifier().verify_endpoint(
                    client,
                    binding,
                    deployment_binding_path,
                )
            except OntopRuntimeVerificationError as exc:
                last_error = exc
                self._sleeper(self.config.verify_interval_seconds)
        raise OntopRuntimeVerificationError(
            f"Ontop runtime did not become ready before timeout: {last_error}"
        )

    @staticmethod
    def _verify_query_validation_cases(
        binding: Any, endpoint: str, *, timeout_seconds: float = 120.0,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        capabilities = getattr(binding, "ontop_query_capabilities", {}) or {}
        if not any(capability.get("validation_cases") for capability in capabilities.values()):
            return verify_query_validation_cases(None, capabilities)
        client = ReleaseBindingLoader().build_ontop_client(
            binding, endpoint, timeout=_validation_timeout(timeout_seconds),
        )
        return verify_query_validation_cases(
            client, capabilities,
            timeout_seconds=timeout_seconds, progress_callback=progress_callback,
        )

    @staticmethod
    def _verify_document_cq_validation_cases(binding: Any) -> dict[str, Any]:
        from services.realtime_qa.document_cq import execute_document_cqs

        results = []
        for name, capability in sorted((getattr(binding, "document_fact_queries", {}) or {}).items()):
            if capability.get("cq_bindings"):
                executed = execute_document_cqs(binding, name, validate=True)
                if not executed or any(item.get("status") != "PASSED" for item in executed):
                    raise ValueError(f"document fact CQ validation failed: {name}")
                results.extend(executed)
        return {"status": "PASSED" if results else "NOT_DECLARED", "case_count": len(results), "results": results}

    @staticmethod
    def _verify_reasoning_validation_cases(
        binding: Any,
        endpoint: str,
        semantica_url: str,
        *,
        timeout_seconds: float = 120.0,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        capabilities = getattr(binding, "reasoning_capabilities", {}) or {}
        if not capabilities:
            return {"status": "NOT_DECLARED", "case_count": 0, "results": []}
        timeout = _validation_timeout(timeout_seconds)
        # Document evidence capabilities have no Ontop assets or client.
        # Construct a structured client lazily only for a structured evidence query.
        client = None
        semantica = SemanticaReasoningClient(semantica_url, timeout=timeout)
        results: list[dict[str, Any]] = []
        name: str | None = None
        step = "SERVICE_HEALTH"
        started = time.monotonic()

        def report(status: str, error: Exception | None = None) -> dict[str, Any]:
            progress = {
                "phase": "REASONING_VALIDATION", "step": step, "status": status,
                "capability_name": name, "completed": len(results), "total": len(capabilities),
                "elapsed_seconds": time.monotonic() - started, "results": list(results),
                **({"error": f"{type(error).__name__}: {error}"} if error is not None else {}),
                **({"failure_category": "RELEASE_CONTRACT_INVALID", "contract_error": error.to_dict()}
                   if isinstance(error, FormalFactContractError) else {}),
            }
            if progress_callback is not None:
                progress_callback(progress)
            return progress

        report("RUNNING")
        try:
            try:
                semantica.health()
            except httpx.TimeoutException as exc:
                raise OntopRuntimeVerificationError(
                    f"reasoning service health timed out after {timeout:g}s"
                ) from exc
            for name, capability in sorted(capabilities.items()):
                step = "EVIDENCE_FACTS"
                report("RUNNING")
                validation = dict(capability["runtime_validation"])
                parameters = dict(validation.get("parameters") or {})
                rows: list[dict[str, Any]] = []
                symbol_table: dict[str, str] = {}
                evidence_query = str(capability["evidence_query"])
                document_fact_query = (
                    getattr(binding, "document_fact_queries", {}) or {}
                ).get(evidence_query)
                if document_fact_query is not None:
                    facts = document_evidence_facts(
                        list(document_fact_query.get("facts") or []),
                        list(capability["fact_bindings"]),
                        symbol_table,
                        parameters=parameters,
                    )
                else:
                    if client is None:
                        client = ReleaseBindingLoader().build_ontop_client(
                            binding, endpoint, timeout=timeout
                        )
                    try:
                        rows = client.select(evidence_query, **parameters)
                    except httpx.TimeoutException as exc:
                        raise OntopRuntimeVerificationError(
                            f"reasoning evidence query timed out after {timeout:g}s: {name}.{evidence_query}"
                        ) from exc
                    facts = facts_from_rows(
                        rows,
                        parameters,
                        list(capability["fact_bindings"]),
                        symbol_table,
                    )
                formal_rules = list(binding.reasoning_rule_packages[name]["rules"])
                closed_world_predicates = [
                    str(item["predicate"])
                    for item in capability.get("closed_world_inputs") or []
                ]
                step = "RULE_EXECUTION"
                report("RUNNING")
                try:
                    response = semantica.run_forward(
                        facts=facts,
                        rules=[str(rule["expression"]) for rule in formal_rules],
                        **(
                            {"closed_world_predicates": closed_world_predicates}
                            if closed_world_predicates
                            else {}
                        ),
                    )
                except httpx.TimeoutException as exc:
                    raise OntopRuntimeVerificationError(
                        f"reasoning execution timed out after {timeout:g}s: {name}"
                    ) from exc
                selected = result_facts(
                    list(response["inferred_facts"]),
                    list(capability["result_predicates"]),
                )
                semantic_facts(
                    selected,
                    dict(capability["ontology_terms"]),
                    symbol_table,
                )
                minimum_input = int(validation["min_input_facts"])
                minimum_results = int(validation["min_result_facts"])
                rules_fired = int(response.get("rules_fired") or 0)
                expected_live_outcome = str(
                    validation.get("expected_live_outcome") or "POSITIVE"
                ).upper()
                if (
                    len(facts) < minimum_input
                    or len(selected) < minimum_results
                    or (validation["require_rules_fired"] and rules_fired < 1)
                    or (selected and not list(response.get("trace") or []))
                    or (expected_live_outcome == "POSITIVE" and not selected)
                    or (expected_live_outcome == "NEGATIVE" and bool(selected))
                ):
                    raise OntopRuntimeVerificationError(
                        f"reasoning validation failed: {name}"
                    )
                step = "REASONING_CQ_VALIDATION"
                report("RUNNING")
                results.append(
                    {
                        "capability_name": name,
                        "evidence_row_count": len(rows),
                        "input_fact_count": len(facts),
                        "result_fact_count": len(selected),
                        "rules_fired": rules_fired,
                        "expected_live_outcome": expected_live_outcome,
                        "rule_sha256": capability["rule_sha256"],
                        "cq_validation": execute_reasoning_cqs(
                            binding, name, facts, list(response["inferred_facts"]),
                            symbol_table, validate=True,
                        ),
                        "status": "PASSED",
                    }
                )
                report("RUNNING")
        except Exception as exc:
            if isinstance(exc, FormalFactContractError) and not exc.capability_name:
                exc.capability_name = name
            # Registration also calls this method without a callback. Carry the
            # same diagnostic to the document-only deployment failure handler.
            exc.reasoning_validation_progress = report("FAILED", exc)
            raise
        report("PASSED")
        return {"status": "PASSED", "case_count": len(results), "results": results}

    def _deployment_input(self, path: Path) -> dict[str, Any]:
        payload = self._read_json(path)
        allowed = {
            "endpoint",
            "properties_path",
            "read_only_attestation_path",
            "image",
            "network_name",
            "set_default",
            "semantica_url",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"unsupported deployment input fields: {sorted(unknown)}")
        base = path.parent

        def resolve_file(name: str) -> Path:
            value = str(payload.get(name) or "").strip()
            if not value:
                raise ValueError(f"deployment input is missing {name}")
            candidate = Path(value).expanduser()
            return (candidate if candidate.is_absolute() else base / candidate).resolve()

        endpoint = str(payload.get("endpoint") or "").strip()
        if not endpoint:
            raise ValueError("deployment input is missing endpoint")
        return {
            "endpoint": endpoint,
            "properties_path": resolve_file("properties_path"),
            "read_only_attestation_path": resolve_file("read_only_attestation_path"),
            "image": str(
                payload.get("image") or "ontology-workorder-agent/ontop:5.3.0"
            ),
            "network_name": str(payload.get("network_name") or "").strip() or None,
            "set_default": bool(payload.get("set_default", False)),
            "semantica_url": str(
                payload.get("semantica_url")
                or os.getenv("SEMANTICA_API_URL")
                or "http://127.0.0.1:8001"
            ).rstrip("/"),
        }

    def _fail(
        self,
        path: Path,
        current: dict[str, Any],
        state: str,
        reason: str,
        **fields: Any,
    ) -> dict[str, Any]:
        payload = self._transition(
            current,
            state,
            runtime_verified=False,
            **fields,
            degraded_reason=reason,
        )
        self._write_status(path, payload)
        return payload

    @staticmethod
    def _transition(
        current: dict[str, Any],
        state: str,
        **fields: Any,
    ) -> dict[str, Any]:
        changed_at = _now()
        return {
            **current,
            **fields,
            "state": state,
            "updated_at": changed_at,
            "history": [
                *list(current.get("history") or []),
                {"state": state, "at": changed_at},
            ],
        }

    def _status_path(self, project_id: str, release_version: str) -> Path:
        safe_version = release_version.replace("/", "_")
        return self.config.deployment_root / "jobs" / project_id / f"{safe_version}.json"

    @staticmethod
    def _job_id(project_id: str, release_version: str) -> str:
        digest = hashlib.sha256(f"{project_id}\n{release_version}".encode()).hexdigest()
        return f"s7deploy-{digest[:20]}"

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"JSON object required: {path.name}")
        return payload

    def _write_status(self, path: Path, payload: dict[str, Any]) -> None:
        # Persist and project the same JSON-safe representation of typed query values.
        payload = json.loads(json.dumps(payload, default=query_json_scalar, allow_nan=False))
        self._write_status_file(path, payload)
        if self._status_listener is None:
            return
        project_dir = self.config.workflow_root / str(payload.get("project_id") or "")
        release_version = str(payload.get("release_version") or "")
        try:
            self._status_listener(project_dir, release_version, payload)
        except Exception as exc:
            # Runtime promotion must remain durable even when the workflow projection
            # cannot be refreshed.  The API surfaces this field and the next status
            # reconciliation can safely replay the transition.
            self._write_status_file(
                path,
                {
                    **payload,
                    "workflow_status_sync_error": f"{type(exc).__name__}: {exc}",
                },
            )

    @staticmethod
    def _write_status_file(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2,
                          default=query_json_scalar, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _run_command(command: list[str], timeout: float) -> None:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )


def build_s7_deployment_automation(
    workflow_root: Path,
    *,
    status_listener: Callable[[Path, str, dict[str, Any]], None] | None = None,
    ontology_synchronizer: Callable[..., dict[str, Any]] | None = None,
) -> S7DeploymentAutomation | None:
    if not _enabled(os.getenv("ORION_S7_AUTO_DEPLOY")):
        return None
    automation = S7DeploymentAutomation(
        DeploymentAutomationConfig.from_env(workflow_root),
        status_listener=status_listener,
        ontology_synchronizer=ontology_synchronizer,
    )
    automation.recover_pending()
    return automation
