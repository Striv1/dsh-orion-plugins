"""Bounded generated SELECTs against immutable, released source contracts.

A successful execution checks syntax, scope and resources, not business intent.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import threading
from pathlib import Path
from typing import Any

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, URIRef, Variable
from rdflib.plugins.sparql import prepareQuery
from rdflib.plugins.sparql.parserutils import CompValue

from services.ontology_engineering.sparql_execution import ensure_spec_aggregates

ensure_spec_aggregates()  # generated checked SELECTs follow SPARQL 1.1 aggregates

MAX_QUERY_CHARS = 4096
MAX_SOURCE_BYTES = 32 * 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
WORKER_TIMEOUT_SECONDS = 8.0
RUNTIME_ARTIFACT = "05-运行时/realtime-runtime.json"
SEMANTIC_STATUS = "GENERATED_CHECKED_NOT_BUSINESS_APPROVED"
_WORKER_SLOTS = threading.BoundedSemaphore(2)


class CheckedQueryError(ValueError):
    pass


class CheckedQueryTimeout(CheckedQueryError, TimeoutError):
    pass


_ALLOWED_ALGEBRA = frozenset({
    "SelectQuery", "Project", "Distinct", "Reduced", "Slice", "OrderBy", "OrderCondition",
    "BGP", "Join", "Filter", "Extend", "Group", "AggregateJoin", "Aggregate_Count",
    "Aggregate_Sum", "Aggregate_Min", "Aggregate_Max", "Aggregate_Avg", "Aggregate_Sample",
    "RelationalExpression", "ConditionalAndExpression", "ConditionalOrExpression",
    "AdditiveExpression", "MultiplicativeExpression", "UnaryNot", "UnaryPlus", "UnaryMinus",
    "Builtin_STR", "Builtin_LANG", "Builtin_DATATYPE", "Builtin_BOUND", "Builtin_IF",
    "Builtin_COALESCE", "Builtin_STRLEN", "Builtin_LCASE", "Builtin_UCASE",
    "Builtin_CONTAINS", "Builtin_STRSTARTS", "Builtin_STRENDS", "Builtin_SUBSTR", "Builtin_CONCAT",
})


def validate_checked_select(sparql: str, *, term_kinds: dict[str, str], allowed_iris: set[str],
                            term_datatypes: dict[str, str] | None = None,
                            domains: dict[str, set[str]] | None = None,
                            disjoint_classes: set[frozenset[str]] | None = None) -> Any:
    """An algebra allowlist, fixed mapped predicates and one connected join graph."""
    if not isinstance(sparql, str) or not 1 <= len(sparql) <= MAX_QUERY_CHARS:
        raise CheckedQueryError("SELECT text must contain 1..4096 characters")
    try:
        prepared = prepareQuery(sparql)
    except Exception as exc:
        raise CheckedQueryError("Query is not a supported SPARQL SELECT") from exc
    algebra = prepared.algebra
    if algebra.name != "SelectQuery" or algebra.get("datasetClause"):
        raise CheckedQueryError("Only local SELECT without FROM is allowed")
    triples = []
    node_count = 0

    def visit(value: Any, depth: int = 0) -> None:
        nonlocal node_count
        node_count += 1
        if depth > 40 or node_count > 1500:
            raise CheckedQueryError("Query expression complexity exceeds the supported limit")
        if isinstance(value, CompValue):
            if value.name not in _ALLOWED_ALGEBRA:
                raise CheckedQueryError(f"Unsupported SELECT operation: {value.name}")
            if value.name == "BGP":
                triples.extend(value["triples"])
            if value.name == "Slice" and int(value.get("start") or 0) > 10000:
                raise CheckedQueryError("OFFSET exceeds 10000")
            for key, child in value.items():
                if key != "_vars":
                    visit(child, depth + 1)
        elif isinstance(value, list | tuple):
            for child in value:
                visit(child, depth + 1)
        elif isinstance(value, URIRef) and str(value) not in allowed_iris | {str(RDF.type)}:
            raise CheckedQueryError("Query IRI is absent from the selected released vocabulary/data")
    visit(algebra)
    if not 1 <= len(triples) <= 16:
        raise CheckedQueryError("Query requires 1..16 source triple patterns")
    if not 1 <= len(algebra["PV"]) <= 20:
        raise CheckedQueryError("Query must project 1..20 variables")
    components = []
    explicit_types: dict[Any, set[str]] = {}
    for subject, predicate, obj in triples:
        if predicate == RDF.type and isinstance(obj, URIRef):
            explicit_types.setdefault(subject, set()).add(str(obj))
    for subject, predicate, obj in triples:
        if not isinstance(predicate, URIRef):
            raise CheckedQueryError("Variable predicates and property paths are not supported")
        if predicate == RDF.type:
            if not isinstance(obj, URIRef) or term_kinds.get(str(obj)) != "CLASS":
                raise CheckedQueryError("rdf:type requires an explicitly mapped released class")
        elif term_kinds.get(str(predicate)) not in {"DATA_PROPERTY", "OBJECT_PROPERTY"}:
            raise CheckedQueryError("Predicate is not a mapped released property")
        kind = term_kinds.get(str(predicate))
        if kind == "OBJECT_PROPERTY" and isinstance(obj, Literal):
            raise CheckedQueryError("Object property cannot be queried with a literal object")
        if kind == "DATA_PROPERTY" and isinstance(obj, URIRef):
            raise CheckedQueryError("Data property cannot be queried with an IRI object")
        if kind == "DATA_PROPERTY" and isinstance(obj, Literal):
            expected_datatype = (term_datatypes or {}).get(str(predicate))
            if expected_datatype and str(obj.datatype or XSD.string) != expected_datatype:
                raise CheckedQueryError("Literal datatype differs from the declared property range")
            if obj.ill_typed is True:
                raise CheckedQueryError("Ill-typed query literal")
        for explicit in explicit_types.get(subject, set()):
            for required in (domains or {}).get(str(predicate), set()):
                if frozenset((explicit, required)) in (disjoint_classes or set()):
                    raise CheckedQueryError("Explicit class conflicts with the property's declared domain")
        if not isinstance(subject, URIRef | Variable) or not isinstance(obj, URIRef | Variable | Literal):
            raise CheckedQueryError("Blank-node query patterns are not supported")
        variables = {term for term in (subject, obj) if isinstance(term, Variable)}
        if not variables:
            raise CheckedQueryError("Ground-only patterns cannot establish a checked result binding")
        components.append(variables)
    connected = components.pop(0)
    while components:
        matching = next((index for index, variables in enumerate(components) if variables & connected), None)
        if matching is None:
            raise CheckedQueryError("Disconnected patterns would create a Cartesian product")
        connected |= components.pop(matching)
    return prepared


def _snapshot_payload(runtime: Any) -> dict[str, Any]:
    binding = runtime.binding
    return {"package_path": str(binding.package_path), "release_fingerprint": binding.release_fingerprint,
            "project_id": binding.project_id, "release_version": binding.release_version,
            "ontology_artifact": binding.ontology_artifact,
            "artifact_checksums": dict(binding.artifact_checksums),
            "document_names": list(binding.document_fact_queries)}


def _read_checked(payload: dict[str, Any], relative: str, manifest: dict[str, str]) -> bytes:
    root = Path(payload["package_path"]).resolve()
    rel = Path(relative)
    target = root / rel
    if rel.is_absolute() or ".." in rel.parts or root not in target.resolve().parents or any(
        parent.is_symlink() for parent in (target, *target.parents) if root in parent.parents
    ):
        raise CheckedQueryError("Release artifact path is unsafe")
    expected = manifest.get(relative)
    # Runtime loaders retain execution artifact checksums, not necessarily the
    # runtime definition itself. The fingerprint-verified manifest is the full
    # authority; a retained checksum, when present, must still agree with it.
    if not expected or payload["artifact_checksums"].get(relative, expected) != expected:
        raise CheckedQueryError("Artifact is not bound to this release manifest")
    if target.stat().st_size > MAX_SOURCE_BYTES:
        raise CheckedQueryError("Released source exceeds checked-query size limit")
    data = target.read_bytes()
    if len(data) > MAX_SOURCE_BYTES or "sha256:" + hashlib.sha256(data).hexdigest() != expected:
        raise CheckedQueryError("Released source checksum mismatch")
    return data


def _load_release(payload: dict[str, Any]) -> tuple[dict[str, Any], Graph, dict[str, str], dict[str, str], dict[str, str]]:
    from services.ontology_engineering.formal_facts import ontology_materialization_contract

    path = Path(payload["package_path"]).resolve() / "manifest.json"
    if path.is_symlink() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise CheckedQueryError("Release manifest path/size is invalid")
    data = path.read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != payload["release_fingerprint"]:
        raise CheckedQueryError("Release manifest fingerprint mismatch")
    files = json.loads(data)["files"]
    manifest = {item["path"]: item["sha256"] for item in files}
    if len(manifest) != len(files):
        raise CheckedQueryError("Release manifest contains duplicate artifacts")
    contract = json.loads(_read_checked(payload, RUNTIME_ARTIFACT, manifest))
    ontology = Graph().parse(data=_read_checked(payload, payload["ontology_artifact"], manifest), format="turtle")
    if any(ontology.objects(None, OWL.imports)):
        raise CheckedQueryError("Imported ontologies are outside this checked-query snapshot")
    kinds, datatypes = ontology_materialization_contract(ontology)
    return contract, ontology, kinds, datatypes, manifest


def _document_mapping(contract: dict[str, Any], name: str) -> dict[str, Any]:
    source = contract.get("document_fact_queries", {}).get(name)
    if not isinstance(source, dict):
        raise CheckedQueryError("Document source is absent from released runtime")
    if source.get("ontology_terms"):
        return source
    terms: dict[str, str] = {}
    bindings: dict[str, dict[str, Any]] = {}
    for capability in contract.get("reasoning_capabilities", {}).values():
        if capability.get("evidence_query") != name:
            continue
        for binding in capability.get("fact_bindings") or []:
            predicate = binding["predicate"]
            iri = (capability.get("ontology_terms") or {}).get(predicate)
            if not iri:
                raise CheckedQueryError("Released reasoning source lacks a mapped input term")
            if predicate in terms and (terms[predicate] != iri or bindings[predicate] != binding):
                raise CheckedQueryError("Released source has conflicting input mappings")
            terms[predicate] = iri
            bindings[predicate] = binding
    if not terms:
        raise CheckedQueryError("Document source has no released input term mapping")
    return {**source, "ontology_terms": terms, "fact_bindings": list(bindings.values())}


def _document_graph(payload: dict[str, Any], name: str, materialize: bool = True) -> tuple[Graph, dict[str, str], dict[str, Any]]:
    from services.ontology_engineering.formal_facts import (
        materialize_formal_fact,
        validate_materialization_bindings,
    )

    from .reasoning import document_evidence_facts
    from .reasoning_contract import _normalize_document_facts

    contract, ontology, kinds, datatypes, manifest = _load_release(payload)
    if name not in payload["document_names"]:
        raise CheckedQueryError("Document dataset is not bound to the active runtime")
    cap = _document_mapping(contract, name)
    data = _read_checked(payload, cap["fact_artifact"], manifest)
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if digest != cap["fact_sha256"]:
        raise CheckedQueryError("Document fact contract checksum mismatch")
    facts = _normalize_document_facts(name, json.loads(data)["facts"])
    if len(facts) > 100000:
        raise CheckedQueryError("Complete fact source exceeds checked-query resource budget")
    # The execution does not depend on a named CQ. Its fact-mapping contract must
    # nevertheless preserve all facts and positions, with no runtime substitution.
    predicates = {item["predicate"] for item in facts}
    bindings = cap["fact_bindings"]
    if predicates - {b["predicate"] for b in bindings} or set(cap["ontology_terms"]) != {b["predicate"] for b in bindings}:
        raise CheckedQueryError("Document vocabulary/bindings do not cover the complete source")
    for b in bindings:
        if b.get("when") or any(set(a) != {"field"} or type(a["field"]) is not int or a["field"] != i for i, a in enumerate(b["arguments"])):
            raise CheckedQueryError("Checked document dataset requires identity field bindings")
    validate_materialization_bindings(cap, kinds)
    graph = Graph()
    symbols: dict[str, str] = {}
    for fact in document_evidence_facts(facts, bindings, symbols) if materialize else []:
        materialize_formal_fact(graph, fact, cap["ontology_terms"], symbols,
                               term_kinds=kinds, term_datatypes=datatypes, strict_types=True)
    inferred_only = {
        predicate: capability.get("ontology_terms", {}).get(predicate)
        for capability in contract.get("reasoning_capabilities", {}).values()
        if capability.get("evidence_query") == name
        for predicate in capability.get("result_predicates") or [] if predicate not in predicates
    }
    mapped_kinds = {iri: kinds[iri] for predicate, iri in cap["ontology_terms"].items()
                    if iri in kinds and predicate not in inferred_only}
    evidence = {json.dumps(item.get("provenance") or {}, sort_keys=True, ensure_ascii=False) for item in facts}
    provenance = {"scope": "DATASET_LEVEL_NOT_ROW_LINEAGE", "fact_count": len(facts),
                  "evidence_reference_count": len(evidence),
                  "evidence_references": [json.loads(item) for item in sorted(evidence)[:5]],
                  "evidence_references_truncated": len(evidence) > 5}
    return graph, mapped_kinds, {"dataset": "document_fact:" + name,
        "project_id": payload["project_id"], "release_version": payload["release_version"],
        "release_fingerprint": payload["release_fingerprint"], "fact_artifact": cap["fact_artifact"],
        "fact_sha256": digest, "fact_source": str(cap.get("fact_source") or ""),
        "source_scope": cap.get("source_scope"),
        "scope_policy": "Complete selected fact dataset; named CQ filters are not automatically applied",
        "unavailable_terms": [{"predicate": predicate, "iri": iri, "status": "UNAVAILABLE",
                               "reason": "Inference-only conclusion is not materialized in this source dataset"}
                              for predicate, iri in inferred_only.items()],
        "query_schema": {"datatypes": datatypes,
                         "domains": {iri: {str(v) for v in ontology.objects(URIRef(iri), RDFS.domain)} for iri in mapped_kinds},
                         "disjoint_classes": {frozenset((str(a), str(b))) for a, b in ontology.subject_objects(OWL.disjointWith)}},
        "ontology_artifact": payload["ontology_artifact"],
        "ontology_sha256": manifest[payload["ontology_artifact"]], "provenance": provenance}


def _execute_document(payload: dict[str, Any], sparql: str, name: str, limit: int) -> dict[str, Any]:
    graph, kinds, source = _document_graph(payload, name)
    iris = {str(term) for triple in graph for term in triple if isinstance(term, URIRef)} | set(kinds)
    schema = source.pop("query_schema")
    prepared = validate_checked_select(sparql, term_kinds=kinds, allowed_iris=iris,
                                      term_datatypes=schema["datatypes"], domains=schema["domains"],
                                      disjoint_classes=schema["disjoint_classes"])
    selected = graph.query(prepared)
    variables = [str(v) for v in selected.vars or []]
    rows = []
    row_terms = []
    truncated = False
    for row in selected:
        if len(rows) >= limit:
            truncated = True
            break
        values = {field: str(row.get(field)) if row.get(field) is not None else None for field in variables}
        if any(value is not None and len(value) > 16000 for value in values.values()):
            raise CheckedQueryError("Query result cell exceeds output bound")
        rows.append(values)
        row_terms.append({field: ({"type": "uri", "value": str(row.get(field))}
            if isinstance(row.get(field), URIRef) else {"type": "literal", "value": str(row.get(field)),
                **({"datatype": str(row.get(field).datatype)} if row.get(field).datatype else {}),
                **({"language": row.get(field).language} if row.get(field).language else {})})
            for field in variables if row.get(field) is not None})
        if len(json.dumps([rows, row_terms], ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
            raise CheckedQueryError("Query result exceeds output byte bound")
    provenance = source.pop("provenance")
    return {"rows": rows, "row_terms": row_terms, "variables": variables, "row_count": len(rows), "truncated": truncated,
            "query_sha256": "sha256:" + hashlib.sha256(sparql.encode()).hexdigest(),
            "source": source, "provenance": provenance, "semantic_status": SEMANTIC_STATUS}


def _worker_entry(connection: Any, target: Any, args: tuple[Any, ...]) -> None:
    try:
        result = target(*args)
        if len(json.dumps(result, ensure_ascii=False, default=str).encode()) > MAX_RESULT_BYTES:
            raise CheckedQueryError("Checked query response exceeds the total output byte bound")
        connection.send((True, result))
    except BaseException as exc:
        connection.send((False, f"{type(exc).__name__}: {str(exc)[:1000]}"))
    finally:
        connection.close()


def _run_isolated(target: Any, args: tuple[Any, ...], timeout: float) -> Any:
    if not _WORKER_SLOTS.acquire(blocking=False):
        raise CheckedQueryError("BUSY: checked-query workers are in use; retry later")
    try:
        return _run_isolated_process(target, args, timeout)
    finally:
        _WORKER_SLOTS.release()


def _run_isolated_process(target: Any, args: tuple[Any, ...], timeout: float) -> Any:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_worker_entry, args=(child, target, args), daemon=True)
    try:
        process.start()
    except BaseException:
        parent.close()
        child.close()
        process.close()
        raise
    child.close()
    try:
        if not parent.poll(timeout):
            raise CheckedQueryTimeout("Checked query exceeded its worker execution deadline")
        try:
            success, result = parent.recv()
        except EOFError as exc:
            raise CheckedQueryError("Checked query worker exited without a result") from exc
        if not success:
            raise CheckedQueryError(result)
        return result
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
        process.join(1)
        if process.is_alive():
            process.kill()
            process.join()
        process.close()


def _execute_structured(config: dict[str, Any], sparql: str, limit: int) -> dict[str, Any]:
    from .checked_structured import execute_checked_structured

    prepared = validate_checked_select(sparql, term_kinds=config["term_kinds"], allowed_iris=set(config["term_kinds"]),
                                      term_datatypes=config.get("term_datatypes"), domains=config.get("domains"),
                                      disjoint_classes=config.get("disjoint_classes"))
    top = prepared.algebra["p"]
    if top.name == "Slice" and "length" in top:
        if int(top["length"]) > limit + 1:
            raise CheckedQueryError("Explicit LIMIT exceeds the requested result bound")
        execution_query = sparql
    else:
        # A newline prevents a trailing comment from hiding the enforced limit.
        execution_query = sparql + f"\nLIMIT {limit + 1}"
    result = execute_checked_structured(config, execution_query, limit)
    return {"rows": result["rows"], "variables": result["variables"], "row_count": result["row_count"],
            "row_terms": result.get("row_terms", []), "truncated": result["truncated"],
            "query_sha256": "sha256:" + hashlib.sha256(sparql.encode()).hexdigest(),
            "source": {"dataset": "structured", "release_fingerprint": result["release_fingerprint"],
                       "deployment_id": result["deployment_id"], "mapping_sha256": result["mapping_sha256"],
                       "execution_query_sha256": result["execution_query_sha256"]},
            "provenance": {"scope": "RELEASE_MAPPING_NOT_ROW_LINEAGE"}, "semantic_status": SEMANTIC_STATUS}


def execute_checked_query(runtime: Any, sparql: str, dataset: str, limit: int = 100) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CheckedQueryError("limit must be an integer between 1 and 100")
    if not isinstance(sparql, str) or not 1 <= len(sparql) <= MAX_QUERY_CHARS:
        raise CheckedQueryError("SELECT text must contain 1..4096 characters")
    if dataset == "structured":
        from .checked_structured import prepare_checked_structured

        try:
            config = prepare_checked_structured(runtime, dataset)
        except (ValueError, AttributeError, KeyError, OSError) as exc:
            raise CheckedQueryError("UNAVAILABLE: structured dataset lacks a verified isolated deployment") from exc
        return _run_isolated(_execute_structured, (config, sparql, limit), WORKER_TIMEOUT_SECONDS)
    if not isinstance(dataset, str) or not dataset.startswith("document_fact:"):
        raise CheckedQueryError("UNAVAILABLE: dataset has no proven release-isolated checked query route")
    return _run_isolated(_execute_document, (_snapshot_payload(runtime), sparql, dataset.split(":", 1)[1], limit), WORKER_TIMEOUT_SECONDS)


def _discover_documents(payload: dict[str, Any]) -> list[dict[str, Any]]:
    contract, ontology, kinds, datatypes, _ = _load_release(payload)
    datasets = []
    for name, cap in contract.get("document_fact_queries", {}).items():
        if name not in payload["document_names"]:
            continue
        try:
            cap = _document_mapping(contract, name)
            _, mapped_kinds, source = _document_graph(payload, name, materialize=False)
            terms = cap["ontology_terms"]
            available = True
            reason = None
        except (ValueError, RuntimeError, KeyError, OSError) as exc:
            terms = {}
            available = False
            reason = str(exc)[:300]
        datasets.append({"dataset": "document_fact:" + name, "status": "AVAILABLE" if available else "UNAVAILABLE",
            "description_zh": str(cap.get("description_zh") or ""),
            "fact_source": str(cap.get("fact_source") or ""), "source_scope": cap.get("source_scope"),
            "scope_policy": "Complete selected fact dataset; named CQ filters are not automatically applied",
            "reason": reason,
            "unavailable_terms": source["unavailable_terms"] if available else [],
            "terms": [{"predicate": predicate, "iri": iri, "kind": kinds.get(iri),
                       "label": [str(v) for v in ontology.objects(URIRef(iri), RDFS.label)],
                       "comment": [str(v) for v in ontology.objects(URIRef(iri), RDFS.comment)],
                       "datatype": datatypes.get(iri),
                       "domain": [str(v) for v in ontology.objects(URIRef(iri), RDFS.domain)],
                       "range": [str(v) for v in ontology.objects(URIRef(iri), RDFS.range)]}
                      for predicate, iri in terms.items() if iri in mapped_kinds]})
    return datasets


def discover_checked_query(runtime: Any) -> dict[str, Any]:
    datasets = _run_isolated(_discover_documents, (_snapshot_payload(runtime),), WORKER_TIMEOUT_SECONDS)
    from .checked_structured import discover_checked_structured

    try:
        structured = {**discover_checked_structured(runtime), "status": "AVAILABLE", "dataset": "structured"}
        datasets.append(structured)
    except (ValueError, AttributeError, KeyError, OSError):
        structured = {"status": "UNAVAILABLE", "reason": "No verified release-isolated structured deployment and mapped vocabulary"}
    return {"datasets": datasets, "structured": structured,
            "semantic_status": SEMANTIC_STATUS, "max_limit": 100, "max_query_chars": MAX_QUERY_CHARS, "worker_timeout_seconds": WORKER_TIMEOUT_SECONDS,
            "supported_subset": "Connected mapped BGP, FILTER/BIND, ORDER, GROUP and bounded scalar aggregates; no SERVICE/FROM/GRAPH, paths, variable predicates, OPTIONAL, UNION, subqueries or custom functions"}
