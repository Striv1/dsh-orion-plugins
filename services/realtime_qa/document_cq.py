"""Reviewed SELECT/aggregate queries over complete protected document facts."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from rdflib import Graph

from services.ontology_contracts.cq_answers import (
    cq_answer_contract,
    validate_cq_required_bindings,
    validate_cq_select_semantics,
)
from services.ontology_engineering.sparql_execution import ensure_spec_aggregates

from .cq_contract import compile_reviewed_cq, normalize_cq_bindings
from .query_capabilities import (
    _normalize_parameter_spec,
    _normalize_validation_cases,
    render_query_parameters,
)

ensure_spec_aggregates()  # reviewed document aggregates follow SPARQL 1.1


def normalize_document_cq_contract(name: str, raw: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    """Optional additive contract; facts remain independent of query parameters."""
    prefix = f"document_fact_queries.{name}"
    required = ("sparql", "ontology_terms", "parameters", "business_question_ids", "validation_cases")
    missing = [field for field in required if field not in raw]
    if missing:
        raise ValueError(f"{prefix}: missing document CQ fields: {', '.join(missing)}")
    sparql = raw["sparql"]
    terms = raw["ontology_terms"]
    ids = raw["business_question_ids"]
    fields = query["result_fields"]
    if not isinstance(sparql, str) or not sparql.strip():
        raise ValueError(f"{prefix}: sparql must be a complete SELECT")
    if not isinstance(ids, list) or not ids or any(not isinstance(i, str) or not i.strip() for i in ids) or len(ids) != len(set(ids)):
        raise ValueError(f"{prefix}: business_question_ids must be nonempty and unique")
    if not fields or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", f) for f in fields):
        raise ValueError(f"{prefix}: invalid SELECT result_fields")
    predicates = {b["predicate"] for b in query["fact_bindings"]}
    if not isinstance(terms, dict) or set(terms) != predicates or any(
        not isinstance(iri, str) or not re.match(r"^(?:https?://|urn:)[^\s<>]+$", iri) for iri in terms.values()
    ):
        raise ValueError(f"{prefix}: ontology_terms must exactly bind all fact predicates to absolute IRIs")
    for binding in query["fact_bindings"]:
        if binding.get("when") is not None or any(
            set(argument) != {"field"} or type(argument["field"]) is not int or argument["field"] != index
            for index, argument in enumerate(binding["arguments"])
        ):
            raise ValueError(f"{prefix}: document CQ facts require identity field indexes; parameters cannot change facts")
    source_predicates = {fact.get("predicate") or fact["fact"].split("(")[0] for fact in query["facts"]}
    if source_predicates - predicates:
        raise ValueError(f"{prefix}: fact_bindings would discard source facts")
    if not isinstance(raw["parameters"], dict):
        raise ValueError(f"{prefix}: parameters must be an object")
    parameters = {key: _normalize_parameter_spec(name, key, value) for key, value in raw["parameters"].items()}
    cases = _normalize_validation_cases(name, raw["validation_cases"], fields)
    bindings = normalize_cq_bindings(raw["cq_bindings"], question_ids=ids, cases=cases, fields=fields)
    if set(bindings) != set(ids):
        raise ValueError(f"{prefix}: cq_bindings must cover every business_question_id")
    if any(binding["answer_mode"] != "FACT_QUERY" for binding in bindings.values()):
        raise ValueError(f"{prefix}: document SELECT/aggregation CQ requires FACT_QUERY")
    result = {"sparql": sparql, "ontology_terms": dict(terms), "parameters": parameters,
              "business_question_ids": list(ids), "validation_cases": cases, "cq_bindings": bindings}
    for question_id in bindings:
        compiled = compile_reviewed_cq(question_id=question_id, query_name=name, query=sparql,
                                       capability={**query, **result})
        if compiled is None:
            raise ValueError(f"{prefix}: missing compiled CQ")
        selected = Graph().query(compiled["sparql"])
        projected = {str(v) for v in selected.vars or []}
        if not set(compiled["answer_contract"]["required_bindings"]).issubset(projected):
            raise ValueError(f"{prefix}: expected fields missing from SELECT projection")
    return result


def _protected_bytes(binding: Any, relative: str) -> bytes:
    root = Path(binding.package_path).resolve()
    rel = Path(relative)
    target = root / rel
    if rel.is_absolute() or ".." in rel.parts or root not in target.resolve().parents or any(
        parent.is_symlink() for parent in (target, *target.parents) if root in parent.parents
    ):
        raise ValueError("Document CQ artifact must be inside protected release package")
    data = target.read_bytes()
    if binding.artifact_checksums.get(relative) != "sha256:" + hashlib.sha256(data).hexdigest():
        raise ValueError("Document CQ artifact checksum differs from approved release")
    return data


def execute_document_cqs(binding: Any, query_name: str, *, parameters: dict[str, Any] | None = None,
                         validate: bool = False) -> list[dict[str, Any]]:
    """Materialize all protected source facts; SELECT parameters never alter facts."""
    from services.ontology_engineering.formal_facts import (
        materialize_formal_fact,
        ontology_materialization_contract,
        validate_materialization_bindings,
    )

    from .reasoning import document_evidence_facts
    from .reasoning_contract import _normalize_document_facts

    capability = binding.document_fact_queries[query_name]
    if not capability.get("cq_bindings"):
        return []
    ontology = _protected_bytes(binding, binding.ontology_artifact)
    fact_data = _protected_bytes(binding, capability["fact_artifact"])
    if "sha256:" + hashlib.sha256(fact_data).hexdigest() != capability["fact_sha256"]:
        raise ValueError("Document CQ fact checksum differs from bound source")
    actual_facts = _normalize_document_facts(query_name, json.loads(fact_data)["facts"])
    if actual_facts != capability["facts"]:
        raise ValueError("Document CQ facts differ from protected source snapshot")
    graph = Graph().parse(data=ontology, format="turtle")
    kinds, datatypes = ontology_materialization_contract(graph)
    validate_materialization_bindings(capability, kinds)
    symbols: dict[str, str] = {}
    for fact in document_evidence_facts(actual_facts, capability["fact_bindings"], symbols):
        materialize_formal_fact(graph, fact, capability["ontology_terms"], symbols,
                               term_kinds=kinds, term_datatypes=datatypes, strict_types=True)
    executions: dict[str, tuple[list[str], list[dict[str, Any]]]] = {}
    results = []
    for question_id, cq in sorted(capability["cq_bindings"].items()):
        compiled = compile_reviewed_cq(question_id=question_id, query_name=query_name,
                                       query=capability["sparql"], capability=capability)
        if compiled is None:
            raise ValueError("Document CQ binding is missing")
        case = next(case for case in capability["validation_cases"] if case["id"] == cq["validation_case_id"])
        selected_parameters = dict(case["parameters"] if validate else parameters or {})
        query = compiled["sparql"] if validate else render_query_parameters(capability["sparql"], selected_parameters, capability)
        if query not in executions:
            selected = graph.query(query)
            variables = [str(v) for v in selected.vars or []]
            rows = [{field: row.get(field).toPython() for field in variables if row.get(field) is not None} for row in selected]
            executions[query] = variables, rows
        variables, rows = executions[query]
        contract = cq_answer_contract(compiled)
        nulls = validate_cq_required_bindings(
            question_id=question_id, contract={**contract, "min_rows": contract["min_rows"] if validate else 0},
            variables=variables, rows=rows,
        )
        assertions = validate_cq_select_semantics(
            question_id=question_id, sparql=query, contract=contract, rows=rows,
        ) if validate else None
        results.append({"source_question_id": question_id, "capability_name": query_name,
                        "validation_case_id": case["id"] if validate else None,
                        "parameters": selected_parameters, "variables": variables, "rows": rows, "row_count": len(rows),
                        "query_sha256": "sha256:" + hashlib.sha256(query.encode()).hexdigest(),
                        "ontology_sha256": "sha256:" + hashlib.sha256(ontology).hexdigest(),
                        "fact_sha256": capability["fact_sha256"], "release_fingerprint": binding.release_fingerprint,
                        "conditional_nulls": nulls, "answer_scope_zh": cq["answer_scope_zh"], "source_refs": cq["source_refs"],
                        "semantic_validation": assertions, "status": "PASSED" if validate else "EXECUTED"})
    return results
