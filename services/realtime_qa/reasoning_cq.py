"""Execute reviewed rule CQ projections on the exact release-bound facts.

S7 validation and QA use the same RDF term/identity materializer as S6. This
does not run the reasoner or substitute expected answers for actual query rows.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from rdflib import Graph

from services.ontology_contracts.cq_answers import (
    cq_answer_contract,
    validate_cq_required_bindings,
    validate_cq_select_semantics,
)
from services.realtime_qa.cq_contract import compile_reviewed_cq
from services.realtime_qa.query_capabilities import render_query_parameters


def execute_reasoning_cqs(
    binding: Any,
    capability_name: str,
    input_facts: list[str],
    inferred_facts: list[str],
    symbol_table: dict[str, str],
    *,
    parameters: dict[str, Any] | None = None,
    validate: bool = False,
) -> list[dict[str, Any]]:
    # Load RDF materialization only when executing a release-bound query.
    from services.ontology_engineering.formal_facts import (
        FormalFactContractError,
        materialize_formal_fact,
        ontology_materialization_contract,
        validate_materialization_bindings,
    )

    capability = binding.reasoning_capabilities[capability_name]
    cq_bindings = capability.get("cq_bindings") or {}
    if not cq_bindings:
        return []
    root = Path(binding.package_path).resolve()
    relative = Path(binding.ontology_artifact)
    path = root / relative
    if relative.is_absolute() or ".." in relative.parts or any(
        parent.is_symlink() for parent in (path, *path.parents)
        if parent != root and root in parent.parents
    ) or root not in path.resolve().parents:
        raise ValueError("Rule CQ ontology must be inside the protected release package")
    data = path.read_bytes()
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if binding.artifact_checksums.get(binding.ontology_artifact) != digest:
        raise ValueError("Rule CQ ontology checksum differs from the approved release")
    graph = Graph().parse(data=data, format="turtle")
    kinds, datatypes = ontology_materialization_contract(graph)
    validate_materialization_bindings(capability, kinds)
    query_artifact = (getattr(binding, "ontop_query_artifacts", {}) or {}).get(capability.get("evidence_query"))
    if query_artifact:
        from services.realtime_qa.document_cq import _protected_bytes
        from services.realtime_qa.fact_binding_types import validate_fact_binding_types

        query = render_query_parameters(
            _protected_bytes(binding, query_artifact).decode("utf-8"),
            dict((capability.get("runtime_validation") or {}).get("parameters") or {})
            if validate else dict(parameters or {}),
            binding.ontop_query_capabilities.get(capability["evidence_query"]),
        )
        validate_fact_binding_types({"reasoning_capabilities": {capability_name: capability},
                                     "ontop_queries": {capability["evidence_query"]: query}}, kinds)
    for fact in dict.fromkeys([*input_facts, *inferred_facts]):
        try:
            materialize_formal_fact(
                graph, fact, capability["ontology_terms"], symbol_table,
                term_kinds=kinds, term_datatypes=datatypes, strict_types=True,
            )
        except FormalFactContractError as exc:
            exc.capability_name = capability_name
            raise

    results: list[dict[str, Any]] = []
    executions: dict[str, tuple[list[str], list[dict[str, Any]], dict[str, Any]]] = {}
    for question_id, cq_binding in sorted(cq_bindings.items()):
        compiled = compile_reviewed_cq(
            question_id=question_id, query_name=capability_name,
            query=cq_binding["cq_sparql"], capability=capability,
        )
        if compiled is None:
            raise ValueError(f"Reviewed rule CQ binding is missing: {question_id}")
        case = next(case for case in capability["validation_cases"]
                    if case["id"] == cq_binding["validation_case_id"])
        # Live QA accepts caller parameters, never silently substitutes the
        # validation case's example input for the user's question.
        query_parameters = dict(case["parameters"] if validate else parameters or {})
        query = compiled["sparql"] if validate else render_query_parameters(
            cq_binding["cq_sparql"], query_parameters, capability,
        )
        if query not in executions:
            from services.ontology_engineering.sparql_execution import execute_local_query

            selected, query_execution_receipt = execute_local_query(graph, query)
            variables = [str(value) for value in selected.vars or []]
            rows = [{field: row.get(field).toPython() for field in variables
                     if row.get(field) is not None} for row in selected]
            executions[query] = variables, rows, query_execution_receipt
        variables, rows, query_execution_receipt = executions[query]
        contract = cq_answer_contract(compiled)
        # Live queries may legitimately be empty or differ from the frozen test
        # example; only S7 replays that example's counts and value assertions.
        checked_contract = {**contract, "min_rows": contract["min_rows"] if validate else 0}
        nulls = validate_cq_required_bindings(
            question_id=question_id, contract=checked_contract,
            variables=variables, rows=rows,
        )
        assertions = validate_cq_select_semantics(
            question_id=question_id, sparql=query, contract=contract, rows=rows,
        ) if validate else None
        results.append({
            "source_question_id": question_id,
            "capability_name": capability_name,
            "validation_case_id": case["id"] if validate else None,
            "parameters": query_parameters,
            "query_sha256": "sha256:" + hashlib.sha256(query.encode()).hexdigest(),
            "query_execution": query_execution_receipt,
            "ontology_sha256": digest,
            "release_fingerprint": binding.release_fingerprint,
            "variables": variables, "row_count": len(rows), "rows": rows,
            "conditional_nulls": nulls,
            "answer_scope_zh": cq_binding["answer_scope_zh"],
            "source_refs": list(cq_binding["source_refs"]),
            "semantic_validation": assertions,
            "status": "PASSED" if validate else "EXECUTED",
        })
    return results
