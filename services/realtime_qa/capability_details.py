"""On-demand release metadata; never execute a query or infer field meanings."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from rdflib import URIRef, Variable
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.parserutils import CompValue

from services.realtime_qa.models import (
    DocumentQueryRequest,
    EvidenceRecord,
    OntologyReleaseBinding,
    RealtimeEvidenceRequest,
    RealtimeReleaseExpectation,
)
from services.realtime_qa.runtime import RealtimeRuntimeNotFound, RealtimeRuntimeUnavailable

MAX_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_DEFINITION_CHARS = 12000


class CapabilityIntegrityError(ValueError):
    pass


class CapabilityDefinitionTooLarge(ValueError):
    pass


class CapabilityDescriptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(pattern=r"^session-[a-f0-9-]{36}$")
    expected_release: RealtimeReleaseExpectation
    kind: Literal["structured", "reasoning", "document", "document_fact"] | None = None
    name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{1,63}$")


def _read_artifact(binding: OntologyReleaseBinding, relative: str) -> bytes:
    root = Path(binding.package_path).resolve()
    path = (root / relative).resolve()
    if Path(relative).is_absolute() or not path.is_relative_to(root):
        raise CapabilityIntegrityError("definition artifact escapes the release package")
    expected = binding.artifact_checksums.get(relative)
    if not expected:
        raise CapabilityIntegrityError("definition artifact is not bound by the release manifest")
    try:
        if path.stat().st_size > MAX_ARTIFACT_BYTES:
            # Verify even omitted content, without loading or parsing a large file.
            with path.open("rb") as stream:
                actual = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != expected:
                raise CapabilityIntegrityError("definition artifact checksum mismatch")
            raise CapabilityDefinitionTooLarge("definition artifact exceeds the read limit; checksum verified, content omitted")
        data = path.read_bytes()
    except OSError as exc:
        raise CapabilityIntegrityError("definition artifact is unavailable") from exc
    if "sha256:" + hashlib.sha256(data).hexdigest() != expected:
        raise CapabilityIntegrityError("definition artifact checksum mismatch")
    return data


def _definition_binding(binding: OntologyReleaseBinding) -> OntologyReleaseBinding:
    try:
        data = (Path(binding.package_path) / "manifest.json").read_bytes()
    except OSError as exc:
        raise CapabilityIntegrityError("release manifest is unavailable") from exc
    if "sha256:" + hashlib.sha256(data).hexdigest() != binding.release_fingerprint:
        raise CapabilityIntegrityError("release manifest checksum mismatch")
    manifest = json.loads(data)
    checksums = dict(binding.artifact_checksums)
    # The runtime loader retains only execution artifacts. Resolve these fixed
    # definition files through the same fingerprint, never an input file path.
    for item in manifest.get("files", []):
        relative = item.get("path")
        if relative in {"02-工程定义/mapping.yaml", "02-工程定义/ontology-design.yaml"}:
            expected = item.get("sha256")
            if not isinstance(expected, str) or not expected.startswith("sha256:"):
                raise CapabilityIntegrityError("definition artifact has no manifest checksum")
            if relative in checksums and checksums[relative] != expected:
                raise CapabilityIntegrityError("definition artifact checksum binding mismatch")
            checksums[relative] = expected
    return binding.model_copy(update={"artifact_checksums": checksums})


def _reference(binding: OntologyReleaseBinding, path: str) -> dict[str, str]:
    return {"artifact": path, "sha256": binding.artifact_checksums[path]}


def _public_cq_contracts(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose reviewed business meaning separately from validation examples.

    Rebuild both levels from scalar allowlists: copying cq_bindings or whole
    dimensions would expose expected answers, assertions or provenance hashes.
    """
    bindings = raw.get("cq_bindings")
    if not isinstance(bindings, dict):
        return []
    result = []
    dimension_fields = (
        "dimension", "label_zh", "applicability", "binding", "ontology_term", "path", "reason_zh",
    )
    for question_id, binding in bindings.items():
        if not isinstance(question_id, str) or not isinstance(binding, dict):
            continue
        item: dict[str, Any] = {"source_question_id": question_id}
        for field in ("answer_mode", "answer_scope_zh"):
            if isinstance(binding.get(field), str):
                item[field] = binding[field]
        dimensions = binding.get("required_business_dimensions")
        if isinstance(dimensions, list):
            item["required_business_dimensions"] = [
                {key: dimension[key] for key in dimension_fields if isinstance(dimension.get(key), str)}
                for dimension in dimensions if isinstance(dimension, dict)
            ]
        result.append(item)
    return result


