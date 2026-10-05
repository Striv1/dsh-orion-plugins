"""Independent CQ validation after shared S6 graph checks have passed.

Collect answer/semantic failures across SELECT, ASK and CONSTRUCT without
labeling a failed question PASSED. Shared graph and execution failures still
propagate immediately; no business contract is relaxed.
"""
from __future__ import annotations

from typing import Any

from rdflib import BNode, Literal, URIRef
from rdflib.plugins.sparql.results.jsonresults import JSONResult

from services.ontology_contracts.errors import WorkflowGateError

from .cq_failure_collector import CqFailureCollector


def _binding_value(value):
    if isinstance(value, Literal):
        # Invalid/unknown datatypes must reach the exact comparator as RDF
        # terms, which it rejects; never turn them into strings or false.
        return value if value.ill_typed else value.toPython()
    if isinstance(value, URIRef | BNode):
        return str(value)
    return value


def _select_bindings(result):
    # Result.__iter__ drops empty binding dictionaries. They are still rows
    # and must count towards an explicit complete multiset (including []).
    # An unbound variable is absent, not an explicitly supplied null.
    return [{str(var): binding[var] for var in result.vars or [] if var in binding}
            for binding in result.bindings]


def decode_s6_select(payload: dict[str, Any]) -> dict[str, Any]:
    """Decode SPARQL JSON types while retaining the legacy lexical receipt."""
    result = JSONResult(payload)
    if result.type != "SELECT":
        raise ValueError("S6 SELECT requires a SPARQL SELECT result")
    return {
        "variables": [str(var) for var in result.vars or []],
        "rows": [{key: value.get("value") for key, value in row.items()}
                 for row in payload["results"]["bindings"]],
        "typed_rows": [{key: _binding_value(value) for key, value in row.items()}
                       for row in _select_bindings(result)],
    }


def validate_cq_answers(service, *, project_dir, questions, combined_graph,
                        remote_cq_endpoint, submitted_cq_validation_mode, emit, fingerprint):
    collector = CqFailureCollector()
    executions = []
    total = len(questions)
    emit("CQ", "RUNNING", {"passed": 0, "failed": 0, "total": total})
    for question in questions:
        question_id = str(question["id"])
        progress = {"passed": len(executions), "failed": len(collector.failures),
                    "total": total, "current_question_id": question_id}
        emit("CQ", "RUNNING", progress)
        try:
            execution = _validate_one(
                service, project_dir=project_dir, question=question, combined_graph=combined_graph,
                remote_cq_endpoint=remote_cq_endpoint, submitted_cq_validation_mode=submitted_cq_validation_mode,
                fingerprint=fingerprint,
            )
        except WorkflowGateError as exc:
            collector.record(question_id, exc)
        else:
            executions.append(execution)
        progress.update(passed=len(executions), failed=len(collector.failures))
        emit("CQ", "RUNNING", progress)
    if collector.failures:
        emit("CQ", "FAILED", {"passed": len(executions), "failed": len(collector.failures),
                              "total": total, "failures": collector.details})
        collector.raise_if_any()
    emit("CQ", "PASSED", {"passed": len(executions), "failed": 0, "total": total})
    return executions


