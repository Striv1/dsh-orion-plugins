from __future__ import annotations

import json

import httpx
import pytest

from services.realtime_qa.capabilities import platform_capability_catalog
from services.realtime_qa.reasoning import (
    SemanticaReasoningClient,
    document_evidence_facts,
    facts_from_rows,
    semantic_facts,
)
from services.realtime_qa.row_fact_conditions import (
    normalize_row_condition,
    validate_fact_source_fields,
)


def test_platform_catalog_exposes_fact_rule_owl_and_validation_engines() -> None:
    catalog = platform_capability_catalog()

    assert "ONTOP_FACT_QUERY_V1" in catalog["query_engines"]
    assert "FUSEKI_DOCUMENT_EVIDENCE_V1" in catalog["query_engines"]
    assert set(catalog["reasoning"]) >= {
        "SEMANTICA_FORWARD_V1",
        "CLOSED_WORLD_SET_DIFFERENCE_V1",
        "OWL_RL_CLOSURE_V1",
        "HERMIT_OWL_DL_VALIDATION_V1",
        "SHACL_VALIDATION_V1",
    }


def test_semantica_client_forces_read_only_forward_reasoning() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "inferred_facts": ["NeedsReview(batch-1)"],
                "rules_fired": 1,
                "added_edges": 0,
                "updated_properties": 0,
                "mutated": False,
                "trace": [],
                "warnings": [],
            },
        )

    client = SemanticaReasoningClient(
        "http://semantica.test",
        transport=httpx.MockTransport(handler),
    )
    result = client.run_forward(
        facts=["AbnormalBatch(batch-1)"],
        rules=["IF AbnormalBatch(?batch) THEN NeedsReview(?batch)"],
    )

    assert result["inferred_facts"] == ["NeedsReview(batch-1)"]
    assert captured == {
        "path": "/api/reason",
        "payload": {
            "facts": ["AbnormalBatch(batch-1)"],
            "rules": ["IF AbnormalBatch(?batch) THEN NeedsReview(?batch)"],
            "mode": "forward",
            "apply_to_graph": False,
        },
    }


