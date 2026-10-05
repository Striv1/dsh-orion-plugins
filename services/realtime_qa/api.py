from __future__ import annotations

import argparse
import threading
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query

from services.config import Settings
from services.realtime_qa.analytics_api import register_analytics_api
from services.realtime_qa.answer import RealtimeAnswerService
from services.realtime_qa.capabilities import reasoning_execution_capabilities
from services.realtime_qa.capability_details import register_capability_api
from services.realtime_qa.checked_query_api import register_checked_query_api
from services.realtime_qa.code_identity import service_code_fingerprint
from services.realtime_qa.evidence_receipts import (
    EvidenceReceiptStore,
    persist_session_answer,
    register_evidence_receipt_api,
)
from services.realtime_qa.models import (
    EvidenceBundle,
    RealtimeAnswer,
    RealtimeAnswerRequest,
    RealtimeEvidenceRequest,
    RealtimeRuntimeBinding,
    RealtimeRuntimeCatalog,
    RealtimeSessionAnswer,
    RealtimeSessionAnswerRequest,
)
from services.realtime_qa.result_pages import register_result_page_api
from services.realtime_qa.runtime import (
    RealtimeEvidenceRuntime,
    RealtimeRuntimeNotFound,
    RealtimeRuntimeRegistry,
    RealtimeRuntimeSelectionRequired,
    RealtimeRuntimeUnavailable,
    build_realtime_runtime_registry,
    runtime_binding,
)
from services.realtime_qa.wren_api import register_wren_api

SERVICE_ID = "orion-core-realtime-qa"
SERVICE_CODE_FINGERPRINT = service_code_fingerprint()


class CoreRealtimeRuntimeProvider:
    """Hot-reload the release registry without initializing SupplyGuard services."""

    def __init__(self, settings: Settings | None = None, *, timeout: float = 5.0) -> None:
        self.settings = settings or Settings.from_env()
        raw_path = self.settings.realtime_runtime_registry_path.strip()
        if not raw_path:
            raise RuntimeError("ORION_REALTIME_RUNTIME_REGISTRY is required")
        self.registry_path = Path(raw_path).expanduser().resolve()
        self.timeout = timeout
        self._lock = threading.RLock()
        self._mtime_ns: int | None = None
        self._registry: RealtimeRuntimeRegistry | None = None
        self._last_error: str | None = None
        self.refresh(force=True)

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def refresh(self, *, force: bool = False) -> None:
        try:
            mtime_ns = self.registry_path.stat().st_mtime_ns
        except OSError as exc:
            self._last_error = f"realtime runtime registry unavailable: {exc}"
            return
        if not force and mtime_ns == self._mtime_ns:
            return
        with self._lock:
            try:
                registry = build_realtime_runtime_registry(
                    registry_path=self.registry_path,
                    database_url=self.settings.database_url,
                    fuseki_url=self.settings.fuseki_url,
                    semantica_url=self.settings.semantica_url,
                    timeout=self.timeout,
                )
            except (OSError, RuntimeError, ValueError) as exc:
                # An invalid atomic replacement cannot evict the last verified registry.
                self._last_error = f"realtime runtime registry reload failed: {exc}"
                return
            self._registry = registry
            self._mtime_ns = mtime_ns
            self._last_error = None

    def registry(self) -> RealtimeRuntimeRegistry:
        self.refresh()
        if self._registry is None:
            raise RealtimeRuntimeUnavailable(
                self._last_error or "realtime runtime registry is unavailable"
            )
        return self._registry

    def resolve(self, project_id: str | None) -> RealtimeEvidenceRuntime:
        return self.registry().resolve(project_id)

    def catalog(self) -> RealtimeRuntimeCatalog:
        return self.registry().catalog()

    def health(self) -> dict[str, Any]:
        try:
            catalog = self.catalog()
        except ValueError as exc:
            return {
                "service": SERVICE_ID,
                "status": "UNAVAILABLE",
                "registered_runtime_count": 0,
                "runtime_verified_count": 0,
                "reasoning_runtime_verified_count": 0,
                "error": str(exc),
            }
        runtime_verified = sum(item.runtime_verified for item in catalog.runtimes)
        reasoning_declared = sum(bool(item.reasoning_capabilities) for item in catalog.runtimes)
        reasoning_verified = sum(
            bool(item.reasoning_capabilities) and item.reasoning_runtime_verified
            for item in catalog.runtimes
        )
        healthy = bool(catalog.runtimes) and runtime_verified == len(catalog.runtimes)
        healthy = healthy and reasoning_verified == reasoning_declared
        healthy = healthy and not catalog.unavailable_projects and not self._last_error
        return {
            "service": SERVICE_ID,
            "status": "READY" if healthy else "DEGRADED",
            "readiness_reason": (
                "NO_PUBLISHED_RUNTIME"
                if not catalog.runtimes and not catalog.unavailable_projects and not self._last_error
                else None
            ),
            "registry_path": str(self.registry_path),
            "registered_runtime_count": len(catalog.runtimes),
            "runtime_verified_count": runtime_verified,
            "reasoning_runtime_declared_count": reasoning_declared,
            "reasoning_runtime_verified_count": reasoning_verified,
            "default_project_id": catalog.default_project_id,
            "unavailable_projects": catalog.unavailable_projects,
            "reload_error": self._last_error,
        }