def _public_contract(raw: dict[str, Any]) -> dict[str, Any]:
    # Validation answers are test oracles, not evidence from the user's query.
    keys = (
        "description_zh", "parameters", "result_fields", "question_examples",
        "query_mode", "source_scope", "source_ids", "source_tables", "source_columns",
        "source_tables_by_id", "source_columns_by_id", "engine", "evidence_query",
        "result_predicates", "rule_artifact", "rule_sha256", "closed_world_predicates",
        "execution_scope", "fact_bindings", "source_rule_ids", "ontology_terms",
        "fact_source", "fact_artifact", "fact_sha256",
    )
    contract = {key: raw[key] for key in keys if key in raw}
    cq_contracts = _public_cq_contracts(raw)
    if cq_contracts:
        contract["cq_contracts"] = cq_contracts
    return contract


def _field_predicates(query: str) -> dict[str, set[str]]:
    """Only accept direct SPARQL triple bindings, never guess from aliases."""
    result: dict[str, set[str]] = {}

    def walk(value: CompValue) -> None:
        # Walk only the positive binding algebra. Expressions, UNION branches,
        # MINUS and subqueries can contain same-named variables that do not
        # define an output. Treat unsupported algebra as unknown, not lineage.
        if value.name == "BGP":
            for _, predicate, obj in value["triples"]:
                if isinstance(obj, Variable):
                    if not isinstance(predicate, URIRef):
                        raise ValueError("Variable or path predicates have no single frozen field definition")
                    result.setdefault(str(obj), set()).add(str(predicate))
        elif value.name in {"SelectQuery", "Project", "Distinct", "Reduced", "Slice", "OrderBy", "Filter"}:
            walk(value["p"])
        elif value.name in {"Join", "LeftJoin"}:
            walk(value["p1"])
            walk(value["p2"])
        else:
            raise ValueError("Query binding algebra is not supported for static field definitions")

    algebra = prepareQuery(query).algebra
    walk(algebra)
    projected = {str(variable) for variable in algebra["PV"]}
    return {field: predicates for field, predicates in result.items() if field in projected}


def _query_definitions(binding: OntologyReleaseBinding, name: str) -> dict[str, Any]:
    capability = binding.ontop_query_capabilities.get(name, {})
    fields = {field: {"status": "UNKNOWN", "reason": "No unambiguous frozen field definition located"}
              for field in capability.get("result_fields", [])}
    result: dict[str, Any] = {
        "status": "UNKNOWN", "fields": fields, "projections": [], "artifacts": [],
        "interpretation_policy": "Definitions are source excerpts, not query results. Do not infer status, causality or disjoint categories from field names.",
    }
    query_path = binding.ontop_query_artifacts.get(name)
    mapping_path = "02-工程定义/mapping.yaml"
    if not query_path or mapping_path not in binding.artifact_checksums:
        return result
    result["artifacts"] = [_reference(binding, query_path), _reference(binding, mapping_path)]
    try:
        query = _read_artifact(binding, query_path).decode("utf-8")
        mapping = yaml.safe_load(_read_artifact(binding, mapping_path))
    except CapabilityDefinitionTooLarge as exc:
        result["reason"] = str(exc)
        return result
    except yaml.YAMLError as exc:
        raise CapabilityIntegrityError("frozen mapping definition cannot be parsed") from exc
    try:
        predicates = _field_predicates(query)
    except Exception:
        # Parameterized templates or complex unsupported algebra remain unknown.
        result["reason"] = "Query template cannot be resolved statically; no field meaning was inferred"
        return result
    if not isinstance(mapping, dict) or not isinstance(mapping.get("mappings"), list):
        return result
    mappings = [item for item in mapping["mappings"] if isinstance(item, dict)]
    namespace = str(mapping.get("namespace") or "")
    projections: dict[str, dict[str, Any]] = {}
    remaining = MAX_DEFINITION_CHARS
    for field in fields:
        field_predicates = predicates.get(field, set())
        if len(field_predicates) != 1:
            continue
        iri = next(iter(field_predicates))
        matches = [item for item in mappings if
                   (str(item.get("target")) if str(item.get("target", "")).startswith(("http://", "https://", "urn:"))
                    else namespace + str(item.get("target", ""))) == iri]
        if len(matches) != 1:
            continue
        item = matches[0]
        if item.get("mapping_type") == "COLUMN_TO_DATA_PROPERTY":
            # The standard mapping contract stores an exact table.column
            # reference. Quote-aware matching retains it verbatim; no SQL or
            # business calculation is manufactured from the column name.
            identifier = r'(?:[A-Za-z_][A-Za-z0-9_$]*|"(?:[^\"]|\"\")+")'
            source_reference = item.get("source")
            match = re.fullmatch(rf"(?P<table>{identifier}(?:\.{identifier})*)\.(?P<column>{identifier})", source_reference) if isinstance(source_reference, str) else None
            if match is None:
                continue
            definition = {
                "status": "SOURCE_DEFINITION", "predicate_iri": iri,
                "mapping_id": item.get("id"), "mapping_type": "COLUMN_TO_DATA_PROPERTY",
                "source_reference": source_reference,
                "label_zh": item.get("target_label_zh"), "comment_zh": item.get("target_comment_zh"),
                "source_refs": item.get("source_refs", []),
                "source": _reference(binding, mapping_path),
            }
            parents = [candidate for candidate in mappings if
                       candidate.get("mapping_type") == "TABLE_TO_CLASS"
                       and candidate.get("source") == match.group("table")]
            if len(parents) == 1:
                definition["table_mapping"] = {key: parents[0][key] for key in
                    ("id", "source", "target", "target_label_zh", "target_comment_zh", "source_refs") if key in parents[0]}
            definition_size = len(json.dumps(definition, ensure_ascii=False))
            if definition_size <= remaining:
                fields[field] = definition
                remaining -= definition_size
            continue
        projection_id = item.get("source_projection_id")
        source_column = item.get("source_column")
        parents = [candidate for candidate in mappings if candidate.get("id") == projection_id]
        if not projection_id or not source_column or len(parents) != 1:
            continue
        parent = parents[0]
        if parent.get("mapping_type") != "SQL_TO_CLASS" or not isinstance(parent.get("source"), str):
            continue
        if projection_id not in projections:
            sql = parent["source"]
            included = len(sql) <= remaining
            projections[projection_id] = {
                "mapping_id": projection_id, "status": "SOURCE_DEFINITION" if included else "OMITTED_SIZE_LIMIT",
                "sql_definition": sql if included else None,
                "definition_chars": len(sql), "execution_authorized": False,
                "source": _reference(binding, mapping_path),
            }
            if included:
                remaining -= len(sql)
        fields[field] = {
            "status": "SOURCE_DEFINITION" if projections[projection_id]["sql_definition"] is not None else "UNKNOWN",
            "predicate_iri": iri, "mapping_id": item.get("id"),
            "source_projection_id": projection_id, "source_column": source_column,
            "label_zh": item.get("target_label_zh"), "comment_zh": item.get("target_comment_zh"),
            "source": _reference(binding, mapping_path),
        }
    result["projections"] = list(projections.values())
    resolved = sum(value["status"] == "SOURCE_DEFINITION" for value in fields.values())
    result["status"] = "SOURCE_DEFINITION" if fields and resolved == len(fields) else "PARTIAL" if resolved else "UNKNOWN"
    return result


