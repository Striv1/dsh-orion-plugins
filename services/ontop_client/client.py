from __future__ import annotations

import hashlib
import re
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

import httpx
from rdflib import Graph

from services.ontop_client.query_execution import compile_ontop_select
from services.realtime_qa.query_capabilities import (
    QueryCapabilityError,
    render_query_parameters,
)

SPARQL_WRITE_OPERATION = re.compile(
    r"\b(?:INSERT|DELETE|LOAD|CLEAR|CREATE|DROP|COPY|MOVE|ADD)\b",
    re.IGNORECASE,
)
# Query templates must be supplied by an explicitly bound release.
ALLOWED_QUERIES: frozenset[str] = frozenset()


class QueryTemplateError(ValueError):
    pass


class OntopQueryHTTPError(httpx.HTTPStatusError):
    """HTTP failure that keeps the query name and Ontop's diagnostic body."""

    def __init__(self, name: str, response: httpx.Response) -> None:
        body = (response.text or "").strip()
        detail = body[:2000] + ("…" if len(body) > 2000 else "")
        super().__init__(
            f"Ontop HTTP {response.status_code} for query {name!r}: {detail or '<empty body>'}",
            request=response.request,
            response=response,
        )
        self.query_name = name
        self.response_detail = detail


def _raise_for_query_status(response: httpx.Response, name: str) -> None:
    if response.is_error:
        raise OntopQueryHTTPError(name, response)


class OntopClient:
    def __init__(
        self,
        endpoint: str,
        query_dir: Path | None = None,
        timeout: float = 10.0,
        allowed_queries: Collection[str] | None = None,
        query_files: Mapping[str, Path] | None = None,
        query_checksums: Mapping[str, str] | None = None,
        query_capabilities: Mapping[str, dict[str, Any]] | None = None,
        bound_release_fingerprint: str | None = None,
        health_timeout: float | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.query_dir = query_dir
        self.timeout = timeout
        self.health_timeout = timeout if health_timeout is None else health_timeout
        self.allowed_queries = frozenset(
            ALLOWED_QUERIES if allowed_queries is None else allowed_queries
        )
        self.query_files = dict(query_files or {})
        self.query_checksums = dict(query_checksums or {})
        self.query_capabilities = dict(query_capabilities or {})
        self.bound_release_fingerprint = bound_release_fingerprint
        self.runtime_verification_status = "NOT_RUNTIME_VERIFIED"
        self.deployment_identity: dict[str, Any] | None = None

    def _mark_runtime_verified(self, identity: dict[str, Any]) -> None:
        self.runtime_verification_status = "VERIFIED"
        self.deployment_identity = dict(identity)

    def render(self, name: str, **params: Any) -> str:
        if name not in self.allowed_queries:
            raise QueryTemplateError(f"query template is not allowed: {name}")
        query_path = self.query_files.get(name)
        if query_path is None:
            if self.query_dir is None:
                raise QueryTemplateError(f"query template path is not configured: {name}")
            query_path = self.query_dir / f"{name}.rq"
        query_bytes = query_path.read_bytes()
        expected_checksum = self.query_checksums.get(name)
        if expected_checksum is not None:
            actual_checksum = f"sha256:{hashlib.sha256(query_bytes).hexdigest()}"
            if actual_checksum != expected_checksum:
                raise QueryTemplateError(f"query template checksum mismatch: {name}")
        try:
            return render_query_parameters(
                query_bytes.decode("utf-8"),
                params,
                self.query_capabilities.get(name),
            )
        except QueryCapabilityError as exc:
            raise QueryTemplateError(str(exc)) from exc

    def select(self, name: str, **params: Any) -> list[dict[str, Any]]:
        payload = self._select_payload(name, **params)
        return self._select_rows(payload)

    def select_with_metadata(self, name: str, **params: Any) -> dict[str, Any]:
        """Preserve projected variables, including unbound columns and empty results."""
        payload = self._select_payload(name, **params)
        head = payload.get("head")
        variables = head.get("vars") if isinstance(head, dict) else None
        if (
            not isinstance(variables, list)
            or any(not isinstance(value, str) or not value for value in variables)
            or len(set(variables)) != len(variables)
        ):
            raise QueryTemplateError("SELECT response is missing valid head.vars")
        rows = self._select_rows(payload)
        if any(set(row) - set(variables) for row in rows):
            raise QueryTemplateError("SELECT result bindings do not match head.vars")
        return {"variables": list(variables), "rows": rows,
                "row_terms": [
                    {field: dict(term) for field, term in row.items()}
                    for row in payload["results"]["bindings"]
                ],
                **({"query_execution": payload["_orion_query_execution"]} if "_orion_query_execution" in payload else {})}

    @staticmethod
    def _select_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {key: value.get("value") for key, value in row.items()}
            for row in payload["results"]["bindings"]
        ]

    def _select_payload(self, name: str, **params: Any) -> dict[str, Any]:
        query = self.render(name, **params)
        self._require_read_only_form(query, "SELECT")
        query, execution = compile_ontop_select(query)
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            response = client.post(
                self.endpoint,
                data={"query": query},
                headers={"Accept": "application/sparql-results+json"},
            )
        _raise_for_query_status(response, name)
        payload = response.json()
        if execution["strategy"] != "ORIGINAL":
            payload["_orion_query_execution"] = execution
        return payload

    def construct(self, name: str, **params: Any) -> Graph:
        query = self.render(name, **params)
        self._require_read_only_form(query, "CONSTRUCT")
        with httpx.Client(timeout=self.timeout, trust_env=False) as client:
            response = client.post(
                self.endpoint,
                data={"query": query},
                headers={"Accept": "text/turtle"},
            )
        _raise_for_query_status(response, name)
        return Graph().parse(data=response.text, format="turtle")

    @staticmethod
    def _require_read_only_form(query: str, form: str) -> None:
        if SPARQL_WRITE_OPERATION.search(query) or not re.search(
            rf"\b{form}\b",
            query,
            re.IGNORECASE,
        ):
            raise QueryTemplateError(f"query template must be a read-only {form}")

    def health(self) -> dict[str, Any]:
        try:
            with httpx.Client(timeout=self.health_timeout, trust_env=False) as client:
                response = client.post(
                    self.endpoint,
                    data={"query": "SELECT (1 AS ?ok) WHERE {}"},
                    headers={"Accept": "application/sparql-results+json"},
                )
            response.raise_for_status()
            rows = response.json()["results"]["bindings"]
            return {
                "status": "healthy" if rows else "degraded",
                "endpoint_reachable": bool(rows),
                "runtime_verification": self.runtime_verification_status,
            }
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return {"status": "unavailable", "error": str(exc)}
