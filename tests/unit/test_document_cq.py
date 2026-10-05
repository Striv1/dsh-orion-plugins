import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.ontology_engineering.workflow import WorkflowGateError
from services.realtime_qa.document_cq import execute_document_cqs
from services.realtime_qa.reasoning_contract import (
    ReasoningCapabilityError,
    normalize_document_fact_queries,
)


def raw_query():
    return {
        "description_zh": "完整来源去重月数",
        "fact_source": "授权资料",
        "question_examples": ["哪些对象至少两个有效月"],
        "fact_bindings": [{"predicate": "activeMonth", "arguments": [{"field": 0}, {"field": 1}]}],
        "facts": [
            {"fact": f"activeMonth({subject},{month})", "provenance": {"evidence_id": "EV-1"}}
            for subject, month in [
                ("urn:test:a", "2025-01"),
                ("urn:test:a", "2025-01"),
                ("urn:test:a", "2025-02"),
                ("urn:test:b", "2025-01"),
            ]
        ],
        "result_fields": ["candidate", "months"],
        "ontology_terms": {"activeMonth": "urn:test:activeMonth"},
        "parameters": {
            "minimum": {"type": "integer", "required": True, "description_zh": "最少月数"}
        },
        "business_question_ids": ["CQ02"],
        "sparql": "SELECT ?candidate (COUNT(DISTINCT ?month) AS ?months) WHERE { ?candidate <urn:test:activeMonth> ?month } GROUP BY ?candidate HAVING(COUNT(DISTINCT ?month) >= {{minimum}}) ORDER BY ?candidate",
        "validation_cases": [
            {
                "id": "two_months",
                "question": "去重后至少两月",
                "parameters": {"minimum": 2},
                "min_rows": 1,
                "expected_first_row": {"candidate": "urn:test:a", "months": 2},
            }
        ],
        "cq_bindings": {
            "CQ02": {
                "validation_case_id": "two_months",
                "answer_mode": "FACT_QUERY",
                "answer_scope_zh": "只查询完整已授权事实，月份去重",
                "source_refs": ["EV-1"],
                "boundary_assertions": [
                    {"binding": "months", "operator": "GTE", "expected": 2, "row": "ALL"}
                ],
            }
        },
    }


def package(tmp_path):
    raw = raw_query()
    cap = normalize_document_fact_queries({"months": raw})["months"]
    ontology = tmp_path / "ontology.ttl"
    ontology.write_text(
        "@prefix owl: <http://www.w3.org/2002/07/owl#> . @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> . <urn:test:activeMonth> a owl:DatatypeProperty; rdfs:range <http://www.w3.org/2001/XMLSchema#string> ."
    )
    fact = tmp_path / "facts.json"
    fact.write_text(json.dumps({"facts": cap["facts"]}))
    hashes = {
        p.name: "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest() for p in [ontology, fact]
    }
    cap.update(fact_artifact=fact.name, fact_sha256=hashes[fact.name])
    binding = SimpleNamespace(
        package_path=str(tmp_path),
        ontology_artifact=ontology.name,
        artifact_checksums=hashes,
        release_fingerprint="sha256:" + "a" * 64,
        document_fact_queries={"months": cap},
    )
    return binding


def test_aggregate_real_complete_facts_and_live_parameters(tmp_path):
    binding = package(tmp_path)
    result = execute_document_cqs(binding, "months", validate=True)[0]
    assert result["rows"] == [{"candidate": "urn:test:a", "months": 2}]
    assert result["status"] == "PASSED"
    live = execute_document_cqs(binding, "months", parameters={"minimum": 1})[0]
    assert len(live["rows"]) == 2 and live["semantic_validation"] is None
    assert execute_document_cqs(binding, "months", parameters={"minimum": 3})[0]["rows"] == []
    with pytest.raises((ValueError, WorkflowGateError)):
        execute_document_cqs(binding, "months")


@pytest.mark.parametrize(
    "change",
    ["mode", "parameter_fact", "missing_terms", "external", "uncovered", "projection", "override"],
)
def test_invalid_document_cq_contract(change):
    raw = raw_query()
    if change == "mode":
        raw["cq_bindings"]["CQ02"]["answer_mode"] = "RULE_INFERENCE"
    elif change == "parameter_fact":
        raw["fact_bindings"][0]["arguments"][0] = {"parameter": "candidate"}
    elif change == "missing_terms":
        raw["ontology_terms"] = {}
    elif change == "external":
        raw["sparql"] = (
            "SELECT ?candidate ?months WHERE { SERVICE <http://external.invalid> { ?candidate ?p ?months } }"
        )
    elif change == "uncovered":
        raw["facts"].append({"fact": "Other(urn:test:a)", "provenance": {"evidence_id": "EV-1"}})
    elif change == "projection":
        raw["sparql"] = "SELECT ?other WHERE { ?other a <urn:test:Other> }"
    else:
        raw["cq_bindings"]["CQ02"]["cq_sparql"] = raw["sparql"]
    with pytest.raises(ReasoningCapabilityError):
        normalize_document_fact_queries({"months": raw})