def _document_fact_definitions(binding: OntologyReleaseBinding, name: str) -> dict[str, Any]:
    capability = binding.document_fact_queries[name]
    result: dict[str, Any] = {"status": "UNKNOWN", "reason": "No frozen document fact artifact is declared"}
    path = capability.get("fact_artifact")
    if not path or path not in binding.artifact_checksums:
        return result
    if capability.get("fact_sha256") != binding.artifact_checksums[path]:
        raise CapabilityIntegrityError("document fact artifact checksum binding mismatch")
    source = _reference(binding, path)
    try:
        package = json.loads(_read_artifact(binding, path))
    except CapabilityDefinitionTooLarge as exc:
        return {"status": "UNKNOWN", "reason": str(exc), "source": source}
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CapabilityIntegrityError("frozen document fact definition cannot be parsed") from exc
    if not isinstance(package, dict) or package.get("query_name") != name:
        raise CapabilityIntegrityError("document fact definition query binding mismatch")
    keys = ("description_zh", "fact_source", "fact_bindings", "source_scope")
    if any(package.get(key) != capability.get(key) for key in keys):
        raise CapabilityIntegrityError("document fact definition contract binding mismatch")
    # Facts are query evidence, not capability definitions. Never return the
    # materialized records or derive labels from their observed values here.
    definition = {key: package[key] for key in keys if key in package}
    if not definition.get("fact_source") or not definition.get("fact_bindings"):
        return {"status": "UNKNOWN", "reason": "Frozen document fact definition is incomplete", "source": source}
    if len(json.dumps(definition, ensure_ascii=False)) > MAX_DEFINITION_CHARS:
        return {"status": "UNKNOWN", "reason": "Document fact definition exceeds the output limit", "source": source}
    return {"status": "SOURCE_DEFINITION", "definition": definition, "source": source,
            "interpretation_policy": "This is the frozen fact-source contract, not queried document evidence. Fact bindings define rule inputs; they do not establish result-field business meanings."}


