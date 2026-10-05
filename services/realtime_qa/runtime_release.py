from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

from rdflib.plugins.sparql.parser import parseQuery

from services.ontology_contracts.obda import obda_structure_issues
from services.ontop_client.client import OntopClient, QueryTemplateError
from services.realtime_qa.cq_contract import CQBindingError, compile_reviewed_cq
from services.realtime_qa.query_capabilities import (
    QueryCapabilityError,
    normalize_document_query_capabilities,
    normalize_query_capabilities,
    render_query_parameters,
    validate_query_template_contract,
)
from services.realtime_qa.reasoning_contract import (
    ReasoningCapabilityError,
    normalize_document_fact_queries,
    normalize_reasoning_capabilities,
    normalize_rule_package,
    validate_ontology_term_binding,
)
from services.realtime_qa.row_fact_conditions import validate_fact_source_fields
from services.realtime_qa.rule_cq_graph_contract import validate_rule_cq_graph_closure
from services.realtime_qa.source_provenance import snapshot_contract_for_publication
from services.realtime_qa.sparql_terms import sparql_references_iri
from services.structured_data.release_contract import (
    MultiSourceReleaseContractError,
    validate_multi_source_release_contract,
)

QUERY_NAME = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,159}$")
SQL_WRITE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|CREATE|GRANT|REVOKE|MERGE|CALL|COPY)\b",
    re.IGNORECASE,
)
OBDA_SOURCE = re.compile(r"(?im)^\s*source\s+(.+?)\s*$")
IDENTITY_QUERY_NAME = "orion_deployment_identity"
RUNTIME_SOURCE_DIR = Path("03-mapping-review/runtime")
REASONING_REQUIREMENTS = {"REQUIRED", "NOT_APPLICABLE"}
MIN_REASONING_RATIONALE_LENGTH = 12

# These implementations determine whether a returned SELECT result satisfies
# its business contract. Asset hashes alone do not identify that validator.
QUERY_VALIDATION_SOURCE_FILES = (
    "services/realtime_qa/runtime_release.py",
    "services/realtime_qa/deployment_automation.py",
    "services/realtime_qa/rdf_results.py",
    "services/realtime_qa/query_capabilities.py",
    "services/realtime_qa/cq_contract.py",
    "services/ontology_contracts/cq_answers.py",
    "services/ontology_contracts/nullable_bindings.py",
)