def _http_error(exc: ValueError) -> HTTPException:
    if isinstance(exc, RealtimeRuntimeSelectionRequired):
        return HTTPException(400, str(exc))
    if isinstance(exc, RealtimeRuntimeNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, RealtimeRuntimeUnavailable):
        return HTTPException(503, str(exc))
    return HTTPException(400, str(exc))


def create_core_realtime_app(
    provider: CoreRealtimeRuntimeProvider | Any | None = None,
    *, evidence_receipts: EvidenceReceiptStore | None = None,
) -> FastAPI:
    runtimes = provider or CoreRealtimeRuntimeProvider()
    answerer = RealtimeAnswerService()
    app = FastAPI(title="ORION Core 发布绑定问答服务", version="1.0.0")
    app.state.runtime_provider = runtimes
    register_capability_api(app, runtimes.resolve)
    receipts = evidence_receipts or EvidenceReceiptStore()
    register_evidence_receipt_api(app, receipts)
    register_result_page_api(app, receipts, runtimes.resolve)
    register_checked_query_api(app, receipts, runtimes.resolve)
    register_wren_api(app, receipts, runtimes.resolve)
    register_analytics_api(app, receipts, runtimes.resolve)

    @app.get("/health")
    def health() -> dict[str, Any]:
        payload = dict(runtimes.health())
        payload["code_fingerprint"] = SERVICE_CODE_FINGERPRINT
        payload["reasoning_execution_capabilities"] = reasoning_execution_capabilities()
        return payload

    @app.get("/ontology/realtime/runtimes", response_model=RealtimeRuntimeCatalog)
    def realtime_runtimes() -> RealtimeRuntimeCatalog:
        try:
            return runtimes.catalog()
        except ValueError as exc:
            raise _http_error(exc) from exc

    @app.get("/ontology/realtime/binding", response_model=RealtimeRuntimeBinding)
    def realtime_binding(
        project_id: str | None = Query(default=None, min_length=1, max_length=160),
    ) -> RealtimeRuntimeBinding:
        try:
            return runtime_binding(runtimes.resolve(project_id))
        except ValueError as exc:
            raise _http_error(exc) from exc

    @app.post("/ontology/realtime/evidence", response_model=EvidenceBundle)
    def realtime_evidence(request: RealtimeEvidenceRequest) -> EvidenceBundle:
        try:
            return runtimes.resolve(request.project_id).collect(request)
        except ValueError as exc:
            raise _http_error(exc) from exc

    @app.post("/ontology/realtime/answer", response_model=RealtimeAnswer)
    def realtime_answer(request: RealtimeAnswerRequest) -> RealtimeAnswer:
        try:
            bundle = runtimes.resolve(request.evidence_request.project_id).collect(
                request.evidence_request
            )
            return answerer.answer(request, bundle)
        except ValueError as exc:
            raise _http_error(exc) from exc

    @app.post(
        "/ontology/realtime/session-answer",
        response_model=RealtimeSessionAnswer,
    )
    def realtime_session_answer(
        request: RealtimeSessionAnswerRequest,
    ) -> RealtimeSessionAnswer:
        expected = request.expected_release
        embedded_project = request.answer_request.evidence_request.project_id
        if embedded_project is not None and embedded_project != expected.project_id:
            raise HTTPException(
                409,
                "evidence request project does not match the Harness session release",
            )
        try:
            runtime = runtimes.resolve(expected.project_id)
        except ValueError as exc:
            raise _http_error(exc) from exc
        actual = runtime.binding
        if (
            expected.project_id != actual.project_id
            or expected.release_version != actual.release_version
            or expected.release_fingerprint != actual.release_fingerprint
        ):
            raise HTTPException(
                409,
                "Harness session release does not match the configured realtime runtime",
            )
        try:
            bundle = runtime.collect(request.answer_request.evidence_request)
            answer = answerer.answer(request.answer_request, bundle)
        except ValueError as exc:
            raise _http_error(exc) from exc
        return persist_session_answer(receipts, RealtimeSessionAnswer(
            session_id=request.session_id,
            runtime_binding=runtime_binding(runtime),
            answer=answer,
        ))

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="运行 ORION Core 发布绑定问答服务。")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Core realtime API 只能绑定本机回环地址。")
    uvicorn.run(create_core_realtime_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