def test_semantica_client_rejects_an_unexpected_graph_mutation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "inferred_facts": ["NeedsReview(batch-1)"],
                "rules_fired": 1,
                "added_edges": 1,
                "updated_properties": 0,
                "mutated": True,
                "trace": [],
                "warnings": [],
            },
        )

    client = SemanticaReasoningClient(
        "http://semantica.test",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(RuntimeError, match="unexpectedly mutated"):
        client.run_forward(
            facts=["AbnormalBatch(batch-1)"],
            rules=["IF AbnormalBatch(?batch) THEN NeedsReview(?batch)"],
        )


def test_fact_bindings_only_emit_source_backed_normalized_facts() -> None:
    facts = facts_from_rows(
        [
            {"batch": "BATCH 1", "exceeds": True},
            {"batch": "BATCH 2", "exceeds": False},
        ],
        {"threshold": 0.2},
        [
            {
                "predicate": "ThresholdExceeded",
                "arguments": [
                    {"field": "batch"},
                    {"parameter": "threshold"},
                ],
                "when": {"field": "exceeds", "equals": True},
            }
        ],
    )

    assert facts == ["ThresholdExceeded(BATCH_1, 0.2)"]


def test_fact_binding_boolean_condition_accepts_sparql_json_boolean_text() -> None:
    facts = facts_from_rows(
        [{"equipment": "urn:equipment:1", "isConcentrated": "true"}],
        {},
        [
            {
                "predicate": "EquipmentThresholdExceeded",
                "arguments": [{"field": "equipment"}],
                "when": {"field": "isConcentrated", "equals": True},
            }
        ],
    )

    assert facts == ["EquipmentThresholdExceeded(urn:equipment:1)"]


def test_optional_workorder_rows_only_emit_positive_p1_fact_when_all_fields_match() -> None:
    condition = normalize_row_condition({"all": [
        {"field": "woType", "equals": "PREDICTIVE"},
        {"field": "woStatus", "in": ["OPEN", "IN_PROGRESS"]},
        {"field": "priority", "equals": "P1"},
    ]})
    rows = [
        {"equipmentCode": "eq-1", "woType": None, "woStatus": None, "priority": None},
        {"equipmentCode": "eq-2", "woType": "CORRECTIVE", "woStatus": "OPEN", "priority": "P1"},
        {"equipmentCode": "eq-3", "woType": "PREDICTIVE", "woStatus": "CLOSED", "priority": "P1"},
        {"equipmentCode": "eq-4", "woType": "PREDICTIVE", "woStatus": "IN_PROGRESS", "priority": "P1"},
    ]
    facts = facts_from_rows(rows, {}, [
        {"predicate": "ClassAEquipment", "arguments": [{"field": "equipmentCode"}]},
        {"predicate": "P1PredictiveWorkOrder", "arguments": [{"field": "equipmentCode"}], "when": condition},
    ])
    assert facts == [
        "ClassAEquipment(eq-1)", "ClassAEquipment(eq-2)",
        "ClassAEquipment(eq-3)", "ClassAEquipment(eq-4)",
        "P1PredictiveWorkOrder(eq-4)",
    ]


def test_row_fact_condition_rejects_unbounded_or_unsupported_predicates() -> None:
    with pytest.raises(ValueError, match="1 到 8"):
        normalize_row_condition({"all": []})
    with pytest.raises(ValueError, match="仅支持"):
        normalize_row_condition({"all": [{"sql": "TRUE"}]})


def test_reasoning_binding_rejects_columns_missing_from_query_result_contract() -> None:
    capability = {
        "rule_1": {
            "evidence_query": "equipment_readings",
            "fact_bindings": [{
                "predicate": "P1PredictiveWorkOrder",
                "arguments": [{"field": "equipmentCode"}],
                "when": {"all": [
                    {"field": "woType", "equals": "PREDICTIVE"},
                    {"field": "priority", "equals": "P1"},
                ]},
            }],
        },
    }
    query = {"equipment_readings": {"result_fields": ["equipmentCode", "woType"]}}
    with pytest.raises(ValueError, match="priority"):
        validate_fact_source_fields(capability, query)
    query["equipment_readings"]["result_fields"].append("priority")
    validate_fact_source_fields(capability, query)


def test_semantic_facts_bind_every_result_to_an_ontology_iri() -> None:
    assert semantic_facts(
        ["NeedsAnalysis(urn:equipment:1)"],
        {"NeedsAnalysis": "https://example.com/NeedsAnalysis"},
    ) == [
        {
            "fact": "NeedsAnalysis(urn:equipment:1)",
            "predicate": "NeedsAnalysis",
            "predicate_iri": "https://example.com/NeedsAnalysis",
            "arguments": ["urn:equipment:1"],
            "engine_arguments": ["urn:equipment:1"],
        }
    ]


def test_semantic_facts_restore_original_source_value_from_engine_symbol() -> None:
    symbols: dict[str, str] = {}
    facts = facts_from_rows(
        [{"equipment": "https://example.com/equipment/CM4-01"}],
        {},
        [{"predicate": "Equipment", "arguments": [{"field": "equipment"}]}],
        symbols,
    )

    assert facts == ["Equipment(https:_example.com_equipment_CM4-01)"]
    assert symbols == {
        "https:_example.com_equipment_CM4-01": "https://example.com/equipment/CM4-01"
    }
    assert semantic_facts(
        ["ConcentratedEquipment(https:_example.com_equipment_CM4-01)"],
        {"ConcentratedEquipment": "https://example.com/ConcentratedEquipment"},
        symbols,
    )[0]["arguments"] == ["https://example.com/equipment/CM4-01"]


def test_document_evidence_facts_bind_provenance_facts_with_symbol_table() -> None:
    materialized = [
        {
            "fact": "ObligedSubject(机关团体, 档案管理)",
            "predicate": "ObligedSubject",
            "fact_kind": "document_evidence_fact",
            "provenance": {
                "evidence_id": "EVD-0001",
                "document_id": "DOC-0001",
                "source_locator": "Word 段落 24",
                "source_sha256": "sha256:ab" * 32,
                "document_version": "2020-06-20",
            },
        }
    ]
    symbols: dict[str, str] = {}
    facts = document_evidence_facts(
        materialized,
        [
            {
                "predicate": "ObligedSubject",
                "arguments": [{"field": 0}, {"field": 1}],
            }
        ],
        symbols,
    )

    assert facts == ["ObligedSubject(机关团体, 档案管理)"]
    assert symbols["机关团体"] == "机关团体"
    assert symbols["档案管理"] == "档案管理"


def test_fact_symbol_collision_fails_closed() -> None:
    symbols: dict[str, str] = {}
    bindings = [{"predicate": "Entity", "arguments": [{"field": "value"}]}]

    facts_from_rows([{"value": "A/B"}], {}, bindings, symbols)

    with pytest.raises(ValueError, match="same Semantica symbol"):
        facts_from_rows([{"value": "A B"}], {}, bindings, symbols)


def test_closed_world_set_difference_is_rewritten_for_semantica_with_trace() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["payload"] = payload
        witness = next(
            fact for fact in payload["facts"] if fact.startswith("NotSubmittedMaterial(")
        )
        rewritten = payload["rules"][0]
        return httpx.Response(
            200,
            json={
                "inferred_facts": ["MissingMaterial(case-1, material-a)"],
                "rules_fired": 1,
                "added_edges": 0,
                "updated_properties": 0,
                "mutated": False,
                "trace": [
                    {
                        "rule_id": "rule_1",
                        "rule_text": rewritten,
                        "premises": [
                            "RequiredMaterial(case-1, material-a)",
                            witness,
                        ],
                        "conclusion": "MissingMaterial(case-1, material-a)",
                        "confidence": 1.0,
                    }
                ],
                "warnings": [],
            },
        )

    client = SemanticaReasoningClient(
        "http://semantica.test",
        transport=httpx.MockTransport(handler),
    )
    original_rule = (
        "IF RequiredMaterial(?case,?material) "
        "AND NOT SubmittedMaterial(?case,?material) "
        "THEN MissingMaterial(?case,?material)"
    )
    result = client.run_forward(
        facts=["RequiredMaterial(case-1, material-a)"],
        rules=[original_rule],
        closed_world_predicates=["SubmittedMaterial"],
    )

    assert result["inferred_facts"] == ["MissingMaterial(case-1, material-a)"]
    assert result["closed_world"] == {
        "operator": "ORION_SAFE_ANTI_JOIN_V1",
        "predicates": ["SubmittedMaterial"],
        "witness_count": 1,
        "materialized_facts": ["NotSubmittedMaterial(case-1, material-a)"],
    }
    assert result["trace"][0]["rule_text"] == original_rule
    assert result["trace"][0]["premises"] == [
        "RequiredMaterial(case-1, material-a)",
        "NotSubmittedMaterial(case-1, material-a)",
    ]
    assert result["trace"][0]["closed_world_negations"] == [
        "NOT SubmittedMaterial(case-1, material-a)"
    ]
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert "NOT SubmittedMaterial" not in payload["rules"][0]


def test_closed_world_set_difference_does_not_fire_for_submitted_material() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert not any(fact.startswith("NotSubmittedMaterial(") for fact in payload["facts"])
        return httpx.Response(
            200,
            json={
                "inferred_facts": [],
                "rules_fired": 0,
                "added_edges": 0,
                "updated_properties": 0,
                "mutated": False,
                "trace": [],
                "warnings": [],
            },
        )

    client = SemanticaReasoningClient(
        "http://semantica.test",
        transport=httpx.MockTransport(handler),
    )
    result = client.run_forward(
        facts=[
            "RequiredMaterial(case-1, material-a)",
            "SubmittedMaterial(case-1, material-a)",
        ],
        rules=[
            "IF RequiredMaterial(?case,?material) "
            "AND NOT SubmittedMaterial(?case,?material) "
            "THEN MissingMaterial(?case,?material)"
        ],
        closed_world_predicates=["SubmittedMaterial"],
    )

    assert result["inferred_facts"] == []
    assert result["closed_world"]["witness_count"] == 0


def test_closed_world_negation_requires_explicit_authorization() -> None:
    client = SemanticaReasoningClient("http://semantica.test")

    with pytest.raises(ValueError, match="explicit closed_world_predicates"):
        client.run_forward(
            facts=["RequiredMaterial(case-1, material-a)"],
            rules=[
                "IF RequiredMaterial(?case,?material) "
                "AND NOT SubmittedMaterial(?case,?material) "
                "THEN MissingMaterial(?case,?material)"
            ],
        )


def test_closed_world_negation_rejects_variables_not_bound_by_positive_guard() -> None:
    client = SemanticaReasoningClient("http://semantica.test")

    with pytest.raises(ValueError, match="not positively bound"):
        client.run_forward(
            facts=["KnownCase(case-1)"],
            rules=[
                "IF KnownCase(?case) AND NOT SubmittedMaterial(?case,?material) "
                "THEN MissingMaterial(?case,?material)"
            ],
            closed_world_predicates=["SubmittedMaterial"],
        )


def test_multiple_closed_world_negations_materialize_only_absent_witnesses() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["payload"] = payload
        return httpx.Response(
            200,
            json={
                "inferred_facts": [],
                "rules_fired": 0,
                "added_edges": 0,
                "updated_properties": 0,
                "mutated": False,
                "trace": [],
                "warnings": [],
            },
        )

    client = SemanticaReasoningClient(
        "http://semantica.test",
        transport=httpx.MockTransport(handler),
    )
    result = client.run_forward(
        facts=[
            "RequiredMaterial(case-1, material-a)",
            "SubmittedMaterial(case-1, material-a)",
        ],
        rules=[
            "IF RequiredMaterial(?case,?material) "
            "AND NOT SubmittedMaterial(?case,?material) "
            "AND NOT ExemptMaterial(?case,?material) "
            "THEN MissingMaterial(?case,?material)"
        ],
        closed_world_predicates=["SubmittedMaterial", "ExemptMaterial"],
    )

    assert result["closed_world"]["materialized_facts"] == ["NotExemptMaterial(case-1, material-a)"]


def test_row_fact_condition_not_in_and_missing_express_invalid_code_sets() -> None:
    condition = normalize_row_condition({"any": [
        {"field": "disposition", "missing": True},
        {"field": "disposition", "not_in": ["RELEASE", "SCRAP", "RETURN"]},
    ]})
    rows = [
        {"event": "e1", "disposition": "SCRAP"},
        {"event": "e2"},
        {"event": "e3", "disposition": "  "},
        {"event": "e4", "disposition": "PENDING"},
    ]
    facts = facts_from_rows(rows, {}, [
        {"predicate": "Event", "arguments": [{"field": "event"}]},
        {"predicate": "DispositionInvalid", "arguments": [{"field": "event"}], "when": condition},
    ])
    assert [fact for fact in facts if fact.startswith("DispositionInvalid")] == [
        "DispositionInvalid(e2)", "DispositionInvalid(e3)", "DispositionInvalid(e4)",
    ]
    with pytest.raises(ValueError, match="missing"):
        normalize_row_condition({"field": "x", "missing": "yes"})
    with pytest.raises(ValueError, match="not_in"):
        normalize_row_condition({"field": "x", "not_in": []})
