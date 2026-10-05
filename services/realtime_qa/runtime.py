from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import psycopg
from pydantic import ValidationError

from services.fuseki_client.client import FusekiClient
from services.ontop_client.client import OntopClient
from services.realtime_qa.binding import (
    OntopDeploymentVerifier,
    OntopRuntimeVerificationError,
    ReleaseBindingError,
    ReleaseBindingLoader,
)
from services.realtime_qa.deployment import verify_ontop_deployment_binding
from services.realtime_qa.models import (
    EvidenceBundle,
    OntologyReleaseBinding,
    RealtimeEvidenceRequest,
    RealtimeRuntimeBinding,
    RealtimeRuntimeCatalog,
    RealtimeRuntimeRegistryConfig,
)
from services.realtime_qa.postgres_registry import PostgresDocumentCurrentRegistry
from services.realtime_qa.reasoning import SemanticaReasoningClient
from services.realtime_qa.service import HybridRealtimeEvidenceService


class DocumentOnlyOntopClient:
    """Non-network placeholder that keeps document-only evidence fail-closed."""

    allowed_queries = frozenset()
    runtime_verification_status = "NOT_APPLICABLE"

    def __init__(self, release_fingerprint: str) -> None:
        self.bound_release_fingerprint = release_fingerprint

    def select(self, name: str, **parameters: Any) -> list[dict[str, Any]]:
        raise RuntimeError("document-only release does not allow structured queries")


@dataclass(frozen=True)
class RealtimeEvidenceRuntime:
    binding: OntologyReleaseBinding
    ontop: OntopClient | DocumentOnlyOntopClient
    fuseki: FusekiClient
    current_registry: PostgresDocumentCurrentRegistry
    service: HybridRealtimeEvidenceService
    semantica: SemanticaReasoningClient | None = None
    runtime_verification_error: str | None = None
    reasoning_runtime_verification_error: str | None = None
    deployment_binding_path: Path | None = None
    project_dir: Path | None = None
    allow_pre_runtime: bool = False

    @property
    def runtime_verified(self) -> bool:
        return (
            not self.binding.structured_query_enabled
            or self.ontop.runtime_verification_status == "VERIFIED"
        )

    def ensure_available(self) -> None:
        if self.project_dir is None:
            return  # In-memory adapters; production builders always supply provenance.
        try:
            ReleaseBindingLoader().validate_publication(
                self.project_dir, binding=self.binding,
                allow_pre_runtime=self.allow_pre_runtime,
            )
        except (ReleaseBindingError, OSError, UnicodeError) as exc:
            raise RealtimeRuntimeUnavailable(str(exc)) from exc

    def collect(self, request: RealtimeEvidenceRequest) -> EvidenceBundle:
        self.ensure_available()
        return self.service.collect(request)

    @property
    def reasoning_runtime_verified(self) -> bool:
        return bool(self.binding.reasoning_capabilities) and (
            self.reasoning_runtime_verification_error is None
        )


class RealtimeRuntimeRegistryError(ValueError):
    """The runtime registry cannot safely resolve a requested ontology project."""


class RealtimeRuntimeSelectionRequired(RealtimeRuntimeRegistryError):
    pass


class RealtimeRuntimeNotFound(RealtimeRuntimeRegistryError):
    pass


class RealtimeRuntimeUnavailable(RealtimeRuntimeRegistryError):
    pass


class RealtimeRuntimeRegistry:
    """Resolve isolated, release-bound runtimes by ontology engineering project."""

    def __init__(
        self,
        runtimes: dict[str, RealtimeEvidenceRuntime],
        *,
        unavailable_projects: dict[str, str] | None = None,
        default_project_id: str | None = None,
    ) -> None:
        if default_project_id is not None and default_project_id not in runtimes:
            raise RealtimeRuntimeRegistryError(
                "default realtime project is not an available runtime"
            )
        self._runtimes = dict(runtimes)
        self._unavailable_projects = dict(unavailable_projects or {})
        self.default_project_id = default_project_id

    @classmethod
    def from_single(cls, runtime: RealtimeEvidenceRuntime) -> RealtimeRuntimeRegistry:
        project_id = runtime.binding.project_id
        return cls({project_id: runtime}, default_project_id=project_id)

    @property
    def project_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._runtimes))

    @property
    def unavailable_projects(self) -> dict[str, str]:
        return dict(self._unavailable_projects)

    def resolve(self, project_id: str | None = None) -> RealtimeEvidenceRuntime:
        selected = str(project_id or self.default_project_id or "").strip()
        if not selected:
            if not self._runtimes and not self._unavailable_projects:
                raise RealtimeRuntimeNotFound("no published realtime runtime is registered")
            if len(self._runtimes) == 1:
                selected = next(iter(self._runtimes))
            else:
                raise RealtimeRuntimeSelectionRequired(
                    "project_id is required when multiple realtime runtimes are registered"
                )
        runtime = self._runtimes.get(selected)
        if runtime is not None:
            _ensure_runtime_available(runtime)
            return runtime
        if selected in self._unavailable_projects:
            raise RealtimeRuntimeUnavailable(
                f"realtime runtime is unavailable for project {selected}: "
                f"{self._unavailable_projects[selected]}"
            )
        raise RealtimeRuntimeNotFound(
            f"no realtime runtime is registered for project {selected}"
        )

    def catalog(self) -> RealtimeRuntimeCatalog:
        available = []
        unavailable = self.unavailable_projects
        for project_id in sorted(self._runtimes):
            try:
                available.append(runtime_binding(self._runtimes[project_id]))
            except RealtimeRuntimeUnavailable as exc:
                unavailable[project_id] = str(exc)
        return RealtimeRuntimeCatalog(
            default_project_id=self.default_project_id,
            runtimes=available,
            unavailable_projects=unavailable,
        )