def describe_capability(binding: OntologyReleaseBinding, *, kind: str | None, name: str | None) -> dict[str, Any]:
    binding = _definition_binding(binding)
    groups = {
        "structured": {key: binding.ontop_query_capabilities.get(key, {}) for key in binding.ontop_query_names},
        "reasoning": binding.reasoning_capabilities,
        "document_fact": {key: value for key, value in binding.document_fact_queries.items() if value.get("cq_bindings")},
        "document": {value: {} for value in binding.document_query_capabilities},
    }
    selected = [kind] if kind else list(groups)
    if name is None:
        return {"mode": "catalog", "runtime_mode": binding.runtime_mode, "capabilities": [
            {"kind": group, "name": key, "description_zh": str(raw.get("description_zh") or key)[:500]}
            for group in selected for key, raw in sorted(groups[group].items())
        ]}
    matches = [(group, groups[group][name]) for group in selected if name in groups[group]]
    if not matches:
        raise RealtimeRuntimeNotFound("capability is not declared by this release")
    if len(matches) != 1:
        raise ValueError("capability name is ambiguous; specify kind")
    group, raw = matches[0]
    result = {"mode": "detail", "kind": group, "name": name, "contract": _public_contract(raw)}
    if group == "structured":
        result["field_definitions"] = _query_definitions(binding, name)
    elif group == "document_fact":
        result["request_field"] = "document_fact_query"
        result["document_fact_definitions"] = _document_fact_definitions(binding, name)
    elif group == "reasoning":
        result["rule_definitions"] = {"status": "UNKNOWN", "reason": "No frozen rule artifact is declared"}
        rule_path = raw.get("rule_artifact")
        if rule_path and rule_path in binding.artifact_checksums:
            try:
                rules = json.loads(_read_artifact(binding, rule_path))
            except CapabilityDefinitionTooLarge as exc:
                result["rule_definitions"] = {"status": "UNKNOWN", "reason": str(exc), "source": _reference(binding, rule_path)}
            else:
                definitions = rules.get("rules", []) if isinstance(rules, dict) else []
                result["rule_definitions"] = {
                    "status": "SOURCE_DEFINITION" if definitions and len(json.dumps(definitions)) <= MAX_DEFINITION_CHARS else "UNKNOWN",
                    "rules": definitions if len(json.dumps(definitions)) <= MAX_DEFINITION_CHARS else [],
                    "source": _reference(binding, rule_path),
                }
        query = raw.get("evidence_query")
        if query in binding.document_fact_queries:
            result["evidence_query_contract"] = _public_contract(binding.document_fact_queries[query])
            result["document_fact_definitions"] = _document_fact_definitions(binding, query)
            result["field_definitions"] = {"status": "UNKNOWN", "reason": "Document fact bindings define rule inputs; no independent result-field definitions are declared"}
        elif query in binding.ontop_query_capabilities:
            result["evidence_query_contract"] = _public_contract(binding.ontop_query_capabilities[query])
            result["field_definitions"] = _query_definitions(binding, query)
        else:
            result["field_definitions"] = {"status": "UNKNOWN", "reason": "Evidence is not a structured query with a located frozen mapping"}
    elif name == "current_full_text_search":
        result["contract"] = {
            "request_field": "document_query", "parameters": DocumentQueryRequest.model_json_schema(),
            "result_schema": EvidenceRecord.model_json_schema(),
            "scope": "current document versions in the bound release project",
            "source_resolution": "Actual document IDs, versions and locators come from the query evidence receipt",
        }
    elif name == "reviewed_entity_evidence":
        result["contract"] = {
            "request_field": "entity_iris",
            "parameters": RealtimeEvidenceRequest.model_json_schema()["properties"]["entity_iris"],
            "result_schema": EvidenceRecord.model_json_schema(), "scope": "reviewed entity-linked evidence",
            "source_resolution": "Actual document IDs, versions and locators come from the query evidence receipt",
        }
    else:
        result["contract"] = {"status": "UNKNOWN", "reason": "This document capability has no recognized application contract"}
    return result


def register_capability_api(app: FastAPI, resolve_runtime: Callable[..., Any]) -> None:
    @app.post("/ontology/realtime/session-capability")
    def session_capability(request: CapabilityDescriptionRequest) -> dict[str, Any]:
        expected = request.expected_release
        try:
            runtime = resolve_runtime(expected.project_id)
            actual = runtime.binding
            if any(getattr(expected, key) != getattr(actual, key) for key in
                   ("project_id", "release_version", "release_fingerprint")):
                raise HTTPException(409, "Harness session release does not match the configured realtime runtime")
            result = describe_capability(actual, kind=request.kind, name=request.name)
        except RealtimeRuntimeNotFound as exc:
            raise HTTPException(404, str(exc)) from exc
        except (RealtimeRuntimeUnavailable, CapabilityIntegrityError) as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"session_id": request.session_id, "release_match": "verified",
                "expected_release": expected.model_dump(), "description": result}