def _validate_one(service, *, project_dir, question, combined_graph, remote_cq_endpoint,
                  submitted_cq_validation_mode, fingerprint):
    contract = question["answer_contract"]
    question_id = str(question["id"])
    try:
        query_result = None
        remote_result = None
        query_execution = {"mode": "REMOTE_BASE_RELEASE_ONTOP"}
        # A pre-release candidate graph already contains the complete
        # Ontop materialization plus source-backed rule conclusions.
        # Executing a RULE_INFERENCE CQ directly against Ontop would
        # drop those conclusions and produce a false negative.  A base
        # release endpoint remains valid for legacy fact-only replay;
        # candidate CQs are evaluated against the bound combined graph.
        use_remote_endpoint = bool(
            remote_cq_endpoint
            and submitted_cq_validation_mode == "BASE_RELEASE_FULL_SOURCE_ONTOP"
            and str(contract.get("answer_mode") or "") in {"FACT_QUERY", "EVIDENCE_QUERY"}
            and not (contract.get("reviewed_runtime_binding") or {}).get("document_source_sha256")
        )
        if use_remote_endpoint:
            remote_result = service._execute_s6_read_only_sparql(
                remote_cq_endpoint,
                str(question["sparql"]),
                str(contract["query_type"]),
            )
        else:
            from .sparql_execution import execute_local_query

            query_result, query_execution = execute_local_query(
                combined_graph, str(question["sparql"]),
            )
    except Exception as exc:
        raise WorkflowGateError(
            "G-S6-CQ",
            f"能力问题 {question_id} 的 SPARQL 无法执行：{exc}",
        ) from exc
    query_type = str(contract["query_type"])
    execution: dict[str, Any] = {
        "id": question_id,
        "query_type": query_type,
        "question_sha256": contract["question_sha256"],
        "sparql_sha256": contract["sparql_sha256"],
        "expected_sha256": contract["expected_sha256"],
        "answer_contract": contract,
        "query_execution": query_execution,
    }
    if query_type == "SELECT":
        typed_rows = None
        if remote_result is not None:
            variables = list(remote_result["variables"])
            serialized_rows = list(remote_result["rows"])
            if "expected_rows" in contract:
                typed_rows = remote_result.get("typed_rows")
                if not isinstance(typed_rows, list) or len(typed_rows) != len(serialized_rows):
                    raise WorkflowGateError(
                        "G-S6-CQ-ANSWER",
                        f"能力问题 {question_id} 的远程结果缺少保留 RDF 类型的 typed_rows；"
                        "不能从字符串推导 expected_rows 的实际值。",
                    )
        else:
            assert query_result is not None
            variables = [str(value) for value in (query_result.vars or [])]
            rows = (_select_bindings(query_result) if "expected_rows" in contract
                    else list(query_result))
            serialized_rows = [
                {
                    variable: (
                        str(row.get(variable)) if row.get(variable) is not None else None
                    )
                    for variable in variables
                }
                for row in rows
            ]
            if "expected_rows" in contract:
                typed_rows = [
                    {variable: _binding_value(value) for variable, value in row.items()}
                    for row in rows
                ]
        if contract.get("nullable_bindings") or (
            contract.get("reviewed_runtime_binding") or {}
        ).get("reasoning_source_sha256") or (
            contract.get("reviewed_runtime_binding") or {}
        ).get("document_source_sha256"):
            current_runtime = service._read_json(project_dir / "03-mapping-review/runtime/runtime-source.json")
            if not service._verify_reviewed_cq_binding(project_dir, question, current_runtime):
                raise WorkflowGateError("G-S6-CQ-ANSWER", "CQ 缺少 S3 已审同源绑定。")
        conditional_nulls = service._validate_cq_required_bindings(
            question_id=question_id, contract=contract,
            variables=variables, rows=serialized_rows,
        )
        execution.update(
            {
                "status": "PASSED",
                "row_count": len(serialized_rows),
                "returned_bindings": variables,
                "conditional_null_counts": conditional_nulls,
                "semantic_validation": service._validate_cq_select_semantics(
                    question_id=question_id,
                    sparql=str(question["sparql"]),
                    contract=contract,
                    rows=serialized_rows,
                    **({"typed_rows": typed_rows} if typed_rows is not None else {}),
                ),
                "business_dimension_validation": (
                    service._validate_s6_cq_business_dimensions(
                        project_dir=project_dir,
                        question=question,
                        rows=serialized_rows,
                    )
                ),
                "result_sha256": fingerprint(serialized_rows),
            }
        )
    elif query_type == "ASK":
        actual_boolean = (
            bool(remote_result["boolean"])
            if remote_result is not None
            else bool(getattr(query_result, "askAnswer", False))
        )
        expected_boolean = bool(contract["expected_boolean"])
        if actual_boolean is not expected_boolean:
            raise WorkflowGateError(
                "G-S6-CQ-ANSWER",
                f"能力问题 {question_id} 的 ASK 结果为 {actual_boolean}，契约要求 {expected_boolean}。",
            )
        execution.update(
            {
                "status": "PASSED",
                "actual_boolean": actual_boolean,
                "semantic_validation": service._validate_cq_select_semantics(
                    question_id=question_id,
                    sparql=str(question["sparql"]),
                    contract=contract,
                    rows=[],
                ),
                "result_sha256": fingerprint(actual_boolean),
            }
        )
    else:
        result_graph = (
            remote_result["graph"]
            if remote_result is not None
            else getattr(query_result, "graph", None)
        )
        triple_count = len(result_graph) if result_graph is not None else 0
        minimum_triples = int(contract["min_triples"])
        if triple_count < minimum_triples:
            raise WorkflowGateError(
                "G-S6-CQ-ANSWER",
                f"能力问题 {question_id} 只返回 {triple_count} 条三元组，契约要求至少 {minimum_triples} 条。",
            )
        execution.update(
            {
                "status": "PASSED",
                "triple_count": triple_count,
                "semantic_validation": service._validate_cq_select_semantics(
                    question_id=question_id,
                    sparql=str(question["sparql"]),
                    contract=contract,
                    rows=[],
                ),
                "result_sha256": fingerprint(
                    sorted(
                        tuple(str(term) for term in triple)
                        for triple in (result_graph or [])
                    )
                ),
            }
        )
    return execution