def _ensure_runtime_available(runtime: RealtimeEvidenceRuntime) -> None:
    checker = getattr(runtime, "ensure_available", None)
    if checker is not None:
        checker()



def build_realtime_evidence_runtime(
    *,
    project_dir: Path,
    deployment_binding_path: Path | None,
    database_url: str,
    fuseki_url: str,
    semantica_url: str = "http://127.0.0.1:8001",
    timeout: float = 5.0,
    query_timeout: float = 30.0,
    allow_pre_runtime: bool = False,
) -> RealtimeEvidenceRuntime:
    """Build a release-bound runtime with separate probe and business budgets.

    ``timeout`` bounds endpoint verification and health requests; ``query_timeout``
    bounds each business HTTP request, not the entire multi-source collection.
    """

    _validate_timeouts(timeout, query_timeout)
    loader = ReleaseBindingLoader()
    binding = loader.load(project_dir.resolve(), allow_pre_runtime=allow_pre_runtime)
    runtime_error: str | None = None
    if binding.structured_query_enabled:
        if deployment_binding_path is None:
            raise RealtimeRuntimeRegistryError(
                "structured runtime requires deployment_binding_path"
            )
        deployment_path = deployment_binding_path.resolve()
        deployment = verify_ontop_deployment_binding(binding, deployment_path)
        endpoint = str(deployment.get("endpoint") or "")
        ontop: OntopClient | DocumentOnlyOntopClient = loader.build_ontop_client(
            binding,
            endpoint,
            timeout=timeout,
        )
        try:
            OntopDeploymentVerifier().verify_endpoint(
                ontop,
                binding,
                deployment_path,
            )
        except OntopRuntimeVerificationError as exc:
            # Keep the artifact-bound runtime available for document-only queries.
            # Structured requests remain fail-closed inside HybridRealtimeEvidenceService.
            runtime_error = str(exc)
        ontop.health_timeout = timeout
        ontop.timeout = query_timeout
    else:
        ontop = DocumentOnlyOntopClient(binding.release_fingerprint)

    fuseki = FusekiClient(fuseki_url, timeout=query_timeout, health_timeout=timeout)
    current_registry = PostgresDocumentCurrentRegistry(database_url)
    semantica: SemanticaReasoningClient | None = None
    reasoning_runtime_error: str | None = None
    if binding.reasoning_capabilities:
        semantica = SemanticaReasoningClient(
            semantica_url, timeout=query_timeout, health_timeout=timeout
        )
        try:
            semantica.health()
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            reasoning_runtime_error = f"Semantica runtime verification failed: {exc}"
    service = HybridRealtimeEvidenceService(
        binding,
        ontop,
        fuseki,
        current_registry,
        semantica,
    )
    return RealtimeEvidenceRuntime(
        binding=binding,
        project_dir=project_dir.resolve(),
        allow_pre_runtime=allow_pre_runtime,
        ontop=ontop,
        fuseki=fuseki,
        current_registry=current_registry,
        semantica=semantica,
        service=service,
        deployment_binding_path=deployment_binding_path.resolve() if binding.structured_query_enabled and deployment_binding_path else None,
        runtime_verification_error=runtime_error,
        reasoning_runtime_verification_error=reasoning_runtime_error,
    )


