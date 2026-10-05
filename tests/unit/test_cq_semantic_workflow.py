import copy
import hashlib
import json

import pytest

from services.ontology_engineering import OntologyWorkflowService
from tests.unit.test_orion_workflow import _record_s0


def setup_s2(tmp_path):
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(
        project_name="CQ通用资料工程", domain="supplier", intake_mode="DOCUMENT_ONLY",
        cq_mode="USER_PROVIDED",
        initial_competency_questions=[{"id": "CQ-COUNT", "question": "供应商有多少家？", "expected": "返回供应商总数"}],
        source_scope={"sources": [{"kind": "DOCUMENT", "source_sha256": "sha256:" + "a" * 64}]},
    )
    pid = created["project_id"]
    _record_s0(service, pid)
    qid = json.loads((tmp_path / pid / "00-document-evidence/cq-intake.json").read_text())["questions"][0]["id"]
    payload = {
        "ontology_candidates": [{"id": "CLASS-SUPPLIER", "name": "Supplier", "kind": "CLASS",
                                 "instance_contract": {"business_role": "BUSINESS_OBJECT", "instance_meaning": "一家具备标识的供应商", "generation_mode": "DOCUMENT_FACTS", "identity_rule": "来源供应商标识去重", "mapping_refs": [], "empty_policy": "ALLOW_EMPTY", "empty_reason": "当前制度没有供应商实例记录，等待来源补齐"},
                                 "status": "DOCUMENT_EVIDENCE", "confidence": 0.9, "source_refs": ["EVD-001"]}],
        "business_rule_candidates": [],
        "cq_semantic_assessments": [{
            "question_id": qid, "answer_kind": "AGGREGATION", "business_definition": "统计登记范围内的供应商，按标识去重。",
            "definition_source_refs": ["EVD-001"], "requires_business_confirmation": False,
            "required_candidate_ids": ["CLASS-SUPPLIER"], "required_rule_ids": [],
            "missing_semantics": [], "required_fact_descriptions": ["供应商标识与登记范围"],
            "missing_data": ["制度材料没有供应商实例记录"],
        }],
    }
    return service, pid, payload


def test_s2_token_preserves_assessment_and_readonly_review_does_not_approve(tmp_path):
    service, pid, payload = setup_s2(tmp_path)
    before = service.get_status(pid)["revision"]
    preflight = service.preflight_stage_submission(project_id=pid, stage="S2", payload=payload)
    assert preflight["status"] == "PASSED", preflight.get("issues")
    assert preflight["cq_semantic_review"]["questions"][0]["data_status"] == "MISSING"
    assert service.get_status(pid)["revision"] == before
    committed = service.commit_preflight_stage_submission(project_id=pid, stage="S2", preflight_token=preflight["preflight_token"])
    from harness.orion_workflow_mcp import OrionWorkflowTools

    assert committed["preflight_stage"] == "S2"
    assert committed["current_stage"] == "S3"
    assert OrionWorkflowTools._summary("commit_preflight_stage_submission", committed).startswith("S2 已使用")
    project = tmp_path / pid
    plan = json.loads((project / "02-semantic-recognition/capability-plan.json").read_text())
    assert plan["cq_semantic_assessments"] == payload["cq_semantic_assessments"]
    assert plan["routing_status"] == "READY"
    assert plan["engineering_status"] == "NEEDS_REVIEW"
    snapshot = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in project.rglob("*") if p.is_file()}
    review = service.get_cq_semantic_review(pid)
    assert review["writes_performed"] is False
    assert review["revision"] == committed["revision"]
    assert review["questions"][0]["next_action"] == "WAITING_FOR_SOURCE"
    assert review["questions"][0]["validation_status"] == "NOT_VALIDATED"
    assert snapshot == {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in project.rglob("*") if p.is_file()}


@pytest.mark.parametrize("bad_refs", [["EVD-MADE-UP"], ["Supplier(example-1)"]])
def test_preflight_rejects_unregistered_assessment_sources_without_stage_write(tmp_path, bad_refs):
    service, pid, payload = setup_s2(tmp_path)
    payload["cq_semantic_assessments"][0]["definition_source_refs"] = bad_refs
    before = service.get_status(pid)["revision"]
    result = service.preflight_stage_submission(project_id=pid, stage="S2", payload=payload)
    assert result["status"] == "FAILED"
    assert result["issues"][0]["gate"] == "G-S2-CQ-SEMANTICS"
    assert result["recovery"]["repeat_unchanged_input"] is False
    assert service.get_status(pid)["revision"] == before


def test_historical_s2_without_assessments_is_unassessed_not_migrated(tmp_path):
    from harness.orion_workflow_mcp import OrionWorkflowTools

    service, pid, payload = setup_s2(tmp_path)
    legacy = copy.deepcopy(payload)
    legacy.pop("cq_semantic_assessments")
    service.record_semantic_candidates(project_id=pid, **legacy)
    review = service.get_cq_semantic_review(pid)
    assert review["questions"][0]["business_status"] == "UNASSESSED"
    assert review["questions"][0]["model_status"] == "UNASSESSED"
    assert review["questions"][0]["data_status"] == "MISSING"
    summary = OrionWorkflowTools._summary("get_cq_semantic_review", review)
    assert "CQ 语义检查" in summary and "未修改阶段" in summary
    assert "UNKNOWN" not in summary and "阶段间待推进" not in summary


@pytest.mark.parametrize("status,has_receipt,accepted", [
    ("PASSED", True, True), ("FAILED", True, False), ("PASSED", False, False),
])
def test_assessment_can_reference_executed_s1_sql_evidence(tmp_path, status, has_receipt, accepted):
    service, pid, payload = setup_s2(tmp_path)
    directory = tmp_path / pid / "01-data-understanding"
    directory.mkdir(exist_ok=True)
    row = {"id": "SQL-REGISTERED", "status": status}
    if has_receipt:
        row.update(executed_at="2026-09-06T05:47:17+08:00", result_sha256="sha256:" + "b" * 64)
    (directory / "evidence-sql.json").write_text(json.dumps([row]))
    payload["cq_semantic_assessments"][0]["definition_source_refs"] = ["SQL-REGISTERED"]
    result = service.preflight_stage_submission(project_id=pid, stage="S2", payload=payload)
    assert (result["status"] == "PASSED") is accepted
    if not accepted:
        assert result["issues"][0]["gate"] == "G-S2-CQ-SEMANTICS"


def test_mcp_failed_rule_preflight_returns_authoritative_repair_fields(tmp_path):
    from harness.orion_workflow_mcp import OrionWorkflowTools

    service, pid, payload = setup_s2(tmp_path)
    payload["business_rule_candidates"] = [{"id": "RULE-INCOMPLETE"}]
    before = service.get_status(pid)["revision"]
    result = OrionWorkflowTools(service)._preflight_stage_submission(project_id=pid, stage="S2", payload=payload)
    assert result["status"] == "FAILED"
    rule = result["repair_contract"]["schemas"]["business_rule_candidates_item"]
    assert "rule_type" in rule["required"]
    case = rule["properties"]["test_cases"]["items"]
    assert case["properties"]["case_type"]["enum"] == ["POSITIVE", "NEGATIVE", "BOUNDARY"]
    assert "expected_outcome" in case["required"]
    assert service.get_status(pid)["revision"] == before