def query_validation_code_fingerprint() -> str:
    """Read current source bytes, independently of a possibly stale build manifest."""
    root = Path(__file__).resolve().parents[2]
    sources = {name: _checksum(root / name) for name in QUERY_VALIDATION_SOURCE_FILES}
    return "sha256:" + hashlib.sha256(
        json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class RuntimeReleaseError(ValueError):
    def __init__(self, message: str, *, path: str | None = None, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.path = path
        self.reason_code = reason_code


def load_reviewed_runtime_submission(runtime_dir: Path) -> dict[str, Any]:
    """Rehydrate reviewed runtime artifacts into the canonical S3 submission shape."""

    source_path = runtime_dir / "runtime-source.json"
    if not source_path.is_file():
        raise RuntimeReleaseError("reviewed runtime source is missing")
    source = _read_json(source_path)
    payload = dict(source)
    structured = source.get("structured_query_enabled") is not False
    if structured:
        mapping_path = _safe_relative(runtime_dir, str(source.get("mapping_artifact") or ""))
        payload["mapping_obda"] = mapping_path.read_text(encoding="utf-8")
        payload["ontop_queries"] = {
            str(name): _safe_relative(runtime_dir, str(entry.get("path") or "")).read_text(
                encoding="utf-8"
            )
            for name, entry in dict(source.get("ontop_queries") or {}).items()
            if isinstance(entry, dict)
        }
    capabilities = {
        str(name): dict(capability)
        for name, capability in dict(source.get("reasoning_capabilities") or {}).items()
        if isinstance(capability, dict)
    }
    for _name, capability in capabilities.items():
        rule_path = _safe_relative(runtime_dir, str(capability.get("rule_artifact") or ""))
        rule_package = _read_json(rule_path)
        capability.pop("rule_artifact", None)
        capability.pop("rule_sha256", None)
        capability["rules"] = list(rule_package.get("rules") or [])
    payload["reasoning_capabilities"] = capabilities
    document_fact_queries = {
        str(name): dict(query)
        for name, query in dict(source.get("document_fact_queries") or {}).items()
        if isinstance(query, dict)
    }
    for query in document_fact_queries.values():
        fact_artifact = str(query.pop("fact_artifact", "") or "")
        query.pop("fact_sha256", None)
        if fact_artifact:
            fact_package = _read_json(_safe_relative(runtime_dir, fact_artifact))
            query["facts"] = list(fact_package.get("facts") or [])
    payload["document_fact_queries"] = document_fact_queries
    for packaged_field in (
        "mapping_artifact",
        "mapping_sha256",
        "review_status",
        "reviewed_at",
        "reviewed_by",
        "prepared_at",
    ):
        payload.pop(packaged_field, None)
    return payload


def normalize_runtime_submission(
    payload: dict[str, Any] | None,
    *,
    intake_mode: str | None = None,
    require_explicit_capabilities: bool = False,
) -> dict[str, Any] | None:
    if isinstance(payload, dict) and "cq_bindings" in payload:
        raise RuntimeReleaseError(
            "realtime_runtime.cq_bindings is unsupported and cannot be silently discarded. "
            "The supported structured location is realtime_runtime.query_capabilities.<query_name>.cq_bindings. "
            "Rule inference CQs, including DOCUMENT_ONLY, use "
            "realtime_runtime.reasoning_capabilities.<capability_name>.cq_bindings with reviewed row validation cases. "
            "Document FACT_QUERY CQs use realtime_runtime.document_fact_queries.<query_name>.cq_bindings "
            "with the capability's reviewed sparql and row validation cases. "
            "Preserve the original CQ intent at the appropriate supported location; do not invent Ontop assets or rules."
        )
    normalized_intake_mode = str(intake_mode or "").strip().upper()
    if normalized_intake_mode == "DOCUMENT_ONLY":
        document_payload = payload or {}
        forbidden = {
            "mapping_obda",
            "ontop_queries",
            "ontop_deployment_id",
        } & set(document_payload)
        if forbidden:
            raise RuntimeReleaseError(
                "DOCUMENT_ONLY runtime cannot declare Ontop assets: "
                + ", ".join(sorted(forbidden))
            )
        try:
            document_capabilities = normalize_document_query_capabilities(
                document_payload.get("document_query_capabilities")
            )
        except QueryCapabilityError as exc:
            raise RuntimeReleaseError(
                str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
            ) from exc
        if "current_full_text_search" not in document_capabilities:
            raise RuntimeReleaseError(
                "DOCUMENT_ONLY runtime must allow current_full_text_search"
            )
        try:
            document_fact_queries = normalize_document_fact_queries(
                document_payload.get("document_fact_queries"),
                require_explicit=True,
            )
        except ReasoningCapabilityError as exc:
            raise RuntimeReleaseError(
                str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
            ) from exc
        declared_requirement = str(
            document_payload.get("reasoning_requirement") or "NOT_APPLICABLE"
        ).strip().upper()
        reasoning_capabilities: dict[str, dict[str, Any]] = {}
        if declared_requirement == "REQUIRED":
            try:
                reasoning_capabilities = normalize_reasoning_capabilities(
                    document_payload.get("reasoning_capabilities"),
                    set(),
                    require_rules=True,
                    document_fact_query_names=set(document_fact_queries),
                )
            except ReasoningCapabilityError as exc:
                raise RuntimeReleaseError(
                    str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
                ) from exc
            if not document_fact_queries:
                raise RuntimeReleaseError(
                    "DOCUMENT_ONLY reasoning_requirement=REQUIRED requires "
                    "document_fact_queries"
                )
            for capability in reasoning_capabilities.values():
                if capability["evidence_query"] not in document_fact_queries:
                    raise RuntimeReleaseError(
                        "DOCUMENT_ONLY reasoning capability evidence_query must "
                        "reference a document_fact_query: "
                        + capability["evidence_query"]
                    )
            reasoning_not_applicable_reason = None
        else:
            if document_payload.get("reasoning_capabilities"):
                raise RuntimeReleaseError(
                    "DOCUMENT_ONLY reasoning_requirement=NOT_APPLICABLE cannot "
                    "declare reasoning_capabilities"
                )
            if document_fact_queries and any(
                not query.get("cq_bindings") for query in document_fact_queries.values()
            ):
                raise RuntimeReleaseError(
                    "DOCUMENT_ONLY without reasoning requires reviewed FACT_QUERY CQ bindings "
                    "for every document_fact_query"
                )
            declared_reason = str(
                document_payload.get("reasoning_not_applicable_reason") or ""
            ).strip()
            reasoning_not_applicable_reason = declared_reason or (
                "纯文档运行时没有结构化实时事实输入；业务规则推理不适用，"
                "仍保留版本化全文检索与证据定位能力。"
            )
            if len(reasoning_not_applicable_reason) < MIN_REASONING_RATIONALE_LENGTH:
                raise RuntimeReleaseError(
                    "DOCUMENT_ONLY reasoning_not_applicable_reason must be concrete"
                )
        return {
            "schema_version": 4,
            "runtime_mode": "DOCUMENT_ONLY",
            "structured_query_enabled": False,
            "reasoning_requirement": (
                "REQUIRED" if reasoning_capabilities else "NOT_APPLICABLE"
            ),
            "reasoning_not_applicable_reason": reasoning_not_applicable_reason,
            "reasoning_capabilities": reasoning_capabilities,
            "document_fact_queries": document_fact_queries,
            "database_access_mode": "READ_ONLY",
            "prepared_by": str(
                document_payload.get("prepared_by") or "ORION_DOCUMENT_RUNTIME"
            ).strip(),
            # Preflight tokens contain this normalized submission and commit
            # validates it again. Do not synthesize forbidden Ontop input keys.
            "query_capabilities": {},
            "document_query_capabilities": document_capabilities,
            "document_query_examples": list(
                document_payload.get("document_query_examples")
                or ["在当前版本资料中查找指定业务术语并返回可定位原文"]
            ),
        }
    if payload is None:
        return None
    deployment_id = str(payload.get("ontop_deployment_id") or "").strip()
    prepared_by = str(payload.get("prepared_by") or "").strip()
    access_mode = str(payload.get("database_access_mode") or "").strip().upper()
    mapping_obda = str(payload.get("mapping_obda") or "").strip()
    raw_queries = payload.get("ontop_queries")
    if require_explicit_capabilities and payload.get("query_capabilities") is None:
        raise RuntimeReleaseError(
            "new structured runtimes must declare query_capabilities"
        )
    if not DEPLOYMENT_ID.fullmatch(deployment_id):
        raise RuntimeReleaseError("ontop_deployment_id is invalid")
    if not prepared_by:
        raise RuntimeReleaseError("runtime prepared_by is required")
    if access_mode != "READ_ONLY":
        raise RuntimeReleaseError("runtime database_access_mode must be READ_ONLY")
    _validate_obda(mapping_obda)
    if not isinstance(raw_queries, dict) or not raw_queries:
        raise RuntimeReleaseError("runtime ontop_queries are required")
    queries: dict[str, str] = {}
    for raw_name, raw_query in raw_queries.items():
        name = str(raw_name).strip()
        if not isinstance(raw_query, str):
            raise RuntimeReleaseError(
                f"runtime ontop_queries.{name} must be SPARQL text, not an artifact descriptor"
            )
        query = raw_query.strip()
        if name == IDENTITY_QUERY_NAME or not QUERY_NAME.fullmatch(name):
            raise RuntimeReleaseError(f"runtime query name is invalid or reserved: {name}")
        _validate_query(name, query)
        queries[name] = query.rstrip() + "\n"
    target_backend_cases = []
    try:
        query_capabilities = normalize_query_capabilities(
            payload.get("query_capabilities"),
            set(queries),
            allow_legacy=not require_explicit_capabilities,
        )
        for name, query in queries.items():
            capability = query_capabilities[name]
            if require_explicit_capabilities and (
                capability.get("legacy") is True
                or not capability.get("result_fields")
                or not capability.get("question_examples")
                or not capability.get("validation_cases")
            ):
                raise QueryCapabilityError(
                    "new query capability requires result_fields, question_examples "
                    f"and validation_cases: {name}"
                )
            validate_query_template_contract(query, capability)
            for validation_case in capability.get("validation_cases") or []:
                rendered_query = render_query_parameters(
                    query,
                    validation_case["parameters"],
                    capability,
                )
                _validate_query(name, rendered_query)
                from services.ontop_client.backend_validation import diagnose_select, fingerprint

                target_backend_cases.append({
                    "query_name": name, "case_id": validation_case["id"],
                    "parameters_sha256": fingerprint(validation_case["parameters"]),
                    **diagnose_select(rendered_query),
                })
            sample_parameters = {
                parameter_name: spec["default"]
                for parameter_name, spec in capability["parameters"].items()
                if "default" in spec
            }
            required_without_default = [
                parameter_name
                for parameter_name, spec in capability["parameters"].items()
                if spec.get("required") is True and "default" not in spec
            ]
            if not required_without_default:
                rendered_query = render_query_parameters(query, sample_parameters, capability)
                _validate_query(name, rendered_query)
        document_query_capabilities = normalize_document_query_capabilities(
            payload.get("document_query_capabilities")
            if "document_query_capabilities" in payload
            else ([] if normalized_intake_mode == "DATABASE_ONLY" else None)
        )
        if (
            normalized_intake_mode == "HYBRID"
            and "current_full_text_search" not in document_query_capabilities
        ):
            raise QueryCapabilityError(
                "HYBRID runtime must allow current_full_text_search"
            )
        document_fact_queries = normalize_document_fact_queries(
            payload.get("document_fact_queries"),
            require_explicit=True,
        )
        if normalized_intake_mode == "DATABASE_ONLY" and document_fact_queries:
            raise ReasoningCapabilityError(
                "DATABASE_ONLY runtime cannot declare document_fact_queries"
            )
        reasoning_capabilities = normalize_reasoning_capabilities(
            payload.get("reasoning_capabilities"),
            set(queries),
            require_rules=True,
            document_fact_query_names=set(document_fact_queries),
        )
        if require_explicit_capabilities:
            try:
                from services.realtime_qa.evidence_absence import validate_evidence_absence

                validate_fact_source_fields(reasoning_capabilities, query_capabilities)
                validate_evidence_absence(reasoning_capabilities, queries)
                validate_rule_cq_graph_closure(reasoning_capabilities)
            except ValueError as exc:
                raise ReasoningCapabilityError(str(exc)) from exc
        reasoning_requirement, reasoning_not_applicable_reason = (
            _normalize_reasoning_requirement(
                payload,
                reasoning_capabilities,
                require_explicit=require_explicit_capabilities,
            )
        )
        for capability in reasoning_capabilities.values():
            evidence_query = capability["evidence_query"]
            if evidence_query in queries:
                render_query_parameters(
                    queries[evidence_query],
                    capability["runtime_validation"]["parameters"],
                    query_capabilities[evidence_query],
                )
        for name, capability in query_capabilities.items():
            for question_id, binding in (capability.get("cq_bindings") or {}).items():
                compiled_question = compile_reviewed_cq(
                    question_id=question_id, query_name=name,
                    query=queries[name], capability=capability,
                )
                if binding["answer_mode"] in {"RULE_INFERENCE", "OWL_INFERENCE"}:
                    reason = reasoning_capabilities.get(binding["reasoning_capability"])
                    if not reason or not set(binding["derived_predicates"]).issubset(reason["result_predicates"]):
                        raise CQBindingError(f"CQ references an undeclared reasoning result: {question_id}")
                    if any(
                        not reason["ontology_terms"].get(predicate)
                        or not sparql_references_iri(compiled_question["sparql"], reason["ontology_terms"][predicate])
                        for predicate in binding["derived_predicates"]
                    ):
                        raise CQBindingError(f"CQ must query actual rule conclusions: {question_id}")
    except (QueryCapabilityError, ReasoningCapabilityError, CQBindingError) as exc:
        raise RuntimeReleaseError(
            str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
        ) from exc
    normalized = {
        "schema_version": (
            5
            if payload.get("cross_source_snapshot_set") is not None
            else (
                4
                if require_explicit_capabilities
                else (3 if reasoning_capabilities else 1)
            )
        ),
        "runtime_mode": (
            "HYBRID" if document_query_capabilities else "STRUCTURED"
        ),
        "structured_query_enabled": True,
        "ontop_deployment_id": deployment_id,
        "database_access_mode": access_mode,
        "prepared_by": prepared_by,
        "mapping_obda": mapping_obda.rstrip() + "\n",
        "ontop_queries": queries,
        "target_backend_validation": {
            "policy": "formal-target-backend-validation-v1"
            if all(capability.get("validation_cases") for capability in query_capabilities.values()) else None,
            "status": "STATIC_ONLY" if target_backend_cases else "NO_CASES_DECLARED",
            "runtime_verified": False, "cases": target_backend_cases,
        },
        "query_capabilities": query_capabilities,
        "reasoning_capabilities": reasoning_capabilities,
        "reasoning_requirement": reasoning_requirement,
        "reasoning_not_applicable_reason": reasoning_not_applicable_reason,
        "document_fact_queries": document_fact_queries,
        "document_query_capabilities": document_query_capabilities,
        "document_query_examples": list(
            payload.get("document_query_examples")
            or ["在当前版本资料中查找指定业务术语并返回可定位原文"]
        ),
        "cross_source_snapshot_set": payload.get("cross_source_snapshot_set"),
        "source_bindings": payload.get("source_bindings") or {},
        "snapshot_manifests": payload.get("snapshot_manifests") or {},
        "identity_contracts": payload.get("identity_contracts") or [],
        "query_template_hashes": payload.get("query_template_hashes") or {},
        "source_query_templates": payload.get("source_query_templates") or {},
    }
    live_query_requested = any(
        capability.get("query_mode") in {"REALTIME_REQUIRED", "HYBRID"}
        for capability in query_capabilities.values()
    )
    if live_query_requested and normalized["cross_source_snapshot_set"] is None:
        raise RuntimeReleaseError(
            "G-S6-REALTIME-READONLY: live query requires a bound source snapshot and reviewed plan"
        )
    if normalized["cross_source_snapshot_set"] is not None:
        snapshot_payload = normalized["cross_source_snapshot_set"]
        snapshot_project_id = str(
            snapshot_payload.get("project_id")
            if isinstance(snapshot_payload, dict)
            else ""
        )
        try:
            validate_multi_source_release_contract(
                project_id=snapshot_project_id,
                snapshot_set_payload=normalized["cross_source_snapshot_set"],
                source_bindings_payload=normalized["source_bindings"],
                snapshot_manifests_payload=normalized["snapshot_manifests"],
                identity_contracts_payload=normalized["identity_contracts"],
                query_capabilities=query_capabilities,
                query_template_hashes_payload=normalized["query_template_hashes"],
                query_templates_payload=normalized["source_query_templates"],
                require_production=False,
            )
        except MultiSourceReleaseContractError as exc:
            raise RuntimeReleaseError(
                str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
            ) from exc
    return normalized


def write_runtime_review_assets(
    stage_dir: Path,
    runtime: dict[str, Any] | None,
    *,
    prepared_at: str,
) -> None:
    runtime_dir = stage_dir / "runtime"
    shutil.rmtree(runtime_dir, ignore_errors=True)
    if runtime is None:
        return
    if runtime.get("structured_query_enabled") is False:
        runtime_dir.mkdir(parents=True)
        reasoning_capabilities: dict[str, dict[str, Any]] = {}
        for name, capability in sorted(
            dict(runtime.get("reasoning_capabilities") or {}).items()
        ):
            rules_path = runtime_dir / "rules" / f"{name}.json"
            _write_json(
                rules_path,
                {
                    "schema_version": 1,
                    "capability_name": name,
                    "rules": capability["rules"],
                },
            )
            reasoning_capabilities[name] = {
                **{key: value for key, value in capability.items() if key != "rules"},
                "rule_artifact": f"rules/{name}.json",
                "rule_sha256": _checksum(rules_path),
            }
        document_fact_queries: dict[str, dict[str, Any]] = {}
        for name, query in sorted(
            dict(runtime.get("document_fact_queries") or {}).items()
        ):
            fact_path = runtime_dir / "document-facts" / f"{name}.json"
            fact_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(
                fact_path,
                {
                    "schema_version": 1,
                    "query_name": name,
                    "description_zh": query["description_zh"],
                    "fact_source": query["fact_source"],
                    "fact_bindings": query["fact_bindings"],
                    "facts": query["facts"],
                    "source_scope": query["source_scope"],
                },
            )
            document_fact_queries[name] = {
                **{key: value for key, value in query.items() if key not in {"facts"}},
                "fact_artifact": f"document-facts/{name}.json",
                "fact_sha256": _checksum(fact_path),
            }
        _write_json(
            runtime_dir / "runtime-source.json",
            {
                **runtime,
                "reasoning_capabilities": reasoning_capabilities,
                "document_fact_queries": document_fact_queries,
                "prepared_at": prepared_at,
                "review_status": "DRAFT",
            },
        )
        return
    queries_dir = runtime_dir / "queries"
    queries_dir.mkdir(parents=True)
    mapping_path = runtime_dir / "mapping.obda"
    mapping_path.write_text(str(runtime["mapping_obda"]), encoding="utf-8")
    query_artifacts: dict[str, dict[str, str]] = {}
    for name, query in sorted(dict(runtime["ontop_queries"]).items()):
        relative = f"queries/{name}.rq"
        path = runtime_dir / relative
        path.write_text(str(query), encoding="utf-8")
        query_artifacts[name] = {
            "path": relative,
            "sha256": _checksum(path),
        }
    reasoning_capabilities: dict[str, dict[str, Any]] = {}
    for name, capability in sorted(
        dict(runtime.get("reasoning_capabilities") or {}).items()
    ):
        rules_path = runtime_dir / "rules" / f"{name}.json"
        _write_json(
            rules_path,
            {
                "schema_version": 1,
                "capability_name": name,
                "rules": capability["rules"],
            },
        )
        reasoning_capabilities[name] = {
            **{key: value for key, value in capability.items() if key != "rules"},
            "rule_artifact": f"rules/{name}.json",
            "rule_sha256": _checksum(rules_path),
        }
    document_fact_queries: dict[str, dict[str, Any]] = {}
    for name, query in sorted(dict(runtime.get("document_fact_queries") or {}).items()):
        fact_path = runtime_dir / "document-facts" / f"{name}.json"
        fact_path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(
            fact_path,
            {
                "schema_version": 1,
                "query_name": name,
                "description_zh": query["description_zh"],
                "fact_source": query["fact_source"],
                "fact_bindings": query["fact_bindings"],
                "facts": query["facts"],
                "source_scope": query["source_scope"],
            },
        )
        document_fact_queries[name] = {
            **{key: value for key, value in query.items() if key != "facts"},
            "fact_artifact": f"document-facts/{name}.json",
            "fact_sha256": _checksum(fact_path),
        }
    _write_json(
        runtime_dir / "runtime-source.json",
        {
            "schema_version": runtime.get("schema_version", 2),
            "runtime_mode": runtime.get("runtime_mode", "HYBRID"),
            "structured_query_enabled": True,
            "ontop_deployment_id": runtime["ontop_deployment_id"],
            "database_access_mode": runtime["database_access_mode"],
            "prepared_by": runtime["prepared_by"],
            "prepared_at": prepared_at,
            "review_status": "DRAFT",
            "mapping_artifact": "mapping.obda",
            "mapping_sha256": _checksum(mapping_path),
            "ontop_queries": query_artifacts,
            "target_backend_validation": runtime.get("target_backend_validation"),
            "query_capabilities": runtime.get("query_capabilities") or {},
            "reasoning_capabilities": reasoning_capabilities,
            "reasoning_requirement": runtime.get("reasoning_requirement"),
            "reasoning_not_applicable_reason": runtime.get(
                "reasoning_not_applicable_reason"
            ),
            "document_fact_queries": document_fact_queries,
            "document_query_capabilities": runtime.get("document_query_capabilities") or [],
            "document_query_examples": runtime.get("document_query_examples") or [],
            "cross_source_snapshot_set": runtime.get("cross_source_snapshot_set"),
            "source_bindings": runtime.get("source_bindings") or {},
            "snapshot_manifests": runtime.get("snapshot_manifests") or {},
            "identity_contracts": runtime.get("identity_contracts") or [],
            "query_template_hashes": runtime.get("query_template_hashes") or {},
            "source_query_templates": runtime.get("source_query_templates") or {},
        },
    )


def finalize_runtime_review(
    stage_dir: Path,
    *,
    reviewed_by: str,
    reviewed_at: str,
) -> None:
    source_path = stage_dir / "runtime/runtime-source.json"
    if not source_path.exists():
        return
    source = _read_json(source_path)
    source.update(
        {
            "review_status": "REVIEWED",
            "reviewed_by": reviewed_by,
            "reviewed_at": reviewed_at,
        }
    )
    _write_json(source_path, source)


def package_realtime_runtime(
    project_dir: Path,
    package_dir: Path,
    *,
    project_id: str,
    release_version: str,
    ontology_iri: str,
) -> dict[str, Any] | None:
    source_dir = project_dir / RUNTIME_SOURCE_DIR
    source_path = source_dir / "runtime-source.json"
    if not source_path.exists():
        return None
    source = _read_json(source_path)
    if source.get("review_status") != "REVIEWED":
        raise RuntimeReleaseError("realtime runtime assets have not passed S3 review")
    if source.get("database_access_mode") != "READ_ONLY":
        raise RuntimeReleaseError("realtime runtime access is not READ_ONLY")
    structured_query_enabled = source.get("structured_query_enabled") is not False
    try:
        document_query_capabilities = normalize_document_query_capabilities(
            source.get("document_query_capabilities"),
            legacy_default=int(source.get("schema_version") or 1) < 2,
        )
    except QueryCapabilityError as exc:
        raise RuntimeReleaseError(
            str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
        ) from exc
    runtime_dir = package_dir / "05-运行时"
    runtime_dir.mkdir(parents=True)
    if not structured_query_enabled:
        if "current_full_text_search" not in document_query_capabilities:
            raise RuntimeReleaseError(
                "document-only runtime must package current_full_text_search"
            )
        try:
            reasoning_capabilities = normalize_reasoning_capabilities(
                source.get("reasoning_capabilities"),
                set(),
                require_rules=False,
                document_fact_query_names=set(
                    source.get("document_fact_queries") or {}
                ),
            )
            document_fact_queries = normalize_document_fact_queries(
                source.get("document_fact_queries"),
                require_explicit=False,
            )
            reasoning_requirement, reasoning_not_applicable_reason = (
                _normalize_reasoning_requirement(
                    source,
                    reasoning_capabilities,
                    require_explicit=int(source.get("schema_version") or 1) >= 4,
                )
            )
        except (QueryCapabilityError, ReasoningCapabilityError) as exc:
            raise RuntimeReleaseError(
                str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
            ) from exc
        if reasoning_requirement == "REQUIRED":
            for name, capability in reasoning_capabilities.items():
                if capability["evidence_query"] not in document_fact_queries:
                    raise RuntimeReleaseError(
                        "document-only reasoning capability must reference a "
                        "document_fact_query: " + name
                    )
        packaged_reasoning_capabilities: dict[str, dict[str, Any]] = {}
        for name, capability in sorted(reasoning_capabilities.items()):
            rule_path = _safe_relative(source_dir, capability["rule_artifact"])
            if _checksum(rule_path) != capability["rule_sha256"]:
                raise RuntimeReleaseError(
                    f"reviewed reasoning rule checksum mismatch: {name}"
                )
            target = runtime_dir / "rules" / f"{name}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(rule_path, target)
            packaged_reasoning_capabilities[name] = {
                **capability,
                "rule_artifact": f"05-运行时/rules/{name}.json",
                "rule_sha256": _checksum(target),
            }
        packaged_document_fact_queries: dict[str, dict[str, Any]] = {}
        for name, query in sorted(document_fact_queries.items()):
            fact_path = source_dir / str(query["fact_artifact"])
            if _checksum(fact_path) != query.get("fact_sha256"):
                raise RuntimeReleaseError(
                    f"reviewed document fact checksum mismatch: {name}"
                )
            target = runtime_dir / "document-facts" / f"{name}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(fact_path, target)
            packaged_document_fact_queries[name] = {
                **{key: value for key, value in query.items() if key != "facts"},
                "fact_artifact": f"05-运行时/document-facts/{name}.json",
                "fact_sha256": _checksum(target),
            }
        runtime_contract = {
            "schema_version": max(int(source.get("schema_version") or 2), 4),
            "runtime_mode": "DOCUMENT_ONLY",
            "structured_query_enabled": False,
            "reasoning_requirement": reasoning_requirement,
            "reasoning_not_applicable_reason": reasoning_not_applicable_reason,
            "reasoning_capabilities": packaged_reasoning_capabilities,
            "document_fact_queries": packaged_document_fact_queries,
            "project_id": project_id,
            "release_version": release_version,
            "ontology_iri": ontology_iri,
            "ontology_artifact": "01-本体模型/ontology.ttl",
            "database_access_mode": "READ_ONLY",
            "ontop_queries": {},
            "query_capabilities": {},
            "document_query_capabilities": document_query_capabilities,
            "document_query_examples": list(source.get("document_query_examples") or []),
            "reviewed_by": source.get("reviewed_by"),
            "reviewed_at": source.get("reviewed_at"),
        }
        _write_json(runtime_dir / "realtime-runtime.json", runtime_contract)
        return runtime_contract

    deployment_id = str(source.get("ontop_deployment_id") or "")
    if not DEPLOYMENT_ID.fullmatch(deployment_id):
        raise RuntimeReleaseError("reviewed Ontop deployment id is invalid")

    mapping_path = _safe_relative(source_dir, str(source.get("mapping_artifact") or ""))
    source_mapping_sha256 = _checksum(mapping_path)
    if source_mapping_sha256 != source.get("mapping_sha256"):
        raise RuntimeReleaseError("reviewed OBDA mapping checksum mismatch")
    mapping = mapping_path.read_text(encoding="utf-8")
    _validate_obda(mapping)

    raw_queries = source.get("ontop_queries")
    if not isinstance(raw_queries, dict) or not raw_queries:
        raise RuntimeReleaseError("reviewed Ontop query artifacts are missing")
    queries: dict[str, tuple[Path, str]] = {}
    for name, entry in raw_queries.items():
        if not QUERY_NAME.fullmatch(str(name)) or not isinstance(entry, dict):
            raise RuntimeReleaseError("reviewed Ontop query entry is invalid")
        query_path = _safe_relative(source_dir, str(entry.get("path") or ""))
        if _checksum(query_path) != entry.get("sha256"):
            raise RuntimeReleaseError(f"reviewed Ontop query checksum mismatch: {name}")
        query = query_path.read_text(encoding="utf-8")
        _validate_query(str(name), query)
        queries[str(name)] = (query_path, query)
    try:
        query_capabilities = normalize_query_capabilities(
            source.get("query_capabilities"),
            set(queries),
            allow_legacy=int(source.get("schema_version") or 1) < 2,
        )
        for name, (_query_path, query) in queries.items():
            validate_query_template_contract(query, query_capabilities[name])
        document_fact_queries = normalize_document_fact_queries(
            source.get("document_fact_queries"),
            require_explicit=False,
        )
        reasoning_capabilities = normalize_reasoning_capabilities(
            source.get("reasoning_capabilities"),
            set(queries),
            require_rules=False,
            document_fact_query_names=set(document_fact_queries),
        )
        reasoning_requirement, reasoning_not_applicable_reason = (
            _normalize_reasoning_requirement(
                source,
                reasoning_capabilities,
                require_explicit=int(source.get("schema_version") or 1) >= 4,
            )
        )
    except (QueryCapabilityError, ReasoningCapabilityError) as exc:
        raise RuntimeReleaseError(
            str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
        ) from exc

    rule_packages: dict[str, tuple[Path, dict[str, Any]]] = {}
    for name, capability in reasoning_capabilities.items():
        rule_path = _safe_relative(source_dir, capability["rule_artifact"])
        if _checksum(rule_path) != capability["rule_sha256"]:
            raise RuntimeReleaseError(
                f"reviewed reasoning rule checksum mismatch: {name}"
            )
        try:
            rule_package = normalize_rule_package(
                _read_json(rule_path),
                capability_name=name,
            )
        except ReasoningCapabilityError as exc:
            raise RuntimeReleaseError(
                str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
            ) from exc
        validate_ontology_term_binding(name, capability, rule_package["rules"])
        rule_packages[name] = (rule_path, rule_package)

    query_dir = runtime_dir / "queries"
    query_dir.mkdir(parents=True)
    compiled_mapping = compile_mapping_with_identity(
        mapping,
        deployment_id,
        source_mapping_sha256,
    )
    (runtime_dir / "mapping.obda").write_text(compiled_mapping, encoding="utf-8")
    query_artifacts: dict[str, str] = {}
    for name, (query_path, _query) in sorted(queries.items()):
        target = query_dir / f"{name}.rq"
        shutil.copy2(query_path, target)
        query_artifacts[name] = f"05-运行时/queries/{name}.rq"
    packaged_reasoning_capabilities: dict[str, dict[str, Any]] = {}
    for name, (rule_path, _rule_package) in sorted(rule_packages.items()):
        target = runtime_dir / "rules" / f"{name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rule_path, target)
        packaged_reasoning_capabilities[name] = {
            **reasoning_capabilities[name],
            "rule_artifact": f"05-运行时/rules/{name}.json",
            "rule_sha256": _checksum(target),
        }
    packaged_document_fact_queries: dict[str, dict[str, Any]] = {}
    for name, query in sorted(document_fact_queries.items()):
        fact_path = _safe_relative(source_dir, str(query["fact_artifact"]))
        if _checksum(fact_path) != query.get("fact_sha256"):
            raise RuntimeReleaseError(
                f"reviewed document fact checksum mismatch: {name}"
            )
        target = runtime_dir / "document-facts" / f"{name}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fact_path, target)
        packaged_document_fact_queries[name] = {
            **{key: value for key, value in query.items() if key != "facts"},
            "fact_artifact": f"05-运行时/document-facts/{name}.json",
            "fact_sha256": _checksum(target),
        }
    identity_query = query_dir / f"{IDENTITY_QUERY_NAME}.rq"
    identity_query.write_text(_identity_query(), encoding="utf-8")
    inventory_path = project_dir / "01-data-understanding/datasource-inventory.json"
    schema_path = project_dir / "01-data-understanding/schema-snapshot.json"
    if (source.get("cross_source_snapshot_set") is None and not source.get("source_bindings")
            and inventory_path.is_file() and schema_path.is_file()):
        try:
            source = snapshot_contract_for_publication(
                source, _read_json(inventory_path), _read_json(schema_path), project_id=project_id,
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise RuntimeReleaseError(f"S1 source provenance cannot be frozen: {exc}") from exc
    multi_source_snapshot = source.get("cross_source_snapshot_set")
    source_bindings = source.get("source_bindings") or {}
    snapshot_manifests = source.get("snapshot_manifests") or {}
    if multi_source_snapshot is not None or source_bindings:
        try:
            validate_multi_source_release_contract(
                project_id=project_id,
                snapshot_set_payload=multi_source_snapshot,
                source_bindings_payload=source_bindings,
                snapshot_manifests_payload=snapshot_manifests,
                identity_contracts_payload=source.get("identity_contracts") or [],
                query_capabilities=query_capabilities,
                query_template_hashes_payload=source.get("query_template_hashes") or {},
                query_templates_payload=source.get("source_query_templates") or {},
                require_production=True,
            )
        except MultiSourceReleaseContractError as exc:
            raise RuntimeReleaseError(
                str(exc), path=getattr(exc, "path", None), reason_code=getattr(exc, "reason_code", None),
            ) from exc
    runtime_contract = {
        "schema_version": max(
            int(source.get("schema_version") or 1),
            5
            if multi_source_snapshot is not None
            else (3 if packaged_reasoning_capabilities else 1),
        ),
        "runtime_mode": source.get("runtime_mode") or (
            "HYBRID" if document_query_capabilities else "STRUCTURED"
        ),
        "structured_query_enabled": True,
        "project_id": project_id,
        "release_version": release_version,
        "ontology_iri": ontology_iri,
        "ontology_artifact": "01-本体模型/ontology.ttl",
        "mapping_artifact": "05-运行时/mapping.obda",
        "source_mapping_sha256": source_mapping_sha256,
        "database_access_mode": "READ_ONLY",
        "ontop_deployment_id": deployment_id,
        "identity_query_artifact": (f"05-运行时/queries/{IDENTITY_QUERY_NAME}.rq"),
        "ontop_queries": query_artifacts,
        "query_capabilities": query_capabilities,
        "reasoning_capabilities": packaged_reasoning_capabilities,
        "reasoning_requirement": reasoning_requirement,
        "reasoning_not_applicable_reason": reasoning_not_applicable_reason,
        "document_fact_queries": packaged_document_fact_queries,
        "document_query_capabilities": document_query_capabilities,
        "document_query_examples": list(source.get("document_query_examples") or []),
        "snapshot_set_id": (
            (source.get("cross_source_snapshot_set") or {}).get("snapshot_set_id")
        ),
        "cross_source_snapshot_set": source.get("cross_source_snapshot_set"),
        "source_bindings": source_bindings,
        "snapshot_manifests": snapshot_manifests,
        "identity_contracts": source.get("identity_contracts") or [],
        "query_template_hashes": source.get("query_template_hashes") or {},
        "source_query_templates": source.get("source_query_templates") or {},
        "reviewed_by": source.get("reviewed_by"),
        "reviewed_at": source.get("reviewed_at"),
    }
    _write_json(runtime_dir / "realtime-runtime.json", runtime_contract)
    return runtime_contract


def _normalize_reasoning_requirement(
    payload: dict[str, Any],
    reasoning_capabilities: dict[str, dict[str, Any]],
    *,
    require_explicit: bool,
) -> tuple[str, str | None]:
    raw_requirement = payload.get("reasoning_requirement")
    if raw_requirement in (None, ""):
        if require_explicit:
            raise RuntimeReleaseError(
                "new structured runtimes must declare reasoning_requirement as "
                "REQUIRED or NOT_APPLICABLE"
            )
        # Keep old reviewed releases readable. New S3 submissions always take the
        # explicit branch above, so this compatibility path cannot create a new
        # undeclared production contract.
        return ("REQUIRED", None) if reasoning_capabilities else ("LEGACY_UNDECLARED", None)

    requirement = str(raw_requirement).strip().upper()
    if requirement not in REASONING_REQUIREMENTS:
        raise RuntimeReleaseError(
            "reasoning_requirement must be REQUIRED or NOT_APPLICABLE"
        )
    reason = str(payload.get("reasoning_not_applicable_reason") or "").strip()
    if requirement == "REQUIRED":
        if not reasoning_capabilities:
            raise RuntimeReleaseError(
                "reasoning_requirement=REQUIRED must declare at least one "
                "reasoning_capability"
            )
        if reason:
            raise RuntimeReleaseError(
                "reasoning_not_applicable_reason is forbidden when reasoning is REQUIRED"
            )
        return requirement, None

    if reasoning_capabilities:
        raise RuntimeReleaseError(
            "reasoning_requirement=NOT_APPLICABLE cannot declare reasoning_capabilities"
        )
    if len(reason) < MIN_REASONING_RATIONALE_LENGTH:
        raise RuntimeReleaseError(
            "reasoning_requirement=NOT_APPLICABLE requires a concrete "
            f"reasoning_not_applicable_reason of at least {MIN_REASONING_RATIONALE_LENGTH} characters"
        )
    return requirement, reason


def _validate_obda(mapping: str) -> None:
    if "[MappingDeclaration]" not in mapping or "[PrefixDeclaration]" not in mapping:
        raise RuntimeReleaseError("runtime mapping_obda is not a complete OBDA mapping")
    if SQL_WRITE.search(mapping):
        raise RuntimeReleaseError("runtime OBDA SQL sources must be read-only SELECT")
    structure = obda_structure_issues(mapping)
    if structure:
        raise RuntimeReleaseError(
            "runtime mapping_obda has malformed mapping blocks: "
            + "; ".join(f"{item['mapping_id']}[{item['code']}] {item['message']}" for item in structure[:8])
        )
    sources = OBDA_SOURCE.findall(mapping)
    if not sources:
        raise RuntimeReleaseError("runtime mapping_obda has no SQL source")
    prefix_block = mapping.split("[MappingDeclaration]", 1)[0]
    declared_prefixes = {
        match.group(1)
        for match in re.finditer(
            r"(?m)^\s*([A-Za-z][A-Za-z0-9_-]*|):\s*\S+\s*$",
            prefix_block,
        )
    }
    target_lines = re.findall(r"(?im)^\s*target\s+(.+?)\s*$", mapping)
    used_prefixes = {
        match.group(1)
        for target in target_lines
        for match in re.finditer(
            r"(?<![<A-Za-z0-9_-])([A-Za-z][A-Za-z0-9_-]*|):[A-Za-z_][A-Za-z0-9_.-]*",
            target,
        )
    }
    missing_prefixes = used_prefixes - declared_prefixes
    if missing_prefixes:
        display = ["(default)" if value == "" else value for value in missing_prefixes]
        raise RuntimeReleaseError(
            "runtime mapping_obda uses undeclared prefixes: "
            + ", ".join(sorted(display))
        )
    for sql in sources:
        if SQL_WRITE.search(sql) or not re.match(r"^\s*(?:SELECT|WITH)\b", sql, re.I):
            raise RuntimeReleaseError("runtime OBDA SQL sources must be read-only SELECT")


def _validate_query(name: str, query: str) -> None:
    try:
        OntopClient._require_read_only_form(query, "SELECT")
    except QueryTemplateError as exc:
        raise RuntimeReleaseError(f"runtime query must be a read-only SELECT: {name}") from exc
    # Parameter templates are parsed after rendering their declared validation
    # cases/defaults. A SELECT keyword inside SQL or a serialized object is not
    # evidence that the artifact is an executable SPARQL query.
    if "{{" not in query:
        try:
            parsed = parseQuery(query)
        except Exception as exc:
            raise RuntimeReleaseError(
                f"runtime query must be valid read-only SPARQL SELECT: {name}"
            ) from exc
        if parsed[1].name != "SelectQuery":
            raise RuntimeReleaseError(
                f"runtime query must be valid read-only SPARQL SELECT: {name}"
            )


def compile_mapping_with_identity(
    mapping: str,
    deployment_id: str,
    source_mapping_sha256: str,
) -> str:
    collection_end = mapping.rfind("]]")
    if collection_end < 0:
        raise RuntimeReleaseError("runtime mapping_obda has no mapping collection end")
    marker = f"""

mappingId ORIONDeploymentIdentity
target <urn:orion:ontop:deployment-marker> <urn:orion:ontop:deploymentId> {{deployment_id}} ; <urn:orion:ontop:sourceMappingSha256> {{source_mapping_sha256}} ; <urn:orion:ontop:accessMode> {{access_mode}} .
source SELECT '{deployment_id}' AS deployment_id, '{source_mapping_sha256}' AS source_mapping_sha256, 'READ_ONLY' AS access_mode
"""
    return mapping[:collection_end].rstrip() + marker + "]]\n"


def _identity_query() -> str:
    return """SELECT ?deployment_id ?source_mapping_sha256 ?access_mode WHERE {
  <urn:orion:ontop:deployment-marker>
    <urn:orion:ontop:deploymentId> ?deployment_id ;
    <urn:orion:ontop:sourceMappingSha256> ?source_mapping_sha256 ;
    <urn:orion:ontop:accessMode> ?access_mode .
}
"""


def _safe_relative(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise RuntimeReleaseError("runtime artifact path must be relative")
    root = root.resolve()
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        raise RuntimeReleaseError("runtime artifact path is missing or escapes S3")
    return target


def _checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise RuntimeReleaseError(f"runtime contract is missing or invalid: {path.name}") from exc
    if not isinstance(payload, dict):
        raise RuntimeReleaseError(f"runtime contract must be an object: {path.name}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