@pytest.mark.parametrize("change", ["facts", "ontology", "memory", "path", "expected"])
def test_execution_rejects_drift_and_wrong_answers(tmp_path, change):
    binding = package(tmp_path)
    cap = binding.document_fact_queries["months"]
    if change == "facts":
        (tmp_path / "facts.json").write_text('{"facts":[]}')
    elif change == "ontology":
        (tmp_path / "ontology.ttl").write_text("")
    elif change == "memory":
        cap["facts"] = []
    elif change == "path":
        cap["fact_artifact"] = "../outside.json"
    else:
        cap["validation_cases"][0]["expected_first_row"]["months"] = 99
    with pytest.raises((ValueError, WorkflowGateError)):
        execute_document_cqs(binding, "months", validate=True)


def test_packaged_normalization_preserves_query_contract(tmp_path):
    binding = package(tmp_path)
    raw = deepcopy(binding.document_fact_queries["months"])
    raw.pop("facts")
    cap = normalize_document_fact_queries({"months": raw})["months"]
    assert cap["sparql"] == raw["sparql"] and cap["cq_bindings"] == raw["cq_bindings"]


def test_document_cq_s4_binding_and_s6_full_source_materialization(tmp_path):
    from services.ontology_engineering.document_fact_materialization import (
        materialize_reviewed_document_facts,
    )
    from services.ontology_engineering.formal_facts import (
        materialize_formal_fact,
        validate_materialization_bindings,
    )
    from services.ontology_engineering.workflow import OntologyWorkflowService

    bound = package(tmp_path)
    cap = bound.document_fact_queries["months"]
    project = tmp_path / "project"
    root = project / "03-mapping-review/runtime"
    root.mkdir(parents=True)
    (root / "facts.json").write_bytes((tmp_path / "facts.json").read_bytes())
    runtime = {"document_fact_queries": {"months": cap}}
    service = object.__new__(OntologyWorkflowService)
    bindings = service._reviewed_cq_runtime_bindings(project, runtime)
    questions = service._compile_competency_questions(
        intake_questions=[{"id": "CQ02", "question": "哪些对象至少两个有效月", "expected": "去重统计"}],
        cq_mode="USER_PROVIDED", object_properties=[], classes=[], ontology_iri="urn:test:",
        runtime_query_bindings=bindings,
    )
    question = questions[0]
    question["answer_contract"] = service._cq_answer_contract(question)
    assert service._verify_reviewed_cq_binding(project, question, runtime)
    graph, count = materialize_reviewed_document_facts(
        runtime_dir=root, runtime=runtime, term_kinds={"urn:test:activeMonth": "DATA_PROPERTY"},
        term_datatypes={"urn:test:activeMonth": "http://www.w3.org/2001/XMLSchema#string"},
        strict_types=True, validate_bindings=validate_materialization_bindings,
        materialize_fact=materialize_formal_fact,
    )
    assert count == 4  # Includes duplicate source assertion, COUNT DISTINCT remains 2.
    assert [(str(row.candidate), int(row.months)) for row in graph.query(question["sparql"])] == [("urn:test:a", 2)]
    cap["description_zh"] += "changed"
    with pytest.raises(WorkflowGateError):
        service._verify_reviewed_cq_binding(project, question, runtime)
    runtime["reasoning_capabilities"] = {"same": {"business_question_ids": ["CQ02"]}}
    with pytest.raises(WorkflowGateError):
        service._reviewed_cq_runtime_bindings(project, runtime)


def test_standalone_document_runtime_does_not_require_invented_reasoning():
    from services.realtime_qa.runtime_release import normalize_runtime_submission

    runtime = normalize_runtime_submission({
        "document_query_capabilities": ["current_full_text_search"],
        "reasoning_requirement": "NOT_APPLICABLE",
        "reasoning_not_applicable_reason": "仅对已授权资料事实执行真实去重统计，无须规则推理。",
        "document_fact_queries": {"months": raw_query()},
    }, intake_mode="DOCUMENT_ONLY")
    assert runtime["reasoning_capabilities"] == {}
    assert runtime["document_fact_queries"]["months"]["cq_bindings"]["CQ02"]["answer_mode"] == "FACT_QUERY"


def test_document_cq_survives_review_and_release_packaging(tmp_path):
    from services.realtime_qa.runtime_release import (
        finalize_runtime_review,
        normalize_runtime_submission,
        package_realtime_runtime,
        write_runtime_review_assets,
    )

    normalized = normalize_runtime_submission({
        "document_query_capabilities": ["current_full_text_search"],
        "reasoning_requirement": "NOT_APPLICABLE",
        "reasoning_not_applicable_reason": "只执行完整来源事实的去重聚合，不引入规则推理。",
        "document_fact_queries": {"months": raw_query()},
    }, intake_mode="DOCUMENT_ONLY")
    project = tmp_path / "project"
    stage = project / "03-mapping-review"
    write_runtime_review_assets(stage, normalized, prepared_at="2026-09-07T00:00:00Z")
    finalize_runtime_review(stage, reviewed_by="fixture", reviewed_at="2026-09-07T00:00:00Z")
    release = tmp_path / "release"
    packaged = package_realtime_runtime(project, release, project_id="fixture", release_version="0.1.0", ontology_iri="urn:test:")
    cap = packaged["document_fact_queries"]["months"]
    for key in ("sparql", "ontology_terms", "parameters", "business_question_ids", "validation_cases", "cq_bindings"):
        assert cap[key] == normalized["document_fact_queries"]["months"][key]
    facts = json.loads((release / cap["fact_artifact"]).read_text())["facts"]
    assert facts == normalized["document_fact_queries"]["months"]["facts"]
    assert cap["fact_sha256"] == "sha256:" + hashlib.sha256((release / cap["fact_artifact"]).read_bytes()).hexdigest()