def build_realtime_runtime_registry(
    *,
    registry_path: Path,
    database_url: str,
    fuseki_url: str,
    semantica_url: str = "http://127.0.0.1:8001",
    timeout: float = 5.0,
    query_timeout: float = 30.0,
) -> RealtimeRuntimeRegistry:
    """Load multiple isolated runtimes without allowing one broken entry to mask others."""

    _validate_timeouts(timeout, query_timeout)
    registry_path = registry_path.resolve()
    try:
        raw: Any = json.loads(registry_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RealtimeRuntimeRegistryError("realtime runtime registry is missing") from exc
    except json.JSONDecodeError as exc:
        raise RealtimeRuntimeRegistryError(
            "realtime runtime registry is invalid JSON"
        ) from exc
    try:
        config = RealtimeRuntimeRegistryConfig.model_validate(raw)
    except ValidationError as exc:
        raise RealtimeRuntimeRegistryError(
            f"realtime runtime registry is invalid: {exc.errors()[0]['msg']}"
        ) from exc

    runtimes: dict[str, RealtimeEvidenceRuntime] = {}
    unavailable: dict[str, str] = {}
    for entry in config.runtimes:
        if not entry.enabled:
            continue
        project_dir = _registry_relative_path(registry_path.parent, entry.project_dir)
        deployment_path = (
            _registry_relative_path(registry_path.parent, entry.deployment_binding_path)
            if entry.deployment_binding_path
            else None
        )
        try:
            runtime = build_realtime_evidence_runtime(
                project_dir=project_dir,
                deployment_binding_path=deployment_path,
                database_url=database_url,
                fuseki_url=fuseki_url,
                semantica_url=semantica_url,
                timeout=timeout,
                query_timeout=query_timeout,
            )
            if runtime.binding.project_id != entry.project_id:
                raise RealtimeRuntimeRegistryError(
                    "registry project_id does not match the verified S7 package"
                )
            runtimes[entry.project_id] = runtime
        except (OSError, RuntimeError, ValueError) as exc:
            unavailable[entry.project_id] = str(exc)

    default_project_id = config.default_project_id
    if default_project_id in unavailable:
        default_project_id = None
    return RealtimeRuntimeRegistry(
        runtimes,
        unavailable_projects=unavailable,
        default_project_id=default_project_id,
    )


def _validate_timeouts(timeout: float, query_timeout: float) -> None:
    for name, value in (("timeout", timeout), ("query_timeout", query_timeout)):
        if (
            isinstance(value, bool)
            or not isinstance(value, int | float)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(f"{name} must be a finite positive number")


def _registry_relative_path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (root / path).resolve()


def runtime_binding(runtime: RealtimeEvidenceRuntime) -> RealtimeRuntimeBinding:
    _ensure_runtime_available(runtime)
    binding = runtime.binding
    document_count = 0
    document_runtime_error: str | None = None
    if binding.document_query_capabilities:
        registry = getattr(runtime, "current_registry", None)
        try:
            if registry is None:
                raise RuntimeError("document current-version registry is unavailable")
            document_count = registry.count_current(binding.project_id)
            if document_count == 0:
                document_runtime_error = "no current document versions are indexed"
        except (OSError, RuntimeError, ValueError, psycopg.Error) as exc:
            document_runtime_error = str(exc)
    else:
        document_runtime_error = "release declares no document query capability"
    return RealtimeRuntimeBinding(
        project_id=binding.project_id,
        release_version=binding.release_version,
        release_fingerprint=binding.release_fingerprint,
        runtime_mode=binding.runtime_mode,
        structured_query_enabled=binding.structured_query_enabled,
        artifact_verified=binding.integrity_status == "verified",
        runtime_verified=runtime.runtime_verified,
        runtime_degraded_reason=runtime.runtime_verification_error,
        database_access_mode=binding.database_access_mode,
        ontop_query_names=sorted(binding.ontop_query_names),
        ontop_query_capabilities=binding.ontop_query_capabilities,
        reasoning_capabilities=binding.reasoning_capabilities,
        document_fact_query_capabilities={name: {key: value for key, value in cap.items() if key in {"description_zh", "parameters", "result_fields", "question_examples", "business_question_ids", "fact_source", "fact_artifact", "fact_sha256"}}
            for name, cap in binding.document_fact_queries.items() if cap.get("cq_bindings")},
        reasoning_runtime_verified=bool(
            getattr(runtime, "reasoning_runtime_verified", False)
        ),
        reasoning_runtime_degraded_reason=(
            getattr(runtime, "reasoning_runtime_verification_error", None)
            or (
                "release declares no reasoning capability"
                if not binding.reasoning_capabilities
                else None
            )
        ),
        document_query_capabilities=binding.document_query_capabilities,
        document_query_examples=binding.document_query_examples,
        document_runtime_verified=document_count > 0,
        current_document_count=document_count,
        document_runtime_degraded_reason=document_runtime_error,
    )
