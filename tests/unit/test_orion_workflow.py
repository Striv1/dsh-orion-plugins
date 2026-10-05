from __future__ import annotations

import hashlib
import inspect
import json
import multiprocessing
import os
import re
import shlex
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Any

import pytest
import yaml
from rdflib import Graph

from harness.orion_workflow_mcp import TOOLS, McpServer, OrionWorkflowTools
from services.ontology_engineering import OntologyWorkflowService, WorkflowError, WorkflowGateError
from services.ontology_engineering.dependency_graph import component_revalidation_plan
from services.ontology_engineering.storage import _normalize_event_for_storage
from services.ontology_engineering.workflow import (
    _required_business_dimensions,
    _requires_reasoning,
    _validate_reasoning_term_declarations,
)
from services.realtime_qa.binding import ReleaseBindingLoader


@pytest.mark.parametrize("configured_python", [False, True])
def test_release_review_guide_runs_from_any_directory_and_preserves_the_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, configured_python: bool,
) -> None:
    profile = tmp_path / "Profile with spaces and 'quote"
    workflow_root = profile / "state/workflows"
    package = workflow_root / "source-project/07-release/ontology-engineering-package-0.1.1"
    ontology = package / "01-本体模型/ontology.owl"
    ontology.parent.mkdir(parents=True)
    ontology.write_text("<rdf:RDF xmlns:rdf=\"http://www.w3.org/1999/02/22-rdf-syntax-ns#\"/>\n")
    ontology_sha256 = "sha256:" + hashlib.sha256(ontology.read_bytes()).hexdigest()
    contract = {
        "schema_version": 1,
        "project_id": "source-project",
        "release_version": "0.1.1",
        "ontology_sha256": ontology_sha256,
        "expected_consistent": True,
        "expected_unsatisfiable_class_count": 0,
        "source_validation_run_id": "verified-s6-run",
        "review_mode": "COPY_BEFORE_DESKTOP_REVIEW",
    }
    contract_path = package / "04-发布信息/owl-dl-review-contract.json"
    contract_path.parent.mkdir(parents=True)
    contract_path.write_text(json.dumps(contract), encoding="utf-8")
    declared_files = [
        {
            "path": path.relative_to(package).as_posix(),
            "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in (ontology, contract_path)
    ]
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "project_id": "source-project",
                "release_version": "0.1.1",
                "approval_decision": "APPROVED",
                "file_count": len(declared_files),
                "files": declared_files,
            }
        ),
        encoding="utf-8",
    )
    if configured_python:
        python = profile / ".venvs/core/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to(sys.executable)
        monkeypatch.setenv("ORION_WORKFLOW_PYTHON", str(python))
    else:
        python = Path(sys.executable).absolute()
        monkeypatch.delenv("ORION_WORKFLOW_PYTHON", raising=False)
    publication = {"project_id": "source-project", "release_version": "0.1.1"}
    guide = OntologyWorkflowService._render_protege_hermit_review_guide(
        state={"project_name": "测试工程"},
        publication=publication,
        contract=contract,
        workflow_root=workflow_root,
    )
    command = re.search(r"```bash\n(.*?)\n```", guide, flags=re.S)
    assert command is not None
    argv = shlex.split(command.group(1))
    assert argv == [
        str(python),
        str(Path(__file__).resolve().parents[2] / "scripts/open_release_in_protege.py"),
        "--project-id", "source-project",
        "--release-version", "0.1.1",
        "--workflow-root", str(workflow_root),
        "--review-root", str(workflow_root.parent / ".orion-runtime/protege-release-review"),
    ]
    immutable_files = {path: path.read_bytes() for path in package.rglob("*") if path.is_file()}

    completed = subprocess.run(
        ["/bin/sh", "-c", command.group(1) + " --no-open"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )

    receipt = json.loads(completed.stdout)
    review_ontology = Path(receipt["review_ontology"])
    assert review_ontology == (
        workflow_root.parent
        / ".orion-runtime/protege-release-review/source-project/0.1.1/ontology.owl"
    )
    assert review_ontology.read_bytes() == ontology.read_bytes()
    assert receipt["source_ontology_sha256"] == ontology_sha256
    assert receipt["review_ontology_sha256"] == ontology_sha256
    assert receipt["release_artifact_modified"] is False
    assert receipt["opened"] is False
    assert {path: path.read_bytes() for path in immutable_files} == immutable_files


def test_query_filter_condition_is_not_business_trigger_dimension() -> None:
    dimensions = _required_business_dimensions(
        "在给定查询条件下，全量样本总数是多少？",
        "返回全量样本及总样本量。",
    )

    assert dimensions == []


def test_explicit_trigger_condition_remains_business_dimension() -> None:
    dimensions = _required_business_dimensions(
        "什么条件触发供应商复核？",
        "返回触发条件与对应义务。",
    )

    assert {item["dimension"] for item in dimensions} == {"trigger"}


@pytest.fixture(autouse=True)
def historical_workflow_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the existing v1 regression scenarios explicit; v2 has its own suite."""
    create = OntologyWorkflowService.create_project

    def create_historical(self, **kwargs):
        kwargs.setdefault("_stage_contract_version", "s0-s7-stage-contract-v1")
        return create(self, **kwargs)

    monkeypatch.setattr(OntologyWorkflowService, "create_project", create_historical)


def _record_s0(service: OntologyWorkflowService, project_id: str) -> dict:
    return service.record_document_evidence(
        project_id=project_id,
        documents=[
            {
                "document_id": "DOC-001",
                "source_name": "供应商管理制度.pdf",
                "source_type": "PDF",
                "source_sha256": "sha256:" + "a" * 64,
                "source_path": ".orion-s0-uploads/test/供应商管理制度.pdf",
                "original_storage_backend": "minio",
                "original_source_uri": "minio://orion-workflow-artifacts/test/供应商管理制度.pdf",
                "original_snapshot_sha256": "sha256:" + "a" * 64,
                "page_count": 2,
                "processing_method": "PP_STRUCTURE_V3",
                "structured_markdown_filename": "供应商管理制度.md",
                "structured_markdown": "# 供应商管理制度\n\n## 准入条件\n\n供应商应满足正式准入条件。\n",
            }
        ],
        quality_report={
            "status": "PASSED",
            "processed_pages": 2,
            "failed_pages": 0,
            "low_confidence_pages": 1,
            "reviewed_low_confidence_pages": 1,
            "unreviewed_low_confidence_pages": 0,
        },
        evidence_index=[
            {
                "evidence_id": "EVD-001",
                "document_id": "DOC-001",
                "source_page": 1,
                "markdown_section": "准入条件",
                "content_sha256": "sha256:" + "b" * 64,
            }
        ],
        processing_trace={
            "run_id": "paddleocr-run-001",
            "provider": "PADDLEOCR_MCP",
            "tool_calls": ["mcp__paddleocr__pp_structurev3"],
            "actor": "test-engineer",
        },
    )


@pytest.mark.parametrize("intake_mode", ["DOCUMENT_ONLY", "DATABASE_ONLY", "HYBRID"])
def test_create_project_request_id_is_idempotent_for_all_intake_modes(
    tmp_path: Path,
    intake_mode: str,
) -> None:
    service = OntologyWorkflowService(tmp_path / "workflows")
    request_id = f"acceptance:create:{intake_mode.lower()}"
    arguments = {
        "project_name": f"{intake_mode} 幂等验收工程",
        "domain": f"idempotency-{intake_mode.lower()}",
        "intake_mode": intake_mode,
        "intake_rationale": "验证重复双击和网络重试不会产生第二个工程。",
        "request_id": request_id,
    }

    first = service.create_project(**arguments)
    replay = OntologyWorkflowService(tmp_path / "workflows").create_project(**arguments)

    assert replay["project_id"] == first["project_id"]
    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert replay["request_id"] == request_id
    assert service.list_projects()["count"] == 1
    project_dirs = [path for path in (tmp_path / "workflows").iterdir() if path.is_dir()]
    assert [path.name for path in project_dirs] == [first["project_id"]]


def test_s4_rejects_reasoning_terms_absent_from_ontology_entities() -> None:
    capability = {
        "fact_bindings": [{"predicate": "ObservedRisk", "arguments": []}],
        "result_predicates": ["NeedsReview"],
        "ontology_terms": {
            "ObservedRisk": "https://example.com/ontology/ObservedRisk",
            "NeedsReview": "https://example.com/ontology/NeedsReview",
        },
    }

    with pytest.raises(WorkflowGateError) as error:
        _validate_reasoning_term_declarations(
            question_id="CQ-001",
            capability=capability,
            allowed_predicates={"NeedsReview"},
            entity_iris={"https://example.com/ontology/ObservedRisk"},
        )

    assert error.value.gate_id == "G-S4-REASONING-TERMS"
    assert "NeedsReview" in str(error.value)


def test_reasoning_detection_keeps_risk_distribution_as_fact_aggregation() -> None:
    assert not _requires_reasoning(
        "当前各风险等级与状态的告警数量分布如何？",
        "按风险等级/状态分组的告警计数",
    )
    assert _requires_reasoning(
        "哪些交易被判定为高风险并生成风险告警？",
        "返回高风险交易及其告警",
    )


def test_component_dependency_plan_preserves_unrelated_build_stage() -> None:
    plan = component_revalidation_plan(
        ["RUNTIME_RULES"],
        target_stage="S3",
    )

    assert plan["required_revalidation_stages"] == ["S3", "S4", "S6", "S7"]
    assert "S5" in plan["reusable_stages"]
    assert _requires_reasoning(
        "同一设备关联多个账户的疑似风险主体有哪些？",
        "返回设备聚集风险主体",
    )


def test_runtime_mapping_change_only_revalidates_runtime_dependent_stages() -> None:
    plan = component_revalidation_plan(["RUNTIME_MAPPING"], target_stage="S3")

    assert plan["required_revalidation_stages"] == ["S3", "S6", "S7"]
    assert {"S4", "S5"}.issubset(plan["reusable_stages"])


def test_create_project_rejects_request_id_reuse_with_different_payload(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path / "workflows")
    request_id = "acceptance:create:conflict"
    first = service.create_project(
        project_name="幂等冲突验收工程",
        domain="idempotency-conflict",
        intake_mode="HYBRID",
        intake_rationale="第一次请求。",
        request_id=request_id,
    )

    with pytest.raises(WorkflowError, match="request_id 已用于另一组新建参数"):
        service.create_project(
            project_name="幂等冲突验收工程（错误重用）",
            domain="idempotency-conflict",
            intake_mode="HYBRID",
            intake_rationale="第二次请求改变了参数。",
            request_id=request_id,
        )

    assert service.list_projects()["count"] == 1
    assert service.list_projects()["projects"][0]["project_id"] == first["project_id"]


def test_create_project_from_orion_template_snapshots_immutable_baseline(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path / "workflows")
    created = service.create_project(
        project_name="制造业来料质量客户本体工程",
        domain="制造业供应链与来料质量",
        template_id="orion-manufacturing-supply-quality-v1",
        intake_mode="HYBRID",
        intake_rationale="复用 ORION 模板基线，并结合客户资料和数据库继续构建。",
        request_id="acceptance:create:template-baseline",
    )

    project_root = tmp_path / "workflows" / created["project_id"]
    project = json.loads((project_root / "project.json").read_text(encoding="utf-8"))
    binding = project["template_binding"]
    manifest = json.loads(
        (project_root / "template-baseline/template-manifest.json").read_text(encoding="utf-8")
    )

    assert binding["template_id"] == "orion-manufacturing-supply-quality-v1"
    assert binding["template_version"] == "1.0.0"
    assert binding["immutable"] is True
    assert binding["ontology_sha256"].startswith("sha256:")
    assert manifest["ontology_sha256"] == binding["ontology_sha256"]
    assert (project_root / "template-baseline/05-ontology-build/ontology.ttl").is_file()


def test_create_project_rejects_industry_reference_as_template(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path / "workflows")

    with pytest.raises(WorkflowError, match="行业参考只能查看"):
        service.create_project(
            project_name="错误行业参考工程",
            domain="供应链",
            template_id="iof-supply-chain-202603",
            request_id="acceptance:create:reference-rejected",
        )

    assert not [path for path in (tmp_path / "workflows").iterdir() if path.is_dir()]


def test_project_list_exposes_revision_lineage_and_release_version(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflows"
    service = OntologyWorkflowService(workflow_root)
    created = service.create_project(
        project_name="档案合规本体修订",
        domain="档案合规",
        intake_mode="DOCUMENT_ONLY",
        intake_rationale="验证工程中心修订归组字段。",
        request_id="acceptance:list-project-lineage",
    )
    project_root = workflow_root / created["project_id"]
    project_path = project_root / "project.json"
    project = json.loads(project_path.read_text(encoding="utf-8"))
    project.update(
        {
            "parent_project_id": "ontology-project-parent0001",
            "based_on_release_version": "0.3.0",
            "suggested_release_version": "0.3.1",
        }
    )
    project_path.write_text(
        json.dumps(project, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    publication_root = project_root / "07-release"
    publication_root.mkdir(parents=True, exist_ok=True)
    (publication_root / "publication.json").write_text(
        json.dumps({"release_version": "0.3.1"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    listed = service.list_projects()["projects"][0]
    assert listed["domain"] == "档案合规"
    assert listed["parent_project_id"] == "ontology-project-parent0001"
    assert listed["based_on_release_version"] == "0.3.0"
    assert listed["suggested_release_version"] == "0.3.1"
    assert listed["release_version"] == "0.3.1"


def test_create_project_validation_failure_does_not_create_directory(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflows"
    service = OntologyWorkflowService(workflow_root)

    with pytest.raises(WorkflowError, match="project_name 不能为空"):
        service.create_project(
            project_name="",
            domain="invalid-create",
            request_id="acceptance:create:invalid",
        )

    assert not [path for path in workflow_root.iterdir() if path.is_dir()]


def test_create_project_requires_chinese_business_name(tmp_path: Path) -> None:
    workflow_root = tmp_path / "workflows"
    service = OntologyWorkflowService(workflow_root)

    with pytest.raises(WorkflowError, match="必须使用中文业务名称"):
        service.create_project(
            project_name="c01-wq-anomaly-ontology",
            domain="污水处理 · 进水水质异常事件处置",
            request_id="acceptance:create:chinese-name",
        )

    assert not [path for path in workflow_root.iterdir() if path.is_dir()]


def test_status_poll_does_not_run_full_integrity_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(project_name="轻量状态轮询工程", domain="status-poll")

    def fail_if_called(*_args: object, **_kwargs: object) -> dict:
        raise AssertionError("状态轮询不应执行完整文件哈希")

    monkeypatch.setattr(service, "_verify_project_integrity", fail_if_called)
    status = service.get_status(created["project_id"])

    assert status["integrity"]["status"] == "NOT_VERIFIED"


def test_formal_fingerprint_ignores_reports_but_detects_asset_tampering(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="正式产物指纹工程",
        domain="formal-fingerprint",
        intake_mode="DATABASE_ONLY",
    )["project_id"]
    service.record_s0_scope_decision(
        project_id=project_id,
        intake_mode="DATABASE_ONLY",
        rationale="本次只使用受控数据库结构，不接入文档。",
        decided_by="tester",
    )

    project_dir = tmp_path / project_id
    before = service.verify_project_integrity(project_id)
    service._refresh_manifest(project_dir)
    after_report_refresh = service.verify_project_integrity(project_id)
    assert before["status"] == "PASSED"
    assert after_report_refresh["status"] == "PASSED"

    scope_path = project_dir / "00-document-evidence/scope-decision.json"
    scope_path.write_text(scope_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    tampered = service.verify_project_integrity(project_id)
    assert tampered["status"] == "FAILED"
    assert tampered["stages"][0]["status"] == "MISMATCH"


def test_create_project_records_user_competency_question_intake(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path / "workflows")
    created = service.create_project(
        project_name="供应链业务问题工程",
        domain="supply-chain-cq",
        cq_mode="USER_PLUS_AI",
        initial_competency_questions=[
            {
                "question": "当前有哪些供应商正在影响关键订单？",
                "expected": "返回供应商、受影响订单和可追溯原因。",
                "priority": "HIGH",
            }
        ],
    )

    project_dir = tmp_path / "workflows" / created["project_id"]
    intake = json.loads(
        (project_dir / "00-document-evidence/cq-intake.json").read_text(encoding="utf-8")
    )
    assert intake["mode"] == "USER_PLUS_AI"
    assert intake["question_count"] == 1
    assert intake["questions"][0]["source"] == "USER_PROVIDED"
    assert intake["questions"][0]["priority"] == "HIGH"
    assert "sparql" not in intake["questions"][0]
    assert intake["questions_sha256"].startswith("sha256:")


def test_generate_s4_trusts_reviewed_mapping_endpoints_and_inherits_s0_cq_fields(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="集团工厂本体工程",
        domain="集团工厂关系",
        intake_mode="DATABASE_ONLY",
        cq_mode="USER_PROVIDED",
        initial_competency_questions=[
            {
                "id": "CQ-INTAKE-001",
                "question": "集团拥有哪些工厂？",
                "expected": "返回集团下属工厂清单。",
                "priority": "HIGH",
                "example_entities": ["华辰集团"],
            }
        ],
    )["project_id"]
    service.record_s0_scope_decision(
        project_id=project_id,
        intake_mode="DATABASE_ONLY",
        rationale="本次仅使用已授权数据库结构构建集团与工厂关系。",
        decided_by="tester",
    )
    s1_contract = _production_s1_contract(
        project_id,
        {"sc_companies": 1, "sc_plants": 3},
    )
    service.record_data_understanding(
        project_id=project_id,
        datasource_inventory=s1_contract["datasource_inventory"],
        schema_snapshot={
            "tables": [
                {"name": "sc_companies", "primary_key": ["id"]},
                {
                    "name": "sc_plants",
                    "primary_key": ["id"],
                    "foreign_keys": [{"column": "company_id", "target": "sc_companies.id"}],
                },
            ]
        },
        data_profile=s1_contract["data_profile"],
        relation_candidates=[
            {
                "id": "REL-01",
                "from": "sc_plants.company_id",
                "to": "sc_companies.id",
                "basis": "DECLARED_FOREIGN_KEY",
            }
        ],
        evidence_sql=s1_contract["evidence_sql"],
    )
    service.record_semantic_candidates(
        project_id=project_id,
        ontology_candidates=[
            {
                "id": "CLASS-COMPANY",
                "name": "Company",
                "kind": "CLASS",
                "status": "DATABASE_FACT",
                "confidence": 0.99,
                "source_refs": ["table:sc_companies"],
            },
            {
                "id": "CLASS-PLANT",
                "name": "Plant",
                "kind": "CLASS",
                "status": "DATABASE_FACT",
                "confidence": 0.99,
                "source_refs": ["table:sc_plants"],
            },
            {
                "id": "REL-HAS-PLANT",
                "name": "hasPlant",
                "kind": "OBJECT_PROPERTY",
                "status": "DATABASE_FACT",
                "confidence": 0.99,
                "source_refs": ["REL-01"],
            },
        ],
        business_rule_candidates=[],
    )
    after_s3 = service.prepare_mapping_review(
        project_id=project_id,
        mapping_draft={
            "mapping_version": "0.1.0-draft",
            "mappings": [
                {
                    "id": "MAP-CLASS-COMPANY",
                    "source": "sc_companies",
                    "target": "Company",
                    "target_label_zh": "公司",
                    "target_comment_zh": "依法登记并管理下属工厂的公司主体。",
                    "mapping_type": "TABLE_TO_CLASS",
                    "source_refs": ["table:sc_companies"],
                },
                {
                    "id": "MAP-CLASS-PLANT",
                    "source": "sc_plants",
                    "target": "Plant",
                    "target_label_zh": "工厂",
                    "target_comment_zh": "由公司管理并承担生产活动的工厂。",
                    "mapping_type": "TABLE_TO_CLASS",
                    "source_refs": ["table:sc_plants"],
                },
                {
                    "id": "MAP-OP-COMPANY-PLANT",
                    "source": "sc_plants.company_id",
                    "target": "hasPlant",
                    "target_label_zh": "拥有工厂",
                    "target_comment_zh": "公司拥有并管理下属工厂。",
                    "mapping_type": "FK_TO_OBJECT_PROPERTY",
                    "domain": "Company",
                    "range": "Plant",
                    "source_refs": ["REL-01"],
                },
                {
                    "id": "MAP-DP-COMPANY-NAME",
                    "source": "sc_companies.name",
                    "target": "name",
                    "target_label_zh": "公司名称",
                    "target_comment_zh": "公司的正式业务名称。",
                    "mapping_type": "COLUMN_TO_DATA_PROPERTY",
                    "class": "Company",
                    "source_refs": ["table:sc_companies"],
                },
                {
                    "id": "MAP-DP-PLANT-NAME",
                    "source": "sc_plants.name",
                    "target": "name",
                    "target_label_zh": "工厂名称",
                    "target_comment_zh": "工厂的正式业务名称。",
                    "mapping_type": "COLUMN_TO_DATA_PROPERTY",
                    "class": "Plant",
                    "source_refs": ["table:sc_plants"],
                },
            ],
        },
        confirmations=[],
        automatic_decisions=[],
        realtime_runtime=_strict_runtime(
            {
                "ontop_deployment_id": "ontop-organization-runtime",
                "database_access_mode": "READ_ONLY",
                "prepared_by": "test-engineer",
                "mapping_obda": """[PrefixDeclaration]
ex: https://example.com/organization#

[MappingDeclaration] @collection [[
mappingId Company
target <https://example.com/organization/company-{id}> a ex:Company ; ex:name {name} .
source SELECT id, name FROM sc_companies

mappingId Plant
target <https://example.com/organization/plant-{id}> a ex:Plant ; ex:name {name} .
source SELECT id, name FROM sc_plants

mappingId CompanyPlant
target <https://example.com/organization/company-{company_id}> ex:hasPlant <https://example.com/organization/plant-{id}> .
source SELECT id, company_id FROM sc_plants
]]
""",
                "ontop_queries": {
                    "company_plants": """PREFIX ex: <https://example.com/organization#>
SELECT ?source ?target WHERE { ?source ex:hasPlant ?target . }
LIMIT 100
"""
                },
            }
        ),
        review_policy="AUTO_APPROVE_EVIDENCE_BACKED",
    )
    assert after_s3["stage_statuses"]["S3"] == "PASSED"

    generation_request = {
        "ontology_iri": "https://example.com/organization",
        "competency_questions": [
            {
                "id": "CQ-INTAKE-001",
                "sparql": (
                    "SELECT ?source ?target WHERE { ?source "
                    "<https://example.com/organization#hasPlant> ?target . } LIMIT 100"
                ),
                "answer_contract": {
                    "result_assertions": [
                        {
                            "binding": "source",
                            "operator": "EQ",
                            "expected": "urn:test:company:1",
                        }
                    ],
                    "boundary_assertions": [
                        {
                            "binding": "target",
                            "operator": "EQ",
                            "expected": "urn:test:plant:1",
                        }
                    ],
                    "required_business_dimensions": [
                        {
                            "dimension": "subject",
                            "label_zh": "集团主体",
                            "binding": "source",
                            "ontology_term": "https://example.com/organization#Company",
                            "evidence_refs": ["MAP-CLASS-COMPANY"],
                        },
                        {
                            "dimension": "related_object",
                            "label_zh": "下属工厂",
                            "binding": "target",
                            "ontology_term": "https://example.com/organization#Plant",
                            "path": "https://example.com/organization#hasPlant",
                            "evidence_refs": ["MAP-OP-COMPANY-PLANT"],
                        },
                    ],
                },
            }
        ],
        "logical_axioms": [
            {
                "id": "AX-COMPANY-PLANT-DISJOINT",
                "axiom_type": "DISJOINT_WITH",
                "class": "https://example.com/organization#Company",
                "other": "https://example.com/organization#Plant",
                "source_refs": ["MAP-CLASS-COMPANY", "MAP-CLASS-PLANT"],
            }
        ],
        "review_policy": "AUTO_APPROVE_EVIDENCE_BACKED",
    }

    state_path = tmp_path / project_id / "workflow-state.json"
    state_before = state_path.read_bytes()
    preflight = service.preflight_stage_submission(
        project_id=project_id,
        stage="S4",
        payload=generation_request,
    )
    assert preflight["status"] == "PASSED", preflight
    assert preflight["metrics"]["submission_mode"] == "GENERATION_REQUEST"
    assert preflight["metrics"]["class_count"] == 2
    assert preflight["metrics"]["object_property_count"] == 1
    assert preflight["normalized_payload"]["review_policy"] == ("AUTO_APPROVE_EVIDENCE_BACKED")
    assert state_path.read_bytes() == state_before
    assert not list((tmp_path / project_id / "04-ontology-design").iterdir())

    invalid_request = json.loads(json.dumps(generation_request))
    dimensions = invalid_request["competency_questions"][0]["answer_contract"]["required_business_dimensions"]
    dimensions[0]["evidence_refs"] = ["UNKNOWN-EVIDENCE"]
    dimensions[0]["ontology_term"] = "urn:unknown:Class"
    dimensions[1]["path"] = "urn:unknown:relation"
    invalid_preflight = service.preflight_stage_submission(
        project_id=project_id, stage="S4", payload={"generation_request": invalid_request},
    )
    assert invalid_preflight["status"] == "FAILED"
    assert invalid_preflight["issue_count"] == 3
    assert invalid_preflight["issues_truncated"] is False
    assert {issue["reason_code"] for issue in invalid_preflight["issues"]} == {
        "UNKNOWN_EVIDENCE_REFS", "UNKNOWN_ONTOLOGY_TERM", "MISSING_OR_UNQUERIED_PATH",
    }
    assert all(issue["source_question_id"] for issue in invalid_preflight["issues"])
    assert all(issue["path"].startswith("competency_questions[0].answer_contract.")
               for issue in invalid_preflight["issues"])
    assert invalid_preflight["writes_performed"] is False
    assert "submission_token" not in invalid_preflight
    assert state_path.read_bytes() == state_before
    assert not list((tmp_path / project_id / "04-ontology-design").iterdir())

    for index in range(31):
        dimensions.append({
            **dimensions[1], "dimension": f"related_{index}",
            "path": "https://example.com/organization#hasPlant",
            "evidence_refs": ["UNKNOWN-EVIDENCE"],
        })
    capped = service.preflight_stage_submission(
        project_id=project_id, stage="S4", payload={"generation_request": invalid_request},
    )
    assert capped["issue_count"] == 34
    assert len(capped["issues"]) == 30
    assert capped["issues_truncated"] is True
    assert state_path.read_bytes() == state_before

    result = service.generate_ontology_design(
        project_id=project_id,
        **generation_request,
    )

    assert result["stage_statuses"]["S4"] == "PASSED"
    design = yaml.safe_load(
        (tmp_path / project_id / "04-ontology-design/ontology-design.yaml").read_text(
            encoding="utf-8"
        )
    )
    relation = design["object_properties"][0]
    assert relation["domain"] == "https://example.com/organization#Company"
    assert relation["range"] == "https://example.com/organization#Plant"
    data_properties = {item["name"]: item for item in design["data_properties"]}
    assert set(data_properties) == {"companyName", "plantName"}
    assert data_properties["companyName"]["mapping_target"] == "name"
    assert data_properties["plantName"]["mapping_target"] == "name"
    question = design["competency_questions"][0]
    assert question["question"] == "集团拥有哪些工厂？"
    assert question["expected"] == "返回集团下属工厂清单。"
    assert question["priority"] == "HIGH"
    assert question["example_entities"] == ["华辰集团"]
    assert question["source"] == "USER_PROVIDED"
    assert question["source_question_id"] == "CQ-INTAKE-001"
    assert question["source_question_sha256"].startswith("sha256:")


def test_user_provided_cq_mode_requires_at_least_one_question(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path / "workflows")

    with pytest.raises(WorkflowError, match="至少需要填写 1 个业务问题"):
        service.create_project(
            project_name="供应链人工问题工程",
            domain="supply-chain-user-cq",
            cq_mode="USER_PROVIDED",
        )


def test_select_cq_rejects_wildcard_without_named_answer_bindings() -> None:
    with pytest.raises(WorkflowGateError, match="显式返回至少一个命名变量"):
        OntologyWorkflowService._cq_answer_contract(
            {
                "sparql": "SELECT * WHERE { ?subject ?predicate ?object . }",
                "expected": "返回任意匹配结果。",
            }
        )


def test_select_cq_accepts_prefix_and_base_prologue() -> None:
    questions = OntologyWorkflowService._validate_competency_questions(
        [
            {
                "id": "CQ-PROLOGUE",
                "question": "有哪些业务对象？",
                "expected": "返回对象标识。",
                "sparql": (
                    "BASE <https://example.com/>\n"
                    "PREFIX ex: <https://example.com/ns#>\n"
                    "SELECT ?item WHERE { ?item a ex:Thing . }"
                ),
                "answer_contract": {
                    "result_assertions": [
                        {
                            "binding": "item",
                            "operator": "EQ",
                            "expected": "https://example.com/item-1",
                        }
                    ],
                    "boundary_assertions": [
                        {
                            "binding": "item",
                            "operator": "EQ",
                            "expected": "https://example.com/item-2",
                            "row": "ANY",
                        }
                    ],
                },
            }
        ]
    )

    assert questions[0]["answer_contract"]["query_type"] == "SELECT"
    assert questions[0]["answer_contract"]["required_bindings"] == ["item"]


def test_cq_edit_recompiles_query_and_preserves_s0_lineage(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    source_question = {
        "id": "CQ-INTAKE-001",
        "question": "旧问题是什么？",
        "expected": "返回旧关系。",
    }
    source_sha256 = service._cq_source_question_sha256(source_question)
    draft = {
        "ontology_iri": "https://example.com/cq-edit",
        "classes": [],
        "object_properties": [
            {
                "name": "newRelation",
                "label_zh": "新关系",
                "iri": "https://example.com/cq-edit#newRelation",
                "domain": "https://example.com/cq-edit#Source",
                "range": "https://example.com/cq-edit#Target",
            }
        ],
        "competency_questions": [
            {
                "id": "CQ-001",
                "question": source_question["question"],
                "expected": source_question["expected"],
                "sparql": "SELECT ?old WHERE { ?old ?p ?o . }",
                "source_question_id": source_question["id"],
                "source_question_sha256": source_sha256,
            }
        ],
    }

    completed = service._complete_review_questions(
        draft,
        [
            {
                "id": "CQ-001",
                "question": "哪些对象存在新关系？",
                "expected": "返回关系两端对象。",
            }
        ],
    )[0]
    normalized = service._validate_competency_questions([completed])[0]

    assert "newRelation" in completed["sparql"]
    assert "SELECT ?old" not in completed["sparql"]
    assert completed["source_question_id"] == source_question["id"]
    assert completed["source_question_sha256"] == source_sha256
    assert normalized["answer_contract"]["required_bindings"] == ["source", "target"]
    assert normalized["answer_contract"]["question_sha256"].startswith("sha256:")


def test_s4_cannot_silently_drop_a_user_provided_s0_question(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "project"
    (project_dir / "00-document-evidence").mkdir(parents=True)
    intake_question = {
        "id": "CQ-INTAKE-001",
        "question": "办理业务需要哪些材料？",
        "expected": "返回材料清单和办理渠道。",
    }
    service._write_json(
        project_dir / "00-document-evidence/cq-intake.json",
        {
            "questions": [intake_question],
            "questions_sha256": "sha256:" + "c" * 64,
        },
    )

    with pytest.raises(WorkflowGateError, match="不能在 S4 静默删除"):
        service._validate_competency_question_lineage(project_dir, [])


def _minimal_s6_cq_payload(
    tmp_path: Path,
    question: dict,
) -> tuple[OntologyWorkflowService, Path, dict]:
    service = OntologyWorkflowService(tmp_path / "workflow-root")
    project_dir = tmp_path / "cq-s6-project"
    for folder in (
        "00-document-evidence",
        "03-mapping-review",
        "04-ontology-design",
        "05-ontology-build",
    ):
        (project_dir / folder).mkdir(parents=True, exist_ok=True)
    normalized_question = service._validate_competency_questions([question])[0]
    service._write_json(
        project_dir / "00-document-evidence/cq-intake.json",
        {"questions": [], "questions_sha256": "sha256:" + "0" * 64},
    )
    service._write_yaml(
        project_dir / "03-mapping-review/mapping.yaml",
        {"mappings": []},
    )
    service._write_yaml(
        project_dir / "04-ontology-design/ontology-design.yaml",
        {
            "classes": [],
            "object_properties": [],
            "data_properties": [],
            "competency_questions": [normalized_question],
        },
    )
    (project_dir / "05-ontology-build/ontology.ttl").write_text(
        """@prefix ex: <https://example.com/cq#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Thing a owl:Class .
""",
        encoding="utf-8",
    )
    (project_dir / "05-ontology-build/shapes.ttl").write_text(
        """@prefix ex: <https://example.com/cq#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
ex:ThingShape a sh:NodeShape ; sh:targetClass ex:Thing .
""",
        encoding="utf-8",
    )
    payload = {
        "materialized_ttl": "@prefix ex: <https://example.com/cq#> .\n",
        "hermit_report": {
            "status": "PASSED",
            "consistent": True,
            "reasoner": "HERMIT",
            "run_id": "hermit-cq-test",
        },
        "mapping_report": {"status": "PASSED", "mapped_count": 0, "unmapped_count": 0},
        "semantic_quality_report": {
            "status": "PASSED",
            "checked_entity_count": 0,
            "high_severity_issue_count": 0,
        },
        "competency_question_report": {
            "status": "PASSED",
            "total": 1,
            "passed": 1,
            "question_ids": [str(normalized_question["id"])],
        },
        "semantica_report": {
            "status": "PASSED",
            "instance_count": 1,
            "engine": "SEMANTICA_MCP",
            "run_id": "semantica-cq-test",
        },
    }
    return service, project_dir, payload


@pytest.mark.parametrize(
    "question",
    [
        {
            "id": "CQ-SELECT-EMPTY",
            "question": "当前有哪些对象？",
            "sparql": ("SELECT ?item WHERE { ?item a <https://example.com/cq#Thing> . }"),
            "expected": "至少返回一个对象。",
        },
        {
            "id": "CQ-ASK-FALSE",
            "question": "是否存在目标对象？",
            "sparql": (
                "ASK { <https://example.com/cq#missing> a <https://example.com/cq#Thing> . }"
            ),
            "expected": "应当存在目标对象。",
            "answer_contract": {"query_type": "ASK", "expected_boolean": True},
        },
    ],
)
def test_s6_rejects_executable_query_when_answer_contract_is_not_met(
    tmp_path: Path,
    question: dict,
) -> None:
    service, project_dir, payload = _minimal_s6_cq_payload(tmp_path, question)

    with pytest.raises(WorkflowGateError, match="答案未满足契约|ASK 结果") as exc_info:
        service._validate_s6(project_dir, payload)

    assert exc_info.value.gate_id == "G-S6-CQ-ANSWER"


def test_s6_rejects_caller_cq_totals_that_do_not_match_s4(tmp_path: Path) -> None:
    question = {
        "id": "CQ-TOTAL",
        "question": "当前有哪些对象？",
        "sparql": "SELECT ?item WHERE { ?item a <https://example.com/cq#Thing> . }",
        "expected": "允许没有实例时返回空集。",
    }
    service, project_dir, payload = _minimal_s6_cq_payload(tmp_path, question)
    payload["competency_question_report"]["total"] = 99

    with pytest.raises(WorkflowGateError, match="数量与 S4 正式设计不一致"):
        service._validate_s6(project_dir, payload)


def test_s4_rejects_business_filter_missing_from_sparql() -> None:
    with pytest.raises(WorkflowGateError, match="parameterValue") as exc_info:
        OntologyWorkflowService._cq_answer_contract(
            {
                "id": "CQ-PARAMETER-BOUNDARY",
                "question": "参数值大于 1 的记录有多少？",
                "expected": "返回 count=2。",
                "sparql": "SELECT (COUNT(?sample) AS ?count) WHERE { ?sample a <urn:Sample> . }",
            }
        )

    assert exc_info.value.gate_id == "G-S4-CQ-SEMANTIC"


def test_s4_does_not_invert_forbidden_ng_substitution_into_required_filter() -> None:
    contract = OntologyWorkflowService._cq_answer_contract(
        {
            "id": "CQ-PARAMETER-NOT-NG",
            "question": "参数值大于 1 的记录有多少？",
            "expected": ("返回 count=190；必须使用 parameterValue>1，不能用 checkResult=NG 替代。"),
            "sparql": (
                "SELECT (COUNT(?sample) AS ?count) WHERE { "
                "?sample <urn:parameterValue> ?value . FILTER(?value > 1) }"
            ),
        }
    )

    assert "NG" not in contract["required_sparql_fragments"]
    assert "parameterValue" in contract["required_sparql_fragments"]


@pytest.mark.parametrize(
    "scope",
    [
        "全部 NG 不等于本规则异常。",
        "NG不等于编码异常。",
        "编码异常不等同于NG结果。",
        "NG不代表编码异常。",
        "不能用 checkResult=NG 替代编码异常规则。",
        "NG不替代业务规则判断。",
        "NG不能替代编码规则判断。",
        "不要筛选NG记录。",
        "无需限定检测结果为NG。",
        "样例中检测结果为NG。",
        "例如返回NG记录。",
        "查询所有记录并展示OK/NG结果。",
        "编码说明包含NG字样。",
    ],
)
def test_s4_ng_mentions_do_not_require_positive_filter(scope: str) -> None:
    contract = OntologyWorkflowService._cq_answer_contract(
        {
            "question": "哪些对象满足编码规则？",
            "expected": scope,
            "sparql": "SELECT ?item WHERE { ?item a <urn:CodeAnomaly> . }",
            "answer_contract": {
                "required_sparql_fragments": ["<urn:CodeAnomaly>"],
            },
        }
    )
    assert "NG" not in contract["required_sparql_fragments"]
    assert "<urn:CodeAnomaly>" in contract["required_sparql_fragments"]


@pytest.mark.parametrize(
    "scope",
    [
        "哪些NG记录需要复核？",
        "查询 NG 结果。",
        "检测结果为NG的记录有哪些？",
        "check_result = 'NG' 的记录。",
        "NG记录有多少？",
        "NG不等于编码异常；本题只查询NG记录。",
        "样例中检测结果为OK；请筛选NG记录。",
        "查询NG记录且不能遗漏设备信息。",
    ],
)
def test_s4_explicit_ng_selection_still_requires_filter(scope: str) -> None:
    question = {
        "question": scope,
        "expected": "至少返回一条记录。",
        "sparql": "SELECT ?item WHERE { ?item a <urn:Record> . }",
    }
    with pytest.raises(WorkflowGateError, match="业务问题要求 NG") as exc_info:
        OntologyWorkflowService._cq_answer_contract(question)
    assert exc_info.value.gate_id == "G-S4-CQ-SEMANTIC"

    question["sparql"] = 'SELECT ?item WHERE { ?item <urn:checkResult> "NG" . }'
    contract = OntologyWorkflowService._cq_answer_contract(question)
    assert "NG" in contract["required_sparql_fragments"]
    assert "checkResult" in contract["required_sparql_fragments"]


def test_s4_relation_gate_rejects_class_only_design_for_relationship_question(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "relation-gate-project"
    (project_dir / "03-mapping-review").mkdir(parents=True)
    service._write_yaml(
        project_dir / "03-mapping-review/mapping.yaml",
        {
            "mappings": [
                {
                    "id": "MAP-SUBJECT",
                    "source_refs": ["EVD-ARTICLE-15"],
                }
            ]
        },
    )
    design = {
        "ontology_iri": "https://example.com/archive",
        "version": "0.3.1",
        "title_zh": "档案义务本体",
        "comment_zh": "描述义务主体与法定义务关系。",
        "classes": [
            {
                "name": "ObligatedSubject",
                "iri": "https://example.com/archive#ObligatedSubject",
                "label_zh": "义务主体",
                "comment_zh": "依法承担档案义务的主体。",
                "source_mapping_ids": ["MAP-SUBJECT"],
            }
        ],
        "object_properties": [],
        "data_properties": [],
        "competency_questions": [
            {
                "id": "CQ-001",
                "question": "谁承担什么义务，什么条件触发，有何期限和例外？",
                "expected": "返回义务主体、所负义务、触发条件、义务期限、例外情形。",
                "sparql": "SELECT ?subject WHERE { ?subject a <https://example.com/archive#ObligatedSubject> . }",
            }
        ],
        "logical_axioms": [],
    }

    with pytest.raises(WorkflowGateError) as exc_info:
        service._validate_s4(project_dir, design)

    assert exc_info.value.gate_id == "G-S4-RELATION-COVERAGE"


def test_s4_cq_contract_rejects_expected_five_dimensions_but_subject_only(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "cq-contract-gate-project"
    (project_dir / "03-mapping-review").mkdir(parents=True)
    service._write_yaml(
        project_dir / "03-mapping-review/mapping.yaml",
        {
            "mappings": [
                {"id": "MAP-SUBJECT", "source_refs": ["EVD-ARTICLE-15"]},
                {"id": "MAP-OBLIGATION", "source_refs": ["EVD-ARTICLE-15"]},
                {"id": "MAP-HAS-OBLIGATION", "source_refs": ["EVD-ARTICLE-15"]},
            ]
        },
    )
    base = "https://example.com/archive#"
    design = {
        "ontology_iri": "https://example.com/archive",
        "version": "0.3.1",
        "title_zh": "档案义务本体",
        "comment_zh": "描述义务主体与法定义务关系。",
        "classes": [
            {
                "name": "ObligatedSubject",
                "iri": base + "ObligatedSubject",
                "label_zh": "义务主体",
                "comment_zh": "依法承担档案义务的主体。",
                "source_mapping_ids": ["MAP-SUBJECT"],
            },
            {
                "name": "Obligation",
                "iri": base + "Obligation",
                "label_zh": "法定义务",
                "comment_zh": "档案法规定的具体义务。",
                "source_mapping_ids": ["MAP-OBLIGATION"],
            },
        ],
        "object_properties": [
            {
                "name": "hasObligation",
                "iri": base + "hasObligation",
                "label_zh": "承担义务",
                "comment_zh": "义务主体依法承担具体义务。",
                "domain": base + "ObligatedSubject",
                "range": base + "Obligation",
                "source_mapping_ids": ["MAP-HAS-OBLIGATION"],
            }
        ],
        "data_properties": [],
        "competency_questions": [
            {
                "id": "CQ-001",
                "question": "谁承担什么义务，什么条件触发，有何期限和例外？",
                "expected": "返回义务主体、所负义务、触发条件、义务期限、例外情形。",
                "sparql": (
                    "SELECT ?subject WHERE { ?subject <" + base + "hasObligation> ?obligation . }"
                ),
                "answer_contract": {
                    "result_assertions": [
                        {"binding": "subject", "operator": "EQ", "expected": "urn:test:subject"}
                    ],
                    "boundary_assertions": [
                        {"binding": "subject", "operator": "EQ", "expected": "urn:test:subject"}
                    ],
                    "required_business_dimensions": [
                        {
                            "dimension": "subject",
                            "label_zh": "义务主体",
                            "binding": "subject",
                            "ontology_term": base + "ObligatedSubject",
                            "evidence_refs": ["EVD-ARTICLE-15"],
                        }
                    ],
                },
            }
        ],
        "logical_axioms": [],
    }

    with pytest.raises(WorkflowGateError) as exc_info:
        service._validate_s4(project_dir, design)

    assert exc_info.value.gate_id == "G-S4-CQ-CONTRACT-COVERAGE"


def test_s6_relationship_gate_rejects_nodes_without_edges(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "relationship-s6-project"
    project_dir.mkdir()
    service._write_json(
        project_dir / "workflow-state.json",
        {"stage_fingerprints": {"S5": {"output": "sha256:" + "a" * 64}}},
    )
    base = "https://example.com/archive#"
    design = {
        "classes": [
            {"name": "Subject", "iri": base + "Subject"},
            {"name": "Obligation", "iri": base + "Obligation"},
        ],
        "object_properties": [
            {
                "name": "hasObligation",
                "iri": base + "hasObligation",
                "domain": base + "Subject",
                "range": base + "Obligation",
                "source_mapping_ids": ["MAP-HAS-OBLIGATION"],
            }
        ],
        "data_properties": [
            {
                "name": "obligationCode",
                "iri": base + "obligationCode",
                "domain": base + "Obligation",
                "range": "http://www.w3.org/2001/XMLSchema#string",
                "source_mapping_ids": ["MAP-OBLIGATION-CODE"],
            }
        ],
        "competency_questions": [
            {
                "question": "谁承担什么义务？",
                "expected": "返回义务主体和所负义务。",
                "sparql": (
                    "SELECT ?subject ?obligation WHERE { ?subject <"
                    + base
                    + "hasObligation> ?obligation . }"
                ),
                "answer_contract": {
                    "required_business_dimensions": [
                        {
                            "dimension": "subject",
                            "applicability": "REQUIRED",
                            "path": "",
                        },
                        {
                            "dimension": "obligation",
                            "applicability": "REQUIRED",
                            "path": base + "hasObligation",
                        },
                        {
                            "dimension": "obligation_code",
                            "applicability": "REQUIRED",
                            "path": base + "obligationCode",
                        },
                    ]
                },
            }
        ],
    }
    mapping = {
        "mappings": [
            {
                "id": "MAP-HAS-OBLIGATION",
                "source_refs": ["EVD-ARTICLE-15"],
            }
        ]
    }
    ontology = Graph().parse(
        data=(
            "@prefix ex: <https://example.com/archive#> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
            "ex:hasObligation a owl:ObjectProperty ; "
            "rdfs:domain ex:Subject ; rdfs:range ex:Obligation .\n"
            "ex:obligationCode a owl:DatatypeProperty ; "
            "rdfs:domain ex:Obligation ; "
            "rdfs:range <http://www.w3.org/2001/XMLSchema#string> .\n"
        ),
        format="turtle",
    )
    ontology_path = project_dir / "05-ontology-build/ontology.ttl"
    ontology_path.parent.mkdir(parents=True)
    ontology_path.write_text(str(ontology.serialize(format="turtle")), encoding="utf-8")

    with pytest.raises(WorkflowGateError) as exc_info:
        service._validate_s6_graph_relationships(
            project_dir=project_dir,
            design=design,
            mapping_document=mapping,
            data_graph=Graph(),
            ontology_graph=ontology,
            semantica={
                "relationship_count": 0,
                "import_result": {"nodes_added": 2, "edges_added": 0},
            },
        )

    assert exc_info.value.gate_id == "G-S6-GRAPH-RELATIONSHIP"

    data_graph = Graph().parse(
        data=(
            "@prefix ex: <https://example.com/archive#> .\n"
            "ex:subject-1 a ex:Subject ; ex:hasObligation ex:obligation-1 .\n"
            'ex:obligation-1 a ex:Obligation ; ex:obligationCode "A-15" .\n'
        ),
        format="turtle",
    )
    relationships = [
        {
            "subject": base + "subject-1",
            "predicate": base + "hasObligation",
            "object": base + "obligation-1",
        }
    ]
    relationship_sha256 = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                relationships,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    exact_readback = {
        "status": "VERIFIED",
        "mode": "IDEMPOTENT_READBACK",
        "release_candidate_fingerprint": "sha256:" + "a" * 64,
        "relationship_set_sha256": relationship_sha256,
        "verified_relationships_sha256": relationship_sha256,
        "expected_relationship_count": 1,
        "verified_relationship_count": 1,
        "candidate_relationship_count": 1,
        "verified_predicates": [base + "hasObligation"],
        "verified_relationships": relationships,
        "receipt_sha256": "sha256:" + "b" * 64,
    }

    with pytest.raises(WorkflowGateError) as mismatch_info:
        service._validate_s6_graph_relationships(
            project_dir=project_dir,
            design=design,
            mapping_document=mapping,
            data_graph=data_graph,
            ontology_graph=ontology,
            semantica={
                "relationship_count": 1,
                "relationship_import_result": {"edges_added": 0},
                "candidate_relationship_verification": {
                    **exact_readback,
                    "release_candidate_fingerprint": "sha256:" + "c" * 64,
                },
            },
        )
    assert mismatch_info.value.gate_id == "G-S6-GRAPH-RELATIONSHIP"

    verified = service._validate_s6_graph_relationships(
        project_dir=project_dir,
        design=design,
        mapping_document=mapping,
        data_graph=data_graph,
        ontology_graph=ontology,
        semantica={
            "relationship_count": 1,
            "relationship_import_result": {"edges_added": 0},
            "candidate_relationship_verification": exact_readback,
        },
    )
    assert verified["status"] == "VERIFIED"
    assert verified["semantica_relationship_import_mode"] == "IDEMPOTENT_READBACK"


def test_s6_rejects_wrong_exact_business_result(tmp_path: Path) -> None:
    question = {
        "id": "CQ-EXACT-COUNT",
        "question": "当前对象总数是多少？",
        "sparql": (
            "SELECT (COUNT(?item) AS ?count) WHERE { ?item a <https://example.com/cq#Thing> . }"
        ),
        "expected": "返回 count=3。",
    }
    service, project_dir, payload = _minimal_s6_cq_payload(tmp_path, question)
    payload["materialized_ttl"] = (
        "@prefix ex: <https://example.com/cq#> .\nex:one a ex:Thing .\nex:two a ex:Thing .\n"
    )

    with pytest.raises(WorkflowGateError, match="count 未满足 EQ 3") as exc_info:
        service._validate_s6(project_dir, payload)

    assert exc_info.value.gate_id == "G-S6-CQ-SEMANTIC"


def test_cq_numeric_equality_uses_rdf_value_not_decimal_lexical_form() -> None:
    matcher = OntologyWorkflowService._cq_assertion_matches

    assert matcher("1E+4", {"operator": "EQ", "expected": "10000.00"})
    assert matcher("10000", {"operator": "NE", "expected": "10000.00"}) is False
    assert matcher("001", {"operator": "EQ", "expected": "1"}) is False


def test_sparql_relationship_detection_resolves_prefix_qualified_iris() -> None:
    from services.ontology_engineering.workflow import _sparql_references_iri

    query = """PREFIX fraud: <https://fraud.orion.local/fraud/>
SELECT ?alert ?transaction WHERE {
  ?alert fraud:alertsOnTransaction ?transaction .
}"""

    assert _sparql_references_iri(
        query, "https://fraud.orion.local/fraud/alertsOnTransaction"
    )
    assert not _sparql_references_iri(
        query, "https://fraud.orion.local/fraud/alertsOnCustomer"
    )


def _record_s1(
    service: OntologyWorkflowService,
    project_id: str,
    *,
    supplier_count: int = 10,
    expected_revision: int | None = None,
) -> dict:
    counts = {"sc_suppliers": supplier_count, "sc_purchase_orders": 30}
    contract = _production_s1_contract(project_id, counts)
    return service.record_data_understanding(
        project_id=project_id,
        datasource_inventory=contract["datasource_inventory"],
        schema_snapshot={
            "tables": [
                {"name": "sc_suppliers", "primary_key": ["id"]},
                {
                    "name": "sc_purchase_orders",
                    "primary_key": ["id"],
                    "foreign_keys": [{"column": "supplier_id", "target": "sc_suppliers.id"}],
                },
            ]
        },
        data_profile=contract["data_profile"],
        relation_candidates=[
            {
                "id": "REL-001",
                "from": "sc_purchase_orders.supplier_id",
                "to": "sc_suppliers.id",
                "basis": "DECLARED_FOREIGN_KEY",
            }
        ],
        evidence_sql=contract["evidence_sql"],
        expected_revision=expected_revision,
    )


def _production_s1_contract(
    project_id: str,
    counts: dict[str, int],
) -> dict[str, Any]:
    tables = list(counts)
    total_rows = sum(counts.values())
    return {
        "datasource_inventory": {
            "selected": "ontology_agent",
            "read_only": True,
            "business_tables_scope": tables,
            "datasets": [
                {
                    "project_id": project_id,
                    "dataset_id": "dataset-production-test",
                    "source_sha256": "sha256:" + "d" * 64,
                    "version": "snapshot-production-test-v1",
                    "status": "READY",
                    "row_count": total_rows,
                }
            ],
        },
        "data_profile": {
            "profile_mode": "FULL_IMPORT_WITH_EXACT_COUNTS",
            "table_count": len(tables),
            "total_rows": total_rows,
            "empty_table_count": sum(1 for value in counts.values() if value == 0),
            "tables": [
                {"table": table, "row_count": row_count} for table, row_count in counts.items()
            ],
        },
        "evidence_sql": [
            {
                "id": "SQL-001",
                "purpose": "确认生产数据范围已经由只读查询回读",
                "sql": "SELECT COUNT(*) AS count FROM " + tables[0],
                "source_tables": tables,
                "status": "PASSED",
                "executed_via": "CHAT2DB_MCP",
                "executed_at": "2026-09-04T00:00:00+08:00",
                "result_sha256": "sha256:" + "e" * 64,
                "expected_row_count": 1,
                "actual_row_count": 1,
            }
        ],
    }


def test_s1_preflight_preserves_state_reports_receipt_fields_and_commits_token(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path / "workflows")
    created = service.create_project(project_name="预检", domain="test", intake_mode="DATABASE_ONLY",
                                     intake_rationale="只读快照")
    project_id = created["project_id"]
    service.record_s0_scope_decision(project_id=project_id, decided_by="tester", rationale="只读数据库",
                                     intake_mode="DATABASE_ONLY", datasource_refs=["db-test"])
    before = service.get_status(project_id=project_id)
    payload = {**_production_s1_contract(project_id, {"facts": 10}),
               "schema_snapshot": {"tables": [{"name": "facts", "columns": ["id"]}]},
               "relation_candidates": []}
    payload["evidence_sql"][0]["result_sha256"] = "bad"
    payload["evidence_sql"][0]["actual_row_count"] = True
    failed = service.preflight_stage_submission(project_id=project_id, stage="S1", payload=payload)
    assert failed["status"] == "FAILED"
    assert "result_sha256" in failed["issues"][0]["message"]
    assert "actual_row_count" in failed["issues"][0]["message"]
    assert service.get_status(project_id=project_id)["revision"] == before["revision"]
    payload["evidence_sql"][0]["result_sha256"] = "sha256:" + "e" * 64
    payload["evidence_sql"][0]["actual_row_count"] = 1
    passed = service.preflight_stage_submission(project_id=project_id, stage="S1", payload=payload)
    assert passed["status"] == "PASSED"
    result = service.commit_preflight_stage_submission(project_id=project_id, stage="S1",
        preflight_token=passed["preflight_token"], expected_revision=before["revision"])
    assert result["current_stage"] == "S2"
    assert result["stage_statuses"]["S1"] == "PASSED"
    assert service.commit_preflight_stage_submission(
        project_id=project_id, stage="S1", preflight_token=passed["preflight_token"]
    ) == result


def test_file_preflight_freezes_full_payload_and_commits_without_rereading_draft(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path / "workflows")
    created = service.create_project(project_name="文件预检", domain="test", intake_mode="DATABASE_ONLY",
                                     intake_rationale="只读快照")
    project_id = created["project_id"]
    service.record_s0_scope_decision(project_id=project_id, decided_by="tester", rationale="只读数据库",
                                     intake_mode="DATABASE_ONLY", datasource_refs=["db-test"])
    state = service.get_status(project_id=project_id)
    payload = {**_production_s1_contract(project_id, {"facts": 10}),
               "schema_snapshot": {"tables": [{"name": "facts", "columns": ["id"]}]},
               "relation_candidates": []}
    directory = service._resolve_project(project_id)
    draft = directory / ".submission-drafts" / "request.json"
    draft.parent.mkdir()
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    draft.write_bytes(data)
    api = OrionWorkflowTools(service)
    text, receipt = api.call("preflight_stage_submission", {
        "project_id": project_id, "stage": "S1", "expected_revision": state["revision"],
        "payload_file": {"file_name": "request.json", "sha256": "sha256:" + hashlib.sha256(data).hexdigest()},
    })
    assert receipt["status"] == "PASSED"
    assert receipt["preflight_token"] in text
    assert receipt["payload_source"]["byte_count"] == len(data)
    assert service.get_status(project_id=project_id)["revision"] == state["revision"]
    draft.write_text('{"tampered":true}')
    _, result = api.call("commit_preflight_stage_submission", {
        "project_id": project_id, "stage": "S1", "expected_revision": state["revision"],
        "preflight_token": receipt["preflight_token"],
    })
    assert result["stage_statuses"]["S1"] == "PASSED"


@pytest.mark.parametrize("case", ["traversal", "absolute", "file_symlink", "directory_symlink", "hash", "json", "array", "large", "revision", "both", "neither"])
def test_file_preflight_rejects_unsafe_or_stale_input_without_state_change(tmp_path: Path, case: str) -> None:
    service = OntologyWorkflowService(tmp_path / "workflows")
    project_id = service.create_project(project_name="受控预检", domain="test")["project_id"]
    directory = service._resolve_project(project_id)
    drafts = directory / ".submission-drafts"
    drafts.mkdir()
    path = drafts / "request.json"
    data = b"{}"
    if case == "json":
        data = b"{broken"
    elif case == "array":
        data = b"[]"
    elif case == "large":
        data = b" " * (8 * 1024 * 1024 + 1)
    path.write_bytes(data)
    if case == "file_symlink":
        target = tmp_path / "other.json"
        target.write_bytes(data)
        path.unlink()
        path.symlink_to(target)
    elif case == "directory_symlink":
        drafts.rename(directory / "original-drafts")
        drafts.symlink_to(directory / "original-drafts", target_is_directory=True)
    reference = {"file_name": "request.json", "sha256": "sha256:" + hashlib.sha256(data).hexdigest()}
    if case == "traversal":
        reference["file_name"] = "../workflow-state.json"
    elif case == "absolute":
        reference["file_name"] = str(path)
    elif case == "hash":
        reference["sha256"] = "sha256:" + "0" * 64
    args = {"project_id": project_id, "stage": "S2", "payload_file": reference}
    if case == "revision":
        args["expected_revision"] = 999
    elif case == "both":
        args["payload"] = {}
    elif case == "neither":
        args.pop("payload_file")
    before = (directory / "workflow-state.json").read_bytes()
    with pytest.raises(WorkflowError):
        OrionWorkflowTools(service).call("preflight_stage_submission", args)
    assert (directory / "workflow-state.json").read_bytes() == before
    assert not (directory / ".preflight-submissions").exists()


def test_document_queue_is_idempotent_scoped_and_revision_fenced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "rules.md").write_text("# 规则\n可定位的业务资料")
    monkeypatch.setenv("ORION_WORKSPACE_REFERENCE_ROOTS", str(workspace))
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(tmp_path / "intake"))
    service = OntologyWorkflowService(tmp_path / "workflows")
    api = OrionWorkflowTools(service)
    _, reference = api.call("snapshot_workspace_sources", {"workspace_root": str(workspace), "references": ["rules.md"]})
    created = service.create_project(project_name="资料队列", domain="test", intake_mode="DOCUMENT_ONLY", intake_rationale="引用资料")
    arguments = {"project_id": created["project_id"], "source_path": reference["source_path"],
                 "expected_revision": created["revision"], "actor": "tester"}
    _, first = api.call("start_document_ingestion_job", arguments)
    _, second = api.call("start_document_ingestion_job", arguments)
    assert first["job_id"] == second["job_id"]
    assert second["reused_existing_job"] is True
    assert first["status"] == "QUEUED"
    assert service.get_status(project_id=created["project_id"])["document_ingestion_job"]["job_id"] == first["job_id"]
    with pytest.raises(WorkflowError, match="不属于"):
        api.call("get_document_ingestion_job", {"project_id": "another-project", "job_id": first["job_id"]})
    with pytest.raises(WorkflowError):
        api.call("start_document_ingestion_job", {**arguments, "expected_revision": created["revision"] + 1})
    with pytest.raises(WorkflowError, match="旧修订"):
        api.call("commit_document_ingestion_job", {"project_id": created["project_id"], "job_id": first["job_id"],
            "expected_revision": created["revision"] + 1, "reviewed_by": "tester", "rationale": "review"})
    with pytest.raises(WorkflowError, match="REFERENCE"):
        api.call("start_document_ingestion_job", {**arguments, "source_path": "../outside"})


def test_s1_catalog_readback_rejects_descriptive_dataset_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    monkeypatch.setenv("ORION_S1_CATALOG_READBACK_REQUIRED", "true")
    monkeypatch.setenv(
        "ORION_SOURCE_DATA_READER_URL",
        "postgresql://reader:secret@localhost/orion_source_data",
    )
    monkeypatch.setattr(service, "_read_s1_catalog_records", lambda *_args: {})
    dataset = {
        "project_id": "ontology-project-test",
        "dataset_id": "chat2db-description-only",
        "source_sha256": "sha256:" + "d" * 64,
        "status": "READY",
        "row_count": 10,
    }

    with pytest.raises(WorkflowGateError) as exc_info:
        service._validate_s1_catalog_readback(
            project_id="ontology-project-test",
            datasets=[dataset],
        )

    assert exc_info.value.gate_id == "G-S1-CATALOG-READBACK"
    assert "未在 orion_catalog 中找到" in str(exc_info.value)


def test_s1_catalog_readback_requires_promoted_production_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    monkeypatch.setenv("ORION_S1_CATALOG_READBACK_REQUIRED", "true")
    monkeypatch.setenv(
        "ORION_SOURCE_DATA_READER_URL",
        "postgresql://reader:secret@localhost/orion_source_data",
    )
    dataset = {
        "project_id": "ontology-project-test",
        "dataset_id": "DS-TEST",
        "source_sha256": "sha256:" + "d" * 64,
        "status": "READY",
        "row_count": 10,
    }
    monkeypatch.setattr(
        service,
        "_read_s1_catalog_records",
        lambda *_args: {
            "DS-TEST": {
                "project_id": "ontology-project-test",
                "source_sha256": "sha256:" + "d" * 64,
                "row_count": 10,
                "ready": True,
                "promoted": True,
                "production_evidence": False,
            }
        },
    )

    with pytest.raises(WorkflowGateError) as exc_info:
        service._validate_s1_catalog_readback(
            project_id="ontology-project-test",
            datasets=[dataset],
        )

    assert exc_info.value.gate_id == "G-S1-CATALOG-READBACK"
    assert "生产证据标记" in str(exc_info.value)


def test_s1_catalog_readback_accepts_matching_promoted_dataset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    monkeypatch.setenv("ORION_S1_CATALOG_READBACK_REQUIRED", "true")
    monkeypatch.setenv(
        "ORION_SOURCE_DATA_READER_URL",
        "postgresql://reader:secret@localhost/orion_source_data",
    )
    dataset = {
        "project_id": "ontology-project-test",
        "dataset_id": "DS-TEST",
        "source_sha256": "sha256:" + "d" * 64,
        "status": "READY",
        "row_count": 10,
    }
    monkeypatch.setattr(
        service,
        "_read_s1_catalog_records",
        lambda *_args: {
            "DS-TEST": {
                "project_id": "ontology-project-test",
                "source_sha256": "sha256:" + "d" * 64,
                "row_count": 10,
                "ready": True,
                "promoted": True,
                "production_evidence": True,
            }
        },
    )

    service._validate_s1_catalog_readback(
        project_id="ontology-project-test",
        datasets=[dataset],
    )


def test_s1_normalizes_table_array_schema_and_generates_report(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="数组 Schema 兼容测试",
        domain="供应链数据理解",
    )["project_id"]
    _record_s0(service, project_id)

    s1_contract = _production_s1_contract(project_id, {"sc_suppliers": 10})
    result = service.record_data_understanding(
        project_id=project_id,
        datasource_inventory=s1_contract["datasource_inventory"],
        schema_snapshot=[
            {
                "table": "sc_suppliers",
                "columns": "id bigint, code varchar, name varchar",
            }
        ],
        data_profile=s1_contract["data_profile"],
        relation_candidates=[],
        evidence_sql=s1_contract["evidence_sql"],
    )

    project_dir = tmp_path / project_id
    saved_schema = json.loads(
        (project_dir / "01-data-understanding/schema-snapshot.json").read_text(encoding="utf-8")
    )
    assert result["current_stage"] == "S2"
    assert result["stage_statuses"]["S1"] == "PASSED"
    assert saved_schema["tables"][0]["name"] == "sc_suppliers"
    assert saved_schema["table_columns"]["sc_suppliers"] == ["id", "code", "name"]
    assert (project_dir / "01-data-understanding/data-understanding-report.html").exists()


def test_s1_rejects_invalid_schema_before_writing_stage_artifacts(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(project_name="非法 Schema 测试", domain="测试")[
        "project_id"
    ]
    _record_s0(service, project_id)

    with pytest.raises(WorkflowGateError, match="表定义必须是对象"):
        service.record_data_understanding(
            project_id=project_id,
            datasource_inventory={"selected": "ontology_agent", "read_only": True},
            schema_snapshot=["sc_suppliers"],
            data_profile={},
            relation_candidates=[],
            evidence_sql=[{"id": "SQL-001", "sql": "SELECT 1"}],
        )

    stage_dir = tmp_path / project_id / "01-data-understanding"
    assert not (stage_dir / "datasource-inventory.json").exists()
    assert service.get_status(project_id)["stage_statuses"]["S1"] == "RUNNING"


def _record_s1_in_spawned_process(
    workflow_root: str,
    project_id: str,
    expected_revision: int,
    supplier_count: int,
    role: str,
    holder_acquired: Any,
    contender_started: Any,
    release_holder: Any,
    result_queue: Any,
) -> None:
    """Use an independent service/process to prove the filesystem lock boundary."""

    service = OntologyWorkflowService(
        workflow_root,
        metadata_database_url="",
        metadata_required=False,
    )
    original_lock = service._project_operation_lock

    @contextmanager
    def observed_lock(project_dir: Path):
        if role == "contender":
            contender_started.set()
        with original_lock(project_dir):
            if role == "holder":
                holder_acquired.set()
                if not release_holder.wait(timeout=10):
                    raise TimeoutError("父进程未释放并发测试 holder")
            yield

    service._project_operation_lock = observed_lock  # type: ignore[method-assign]
    try:
        result = _record_s1(
            service,
            project_id,
            supplier_count=supplier_count,
            expected_revision=expected_revision,
        )
    except Exception as exc:  # pragma: no cover - asserted through the process queue
        result_queue.put(
            {
                "status": "ERROR",
                "error_type": type(exc).__name__,
                "message": str(exc),
            }
        )
    else:
        result_queue.put(
            {
                "status": "OK",
                "revision": result["revision"],
                "current_stage": result["current_stage"],
            }
        )


def test_cross_process_s1_same_revision_commits_once_and_keeps_audit_verified(
    tmp_path: Path,
) -> None:
    workflow_root = tmp_path / "workflows"
    service = OntologyWorkflowService(workflow_root)
    project_id = service.create_project(
        project_name="跨进程并发写入验收工程",
        domain="cross-process-lock",
        intake_mode="DATABASE_ONLY",
    )["project_id"]
    scoped = service.record_s0_scope_decision(
        project_id=project_id,
        intake_mode="DATABASE_ONLY",
        rationale="仅使用受控数据库结构验证跨进程写入锁。",
        decided_by="pytest",
    )
    expected_revision = int(scoped["revision"])

    context = multiprocessing.get_context("spawn")
    holder_acquired = context.Event()
    contender_started = context.Event()
    release_holder = context.Event()
    result_queue = context.Queue()
    common_arguments = (
        str(workflow_root),
        project_id,
        expected_revision,
    )
    holder = context.Process(
        target=_record_s1_in_spawned_process,
        args=(
            *common_arguments,
            11,
            "holder",
            holder_acquired,
            contender_started,
            release_holder,
            result_queue,
        ),
    )
    contender = context.Process(
        target=_record_s1_in_spawned_process,
        args=(
            *common_arguments,
            22,
            "contender",
            holder_acquired,
            contender_started,
            release_holder,
            result_queue,
        ),
    )

    try:
        holder.start()
        assert holder_acquired.wait(timeout=8), "holder 没有取得工程 flock"
        contender.start()
        assert contender_started.wait(timeout=8), "contender 没有尝试取得工程 flock"
        release_holder.set()
        holder.join(timeout=12)
        contender.join(timeout=12)
        assert holder.exitcode == 0
        assert contender.exitcode == 0
        results = [result_queue.get(timeout=5), result_queue.get(timeout=5)]
    finally:
        release_holder.set()
        for process in (holder, contender):
            if process.pid is None:
                continue
            if process.is_alive():
                process.terminate()
            process.join(timeout=3)
        result_queue.close()
        result_queue.join_thread()

    assert sorted(item["status"] for item in results) == ["ERROR", "OK"]
    conflict = next(item for item in results if item["status"] == "ERROR")
    assert conflict["error_type"] == "WorkflowError"
    assert "当前阶段" in conflict["message"] or "状态已变化" in conflict["message"]

    final_status = OntologyWorkflowService(workflow_root).get_status(project_id)
    assert final_status["revision"] == expected_revision + 1
    assert final_status["current_stage"] == "S2"
    profile = json.loads(
        (workflow_root / project_id / "01-data-understanding/data-profile.json").read_text(
            encoding="utf-8"
        )
    )
    supplier_profile = next(item for item in profile["tables"] if item["table"] == "sc_suppliers")
    assert supplier_profile["row_count"] in {11, 22}

    integrity = OntologyWorkflowService(workflow_root).verify_project_integrity(project_id)
    assert integrity["status"] == "PASSED"
    assert integrity["audit"]["status"] == "VERIFIED"
    assert integrity["audit"]["errors"] == []
    events = [
        json.loads(line)
        for line in (workflow_root / project_id / "events/agent-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert all(
        current["previous_event_hash"] == previous["event_hash"]
        for previous, current in zip(events, events[1:], strict=False)
    )


@pytest.mark.parametrize(
    "method_name",
    [
        "archive_project",
        "restore_project",
        "record_document_evidence",
        "record_s0_scope_decision",
        "record_data_understanding",
        "record_data_understanding_from_datasets",
        "record_semantic_candidates",
        "prepare_mapping_review",
        "resolve_confirmation",
        "resolve_confirmation_option",
        "retry_failed_stage",
        "record_ontology_design",
        "generate_ontology_design",
        "prepare_ontology_design_review",
        "resolve_competency_question_review",
        "preview_stage_rollback",
        "reopen_stage_for_correction",
        "record_ontology_build",
        "record_quality_validation",
        "publish_ontology_package",
        "sync_published_ontology_to_semantica",
        "defer_ontology_publication",
        "resume_ontology_publication",
        "revoke_ontology_release",
        "create_revision_from_release",
        "export_published_delivery_package",
    ],
)
def test_every_public_project_mutator_declares_a_cross_process_lock(
    method_name: str,
) -> None:
    source = inspect.getsource(getattr(OntologyWorkflowService, method_name))
    if method_name == "record_quality_validation":
        # This public entry delegates both direct and preflight commits to one
        # implementation; its real lock is covered by the S6 public-entry test.
        assert "return self._record_quality_validation_payload(" in source
        source = inspect.getsource(OntologyWorkflowService._record_quality_validation_payload)
        assert "with self._project_mutation_lock(project_id):" in source
    assert "_project_mutation_lock" in source or "_project_operation_lock" in source, (
        f"{method_name} 绕过了跨进程工程写锁"
    )


def _business_instance_contract(name, refs, mode="SOURCE_MAPPING"):
    return {
        "business_role": "BUSINESS_OBJECT", "instance_meaning": f"一个具有稳定编号的{name}",
        "generation_mode": mode, "identity_rule": "来源范围与主键组合，重复行合并为同一对象",
        "mapping_refs": refs, "empty_policy": "REQUIRE_NONEMPTY",
        "empty_reason": "验收来源包含该业务对象，必须生成显式类型成员",
    }


def _record_s2(service: OntologyWorkflowService, project_id: str) -> dict:
    return service.record_semantic_candidates(
        project_id=project_id,
        ontology_candidates=[
            {
                "id": "CLASS-SUPPLIER",
                "name": "Supplier",
                "instance_contract": _business_instance_contract("供应商", ["MAP-SUPPLIER"]),
                "kind": "CLASS",
                "status": "DATABASE_FACT",
                "confidence": 0.99,
                "source_refs": ["table:sc_suppliers", "SQL-001"],
            },
            {
                "id": "REL-ACTUALLY-SUPPLIES",
                "name": "actuallySupplies",
                "kind": "OBJECT_PROPERTY",
                "status": "NEEDS_HUMAN_CONFIRMATION",
                "confidence": 0.55,
                "source_refs": ["REL-001"],
            },
            {
                "id": "CLASS-SUPPLIER-POLICY",
                "name": "SupplierPolicy",
                "instance_contract": _business_instance_contract("供应商政策", [], "DOCUMENT_FACTS"),
                "kind": "CLASS",
                "status": "DOCUMENT_EVIDENCE",
                "confidence": 0.96,
                "source_refs": ["EVD-001", "DOC-001#准入条件"],
            },
        ],
        business_rule_candidates=[],
    )


def _strict_runtime(runtime: dict[str, Any]) -> dict[str, Any]:
    """Make legacy test fixtures satisfy the schema-v2 explicit capability contract."""

    normalized = dict(runtime)
    capabilities = dict(normalized.get("query_capabilities") or {})
    for name, query in dict(normalized.get("ontop_queries") or {}).items():
        result_fields = list(
            dict.fromkeys(
                re.findall(
                    r"\?(\w+)",
                    str(query).split("WHERE", 1)[0],
                    flags=re.IGNORECASE,
                )
            )
        ) or ["result"]
        capabilities.setdefault(
            name,
            {
                "description_zh": "读取已审核本体映射对应的实时业务数据。",
                "parameters": {},
                "result_fields": result_fields,
                "question_examples": ["查询当前实时业务数据"],
                "validation_cases": [
                    {
                        "id": "release_smoke",
                        "question": "查询当前实时业务数据",
                        "parameters": {},
                        "expected_fields": result_fields,
                        "min_rows": 0,
                    }
                ],
            },
        )
    normalized["query_capabilities"] = capabilities
    if normalized.get("reasoning_capabilities"):
        normalized.setdefault("reasoning_requirement", "REQUIRED")
        normalized.pop("reasoning_not_applicable_reason", None)
    else:
        normalized.setdefault("reasoning_requirement", "NOT_APPLICABLE")
        normalized.setdefault(
            "reasoning_not_applicable_reason",
            "当前测试运行时仅验证只读检索与聚合，不产生新的业务判定。",
        )
    normalized.setdefault(
        "document_query_capabilities",
        ["current_full_text_search", "reviewed_entity_evidence"],
    )
    return normalized


def _default_realtime_runtime() -> dict[str, Any]:
    return _strict_runtime(
        {
            "ontop_deployment_id": "ontop-test-supplier-runtime",
            "database_access_mode": "READ_ONLY",
            "prepared_by": "test-engineer",
            "mapping_obda": """[PrefixDeclaration]
ex: https://example.com/test-runtime#

[MappingDeclaration] @collection [[
mappingId SupplierRuntime
target <https://example.com/test-runtime/supplier-{code}> a ex:Supplier .
source SELECT code FROM sc_suppliers

mappingId SupplierRelationRuntime
target <https://example.com/test-runtime/order-{id}> ex:actuallySupplies <https://example.com/test-runtime/supplier-{supplier_id}> ; ex:orderedFrom <https://example.com/test-runtime/supplier-{supplier_id}> .
source SELECT id, supplier_id FROM sc_purchase_orders
]]
""",
            "ontop_queries": {
                "suppliers_live": """PREFIX ex: <https://example.com/test-runtime#>
SELECT ?supplier WHERE { ?supplier a ex:Supplier . }
LIMIT 10
"""
            },
        }
    )


def test_s3_rejects_not_applicable_when_s2_has_business_rule_candidate(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="推理政策门禁工程",
        domain="reasoning-policy",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    service.record_semantic_candidates(
        project_id=project_id,
        ontology_candidates=[
            {
                "id": "CLASS-SUPPLIER",
                "name": "Supplier",
                "kind": "CLASS",
                "status": "DATABASE_FACT",
                "confidence": 0.99,
                "source_refs": ["table:sc_suppliers", "SQL-001"],
            },
            {
                "id": "REL-ACTUALLY-SUPPLIES",
                "name": "actuallySupplies",
                "kind": "OBJECT_PROPERTY",
                "status": "DATABASE_FACT",
                "confidence": 0.9,
                "source_refs": ["REL-001"],
            },
            {
                "id": "CLASS-SUPPLIER-POLICY",
                "name": "SupplierPolicy",
                "kind": "CLASS",
                "status": "DOCUMENT_EVIDENCE",
                "confidence": 0.96,
                "source_refs": ["EVD-001"],
            },
        ],
        business_rule_candidates=[
            {
                "id": "RULE-SUPPLIER-RISK-001",
                "name": "供应商风险复核规则",
                "rule_type": "DERIVED_CLASSIFICATION",
                "description": "供应商命中风险事实后应派生为待复核供应商。",
                "status": "DATABASE_FACT",
                "source_refs": ["SQL-001"],
                "formal_expression": (
                    "IF SupplierRiskFact(?supplier) THEN NeedsSupplierReview(?supplier)"
                ),
                "premise_predicates": ["SupplierRiskFact"],
                "conclusion_predicate": "NeedsSupplierReview",
                "test_cases": [
                    {
                        "id": "positive",
                        "case_type": "POSITIVE",
                        "facts": ["SupplierRiskFact(supplier-1)"],
                        "expected_outcome": "FIRE",
                    },
                    {
                        "id": "negative",
                        "case_type": "NEGATIVE",
                        "facts": ["SupplierApproved(supplier-1)"],
                        "expected_outcome": "NO_FIRE",
                    },
                    {
                        "id": "boundary",
                        "case_type": "BOUNDARY",
                        "facts": ["SupplierRiskPending(supplier-1)"],
                        "expected_outcome": "NO_FIRE",
                    },
                ],
            }
        ],
    )

    with pytest.raises(WorkflowGateError, match="不能把生产推理声明为不适用"):
        _prepare_s3(
            service,
            project_id,
            realtime_runtime=_default_realtime_runtime(),
        )


def test_s3_requires_every_confirmed_production_rule_to_be_runtime_bound(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-coverage-project"
    rules_dir = project_dir / "02-semantic-recognition"
    rules_dir.mkdir(parents=True)
    (rules_dir / "business-rule-candidates.json").write_text(
        json.dumps(
            [
                {
                    "id": "RULE-BOUND",
                    "status": "DATABASE_FACT",
                    "formal_expression": "IF SupplierObserved(?x) THEN SupplierKnown(?x)",
                },
                {
                    "id": "RULE-MISSING",
                    "status": "DOCUMENT_EVIDENCE",
                    "formal_expression": "IF SupplierRisk(?x) THEN NeedsReview(?x)",
                },
                {
                    "id": "RULE-REVIEW",
                    "status": "AI_INFERENCE",
                    "review_required": True,
                },
            ]
        ),
        encoding="utf-8",
    )
    runtime = _default_realtime_runtime()
    runtime["reasoning_requirement"] = "REQUIRED"
    runtime.pop("reasoning_not_applicable_reason", None)
    runtime["reasoning_capabilities"] = {
        "bound_rule": {
            "execution_scope": "FULL_QUERY_RESULT",
            "source_rule_ids": ["RULE-BOUND"],
            "rules": [
                {
                    "rule_id": "RULE-BOUND",
                    "expression": "IF SupplierObserved(?x) THEN SupplierKnown(?x)",
                }
            ],
        }
    }
    payload = {
        "mapping_draft": {
            "mappings": [
                {
                    "id": "MAP-SUPPLIER",
                    "source": "sc_suppliers",
                    "target": "Supplier",
                    "target_label_zh": "供应商",
                    "target_comment_zh": "由供应商主数据映射形成的业务对象。",
                    "mapping_type": "TABLE_TO_CLASS",
                    "source_refs": ["table:sc_suppliers"],
                }
            ]
        },
        "confirmations": [],
        "automatic_decisions": [],
        "realtime_runtime": runtime,
    }

    mismatched = json.loads(json.dumps(payload))
    mismatched["realtime_runtime"]["reasoning_capabilities"]["bound_rule"]["rules"][0][
        "expression"
    ] = "IF SupplierObserved(?x) THEN NeedsReview(?x)"
    with pytest.raises(WorkflowGateError, match="与 S2 正式表达式不一致"):
        service._validate_s3(
            mismatched,
            intake_mode="DATABASE_ONLY",
            project_dir=project_dir,
        )

    with pytest.raises(WorkflowGateError, match="RULE-MISSING"):
        service._validate_s3(
            payload,
            intake_mode="DATABASE_ONLY",
            project_dir=project_dir,
        )

    registered = json.loads((rules_dir / "business-rule-candidates.json").read_text())
    registered[0]["formal_expression"] = (
        "IF SupplierObserved(?x) AND NOT SubmittedMaterial(?x) THEN SupplierKnown(?x)"
    )
    registered[0]["closed_world_inputs"] = [{
        "predicate": "SubmittedMaterial",
        "key_fields": ["workorder_no", "priority", "wo_type", "wo_type"],
        "source_refs": ["table:maintenance_workorder"],
    }]
    (rules_dir / "business-rule-candidates.json").write_text(json.dumps(registered))
    drifted = json.loads(json.dumps(payload))
    drifted["realtime_runtime"]["reasoning_capabilities"]["bound_rule"]["rules"][0][
        "expression"
    ] = registered[0]["formal_expression"]
    drifted["realtime_runtime"]["reasoning_capabilities"]["bound_rule"]["closed_world_inputs"] = [{
        "predicate": "SubmittedMaterial",
        "key_fields": ["workorder_no", "wo_type"],
        "source_refs": ["table:maintenance_workorder"],
    }]
    with pytest.raises(WorkflowGateError, match="S2.*key_fields") as error:
        service._validate_s3(
            drifted,
            intake_mode="DATABASE_ONLY",
            project_dir=project_dir,
        )
    assert error.value.reason_code == "S2_CLOSED_WORLD_INPUT_MISMATCH"
    assert error.value.path == "realtime_runtime.reasoning_capabilities.bound_rule.closed_world_inputs"
    assert '["workorder_no", "priority", "wo_type"]' in str(error.value)

    compatible = json.loads(json.dumps(drifted))
    compatible["realtime_runtime"]["reasoning_capabilities"]["bound_rule"]["closed_world_inputs"][0][
        "key_fields"
    ] = ["workorder_no", "priority", "wo_type"]
    with pytest.raises(WorkflowGateError, match="RULE-MISSING"):
        service._validate_s3(
            compatible,
            intake_mode="DATABASE_ONLY",
            project_dir=project_dir,
        )

    registered[0]["formal_expression"] = "IF SupplierObserved(?x) THEN SupplierKnown(?x)"
    (rules_dir / "business-rule-candidates.json").write_text(json.dumps(registered))
    legacy_positive = json.loads(json.dumps(payload))
    with pytest.raises(WorkflowGateError, match="RULE-MISSING"):
        service._validate_s3(
            legacy_positive,
            intake_mode="DATABASE_ONLY",
            project_dir=project_dir,
        )


def _production_s2_rule_payload() -> dict[str, Any]:
    return {
        "ontology_candidates": [
            {
                "id": "CLASS-RISK-FACT",
                "name": "RiskFact",
                "kind": "CLASS",
                "status": "DATABASE_FACT",
                "source_refs": ["table:risk_fact"],
            }
        ],
        "business_rule_candidates": [
            {
                "id": "RULE-RISK-001",
                "name": "风险复核规则",
                "rule_type": "DERIVED_CLASSIFICATION",
                "description": "风险事实成立时派生待复核结论。",
                "status": "DATABASE_FACT",
                "source_refs": ["table:risk_fact"],
                "formal_expression": "IF RiskFact(?x) THEN NeedsReview(?x)",
                "premise_predicates": ["RiskFact"],
                "conclusion_predicate": "NeedsReview",
                "test_cases": [
                    {
                        "id": "positive",
                        "case_type": "POSITIVE",
                        "facts": ["RiskFact(item-1)"],
                        "expected_outcome": "FIRE",
                    },
                    {
                        "id": "negative",
                        "case_type": "NEGATIVE",
                        "facts": ["Approved(item-1)"],
                        "expected_outcome": "NO_FIRE",
                    },
                    {
                        "id": "boundary",
                        "case_type": "BOUNDARY",
                        "facts": ["RiskPending(item-1)"],
                        "expected_outcome": "NO_FIRE",
                    },
                ],
            }
        ],
    }


def test_s2_rejects_rule_case_that_cannot_produce_claimed_outcome(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-case-project"
    project_dir.mkdir()
    payload = _production_s2_rule_payload()
    payload["business_rule_candidates"][0]["test_cases"][0]["facts"] = ["Approved(item-1)"]

    with pytest.raises(WorkflowGateError, match="用例与声明结果不一致"):
        service._validate_s2(
            payload,
            project_dir=project_dir,
            intake_mode="DATABASE_ONLY",
        )


def test_s2_rejects_rule_predicate_contract_drift(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-predicate-project"
    project_dir.mkdir()
    payload = _production_s2_rule_payload()
    payload["business_rule_candidates"][0]["premise_predicates"] = ["UnrelatedFact"]

    with pytest.raises(WorkflowGateError, match="前提/结论谓词声明不一致"):
        service._validate_s2(
            payload,
            project_dir=project_dir,
            intake_mode="DATABASE_ONLY",
        )


def test_s2_rejects_unused_closed_world_snapshot_on_positive_rule(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-unused-snapshot-project"
    project_dir.mkdir()
    payload = _production_s2_rule_payload()
    payload["business_rule_candidates"][0]["closed_world_inputs"] = [
        {"predicate": "UnusedSnapshot"}
    ]

    with pytest.raises(WorkflowGateError, match="不含 NOT 前提") as error:
        service._validate_s2(payload, project_dir=project_dir, intake_mode="DATABASE_ONLY")
    assert error.value.gate_id == "G-S2-INPUT-INCOMPLETE"


def test_s2_rejects_closed_world_rule_without_complete_input_snapshot(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-negation-project"
    project_dir.mkdir()
    payload = _production_s2_rule_payload()
    rule = payload["business_rule_candidates"][0]
    rule["formal_expression"] = (
        "IF RequiredMaterial(?case,?material) "
        "AND NOT SubmittedMaterial(?case,?material) "
        "THEN MissingMaterial(?case,?material)"
    )
    rule["premise_predicates"] = ["RequiredMaterial", "SubmittedMaterial"]
    rule["conclusion_predicate"] = "MissingMaterial"
    rule["rule_type"] = "CLOSED_WORLD_SET_DIFFERENCE"
    rule["required_capabilities"] = ["CLOSED_WORLD_SET_DIFFERENCE_V1"]

    with pytest.raises(
        WorkflowGateError,
        match="缺少 closed_world_inputs",
    ) as exc_info:
        service._validate_s2(
            payload,
            project_dir=project_dir,
            intake_mode="DATABASE_ONLY",
        )
    assert exc_info.value.gate_id == "G-S2-INPUT-INCOMPLETE"


def test_s2_accepts_closed_world_set_difference_with_complete_snapshot(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-negation-project"
    project_dir.mkdir()
    payload = _production_s2_rule_payload()
    rule = payload["business_rule_candidates"][0]
    rule.update(
        {
            "rule_type": "CLOSED_WORLD_SET_DIFFERENCE",
            "required_capabilities": ["CLOSED_WORLD_SET_DIFFERENCE_V1"],
            "formal_expression": (
                "IF RequiredMaterial(?case,?material) "
                "AND NOT SubmittedMaterial(?case,?material) "
                "THEN MissingMaterial(?case,?material)"
            ),
            "premise_predicates": ["RequiredMaterial", "SubmittedMaterial"],
            "conclusion_predicate": "MissingMaterial",
            "closed_world_inputs": [
                {
                    "predicate": "SubmittedMaterial",
                    "completeness": "COMPLETE_FOR_CASE_AND_SNAPSHOT",
                    "dataset_type": "PRODUCTION_EVIDENCE",
                    "production_evidence": True,
                    "source_refs": ["table:application_submitted_material"],
                    "snapshot_sha256": "sha256:" + "a" * 64,
                    "snapshot_version": "2026-09-04T08:00:00+08:00",
                    "dataset_id": "DS-APPLICATION-MATERIAL-001",
                    "source_sha256": "sha256:" + "b" * 64,
                    "row_count": 1,
                    "key_fields": ["application_id", "material_code"],
                    "field_bindings": {
                        "case_id": "application_id",
                        "service_item_id": "taskcode",
                        "material_code": "material_code",
                        "submit_status": "submit_status",
                        "snapshot_version": "snapshot_version",
                        "source_locator": "source_locator",
                    },
                    "status_filter": ["SUBMITTED", "ACCEPTED"],
                    "pii_scope": {
                        "mode": "MINIMUM_NECESSARY",
                        "allowed_fields": [
                            "application_id",
                            "taskcode",
                            "material_code",
                            "submit_status",
                            "snapshot_version",
                            "source_locator",
                        ],
                        "direct_identifiers_included": False,
                    },
                }
            ],
            "required_set_source": {
                "predicate": "RequiredMaterial",
                "dataset_type": "PRODUCTION_EVIDENCE",
                "production_evidence": True,
                "dataset_id": "DS-MATERIAL-CATALOG-001",
                "source_sha256": "sha256:" + "c" * 64,
                "snapshot_sha256": "sha256:" + "d" * 64,
                "snapshot_version": "2026-09-04T08:00:00+08:00",
                "row_count": 1,
                "source_refs": ["table:service_required_material"],
                "field_bindings": {
                    "service_item_id": "taskcode",
                    "material_code": "material_code",
                    "mandatory": "mandatory",
                    "source_locator": "source_locator",
                },
            },
            "test_cases": [
                {
                    "id": "positive",
                    "case_type": "POSITIVE",
                    "facts": ["RequiredMaterial(case-1,material-a)"],
                    "expected_outcome": "FIRE",
                },
                {
                    "id": "negative",
                    "case_type": "NEGATIVE",
                    "facts": [
                        "RequiredMaterial(case-1,material-a)",
                        "SubmittedMaterial(case-1,material-a)",
                    ],
                    "expected_outcome": "NO_FIRE",
                },
                {
                    "id": "boundary",
                    "case_type": "BOUNDARY",
                    "facts": ["SubmittedMaterial(case-1,material-a)"],
                    "expected_outcome": "NO_FIRE",
                },
            ],
        }
    )

    service._validate_s2(
        payload,
        project_dir=project_dir,
        intake_mode="DATABASE_ONLY",
    )

    duplicate = json.loads(json.dumps(payload))
    duplicate["business_rule_candidates"][0]["closed_world_inputs"][0]["key_fields"].append("material_code")
    with pytest.raises(WorkflowGateError, match="key_fields 含重复列：material_code") as error:
        service._validate_s2(
            duplicate,
            project_dir=project_dir,
            intake_mode="DATABASE_ONLY",
        )
    assert error.value.gate_id == "G-S2-INPUT-INCOMPLETE"


def test_s2_rejects_test_only_closed_world_evidence_for_production_gate(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-test-only-project"
    project_dir.mkdir()
    payload = _production_s2_rule_payload()
    rule = payload["business_rule_candidates"][0]
    rule.update(
        {
            "rule_type": "CLOSED_WORLD_SET_DIFFERENCE",
            "required_capabilities": ["CLOSED_WORLD_SET_DIFFERENCE_V1"],
            "formal_expression": (
                "IF RequiredMaterial(?case,?material) "
                "AND NOT SubmittedMaterial(?case,?material) "
                "THEN MissingMaterial(?case,?material)"
            ),
            "premise_predicates": ["RequiredMaterial", "SubmittedMaterial"],
            "conclusion_predicate": "MissingMaterial",
            "closed_world_inputs": [
                {
                    "predicate": "SubmittedMaterial",
                    "dataset_type": "TEST_ONLY",
                    "production_evidence": False,
                    "dataset_id": "TEST-DS-SUBMITTED",
                    "source_sha256": "sha256:" + "a" * 64,
                    "source_refs": ["test-only:submitted"],
                }
            ],
        }
    )

    with pytest.raises(WorkflowGateError) as exc_info:
        service._validate_s2(
            payload,
            project_dir=project_dir,
            intake_mode="DATABASE_ONLY",
        )
    assert exc_info.value.gate_id == "G-S2-TEST-EVIDENCE-FORBIDDEN"


def test_s2_requires_rules_to_bind_and_cover_reasoning_questions(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-question-lineage-project"
    (project_dir / "00-document-evidence").mkdir(parents=True)
    service._write_json(
        project_dir / "00-document-evidence/cq-intake.json",
        {
            "questions": [
                {
                    "id": "CQ-INTAKE-001",
                    "question": "给定申请人的已提交材料，能否判定还缺少哪些材料？",
                    "expected": "返回缺件材料和判定依据。",
                },
                {
                    "id": "CQ-INTAKE-002",
                    "question": "某事项需要哪些材料？",
                    "expected": "返回材料目录。",
                },
                {
                    "id": "CQ-INTAKE-003",
                    "question": "哪些行为构成档案违法行为及对应责任？",
                    "expected": "返回违法认定、法律责任和处理依据。",
                },
            ]
        },
    )
    payload = _production_s2_rule_payload()

    with pytest.raises(WorkflowGateError, match="必须绑定当前 S0"):
        service._validate_s2(
            payload,
            project_dir=project_dir,
            intake_mode="DATABASE_ONLY",
        )

    payload["business_rule_candidates"][0]["business_question_ids"] = ["CQ-INTAKE-002"]
    with pytest.raises(WorkflowGateError, match="CQ-INTAKE-001.*CQ-INTAKE-003"):
        service._validate_s2(
            payload,
            project_dir=project_dir,
            intake_mode="DATABASE_ONLY",
        )

    payload["business_rule_candidates"][0]["business_question_ids"] = [
        "CQ-INTAKE-001",
        "CQ-INTAKE-003",
    ]
    service._validate_s2(
        payload,
        project_dir=project_dir,
        intake_mode="DATABASE_ONLY",
    )


def _prepare_s3(
    service: OntologyWorkflowService,
    project_id: str,
    *,
    realtime_runtime: dict[str, Any] | None = None,
) -> dict:
    return service.prepare_mapping_review(
        project_id=project_id,
        mapping_draft={
            "mapping_version": "0.1.0-draft",
            "mappings": [
                {
                    "id": "MAP-SUPPLIER",
                    "source": "sc_suppliers",
                    "target": "Supplier",
                    "instance_contract": _business_instance_contract("供应商", ["MAP-SUPPLIER"]),
                    "mapping_type": "TABLE_TO_CLASS",
                    "source_refs": ["table:sc_suppliers"],
                },
                {
                    "id": "MAP-SUPPLIER-RELATION",
                    "source": "sc_purchase_orders.supplier_id",
                    "target": "actuallySupplies",
                    "target_label_zh": "实际供货",
                    "target_comment_zh": "供应商已经完成实际供货的业务关系。",
                    "mapping_type": "FOREIGN_KEY_TO_OBJECT_PROPERTY",
                    "source_refs": ["REL-001", "SQL-001"],
                },
            ],
        },
        confirmations=[
            {
                "id": "CONF-001",
                "title": "订单关系的业务含义",
                "business_question": "采购订单表示已经实际供货，还是仅表示发生了下单？",
                "evidence": {
                    "database_facts": [
                        {
                            "summary": "当前只有采购订单，没有收货或履约事实。",
                            "source_refs": ["REL-001", "SQL-001"],
                        }
                    ],
                    "business_materials": [],
                    "customer_interviews": [],
                    "ai_inference": "订单事实不能证明已经完成实际供货。",
                },
                "confidence": 0.55,
                "options": [
                    {
                        "id": "ordered-from",
                        "label": "建模为下单关系",
                        "summary": "使用 orderedFrom。",
                        "impact": "不把订单错误解释为已经完成供货。",
                        "recommended": True,
                        "mapping_updates": [
                            {
                                "id": "MAP-SUPPLIER-RELATION",
                                "target": "orderedFrom",
                                "target_label_zh": "向供应商下单",
                                "target_comment_zh": "采购订单中的下单关系，不表示已经完成履约。",
                                "definition": "采购订单中的下单关系，不表示已经完成履约",
                            }
                        ],
                    },
                    {
                        "id": "actually-supplies",
                        "label": "建模为实际供货",
                        "summary": "使用 actuallySupplies。",
                        "impact": "可能把尚未履约的订单误判为实际供货。",
                        "recommended": False,
                    },
                ],
                "technical_impact": ["影响订单关系 Mapping 和后续推理规则"],
                "decision_basis": ["数据库证据", "本体工程判断"],
                "affected_mapping_ids": ["MAP-SUPPLIER-RELATION"],
            }
        ],
        automatic_decisions=[
            {
                "id": "AUTO-001",
                "topic": "供应商编号数据类型",
                "decision": "按原始字符串属性保存。",
                "reason": "字段类型可以直接确定，不改变业务含义。",
                "source_refs": ["table:sc_suppliers"],
                "affected_mapping_ids": ["MAP-SUPPLIER"],
            }
        ],
        realtime_runtime=(
            _strict_runtime(realtime_runtime)
            if realtime_runtime is not None
            else _default_realtime_runtime()
        ),
    )


def _complete_s3(
    service: OntologyWorkflowService,
    project_id: str,
    *,
    realtime_runtime: dict[str, Any] | None = None,
) -> dict:
    _prepare_s3(service, project_id, realtime_runtime=realtime_runtime)
    pending = json.loads(
        (service.root / project_id / "03-mapping-review/pending-confirmations.json").read_text(
            encoding="utf-8"
        )
    )
    if any(item["id"] == "CONF-001" for item in pending):
        service.resolve_confirmation_option(
            project_id=project_id,
            confirmation_id="CONF-001",
            selected_option_id="ordered-from",
            decided_by="ontology-engineer",
            rationale="订单表只能证明下单，不能证明已经实际供货。",
        )
    if any(item["id"] == "S3-OVERALL-MAPPING-REVIEW" for item in pending):
        return service.resolve_confirmation_option(
            project_id=project_id,
            confirmation_id="S3-OVERALL-MAPPING-REVIEW",
            selected_option_id="APPROVE-MAPPING",
            decided_by="ontology-engineer",
            rationale="映射范围和业务含义已完成总体复核。",
        )
    status = service.get_status(project_id)
    if (status.get("s3_runtime_review") or {}).get("status") == "AWAITING_RUNTIME_COMPILATION":
        assert status["current_stage"] == "S3"
        assert status["stage_statuses"]["S3"] == "RUNNING"
        # Business decisions must be followed by validation of the effective runtime.
        status = _prepare_s3(service, project_id, realtime_runtime=realtime_runtime)
        assert status["stage_statuses"]["S3"] == "PASSED"
    return status


def test_evidence_backed_policy_blocks_unverified_explicit_s4_query(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="自动评审本体工程",
        domain="auto-review",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)

    after_s3 = service.prepare_mapping_review(
        project_id=project_id,
        mapping_draft={
            "mapping_version": "0.1.0-draft",
            "mappings": [
                {
                    "id": "MAP-SUPPLIER",
                    "source": "sc_suppliers",
                    "target": "Supplier",
                    "mapping_type": "TABLE_TO_CLASS",
                    "source_refs": ["table:sc_suppliers"],
                }
            ],
        },
        confirmations=[],
        automatic_decisions=[],
        realtime_runtime=_default_realtime_runtime(),
        review_policy="AUTO_APPROVE_EVIDENCE_BACKED",
    )
    assert after_s3["stage_statuses"]["S3"] == "PASSED"
    assert after_s3["project_status"] == "S1_S3_READY"
    assert after_s3.get("next_confirmation") is None
    auto_decisions = json.loads(
        (tmp_path / project_id / "03-mapping-review/automatic-decisions.json").read_text(
            encoding="utf-8"
        )
    )
    assert auto_decisions[-1]["id"] == "S3-OVERALL-MAPPING-AUTO"
    assert auto_decisions[-1]["decided_by"] == "ORION_POLICY"

    ontology_design = {
        "ontology_iri": "https://example.com/auto-review",
        "version": "0.1.0",
        "modeling_profile": "TAXONOMY_ONLY",
        "classes": [
            {
                "name": "Supplier",
                "iri": "https://example.com/auto-review#Supplier",
                "source_mapping_ids": ["MAP-SUPPLIER"],
            },
            {
                "name": "SupplierPolicySubject",
                "iri": "https://example.com/auto-review#SupplierPolicySubject",
                "source_mapping_ids": ["MAP-SUPPLIER"],
                "label_zh": "供应商制度适用主体",
                "comment_zh": "依据供应商制度纳入管理范围的业务主体。",
            },
        ],
        "object_properties": [],
        "data_properties": [],
        "constraints": [],
        "competency_questions": [
            {
                "id": "CQ-001",
                "question": "当前有哪些供应商？",
                "sparql": "SELECT ?supplier WHERE { ?supplier a <https://example.com/auto-review#Supplier> . }",
                "expected": "返回已进入本体的供应商。",
                "answer_contract": {
                    "result_assertions": [
                        {"binding": "supplier", "operator": "NE", "expected": ""}
                    ],
                    "boundary_assertions": [
                        {"binding": "supplier", "operator": "NE", "expected": ""}
                    ],
                },
            }
        ],
        "logical_axioms": [
            {
                "id": "AX-SUPPLIER-POLICY-SUBCLASS",
                "axiom_type": "SUBCLASS_OF",
                "child": "https://example.com/auto-review#Supplier",
                "parent": "https://example.com/auto-review#SupplierPolicySubject",
                "source_refs": ["MAP-SUPPLIER", "EVD-001"],
            }
        ],
    }
    after_s4 = service.prepare_ontology_design_review(
        project_id=project_id,
        ontology_design=ontology_design,
        review_policy="AUTO_APPROVE_EVIDENCE_BACKED",
    )
    assert after_s4["stage_statuses"]["S4"] == "BLOCKED_HUMAN"
    assert after_s4["current_stage"] == "S4"
    review = json.loads(
        (tmp_path / project_id / "04-ontology-design/competency-question-review.json").read_text(
            encoding="utf-8"
        )
    )
    assert review["status"] == "PENDING"
    assert review["draft_questions"][0]["coverage_status"] == "NEEDS_REVIEW"
    events = [
        json.loads(line)
        for line in (tmp_path / project_id / "events/agent-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert not any(
        event["event_type"] == "COMPETENCY_QUESTION_REVIEW_AUTO_APPROVED" for event in events
    )


def test_s3_s4_preflight_normalizes_ids_and_compiles_missing_cq_query(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="预检规范化本体工程",
        domain="preflight-normalization",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    mapping_draft = {
        "mapping_version": "0.1.0-draft",
        "mappings": [
            {
                "id": "MAP-SUPPLIER",
                "source": "sc_suppliers",
                "target": "Supplier",
                "target_label_zh": "供应商",
                "target_comment_zh": "提供物料或服务的业务主体。",
                "mapping_type": "TABLE_TO_CLASS",
                "source_refs": ["table:sc_suppliers"],
            }
        ],
    }
    automatic_decisions = [
        {
            "topic": "供应商编号数据类型",
            "decision": "按原始字符串属性保存。",
            "reason": "字段类型可以直接确定，不改变业务含义。",
            "source_refs": ["table:sc_suppliers"],
            "affected_mapping_ids": ["MAP-SUPPLIER"],
        }
    ]
    state_path = tmp_path / project_id / "workflow-state.json"
    events_path = tmp_path / project_id / "events/agent-trace.jsonl"
    state_before = state_path.read_bytes()
    events_before = events_path.read_bytes()

    s3_preflight = service.preflight_stage_submission(
        project_id=project_id,
        stage="S3",
        payload={
            "mapping_draft": mapping_draft,
            "confirmations": [],
            "automatic_decisions": automatic_decisions,
            "realtime_runtime": _default_realtime_runtime(),
            "review_policy": "AUTO_APPROVE_EVIDENCE_BACKED",
        },
    )

    normalized_id = s3_preflight["normalized_payload"]["automatic_decisions"][0]["id"]
    assert s3_preflight["status"] == "PASSED"
    assert normalized_id.startswith("S3-AUTO-")
    assert state_path.read_bytes() == state_before
    assert events_path.read_bytes() == events_before

    after_s3 = service.prepare_mapping_review(
        project_id=project_id,
        mapping_draft=mapping_draft,
        confirmations=[],
        automatic_decisions=automatic_decisions,
        realtime_runtime=_default_realtime_runtime(),
        review_policy="AUTO_APPROVE_EVIDENCE_BACKED",
    )
    assert after_s3["stage_statuses"]["S3"] == "PASSED"
    stored_automatic = json.loads(
        (tmp_path / project_id / "03-mapping-review/automatic-decisions.json").read_text(
            encoding="utf-8"
        )
    )
    assert stored_automatic[0]["id"] == normalized_id

    ontology_design = {
        "ontology_iri": "https://example.com/preflight-normalization",
        "version": "0.1.0",
        "modeling_profile": "TAXONOMY_ONLY",
        "classes": [
            {
                "name": "Supplier",
                "iri": "https://example.com/preflight-normalization#Supplier",
                "source_mapping_ids": ["MAP-SUPPLIER"],
            },
            {
                "name": "SupplierPolicySubject",
                "iri": "https://example.com/preflight-normalization#SupplierPolicySubject",
                "source_mapping_ids": ["MAP-SUPPLIER"],
                "label_zh": "供应商制度适用主体",
                "comment_zh": "依据供应商制度纳入管理范围的业务主体。",
            },
        ],
        "object_properties": [],
        "data_properties": [],
        "constraints": [],
        "competency_questions": [
            {
                "id": "CQ-001",
                "question": "当前有哪些供应商？",
                "expected": "返回已进入本体的供应商。",
                "answer_contract": {
                    "result_assertions": [{"binding": "item", "operator": "NE", "expected": ""}],
                    "boundary_assertions": [{"binding": "item", "operator": "NE", "expected": ""}],
                },
            }
        ],
        "logical_axioms": [
            {
                "id": "AX-SUPPLIER-POLICY-SUBCLASS",
                "axiom_type": "SUBCLASS_OF",
                "child": "https://example.com/preflight-normalization#Supplier",
                "parent": "https://example.com/preflight-normalization#SupplierPolicySubject",
                "source_refs": ["MAP-SUPPLIER", "EVD-001"],
            }
        ],
    }
    s4_preflight = service.preflight_stage_submission(
        project_id=project_id,
        stage="S4",
        payload={"ontology_design": ontology_design},
    )
    assert s4_preflight["status"] == "PASSED", s4_preflight
    compiled_question = s4_preflight["normalized_payload"]["ontology_design"][
        "competency_questions"
    ][0]
    assert compiled_question["sparql"].startswith("SELECT")

    after_s4 = service.prepare_ontology_design_review(
        project_id=project_id,
        ontology_design=ontology_design,
        review_policy="AUTO_APPROVE_EVIDENCE_BACKED",
    )
    assert after_s4["stage_statuses"]["S4"] == "PASSED"
    stored_design = yaml.safe_load(
        (tmp_path / project_id / "04-ontology-design/ontology-design.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert stored_design["competency_questions"][0]["sparql"].startswith("SELECT")


def test_denormalized_value_mappings_generate_business_classes_and_runtime_iris(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="反规范化质检本体工程",
        domain="battery-quality",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    runtime = {
        "ontop_deployment_id": "ontop-battery-quality-001",
        "database_access_mode": "READ_ONLY",
        "prepared_by": "ontology-engineer",
        "mapping_obda": """[PrefixDeclaration]
:	https://example.com/orion/battery/
xsd:	http://www.w3.org/2001/XMLSchema#

[MappingDeclaration] @collection [[
mappingId QualitySample
target :sample-{batch_no} a :QualitySample ; :parameterValue {param_value}^^xsd:decimal .
source SELECT batch_no, param_value FROM orion_data.quality_sample

mappingId Equipment
target :equipment-{equipment_code} a :Equipment .
source SELECT DISTINCT equipment_code FROM orion_data.quality_sample

mappingId ProducedOnEquipment
target :sample-{batch_no} :producedOnEquipment :equipment-{equipment_code} .
source SELECT batch_no, equipment_code FROM orion_data.quality_sample
]]
""",
        "ontop_queries": {
            "abnormal_samples": """PREFIX : <https://example.com/orion/battery/>
SELECT ?sample ?value WHERE { ?sample a :QualitySample ; :parameterValue ?value . }
LIMIT 10
"""
        },
    }
    mappings = [
        {
            "id": "MAP-QUALITY-SAMPLE",
            "source": "SELECT batch_no FROM orion_data.quality_sample",
            "target": "QualitySample",
            "target_label_zh": "质检样本",
            "target_comment_zh": "参与质量参数检测的电芯样本。",
            "mapping_type": "SQL_TO_CLASS",
            "source_refs": ["SQL-001"],
        },
        {
            "id": "MAP-EQUIPMENT",
            "source": 'orion_data."quality_sample".equipment_code',
            "target": "Equipment",
            "target_label_zh": "设备",
            "target_comment_zh": "由质检大表设备编码去重形成的生产设备业务对象。",
            "mapping_type": "COLUMN_VALUE_TO_CLASS",
            "source_refs": ["SQL-002"],
        },
        {
            "id": "MAP-PRODUCED-ON-EQUIPMENT",
            "source": 'orion_data."quality_sample".equipment_code',
            "target": "producedOnEquipment",
            "target_label_zh": "生产于设备",
            "target_comment_zh": "质检样本与实际生产设备之间的业务关系。",
            "mapping_type": "COLUMN_VALUE_TO_OBJECT_PROPERTY",
            "domain": "QualitySample",
            "range": "Equipment",
            "source_refs": ["SQL-002"],
        },
        {
            "id": "MAP-PARAMETER-VALUE",
            "source": 'orion_data."quality_sample".param_value',
            "target": "parameterValue",
            "target_label_zh": "参数值",
            "target_comment_zh": "质检样本对应检测参数的数值。",
            "mapping_type": "COLUMN_TO_DATA_PROPERTY",
            "domain": "QualitySample",
            "datatype": "http://www.w3.org/2001/XMLSchema#decimal",
            "source_refs": ["SQL-001"],
        },
    ]

    after_s3 = service.prepare_mapping_review(
        project_id=project_id,
        mapping_draft={"mapping_version": "0.2.0-draft", "mappings": mappings},
        confirmations=[],
        automatic_decisions=[],
        realtime_runtime=_strict_runtime(runtime),
        review_policy="AUTO_APPROVE_EVIDENCE_BACKED",
    )
    assert after_s3["stage_statuses"]["S3"] == "PASSED"

    after_s4 = service.generate_ontology_design(
        project_id=project_id,
        competency_questions=[
            {
                "id": "CQ-001",
                "question": "哪些质检样本生产于哪些设备？",
                "sparql": (
                    "SELECT ?source ?target WHERE { "
                    "?source <https://example.com/orion/battery/producedOnEquipment> ?target . "
                    "} LIMIT 100"
                ),
                "expected": "返回质检样本及其生产设备，并验证指定正例与边界值。",
                "answer_contract": {
                    "result_assertions": [
                        {
                            "binding": "source",
                            "operator": "EQ",
                            "expected": "urn:test:quality-sample:1",
                        }
                    ],
                    "boundary_assertions": [
                        {
                            "binding": "target",
                            "operator": "EQ",
                            "expected": "urn:test:equipment:1",
                        }
                    ],
                    "required_business_dimensions": [
                        {
                            "dimension": "subject",
                            "label_zh": "质检样本",
                            "binding": "source",
                            "ontology_term": "https://example.com/orion/battery/QualitySample",
                            "evidence_refs": ["MAP-QUALITY-SAMPLE"],
                        },
                        {
                            "dimension": "related_object",
                            "label_zh": "生产设备",
                            "binding": "target",
                            "ontology_term": "https://example.com/orion/battery/Equipment",
                            "path": "https://example.com/orion/battery/producedOnEquipment",
                            "evidence_refs": ["MAP-PRODUCED-ON-EQUIPMENT"],
                        },
                    ],
                },
            }
        ],
        logical_axioms=[
            {
                "id": "AX-QUALITY-EQUIPMENT-DISJOINT",
                "axiom_type": "DISJOINT_WITH",
                "class": "https://example.com/orion/battery/QualitySample",
                "other": "https://example.com/orion/battery/Equipment",
                "source_refs": ["MAP-QUALITY-SAMPLE", "MAP-EQUIPMENT"],
            }
        ],
        review_policy="AUTO_APPROVE_EVIDENCE_BACKED",
    )
    assert after_s4["stage_statuses"]["S4"] == "PASSED"
    design = yaml.safe_load(
        (tmp_path / project_id / "04-ontology-design/ontology-design.yaml").read_text(
            encoding="utf-8"
        )
    )
    classes = {item["name"]: item for item in design["classes"]}
    properties = {item["name"]: item for item in design["object_properties"]}
    values = {item["name"]: item for item in design["data_properties"]}
    assert classes["Equipment"]["iri"] == "https://example.com/orion/battery/Equipment"
    assert properties["producedOnEquipment"]["domain"] == classes["QualitySample"]["iri"]
    assert properties["producedOnEquipment"]["range"] == classes["Equipment"]["iri"]
    assert values["parameterValue"]["domain"] == classes["QualitySample"]["iri"]
    assert values["parameterValue"]["range"].endswith("#decimal")


def test_s3_rejects_ontop_runtime_that_omits_semantic_mapping_target(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(project_name="运行时覆盖门禁工程", domain="runtime")[
        "project_id"
    ]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)

    with pytest.raises(WorkflowGateError, match="OBDA 未覆盖正式 Mapping 目标"):
        service.prepare_mapping_review(
            project_id=project_id,
            mapping_draft={
                "mappings": [
                    {
                        "id": "MAP-EQUIPMENT",
                        "source": "sc_suppliers.equipment_code",
                        "target": "Equipment",
                        "target_label_zh": "设备",
                        "target_comment_zh": "生产设备业务对象。",
                        "mapping_type": "COLUMN_VALUE_TO_CLASS",
                        "source_refs": ["SQL-001"],
                    }
                ]
            },
            confirmations=[],
            automatic_decisions=[],
            realtime_runtime=_strict_runtime(
                {
                    "ontop_deployment_id": "ontop-runtime-missing-target",
                    "database_access_mode": "READ_ONLY",
                    "prepared_by": "ontology-engineer",
                    "mapping_obda": """[PrefixDeclaration]
:	https://example.com/runtime#
[MappingDeclaration] @collection [[
mappingId Other
target :other-{id} a :Other .
source SELECT id FROM sc_suppliers
]]
""",
                    "ontop_queries": {
                        "other_live": "SELECT ?item WHERE { ?item a <https://example.com/runtime#Other> . }"
                    },
                }
            ),
            review_policy="AUTO_APPROVE_EVIDENCE_BACKED",
        )


def test_s1_to_s3_blocks_persists_and_resumes(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(project_name="供应链本体", domain="supply-chain")
    project_id = created["project_id"]

    assert created["current_stage"] == "S0"
    assert created["stage_statuses"]["S0"] == "RUNNING"

    s0 = _record_s0(service, project_id)
    assert s0["current_stage"] == "S1"
    assert s0["stage_statuses"]["S0"] == "PASSED"
    s0_report = tmp_path / project_id / "00-document-evidence/document-evidence-report.html"
    assert s0_report.exists()
    s0_html = s0_report.read_text(encoding="utf-8")
    assert "S0 资料接入与证据整理报告" in s0_html
    assert "已通过" in s0_html
    assert "状态码：PASSED" not in s0_html
    assert "prefers-reduced-motion: reduce" in s0_html
    assert (s0_report.parent / "structured-markdown/供应商管理制度.md").exists()
    assert (s0_report.parent / "evidence-index.json").exists()

    s1 = _record_s1(service, project_id)
    assert s1["current_stage"] == "S2"
    assert s1["stage_statuses"]["S1"] == "PASSED"
    s1_report = tmp_path / project_id / "01-data-understanding/data-understanding-report.html"
    assert s1_report.exists()
    s1_html = s1_report.read_text(encoding="utf-8")
    assert s1_html.startswith("<!-- Generated by Trae Work -->")
    assert "ORION report renderer v6.0 chinese-stage-summary" in s1_html[:200]
    assert "S1 数据理解报告" in s1_html
    assert "本阶段总结" in s1_html
    assert "必要技术标识说明" in s1_html
    assert '<details class="language-guide reveal">' in s1_html
    assert "本阶段的人工智能参与边界" in s1_html
    assert "数据库结构与数据规模" in s1_html
    assert "业务关系" in s1_html
    assert "数据质量与证据边界" in s1_html
    assert "查询证据台账（SQL）" in s1_html
    assert s1_html.index("数据质量与证据边界") < s1_html.index("必要技术标识说明")
    assert "ORION_REPORT_SELF_CONTAINED" in s1_html[:260]
    assert "data:font/ttf;base64," not in s1_html
    assert "--ease: cubic-bezier(.2, .6, .35, 1)" in s1_html
    assert "IntersectionObserver" in s1_html
    assert '<script src="' not in s1_html
    assert '<link rel="stylesheet"' not in s1_html
    assert 'class="metric reveal"' in s1_html
    assert "data-count" in s1_html
    assert "https://cdn." not in s1_html
    assert (s1_report.parent / "assets/s1-charts.js").exists()
    assert not (s1_report.parent / "_shared").exists()

    s2 = _record_s2(service, project_id)
    assert s2["current_stage"] == "S3"
    assert s2["stage_statuses"]["S2"] == "PASSED"
    s2_report = tmp_path / project_id / "02-semantic-recognition/business-semantics-report.html"
    assert s2_report.exists()
    s2_html = s2_report.read_text(encoding="utf-8")
    assert s2_html.startswith("<!-- Generated by Trae Work -->")
    assert "ORION report renderer v6.0 chinese-stage-summary" in s2_html[:200]
    assert "S2 业务语义识别报告" in s2_html
    assert "数据库事实" in s2_html
    assert "人工智能辅助理解" in s2_html
    assert "业务类候选（共" in s2_html
    assert "业务关系候选（共" in s2_html
    assert "证据可追溯性" in s2_html
    assert "S3 语义映射评审就绪度" in s2_html
    assert (s2_report.parent / "assets/s2-charts.js").exists()
    charts_js = (s2_report.parent / "assets/s2-charts.js").read_text(encoding="utf-8")
    assert "animation = false" in charts_js
    assert "appendToBody: true" in charts_js
    assert "window.addEventListener('resize'" in charts_js

    blocked = _prepare_s3(service, project_id)
    assert blocked["project_status"] == "BLOCKED_HUMAN"
    assert blocked["stage_statuses"]["S3"] == "BLOCKED_HUMAN"
    assert blocked["blocking"]["gate"] == "GATE-1"
    assert blocked["next_confirmation"]["id"] == "CONF-001"
    assert blocked["confirmation_progress"] == {"current": 1, "total": 2, "resolved": 0}

    summary, _ = OrionWorkflowTools(service).call(
        "get_ontology_workflow_status", {"project_id": project_id}
    )
    assert "S3 建模决策 1/2" in summary
    assert "当前只有采购订单，没有收货或履约事实" in summary
    assert "建模为下单关系（推荐）" in summary
    assert "置信度**：55%" in summary

    restarted = OntologyWorkflowService(tmp_path)
    resumed = restarted.get_status(project_id)
    assert resumed["project_status"] == "BLOCKED_HUMAN"
    assert resumed["next_confirmation"]["question"] == blocked["next_confirmation"]["question"]

    first_decision = restarted.resolve_confirmation(
        project_id=project_id,
        confirmation_id="CONF-001",
        decision="改为 orderedFrom，等有履约事实后再使用 actuallySupplies。",
        decided_by="test-operator",
        rationale="订单事实不能证明已经完成实际供货。",
        selected_option_id="ordered-from",
        mapping_updates=[
            {
                "id": "MAP-SUPPLIER-RELATION",
                "target": "orderedFrom",
                "definition": "采购订单中的下单关系，不表示已经完成履约",
            }
        ],
    )
    assert first_decision["project_status"] == "BLOCKED_HUMAN"
    assert first_decision["next_confirmation"]["id"] == "S3-OVERALL-MAPPING-REVIEW"
    completed = restarted.resolve_confirmation_option(
        project_id=project_id,
        confirmation_id="S3-OVERALL-MAPPING-REVIEW",
        selected_option_id="APPROVE-MAPPING",
        decided_by="test-operator",
        rationale="映射范围和业务含义已完成总体复核。",
    )
    assert completed["project_status"] == "S1_S3_READY"
    assert completed["current_stage"] is None
    assert completed["stage_statuses"]["S3"] == "PASSED"

    project_dir = tmp_path / project_id
    mapping = yaml.safe_load(
        (project_dir / "03-mapping-review/mapping.yaml").read_text(encoding="utf-8")
    )
    assert mapping["review"]["status"] == "REVIEWED"
    assert mapping["review"]["confirmation_count"] == 2
    assert mapping["review"]["automatic_decision_count"] == 1
    relation_mapping = next(
        item for item in mapping["mappings"] if item["id"] == "MAP-SUPPLIER-RELATION"
    )
    assert relation_mapping["target"] == "orderedFrom"
    decisions = (project_dir / "03-mapping-review/decisions.jsonl").read_text(encoding="utf-8")
    assert "orderedFrom" in decisions
    assert '"selected_option_id": "ordered-from"' in decisions
    automatic = json.loads(
        (project_dir / "03-mapping-review/automatic-decisions.json").read_text(encoding="utf-8")
    )
    assert automatic[0]["status"] == "AUTO_ACCEPTED"
    assert automatic[0]["decided_by"] == "ORION_POLICY"
    manifest = json.loads((project_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
    assert any(item["path"] == "03-mapping-review/mapping.yaml" for item in manifest["files"])
    s3_report = project_dir / "03-mapping-review/mapping-review-report.html"
    assert s3_report.exists()
    s3_html = s3_report.read_text(encoding="utf-8")
    assert "本阶段总结" in s3_html
    assert "echarts.init" not in s3_html
    assert s3_report.stat().st_size < 500_000


def test_database_only_project_records_s0_scope_before_s1(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(
        project_name="数据库直连本体",
        domain="database-only",
        intake_mode="DATABASE_ONLY",
    )
    project_id = created["project_id"]

    scoped = service.record_s0_scope_decision(
        project_id=project_id,
        intake_mode="DATABASE_ONLY",
        rationale="本项目本期只接入 PostgreSQL 结构化业务表，不接入 PDF、图片或制度文档。",
        decided_by="test-operator",
        datasource_refs=["database:ontology_agent", "schema:public"],
    )

    assert scoped["current_stage"] == "S1"
    assert scoped["stage_statuses"]["S0"] == "NOT_APPLICABLE"
    assert scoped["stage_statuses"]["S1"] == "RUNNING"
    assert scoped["s0_disposition"]["intake_mode"] == "DATABASE_ONLY"
    assert not scoped.get("legacy_without_s0", False)
    stage_dir = tmp_path / project_id / "00-document-evidence"
    assert (stage_dir / "scope-decision.json").exists()
    report = (stage_dir / "document-evidence-report.html").read_text(encoding="utf-8")
    assert "S0 资料接入范围判定报告" in report
    assert "当前项目不适用该阶段" in report
    assert "状态码：NOT_APPLICABLE" not in report
    events = [
        json.loads(line)
        for line in (tmp_path / project_id / "events/agent-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["event_type"] == "S0_SCOPE_DECISION_RECORDED"
    assert events[-1]["actor"] == "test-operator"
    assert events[-1]["event_hash"].startswith("sha256:")


def test_business_rationale_requires_content_without_character_quota(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    document_project = service.create_project(
        project_name="资料工程",
        domain="资料",
        intake_mode="DOCUMENT_ONLY",
        intake_rationale="测",
    )
    assert document_project["intake_rationale"] == "测"

    database_project = service.create_project(
        project_name="数据工程",
        domain="数据",
        intake_mode="DATABASE_ONLY",
    )
    scoped = service.record_s0_scope_decision(
        project_id=database_project["project_id"],
        intake_mode="DATABASE_ONLY",
        rationale="无",
        decided_by="test-operator",
    )
    assert scoped["s0_disposition"]["rationale"] == "无"

    another_project = service.create_project(
        project_name="空理由校验",
        domain="空理由",
        intake_mode="DATABASE_ONLY",
    )
    with pytest.raises(WorkflowGateError, match="必须说明为何没有文档资料"):
        service.record_s0_scope_decision(
            project_id=another_project["project_id"],
            intake_mode="DATABASE_ONLY",
            rationale=" ",
            decided_by="test-operator",
        )


def test_legacy_audit_event_is_normalized_without_rewriting_source() -> None:
    source = {
        "at": "2026-08-21T17:48:29+08:00",
        "event_id": "EVT-LEGACY-SOURCE",
        "event_type": "PROJECT_CREATED",
        "project_id": "legacy-project",
        "project_status": "IN_PROGRESS",
    }
    normalized = _normalize_event_for_storage(
        source,
        line_number=1,
        previous_event_hash=None,
    )
    second = _normalize_event_for_storage(
        {**source, "event_id": "EVT-LEGACY-SECOND", "event_type": "STAGE_PASSED"},
        line_number=2,
        previous_event_hash=normalized["event_hash"],
    )

    assert "sequence" not in source
    assert "event_hash" not in source
    assert normalized["sequence"] == 1
    assert normalized["event_hash"].startswith("sha256:")
    assert normalized["storage_compatibility"]["source_file_unchanged"] is True
    assert second["sequence"] == 2
    assert second["previous_event_hash"] == normalized["event_hash"]


def test_document_only_project_skips_s1_and_continues_ontology_flow(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(
        project_name="文件资料本体工程",
        domain="document-only",
        intake_mode="DOCUMENT_ONLY",
        intake_rationale="本项目只使用 PDF、Word 和 Excel 文件资料建设业务本体。",
        cq_mode="USER_PLUS_AI",
        initial_competency_questions=[
            {
                "question": "哪些供应商可以供应哪些物料？",
                "expected": "返回供应商、物料以及两者之间的供应关系。",
                "priority": "HIGH",
            }
        ],
    )

    after_s0 = _record_s0(service, created["project_id"])

    assert after_s0["current_stage"] == "S2"
    assert after_s0["project_status"] == "IN_PROGRESS"
    assert after_s0["stage_statuses"]["S0"] == "PASSED"
    assert after_s0["stage_statuses"]["S1"] == "NOT_APPLICABLE"
    assert after_s0["stage_statuses"]["S2"] == "RUNNING"
    assert all(
        after_s0["stage_statuses"][stage] == "PENDING" for stage in ("S3", "S4", "S5", "S6", "S7")
    )
    assert "S2" in after_s0["resume_point"]
    s1_dir = tmp_path / created["project_id"] / "01-data-understanding"
    assert (s1_dir / "scope-decision.json").exists()
    s1_report = (s1_dir / "data-understanding-report.html").read_text(encoding="utf-8")
    assert "流程不会结束" in s1_report
    assert "继续进入 S2" in s1_report

    after_s2 = service.record_semantic_candidates(
        project_id=created["project_id"],
        ontology_candidates=[
            {
                "id": "CLASS-SUPPLIER",
                "name": "Supplier",
                "kind": "CLASS",
                "status": "DOCUMENT_EVIDENCE",
                "confidence": 0.98,
                "source_refs": ["EVD-001", "DOC-001#准入条件"],
            },
            {
                "id": "CLASS-MATERIAL",
                "name": "Material",
                "kind": "CLASS",
                "status": "DOCUMENT_EVIDENCE",
                "confidence": 0.95,
                "source_refs": ["EVD-001"],
            },
            {
                "id": "REL-SUPPLIES",
                "name": "supplies",
                "kind": "OBJECT_PROPERTY",
                "status": "NEEDS_HUMAN_CONFIRMATION",
                "confidence": 0.72,
                "source_refs": ["EVD-001"],
            },
        ],
        business_rule_candidates=[],
    )
    assert after_s2["current_stage"] == "S3"

    blocked = service.prepare_mapping_review(
        project_id=created["project_id"],
        mapping_draft={
            "mapping_version": "0.1.0-draft",
            "mappings": [
                {
                    "id": "MAP-SUPPLIER",
                    "source": "DOC-001#供应商",
                    "target": "Supplier",
                    "mapping_type": "EVIDENCE_TO_CLASS",
                    "source_refs": ["EVD-001"],
                },
                {
                    "id": "MAP-MATERIAL",
                    "source": "DOC-001#物料",
                    "target": "Material",
                    "mapping_type": "EVIDENCE_TO_CLASS",
                    "source_refs": ["EVD-001"],
                },
                {
                    "id": "MAP-SUPPLIES",
                    "source": "DOC-001#供应关系",
                    "target": "supplies",
                    "mapping_type": "EVIDENCE_TO_OBJECT_PROPERTY",
                    "domain": "Supplier",
                    "range": "Material",
                    "source_refs": ["EVD-001"],
                },
            ],
        },
        confirmations=[],
        automatic_decisions=[],
    )
    assert blocked["next_confirmation"]["id"] == "S3-OVERALL-MAPPING-REVIEW"
    assert "资料证据到本体" in blocked["next_confirmation"]["question"]
    ready = service.resolve_confirmation_option(
        project_id=created["project_id"],
        confirmation_id="S3-OVERALL-MAPPING-REVIEW",
        selected_option_id="APPROVE-MAPPING",
        decided_by="test-operator",
        rationale="资料证据与正式映射已逐项核对。",
    )
    assert ready["project_status"] == "S1_S3_READY"

    s4_review = service.generate_ontology_design(
        project_id=created["project_id"],
        competency_questions=[
            {
                "id": "CQ-INTAKE-001",
                "answer_contract": {
                    "result_assertions": [
                        {
                            "binding": "source",
                            "operator": "EQ",
                            "expected": "urn:test:supplier:1",
                        }
                    ],
                    "boundary_assertions": [
                        {
                            "binding": "target",
                            "operator": "EQ",
                            "expected": "urn:test:material:1",
                        }
                    ],
                    "required_business_dimensions": [
                        {
                            "dimension": "subject",
                            "label_zh": "供应商",
                            "binding": "source",
                            "ontology_term": f"https://orion.local/ontology/{created['project_id']}#Supplier",
                            "evidence_refs": ["MAP-SUPPLIER"],
                        },
                        {
                            "dimension": "related_object",
                            "label_zh": "供应物料",
                            "binding": "target",
                            "ontology_term": f"https://orion.local/ontology/{created['project_id']}#Material",
                            "path": f"https://orion.local/ontology/{created['project_id']}#supplies",
                            "evidence_refs": ["MAP-SUPPLIES"],
                        },
                    ],
                },
            }
        ],
        logical_axioms=[
            {
                "id": "AX-SUPPLIER-MATERIAL-DISJOINT",
                "axiom_type": "DISJOINT_WITH",
                "class": (f"https://orion.local/ontology/{created['project_id']}#Supplier"),
                "other": (f"https://orion.local/ontology/{created['project_id']}#Material"),
                "source_refs": ["MAP-SUPPLIER", "MAP-MATERIAL"],
            }
        ],
    )
    assert s4_review["current_stage"] == "S4"
    assert s4_review["stage_statuses"]["S4"] == "BLOCKED_HUMAN"
    design = yaml.safe_load(
        (
            tmp_path / created["project_id"] / "04-ontology-design/ontology-design-draft.yaml"
        ).read_text(encoding="utf-8")
    )
    assert design["generation_policy"] == "DETERMINISTIC_FROM_REVIEWED_EVIDENCE_MAPPING"
    assert len(design["classes"]) == 2
    assert len(design["object_properties"]) == 1
    assert design["competency_questions"][0]["question"] == "哪些供应商可以供应哪些物料？"
    assert design["competency_questions"][0]["source"] == "USER_PROVIDED"
    assert design["competency_questions"][0]["coverage_status"] == "DIRECT"
    assert "#supplies>" in design["competency_questions"][0]["sparql"]
    assert (
        any(item["source"] == "AI_SUPPLEMENT" for item in design["competency_questions"][1:])
        is False
    )

    events = [
        json.loads(line)
        for line in (tmp_path / created["project_id"] / "events/agent-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert any(event["event_type"] == "S1_SCOPE_DECISION_RECORDED" for event in events)
    assert all(event["event_hash"].startswith("sha256:") for event in events)


def test_user_plus_ai_does_not_promote_uncontracted_relation_supplements() -> None:
    questions = OntologyWorkflowService._compile_competency_questions(
        intake_questions=[
            {
                "id": "CQ-INTAKE-001",
                "question": "哪些主体承担归档义务？",
                "expected": "返回主体和义务。",
            }
        ],
        cq_mode="USER_PLUS_AI",
        object_properties=[
            {
                "name": "hasObligation",
                "iri": "https://orion.local/test#hasObligation",
                "domain": "https://orion.local/test#Subject",
                "range": "https://orion.local/test#Obligation",
                "label_zh": "承担义务",
            },
            {
                "name": "aboutArchive",
                "iri": "https://orion.local/test#aboutArchive",
                "domain": "https://orion.local/test#Obligation",
                "range": "https://orion.local/test#Archive",
                "label_zh": "义务所涉档案",
            },
        ],
        classes=[
            {
                "name": "Subject",
                "iri": "https://orion.local/test#Subject",
                "label_zh": "主体",
            },
            {
                "name": "Obligation",
                "iri": "https://orion.local/test#Obligation",
                "label_zh": "义务",
            },
            {
                "name": "Archive",
                "iri": "https://orion.local/test#Archive",
                "label_zh": "档案",
            },
        ],
        ontology_iri="https://orion.local/test",
    )

    assert len(questions) == 1
    assert questions[0]["source_question_id"] == "CQ-INTAKE-001"


def test_competency_question_prefers_explicit_s3_runtime_binding() -> None:
    runtime_query = (
        "SELECT ?ratio ?exceedsThreshold WHERE { "
        "BIND(0.003 AS ?ratio) "
        "BIND(?ratio > 0.002 AS ?exceedsThreshold) }"
    )

    questions = OntologyWorkflowService._compile_competency_questions(
        intake_questions=[
            {
                "id": "CQ-INTAKE-002",
                "question": "超规格数量占总样本比例是否超过 0.2%？",
                "expected": "返回比例并保留 0.2% 边界判断。",
            }
        ],
        cq_mode="USER_PLUS_AI",
        object_properties=[],
        classes=[],
        ontology_iri="https://orion.local/test",
        runtime_query_bindings={
            "CQ-INTAKE-002": {
                "query_name": "overspec_summary",
                "sparql": runtime_query,
            }
        },
    )

    assert questions[0]["sparql"] == runtime_query
    assert questions[0]["coverage_status"] == "DIRECT"
    assert "overspec_summary" in questions[0]["generation_note"]
    assert all(question["source"] != "AI_SUPPLEMENT" for question in questions)


def test_s0_accepts_word_and_excel_with_type_appropriate_locations(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="批量文件本体工程",
        domain="document-batch",
        intake_mode="DOCUMENT_ONLY",
        intake_rationale="批量处理 Word 与 Excel 文件，并将证据继续用于本体建模。",
    )["project_id"]

    after_s0 = service.record_document_evidence(
        project_id=project_id,
        documents=[
            {
                "document_id": "DOC-WORD",
                "source_name": "供应商准入办法.docx",
                "source_type": "DOCX",
                "source_sha256": "sha256:" + "c" * 64,
                "page_count": 2,
                "processing_method": "WORD_STRUCTURE",
                "structured_markdown": "# 供应商准入办法\n\n## 准入要求\n\n供应商应完成正式准入评审。\n",
            },
            {
                "document_id": "DOC-EXCEL",
                "source_name": "供应商台账.xlsx",
                "source_type": "XLSX",
                "source_sha256": "sha256:" + "d" * 64,
                "sheet_count": 3,
                "processing_method": "SPREADSHEET_STRUCTURE",
                "structured_markdown": "# 供应商台账\n\n## 供应商主数据\n\n包含供应商、物料与准入状态。\n",
            },
        ],
        quality_report={
            "status": "PASSED",
            "processed_units": 5,
            "failed_units": 0,
            "low_confidence_units": 1,
            "reviewed_low_confidence_units": 1,
            "unreviewed_low_confidence_units": 0,
        },
        evidence_index=[
            {
                "evidence_id": "EVD-WORD",
                "document_id": "DOC-WORD",
                "source_page": 2,
                "markdown_section": "准入要求",
                "content_sha256": "sha256:" + "e" * 64,
            },
            {
                "evidence_id": "EVD-EXCEL",
                "document_id": "DOC-EXCEL",
                "source_locator": "工作表：供应商；单元格：A2:H200",
                "markdown_section": "供应商主数据",
                "content_sha256": "sha256:" + "f" * 64,
            },
        ],
        processing_trace={
            "run_id": "document-batch-run-001",
            "provider": "DOCUMENT_PROCESSING_MCP",
            "tool_calls": ["word_to_markdown", "spreadsheet_to_markdown"],
            "actor": "test-engineer",
        },
    )

    assert after_s0["current_stage"] == "S2"
    gate_results = json.loads(
        (tmp_path / project_id / "00-document-evidence/gate-results.json").read_text(
            encoding="utf-8"
        )
    )
    assert gate_results["metrics"]["content_unit_count"] == 5
    assert gate_results["metrics"]["page_count"] == 2
    assert gate_results["metrics"]["sheet_count"] == 3
    report = (
        tmp_path / project_id / "00-document-evidence/document-evidence-report.html"
    ).read_text(encoding="utf-8")
    assert "2 页" in report
    assert "3 个工作表/数据区" in report
    assert "工作表：供应商；单元格：A2:H200" in report


def test_stage_adjustment_preserves_before_state_and_reports_diff(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="可审计供应链本体",
        domain="auditable-supply-chain",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id, supplier_count=11)
    _record_s2(service, project_id)
    _complete_s3(service, project_id)

    preview = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S1",
        requested_by="test-operator",
    )
    assert preview["target_stage"] == "S1"
    assert preview["commit_allowed"] is True
    assert preview["artifact_impact"]["current_count"] > 0
    assert [item["stage"] for item in preview["affected_downstream"][:2]] == ["S2", "S3"]

    reopened = service.reopen_stage_for_correction(
        project_id=project_id,
        stage="S1",
        reason="补充新的数据库范围后重新验证数据理解与 Mapping",
        requested_by="test-operator",
        preview_token=preview["preview_token"],
        project_revision=preview["project_revision"],
    )
    assert reopened["current_stage"] == "S1"
    assert reopened["stage_statuses"]["S1"] == "RUNNING"
    assert reopened["stage_statuses"]["S2"] == "INVALIDATED"
    assert reopened["stage_statuses"]["S3"] == "INVALIDATED"
    revision_id = reopened["active_revision"]["revision_id"]
    revision_dir = tmp_path / project_id / "revisions" / revision_id
    assert (revision_dir / "before-state.json").exists()
    assert (revision_dir / "before/01-data-understanding/data-understanding-report.html").exists()
    manifest = json.loads(
        (tmp_path / project_id / "artifact-manifest.json").read_text(encoding="utf-8")
    )
    invalidated = [
        item for item in manifest["files"] if item.get("lifecycle_status") == "INVALIDATED"
    ]
    assert invalidated
    assert all(item.get("historical_snapshot") for item in invalidated)

    with pytest.raises(WorkflowError, match="已使用|revision"):
        service.reopen_stage_for_correction(
            project_id=project_id,
            stage="S1",
            reason="重复提交不应再次执行",
            requested_by="test-operator",
            preview_token=preview["preview_token"],
            project_revision=preview["project_revision"],
        )

    _record_s1(service, project_id)
    _record_s2(service, project_id)
    _complete_s3(service, project_id)

    revision = json.loads((revision_dir / "revision.json").read_text(encoding="utf-8"))
    diff_payload = json.loads((revision_dir / "diff.json").read_text(encoding="utf-8"))
    assert revision["status"] == "IN_PROGRESS"
    assert service.get_status(project_id)["stage_statuses"]["S4"] == "PENDING"
    assert diff_payload["summary"]["modified"] >= 1
    assert any(
        item["path"].endswith("data-profile.json") and item["change_type"] == "MODIFIED"
        for item in diff_payload["changes"]
    )
    change_report = (revision_dir / "change-report.html").read_text(encoding="utf-8")
    assert "本体工程阶段调整差异报告" in change_report
    assert "旧报告不会被静默覆盖" in change_report

    history = service.get_revision_history(project_id)
    assert history["count"] == 1
    assert history["revisions"][0]["revision_id"] == revision_id
    events = [
        json.loads(line)
        for line in (tmp_path / project_id / "events/agent-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert all(event.get("event_hash", "").startswith("sha256:") for event in events)
    assert all(event["sequence"] == index for index, event in enumerate(events, start=1))
    assert all(
        events[index]["previous_event_hash"] == events[index - 1]["event_hash"]
        for index in range(1, len(events))
    )


def test_stage_rollback_keeps_unregenerated_old_file_invalidated(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="回退旧文件生命周期工程",
        domain="rollback-lifecycle",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    project_dir = tmp_path / project_id
    stale_path = project_dir / "02-semantic-recognition/obsolete-before-rollback.json"
    stale_path.write_text('{"obsolete": true}\n', encoding="utf-8")
    service._refresh_manifest(project_dir)

    preview = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S2",
        requested_by="tester",
    )
    service.reopen_stage_for_correction(
        project_id=project_id,
        stage="S2",
        reason="重新识别语义但不再生成旧辅助文件",
        requested_by="tester",
        preview_token=preview["preview_token"],
        project_revision=preview["project_revision"],
    )
    _record_s2(service, project_id)

    state = json.loads((project_dir / "workflow-state.json").read_text(encoding="utf-8"))
    relative = "02-semantic-recognition/obsolete-before-rollback.json"
    assert state["artifact_lifecycle"][relative]["status"] == "INVALIDATED"
    assert state["active_revision"]["status"] == "IN_PROGRESS"
    assert service.verify_project_integrity(project_id)["status"] == "FAILED"


def test_passed_stage_revalidates_preserved_decision_journal(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="回退决定流水保留工程",
        domain="rollback-decision-journal",
    )["project_id"]
    project_dir = tmp_path / project_id
    decisions = project_dir / "03-mapping-review/decisions.jsonl"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    decisions.write_text('{"decision":"APPROVED"}\n', encoding="utf-8")

    state = service._read_state(project_dir)
    state["stage_statuses"]["S3"] = "PASSED"
    state["artifact_lifecycle"]["03-mapping-review/decisions.jsonl"] = {
        "status": "INVALIDATED",
        "stage": "S3",
        "historical_snapshot": (
            "revisions/REV-TEST/before/03-mapping-review/decisions.jsonl"
        ),
    }
    service._save_state(project_dir, state)

    saved = service._read_state(project_dir)
    assert "03-mapping-review/decisions.jsonl" not in saved["artifact_lifecycle"]


def test_s1_rerun_removes_obsolete_scope_reconciliation_artifact(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="范围纠正重跑工程",
        domain="scope-reconciliation-rerun",
        intake_mode="HYBRID",
        table_scope=["legacy_table"],
    )["project_id"]
    _record_s0(service, project_id)

    def record_current_scope() -> dict:
        s1_contract = _production_s1_contract(
            project_id,
            {"sc_suppliers": 10, "sc_purchase_orders": 30},
        )
        return service.record_data_understanding(
            project_id=project_id,
            datasource_inventory=s1_contract["datasource_inventory"],
            schema_snapshot={
                "tables": [
                    {"name": "sc_suppliers", "primary_key": ["id"]},
                    {"name": "sc_purchase_orders", "primary_key": ["id"]},
                ]
            },
            data_profile=s1_contract["data_profile"],
            relation_candidates=[],
            evidence_sql=s1_contract["evidence_sql"],
        )

    record_current_scope()
    project_dir = tmp_path / project_id
    reconciliation = project_dir / "01-data-understanding/scope-reconciliation.json"
    assert reconciliation.exists()

    preview = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S1",
        requested_by="tester",
    )
    service.reopen_stage_for_correction(
        project_id=project_id,
        stage="S1",
        reason="以已纠正后的相同数据范围重跑 S1。",
        requested_by="tester",
        preview_token=preview["preview_token"],
        project_revision=preview["project_revision"],
    )
    record_current_scope()

    assert not reconciliation.exists()
    integrity = service.verify_project_integrity(project_id)
    assert integrity["invalidated_artifacts"] == []
    assert integrity["status"] == "PASSED"


def test_s0_rollback_preserves_competency_question_intake(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(
        project_name="业务问题回退保护工程",
        domain="cq-rollback-protection",
        cq_mode="USER_PROVIDED",
        initial_competency_questions=[
            {
                "id": "CQ-INTAKE-001",
                "question": "哪些供应商影响关键订单？",
                "expected": "返回供应商和受影响订单。",
            }
        ],
    )
    project_id = created["project_id"]
    project_dir = tmp_path / project_id
    _record_s0(service, project_id)
    intake_path = project_dir / "00-document-evidence/cq-intake.json"
    original_intake = intake_path.read_bytes()

    preview = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S0",
        requested_by="tester",
    )
    assert preview["artifact_impact"]["preserved_inputs"][0]["path"] == (
        "00-document-evidence/cq-intake.json"
    )
    service.reopen_stage_for_correction(
        project_id=project_id,
        stage="S0",
        reason="只重整资料，不删除负责人提出的业务问题。",
        requested_by="tester",
        preview_token=preview["preview_token"],
        project_revision=preview["project_revision"],
    )

    assert intake_path.read_bytes() == original_intake
    state = service.get_status(project_id)
    cq_lifecycle = state["artifact_lifecycle"].get(
        "00-document-evidence/cq-intake.json",
        {},
    )
    assert cq_lifecycle.get("status") != "INVALIDATED"
    manifest = service._read_json(project_dir / "artifact-manifest.json")
    manifest_entry = next(
        item for item in manifest["files"] if item["path"] == "00-document-evidence/cq-intake.json"
    )
    assert manifest_entry["lifecycle_status"] == "CURRENT"


def test_stage_rollback_rejects_stale_and_expired_preview_tokens(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(project_name="回退令牌工程", domain="rollback-token")
    project_id = created["project_id"]
    stale = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S0",
        requested_by="test-operator",
    )
    archived = service.archive_project(
        project_id=project_id,
        archived_by="test-operator",
        reason="制造一次合法的工程 revision 变化",
        expected_revision=stale["project_revision"],
    )
    service.restore_project(
        project_id=project_id,
        restored_by="test-operator",
        reason="恢复后验证旧 preview 失效",
        expected_revision=archived["revision"],
    )
    with pytest.raises(WorkflowError, match="状态已变化"):
        service.reopen_stage_for_correction(
            project_id=project_id,
            stage="S0",
            reason="旧 revision 不得提交",
            requested_by="test-operator",
            preview_token=stale["preview_token"],
            project_revision=stale["project_revision"],
        )

    expired = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S0",
        requested_by="test-operator",
    )
    preview_path = tmp_path / project_id / ".operation-previews" / f"{expired['preview_id']}.json"
    payload = json.loads(preview_path.read_text(encoding="utf-8"))
    payload["expires_at"] = "2000-01-01T00:00:00+08:00"
    preview_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(WorkflowError, match="已过期"):
        service.reopen_stage_for_correction(
            project_id=project_id,
            stage="S0",
            reason="过期令牌不得提交",
            requested_by="test-operator",
            preview_token=expired["preview_token"],
            project_revision=expired["project_revision"],
        )


def test_storage_sync_receipt_is_scoped_to_its_project(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    first = service.create_project(project_name="存储回执甲", domain="storage-a")
    second = service.create_project(project_name="存储回执乙", domain="storage-b")
    receipt = {
        "status": "SYNCED",
        "project_id": first["project_id"],
        "counts": {"events": 1},
    }

    service._write_storage_status(receipt)

    first_receipt = json.loads(
        (tmp_path / first["project_id"] / ".storage-sync-status.json").read_text(encoding="utf-8")
    )
    assert first_receipt == receipt
    assert not (tmp_path / second["project_id"] / ".storage-sync-status.json").exists()
    second_status = service.get_storage_status(second["project_id"])
    assert second_status["status"] == "FILESYSTEM_ONLY"
    assert second_status["sync_status"] == "MISSING"
    assert second_status["project_id"] == second["project_id"]


def test_storage_status_rejects_stale_or_cross_project_receipt(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    first = service.create_project(project_name="存储真值甲", domain="storage-truth-a")
    second = service.create_project(project_name="存储真值乙", domain="storage-truth-b")
    first_dir = tmp_path / first["project_id"]
    second_dir = tmp_path / second["project_id"]
    first_updated_at = json.loads((first_dir / "workflow-state.json").read_text(encoding="utf-8"))[
        "updated_at"
    ]

    service._write_json(
        first_dir / ".storage-sync-status.json",
        {
            "status": "SYNCED",
            "project_id": first["project_id"],
            "checked_at": "2000-01-01T00:00:00+00:00",
        },
    )
    stale = service.get_storage_status(first["project_id"])
    assert stale["status"] == "DEGRADED"
    assert stale["sync_status"] == "STALE"
    assert stale["workflow_updated_at"] == first_updated_at

    service._write_json(
        second_dir / ".storage-sync-status.json",
        {
            "status": "SYNCED",
            "project_id": first["project_id"],
            "checked_at": "2099-01-01T00:00:00+00:00",
        },
    )
    mismatch = service.get_storage_status(second["project_id"])
    assert mismatch["status"] == "DEGRADED"
    assert mismatch["sync_status"] == "PROJECT_MISMATCH"
    assert mismatch["project_id"] == second["project_id"]


def test_stale_postgres_receipt_blocks_next_stage_mutation(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(
        project_name="陈旧账本阻断工程",
        domain="stale-ledger",
        intake_mode="DATABASE_ONLY",
    )
    project_id = created["project_id"]
    project_dir = tmp_path / project_id
    state_before = json.loads((project_dir / "workflow-state.json").read_text(encoding="utf-8"))
    service._write_json(
        project_dir / ".storage-sync-status.json",
        {
            "status": "SYNCED",
            "project_id": project_id,
            "target": {"project_revision": state_before["revision"] - 1},
            "services": {"postgresql": {"status": "CONNECTED"}},
            "read_back": {"storage": "postgresql"},
            "checked_at": "2000-01-01T00:00:00+00:00",
        },
    )

    with pytest.raises(WorkflowError, match="没有可用的账本连接"):
        service.record_s0_scope_decision(
            project_id=project_id,
            intake_mode="DATABASE_ONLY",
            rationale="只处理结构化数据。",
            decided_by="tester",
        )

    state_after = json.loads((project_dir / "workflow-state.json").read_text(encoding="utf-8"))
    assert state_after["revision"] == state_before["revision"]
    assert state_after["current_stage"] == state_before["current_stage"]


def test_metadata_failure_commits_locally_then_reconciles_with_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="账本补同步故障注入工程",
        domain="metadata-outbox",
        intake_mode="DATABASE_ONLY",
    )["project_id"]

    class FlakyMetadataStore:
        artifact_store = None

        def __init__(self) -> None:
            self.fail = True
            self.latest_event_hash: str | None = None
            self.latest_event_sequence: int | None = None
            self.sync_count = 0

        def sync_project(self, project_dir: Path) -> dict[str, int]:
            self.sync_count += 1
            if self.fail:
                raise ConnectionError("injected metadata outage with secret=hidden")
            event = json.loads(
                (project_dir / "events/agent-trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            self.latest_event_hash = str(event["event_hash"])
            self.latest_event_sequence = int(event["sequence"])
            return {"projects": 1, "events": 2, "object_artifacts": 0}

        def status(self, _project_id: str | None = None) -> dict:
            if self.fail:
                raise ConnectionError("injected readback outage")
            return {
                "counts": {"projects": 1, "events": 2},
                "latest_event": {
                    "sequence": self.latest_event_sequence,
                    "event_hash": self.latest_event_hash,
                },
            }

    store = FlakyMetadataStore()
    service._metadata_store = store  # type: ignore[assignment]
    service._metadata_configured = True
    service._metadata_required = True
    advanced = service.record_s0_scope_decision(
        project_id=project_id,
        intake_mode="DATABASE_ONLY",
        rationale="本次只接入数据库。",
        decided_by="tester",
    )

    assert advanced["current_stage"] == "S1"
    outbox_path = tmp_path / ".storage-outbox" / f"{project_id}.json"
    pending = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert pending["status"] == "COMMITTED_SYNC_PENDING"
    assert pending["target"]["event_hash"].startswith("sha256:")
    assert "secret" not in json.dumps(pending, ensure_ascii=False)
    pending_status = service.get_storage_status(project_id)
    assert pending_status["status"] == "COMMITTED_SYNC_PENDING"
    assert pending_status["pending_count"] == 1

    lock_order: list[str] = []
    original_root_lock = service._root_operation_lock
    original_project_lock = service._project_operation_lock

    @contextmanager
    def observed_root_lock():
        lock_order.append("root")
        with original_root_lock():
            yield

    @contextmanager
    def observed_project_lock(project_dir: Path):
        lock_order.append("project")
        with original_project_lock(project_dir):
            yield

    monkeypatch.setattr(service, "_root_operation_lock", observed_root_lock)
    monkeypatch.setattr(service, "_project_operation_lock", observed_project_lock)
    store.fail = False
    reconciled = service.reconcile_metadata_outbox(
        project_id=project_id,
        reconciled_by="tester",
    )
    replay = service.reconcile_metadata_outbox(
        project_id=project_id,
        reconciled_by="tester",
    )

    assert reconciled["status"] == "SYNCED"
    assert reconciled["pending_count"] == 0
    assert lock_order[:2] == ["root", "project"]
    assert not outbox_path.exists()
    assert replay["idempotent_replay"] is True
    read_back = service.get_storage_status(project_id)
    assert read_back["status"] == "CONNECTED"
    assert read_back["read_back"]["latest_event"]["event_hash"] == store.latest_event_hash


def test_metadata_outbox_recovers_manifest_commit_interruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="清单提交中断恢复工程",
        domain="metadata-prepared-manifest",
    )["project_id"]
    project_dir = tmp_path / project_id
    service._atomic_write(project_dir / "prepared-marker.txt", "待写入正式清单\n")

    class CapturingMetadataStore:
        artifact_store = None

        def __init__(self) -> None:
            self.sync_count = 0
            self.latest_event: dict = {}

        def sync_project(self, synced_project_dir: Path) -> dict[str, int]:
            self.sync_count += 1
            self.latest_event = json.loads(
                (synced_project_dir / "events/agent-trace.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[-1]
            )
            return {"projects": 1, "events": 1, "object_artifacts": 0}

        def status(self, _project_id: str | None = None) -> dict:
            return {
                "counts": {"projects": 1, "events": 1},
                "latest_event": {
                    "sequence": self.latest_event.get("sequence"),
                    "event_hash": self.latest_event.get("event_hash"),
                },
            }

    store = CapturingMetadataStore()
    service._metadata_store = store  # type: ignore[assignment]
    service._metadata_configured = True
    manifest_path = project_dir / "artifact-manifest.json"
    prepared_path = project_dir / ".artifact-manifest.pending.json"
    old_manifest_sha256 = service._metadata_sync_target(project_dir)["artifact_manifest_sha256"]
    real_replace = os.replace

    def interrupt_manifest_commit(source: str | Path, target: str | Path) -> None:
        if Path(source) == prepared_path and Path(target) == manifest_path:
            raise OSError("injected manifest commit interruption")
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", interrupt_manifest_commit)
    with pytest.raises(OSError, match="injected manifest commit interruption"):
        service._refresh_manifest(project_dir)

    outbox_path = tmp_path / ".storage-outbox" / f"{project_id}.json"
    pending = json.loads(outbox_path.read_text(encoding="utf-8"))
    assert prepared_path.exists()
    assert pending["target"]["artifact_manifest_sha256"] != old_manifest_sha256
    assert (
        service._metadata_sync_target(project_dir)["artifact_manifest_sha256"]
        == old_manifest_sha256
    )

    monkeypatch.setattr(os, "replace", real_replace)
    reconciled = service.reconcile_metadata_outbox(
        project_id=project_id,
        reconciled_by="tester",
    )

    assert reconciled["status"] == "SYNCED"
    assert store.sync_count == 1
    assert not prepared_path.exists()
    assert not outbox_path.exists()
    assert service._metadata_sync_target(project_dir) == pending["target"]


def test_semantica_supplement_sync_uses_locked_workflow_receipt_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="Semantica 补同步工程",
        domain="semantica-supplement",
    )["project_id"]
    project_dir = tmp_path / project_id
    ontology_path = project_dir / "05-ontology-build/ontology.ttl"
    ontology_path.parent.mkdir(parents=True, exist_ok=True)
    ontology_path.write_text("@prefix ex: <https://example.com/#> .\n", encoding="utf-8")
    publication_path = project_dir / "07-release/publication.json"
    service._write_json(
        publication_path,
        {"project_id": project_id, "release_version": "1.0.0"},
    )
    package_manifest = project_dir / "07-release/ontology-engineering-package-1.0.0/manifest.json"
    service._write_json(package_manifest, {"files": []})
    state = service._read_state(project_dir)
    state["project_status"] = "PUBLISHED"
    state["current_stage"] = None
    state["stage_statuses"]["S5"] = "PASSED"
    state["stage_statuses"]["S7"] = "PASSED"
    state["stage_fingerprints"]["S5"] = {
        "profile": "formal-artifacts-v1",
        "output": service._stage_fingerprint(ontology_path.parent),
    }
    service._write_json(project_dir / "workflow-state.json", state)

    calls: list[str] = []

    class FakeSync:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def sync(self, **kwargs: object) -> dict:
            calls.append(str(kwargs["project_id"]))
            return {
                "status": "SYNCED",
                "project_id": project_id,
                "release_version": "1.0.0",
                "semantica_url": "http://127.0.0.1:8001",
                "idempotent_replay": len(calls) > 1,
            }

    monkeypatch.setattr(
        "services.ontology_engineering.workflow.PublishedOntologySemanticaSync",
        FakeSync,
    )
    operation_id = "semantica-sync:test-project-1"
    receipt = service.sync_published_ontology_to_semantica(
        project_id=project_id,
        synced_by="tester",
        semantica_url="http://127.0.0.1:8001",
        operation_id=operation_id,
    )
    replay = service.sync_published_ontology_to_semantica(
        project_id=project_id,
        synced_by="tester",
        semantica_url="http://127.0.0.1:8001",
        operation_id=operation_id,
    )

    assert receipt["status"] == "SYNCED"
    assert receipt["source_publication_sha256"].startswith("sha256:")
    assert replay["idempotent_replay"] is True
    assert replay["live_verified_at"]
    assert calls == [project_id, project_id]
    manifest = json.loads((project_dir / "artifact-manifest.json").read_text(encoding="utf-8"))
    manifest_entry = next(
        item for item in manifest["files"] if item["path"] == "07-release/semantica-sync.json"
    )
    assert manifest_entry["sha256"].startswith("sha256:")
    assert manifest_entry["lifecycle_status"] == "CURRENT"


def test_stage_rollback_rejects_archived_project(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(project_name="归档回退保护工程", domain="archived-rollback")
    service.archive_project(
        project_id=created["project_id"],
        archived_by="test-operator",
        reason="验证归档状态不能直接回退",
        expected_revision=created["revision"],
    )

    with pytest.raises(WorkflowError, match="恢复"):
        service.preview_stage_rollback(
            project_id=created["project_id"],
            target_stage="S0",
            requested_by="test-operator",
        )


def test_recovered_release_snapshot_must_match_current_contract(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    package_dir = tmp_path / "release-package"
    release_info_dir = package_dir / "04-发布信息"
    release_info_dir.mkdir(parents=True)
    expected = {
        "schema_version": 1,
        "project_id": "project-current",
        "release_version": "1.0.0",
        "formal_stage_fingerprints": {"S0": {"recorded_sha256": "sha256:new"}},
        "pre_publish_chain": {"last_event_hash": "sha256:new"},
    }
    service._write_json(
        release_info_dir / "release-snapshot.json",
        {
            **expected,
            "formal_stage_fingerprints": {"S0": {"recorded_sha256": "sha256:stale"}},
        },
    )

    with pytest.raises(WorkflowGateError, match="旧阶段指纹"):
        service._verify_recovered_release_snapshot(package_dir, expected)


def test_status_marks_historical_release_without_new_cq_contract(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(
        project_name="历史发布合同标记工程",
        domain="legacy-release-contract",
    )
    project_dir = tmp_path / created["project_id"]
    service._write_json(
        project_dir / "07-release/publication.json",
        {
            "project_id": created["project_id"],
            "release_version": "0.1.0",
            "approval_decision": "APPROVED",
        },
    )

    release_contract = service.get_status(created["project_id"])["release_contract"]
    assert release_contract["status"] == "LEGACY_UNVERIFIED"
    assert release_contract["delivery_export_precheck"] == "BLOCKED"


def test_partial_snapshot_accepts_only_audited_s4_to_s6_revision(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    snapshot = {
        "integrity_status": "PARTIAL",
        "formal_stage_fingerprints": {
            "S0": {
                "profile": "legacy-whole-directory",
                "verification_status": "LEGACY_UNVERIFIED",
            },
            **{
                stage: {
                    "profile": "formal-artifacts-v1",
                    "verification_status": "VERIFIED",
                }
                for stage in ("S4", "S5", "S6")
            },
        },
        "pre_publish_chain": {"verification_status": "VERIFIED"},
    }

    assert service._release_snapshot_supports_new_contract(snapshot)
    snapshot["formal_stage_fingerprints"]["S4"]["verification_status"] = "LEGACY_UNVERIFIED"
    assert not service._release_snapshot_supports_new_contract(snapshot)


def test_cq_answer_contract_only_requires_aggregate_outputs() -> None:
    contract = OntologyWorkflowService._cq_answer_contract(
        {
            "id": "CQ-AGGREGATE",
            "question": "各膜卷关联多少异常样本？",
            "sparql": (
                "SELECT ?polarity ?filmRoll "
                "(COUNT(DISTINCT ?sample) AS ?abnormalCount) WHERE { "
                "?sample <urn:tracesTo> ?filmRoll . } "
                "GROUP BY ?polarity ?filmRoll"
            ),
            "expected": "返回极性、膜卷和异常样本数。",
        }
    )

    assert contract["required_bindings"] == [
        "polarity",
        "filmRoll",
        "abnormalCount",
    ]


def test_s3_finalize_clears_current_and_superseded_runtime_lifecycle(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="运行时生命周期重建工程",
        domain="runtime-lifecycle",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    runtime = {
        "ontop_deployment_id": "ontop-runtime-lifecycle-001",
        "database_access_mode": "READ_ONLY",
        "prepared_by": "ontology-engineer",
        "mapping_obda": """[PrefixDeclaration]
ex: https://example.com/runtime-lifecycle#

[MappingDeclaration] @collection [[
mappingId SupplierRuntime
target <https://example.com/runtime-lifecycle/supplier-{code}> a ex:Supplier .
source SELECT code FROM sc_suppliers

mappingId SupplierRelationRuntime
target <https://example.com/runtime-lifecycle/order-{id}> ex:actuallySupplies <https://example.com/runtime-lifecycle/supplier-{supplier_id}> ; ex:orderedFrom <https://example.com/runtime-lifecycle/supplier-{supplier_id}> .
source SELECT id, supplier_id FROM sc_purchase_orders
]]
""",
        "ontop_queries": {
            "suppliers_live": """PREFIX ex: <https://example.com/runtime-lifecycle#>
SELECT ?supplier WHERE { ?supplier a ex:Supplier . }
LIMIT 10
"""
        },
    }
    _prepare_s3(service, project_id, realtime_runtime=runtime)

    project_dir = tmp_path / project_id
    state = service._read_state(project_dir)
    for relative in (
        "03-mapping-review/runtime/runtime-source.json",
        "03-mapping-review/runtime/mapping.obda",
        "03-mapping-review/runtime/queries/suppliers_live.rq",
        "03-mapping-review/runtime/queries/removed_query.rq",
    ):
        state.setdefault("artifact_lifecycle", {})[relative] = {
            "status": "INVALIDATED",
            "invalidated_at": "2026-01-01T00:00:00Z",
        }
    service._save_state(project_dir, state)

    service.resolve_confirmation_option(
        project_id=project_id,
        confirmation_id="CONF-001",
        selected_option_id="ordered-from",
        decided_by="ontology-engineer",
        rationale="订单事实只证明下单关系。",
    )
    finalized = service.resolve_confirmation_option(
        project_id=project_id,
        confirmation_id="S3-OVERALL-MAPPING-REVIEW",
        selected_option_id="APPROVE-MAPPING",
        decided_by="ontology-engineer",
        rationale="运行时映射与查询模板已重新生成并完成评审。",
    )

    assert not {
        path
        for path in finalized["artifact_lifecycle"]
        if path.startswith("03-mapping-review/runtime/")
    }


@pytest.mark.parametrize(
    "stage_contract_version",
    ["s0-s7-stage-contract-v1", "s0-s7-stage-contract-v2"],
    ids=["historical-v1", "joint-design-v2"],
)
def test_s4_to_s7_builds_validates_and_publishes_package(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage_contract_version: str,
) -> None:
    joint_design_enabled = stage_contract_version == "s0-s7-stage-contract-v2"

    class FakeDeploymentAutomation:
        def __init__(self) -> None:
            self.calls: list[tuple[Path, str]] = []
            self.status_listener: Any = None

        def set_status_listener(self, listener: Any) -> None:
            self.status_listener = listener

        def enqueue(self, *, project_dir: Path, release_version: str) -> dict[str, Any]:
            self.calls.append((project_dir, release_version))
            return {
                "project_id": project_dir.name,
                "release_version": release_version,
                "state": "DEPLOYMENT_QUEUED",
            }

        def status(self, *, project_dir: Path, release_version: str) -> dict[str, Any]:
            return {
                "project_id": project_dir.name,
                "release_version": release_version,
                "state": "NOT_QUEUED",
            }

        def mark_ready(self, *, project_dir: Path, release_version: str) -> None:
            assert self.status_listener is not None
            self.status_listener(
                project_dir,
                release_version,
                {
                    "project_id": project_dir.name,
                    "release_version": release_version,
                    "state": "ONTOP_READY",
                    "artifact_verified": True,
                    "runtime_verified": True,
                    "query_validation": {"status": "PASSED"},
                    "reasoning_validation": {"status": "PASSED"},
                    "semantica_sync": {
                        "status": "SYNCED",
                        "registry_verified": True,
                        "source_sha256": "sha256:" + "e" * 64,
                        "source_integrity_status": "PASSED",
                        "exploration_status": "VERIFIED",
                        "snapshot_sha256": "sha256:" + "a" * 64,
                        "exploration_checks": ["stats", "classes", "instances", "node"],
                    },
                },
            )

        def mark_failed(self, *, project_dir: Path, release_version: str) -> None:
            assert self.status_listener is not None
            self.status_listener(
                project_dir,
                release_version,
                {
                    "project_id": project_dir.name,
                    "release_version": release_version,
                    "state": "DEPLOYMENT_FAILED",
                    "artifact_verified": True,
                    "runtime_verified": False,
                    "degraded_reason": "Ontop runtime unavailable",
                    "query_validation": {"status": "PENDING"},
                    "reasoning_validation": {"status": "PENDING"},
                    "semantica_sync": {"status": "SYNCED", "registry_verified": True,
                                      "exploration_status": "VERIFIED",
                                      "snapshot_sha256": "sha256:" + "a" * 64,
                                      "exploration_checks": ["stats", "classes", "instances", "node"]},
                },
            )

    deployment = FakeDeploymentAutomation()
    service = OntologyWorkflowService(
        tmp_path,
        release_deployment_automation=deployment,
    )
    project_id = service.create_project(
        project_name="供应链本体",
        domain="supply-chain",
        _stage_contract_version=stage_contract_version,
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    _complete_s3(
        service,
        project_id,
        realtime_runtime={
            "ontop_deployment_id": f"ontop-{project_id}-1.0.0",
            "database_access_mode": "READ_ONLY",
            "prepared_by": "ontology-engineer",
            "mapping_obda": """[PrefixDeclaration]
ex: https://example.com/supply-chain#

[MappingDeclaration] @collection [[
mappingId SupplierRuntime
target <https://example.com/supply-chain/supplier-{code}> a ex:Supplier .
source SELECT code FROM sc_suppliers

mappingId SupplierRelationRuntime
target <https://example.com/supply-chain/order-{id}> ex:actuallySupplies <https://example.com/supply-chain/supplier-{supplier_id}> ; ex:orderedFrom <https://example.com/supply-chain/supplier-{supplier_id}> .
source SELECT id, supplier_id FROM sc_purchase_orders
]]
""",
            "ontop_queries": {
                "suppliers_live": """PREFIX ex: <https://example.com/supply-chain#>
SELECT ?supplier WHERE { ?supplier a ex:Supplier . }
LIMIT 10
"""
            },
        },
    )

    ontology_design = {
        "ontology_iri": "https://example.com/supply-chain",
        "version": "0.1.0",
        "classes": [
            {
                "name": "Supplier",
                "instance_contract": _business_instance_contract("供应商", ["MAP-SUPPLIER"]),
                "iri": "https://example.com/supply-chain#Supplier",
                "source_mapping_ids": ["MAP-SUPPLIER"],
            },
            {
                "name": "PurchaseOrder",
                "instance_contract": _business_instance_contract("采购订单", ["MAP-SUPPLIER-RELATION"]),
                "iri": "https://example.com/supply-chain#PurchaseOrder",
                "source_mapping_ids": ["MAP-SUPPLIER-RELATION"],
                "label_zh": "采购订单",
                "comment_zh": "向供应商发出的采购订单业务对象。",
            },
        ],
        "object_properties": [
            {
                "name": "orderedFrom",
                "iri": "https://example.com/supply-chain#orderedFrom",
                "domain": "https://example.com/supply-chain#PurchaseOrder",
                "range": "https://example.com/supply-chain#Supplier",
                "source_mapping_ids": ["MAP-SUPPLIER-RELATION"],
            }
        ],
        "data_properties": [],
        "constraints": [],
        "competency_questions": [
            {
                "id": "CQ-001",
                "question": "采购订单向哪个供应商下单？",
                "sparql": "SELECT ?order ?supplier WHERE { ?order <https://example.com/supply-chain#orderedFrom> ?supplier . }",
                "expected": "返回采购订单及其供应商",
                "answer_contract": {
                    "result_assertions": [
                        {
                            "binding": "order",
                            "operator": "EQ",
                            "expected": "https://example.com/supply-chain#order-1",
                        }
                    ],
                    "boundary_assertions": [
                        {
                            "binding": "supplier",
                            "operator": "EQ",
                            "expected": "https://example.com/supply-chain#supplier-1",
                        }
                    ],
                    "required_business_dimensions": [
                        {
                            "dimension": "subject",
                            "label_zh": "采购订单",
                            "binding": "order",
                            "ontology_term": "https://example.com/supply-chain#PurchaseOrder",
                            "evidence_refs": ["MAP-SUPPLIER-RELATION"],
                        },
                        {
                            "dimension": "related_object",
                            "label_zh": "接单供应商",
                            "binding": "supplier",
                            "ontology_term": "https://example.com/supply-chain#Supplier",
                            "path": "https://example.com/supply-chain#orderedFrom",
                            "evidence_refs": ["MAP-SUPPLIER-RELATION"],
                        },
                    ],
                },
            }
        ],
        "logical_axioms": [
            {
                "id": "AX-ORDER-SUPPLIER-DISJOINT",
                "axiom_type": "DISJOINT_WITH",
                "class": "https://example.com/supply-chain#PurchaseOrder",
                "other": "https://example.com/supply-chain#Supplier",
                "source_refs": ["MAP-SUPPLIER", "MAP-SUPPLIER-RELATION"],
            }
        ],
    }
    review = service.prepare_ontology_design_review(
        project_id=project_id,
        ontology_design=ontology_design,
    )
    assert review["stage_statuses"]["S4"] == "BLOCKED_HUMAN"
    approved_questions = [
        {
            "id": "CQ-001",
            "question": "每一张采购订单具体向哪一家供应商下单？",
            "expected": "返回采购订单及其供应商",
        }
    ]
    s4 = service.resolve_competency_question_review(
        project_id=project_id,
        questions=approved_questions,
        decision="APPROVED",
        decided_by="test-operator",
        rationale="调整为业务人员更容易理解的表述，并确认可验证订单与供应商关系。",
        expected_revision=review["revision"],
    )
    if joint_design_enabled:
        assert s4["stage_statuses"]["S4"] == "BLOCKED_HUMAN"
        assert s4["competency_question_review"]["status"] == "PENDING"
        assert s4["competency_question_review"]["joint_design_fingerprint"] != (
            review["competency_question_review"]["joint_design_fingerprint"]
        )
        approval_fingerprint = s4["competency_question_review"]["joint_design_fingerprint"]
        complete_approved_questions = s4["competency_question_review"]["draft_questions"]
        compact_review = OrionWorkflowTools(service)._compact_commit_result(service._resolve_project(project_id), s4)
        assert compact_review["review_ref"]["joint_design_fingerprint"] == approval_fingerprint
        assert compact_review["review_ref"]["project_revision"] == s4["revision"]
        s4 = service.resolve_competency_question_review(
            project_id=project_id,
            questions=compact_review["competency_question_review"]["approval_questions"],
            decision="APPROVED",
            decided_by="test-operator",
            rationale="已重新审阅修改后的业务问题、本体、映射、查询与验收设计。",
            expected_revision=s4["revision"],
        )
        assert s4["joint_design_fingerprint"] == approval_fingerprint
        assert s4["competency_question_review"]["approved_questions"] == complete_approved_questions
    assert s4["current_stage"] == "S5"
    assert s4["stage_statuses"]["S4"] == "PASSED"
    s4_dir = tmp_path / project_id / "04-ontology-design"
    assert (s4_dir / "ontology-design-report.html").exists()
    cq_review = json.loads((s4_dir / "competency-question-review.json").read_text(encoding="utf-8"))
    assert cq_review["status"] == "APPROVED"
    assert cq_review["difference"]["changed_item_count"] == (0 if joint_design_enabled else 1)
    assert cq_review["decided_by"] == "test-operator"
    assert cq_review["approved_questions"][0]["question"].startswith("每一张采购订单")
    assert cq_review["approved_questions"][0]["sparql"].startswith("SELECT")

    ontology_ttl = """@prefix ex: <https://example.com/supply-chain#> .
@prefix dcterms: <http://purl.org/dc/terms/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
<https://example.com/supply-chain> a owl:Ontology ;
  dcterms:title "供应链本体"@zh ;
  rdfs:comment "面向采购供应业务构建的企业本体，用于统一供应商、采购订单及其下单关系，支持采购查询、关系校验和后续推理；结论只反映已加载公理与实例。"@zh .
ex:Supplier a owl:Class ;
  rdfs:label "供应商"@zh ;
  rdfs:comment "提供物料或服务的业务主体。"@zh .
ex:PurchaseOrder a owl:Class ;
  owl:disjointWith ex:Supplier ;
  rdfs:label "采购订单"@zh ;
  rdfs:comment "企业向供应商发出的采购业务单据。"@zh .
ex:orderedFrom a owl:ObjectProperty ;
  rdfs:domain ex:PurchaseOrder ;
  rdfs:range ex:Supplier ;
  rdfs:label "向供应商下单"@zh ;
  rdfs:comment "采购订单与接单供应商之间的业务关系。"@zh .
"""
    ontology_owl = str(Graph().parse(data=ontology_ttl, format="turtle").serialize(format="xml"))
    shapes_ttl = """@prefix ex: <https://example.com/supply-chain#> .
@prefix sh: <http://www.w3.org/ns/shacl#> .
ex:SupplierShape a sh:NodeShape ;
  sh:targetClass ex:Supplier ;
  sh:property [ sh:path ex:supplierCode ; sh:minCount 1 ] .
"""
    build_payload = {
        "ontology_owl": ontology_owl,
        "ontology_ttl": ontology_ttl,
        "shapes_ttl": shapes_ttl,
        "protege_build_report": {
            "builder": "PROTEGE_MCP",
            "status": "PASSED",
            "run_id": "protege-build-1",
            "exported_formats": ["OWL", "TTL", "SHACL"],
            "tool_calls": [
                "create_class",
                "create_object_property",
                "run_reasoner",
                "save_ontology",
            ],
            "reasoner_report": {
                "status": "CONSISTENT",
                "consistent": True,
                "inconsistent": False,
                "reasoner": "HERMIT",
            },
            "shacl_report": {"conforms": True, "node_shape_count": 1},
            "ontology_annotation_summary": {
                "count": 5,
                "comment_zh": "面向采购供应业务构建的企业本体，用于统一供应商、采购订单及其下单关系，支持采购查询、关系校验和后续推理；结论只反映已加载公理与实例。",
                "items": [],
            },
        },
    }
    state_path = tmp_path / project_id / "workflow-state.json"
    events_path = tmp_path / project_id / "events/agent-trace.jsonl"
    state_before_preflight = state_path.read_bytes()
    events_before_preflight = events_path.read_bytes()
    preflight = service.preflight_stage_submission(
        project_id=project_id,
        stage="S5",
        payload=build_payload,
    )
    assert preflight["status"] == "PASSED"
    assert preflight["writes_performed"] is False
    assert state_path.read_bytes() == state_before_preflight
    assert events_path.read_bytes() == events_before_preflight

    s5 = service.record_ontology_build(project_id=project_id, **build_payload)
    assert s5["current_stage"] == "S6"
    assert s5["stage_statuses"]["S5"] == "PASSED"
    assert (tmp_path / project_id / "05-ontology-build/ontology-build-report.html").exists()
    build_report = json.loads(
        (tmp_path / project_id / "05-ontology-build/protege-build-report.json").read_text(
            encoding="utf-8"
        )
    )
    assert build_report["verified_metrics"]["ontology_title_zh"] == "供应链本体"
    assert build_report["verified_metrics"]["chinese_label_count"] == 3

    materialized_ttl = """@prefix ex: <https://example.com/supply-chain#> .
ex:supplier-1 a ex:Supplier ; ex:supplierCode "S-001" .
ex:order-1 a ex:PurchaseOrder ; ex:orderedFrom ex:supplier-1 .
"""
    s5_fingerprint = service.get_status(project_id)["stage_fingerprints"]["S5"]["output"]
    s6_relationships = [
        {
            "subject": "https://example.com/supply-chain#order-1",
            "predicate": "https://example.com/supply-chain#orderedFrom",
            "object": "https://example.com/supply-chain#supplier-1",
        }
    ]
    s6_relationship_sha256 = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                s6_relationships,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    # This state-machine fixture uses an isolated in-memory graph; exercise the
    # new server gate with an explicit candidate execution stub (live backend
    # contracts are separately tested in test_target_backend_validation.py).
    backend_calls = []
    monkeypatch.setattr(service, "_validated_s6_base_release_endpoint", lambda *_: None)
    def verify_candidate(**kwargs):
        backend_calls.append(kwargs["purpose"])
        return {"policy": "formal-target-backend-validation-v1", "status": "PASSED"}
    monkeypatch.setattr("services.ontop_client.backend_validation.validate_candidate_queries", verify_candidate)
    s6 = service.record_quality_validation(
        project_id=project_id,
        materialized_ttl=materialized_ttl,
        hermit_report={
            "status": "PASSED",
            "consistent": True,
            "reasoner": "HERMIT",
            "run_id": "hermit-1",
        },
        mapping_report={"status": "PASSED", "unmapped_count": 0, "mapped_count": 2},
        semantic_quality_report={
            "status": "PASSED",
            "high_severity_issue_count": 0,
            "checked_entity_count": 3,
        },
        competency_question_report={"status": "PASSED", "total": 1, "passed": 1, "validation_mode": "PRE_RELEASE_FULL_SOURCE_ONTOP"},
        semantica_report={
            "status": "PASSED",
            "instance_count": 2,
            "relationship_count": 2,
            "engine": "SEMANTICA_MCP",
            "run_id": "sem-1",
            "import_result": {"nodes_added": 2, "edges_added": 1},
            "relationship_import_result": {"nodes_added": 2, "edges_added": 1},
            "candidate_relationship_verification": {
                "status": "VERIFIED",
                "mode": "NEW_RELATIONSHIPS_ADDED",
                "release_candidate_fingerprint": s5_fingerprint,
                "relationship_set_sha256": s6_relationship_sha256,
                "verified_relationships_sha256": s6_relationship_sha256,
                "expected_relationship_count": 1,
                "verified_relationship_count": 1,
                "candidate_relationship_count": 1,
                "verified_predicates": ["https://example.com/supply-chain#orderedFrom"],
                "verified_relationships": s6_relationships,
                "receipt_sha256": "sha256:" + "c" * 64,
            },
            "production_coverage": {
                "status": "VERIFIED",
                "validation_scope": "FULL_SOURCE_VALIDATION",
                "populations": {
                    "database_rows": {"expected": 40, "evaluated": 40, "failed": 0},
                    "document_evidence_units": {
                        "expected": 1,
                        "evaluated": 1,
                        "failed": 0,
                    },
                },
                "source_stage_fingerprints": {
                    stage: service.get_status(project_id)["stage_fingerprints"][stage]["output"]
                    for stage in ("S0", "S1", "S2", "S3", "S4", "S5")
                },
                "receipt_id": "coverage-test-1",
            },
        },
    )
    assert s6["current_stage"] == "S7"
    assert s6["stage_statuses"]["S6"] == "PASSED"
    preview_path = tmp_path / project_id / "06-quality-validation/protege-model-instance-preview.owl"
    assert preview_path.exists() is joint_design_enabled
    quality_file = tmp_path / project_id / "06-quality-validation/quality-summary.json"
    mapping_diagnostics = json.loads(quality_file.read_text()).get("mapping_quality_report")
    assert bool(mapping_diagnostics) is joint_design_enabled
    if joint_design_enabled:
        assert mapping_diagnostics["error_count"] == 0
        assert mapping_diagnostics["relationships"][0]["relationship_count"] > 0
        assert mapping_diagnostics["source_to_instance_reconciliation"]["status"] == "NOT_EVALUATED"

    if joint_design_enabled:
        preview_graph = Graph().parse(preview_path, format="xml")
        assert len(preview_graph) > 0
        preview_report = json.loads((preview_path.parent / "protege-instance-preview.json").read_text())
        assert preview_report["is_full_dataset"] is False
        assert preview_report["sample_fact_count"] > 0
        assert preview_report["snapshot_sha256"].startswith("sha256:")

    assert (tmp_path / project_id / "06-quality-validation/quality-validation-report.html").exists()
    assert s6["s6_validation_run"]["status"] == "PASSED"
    assert all(item["status"] == "PASSED" for item in s6["s6_validation_run"]["subgates"])

    cq_report_path = tmp_path / project_id / "06-quality-validation/competency-question-report.json"
    original_cq_report = cq_report_path.read_bytes()
    full_source_cq_report = json.loads(original_cq_report)
    assert backend_calls == ["FORMAL_GATE_REEXECUTION"]
    full_source_cq_report["validation_mode"] = "SERVER_EXECUTED_BASE_RELEASE_FULL_SOURCE_ONTOP"
    cq_report_path.write_text(
        json.dumps(full_source_cq_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    assert (
        service._verify_competency_question_release_lineage(tmp_path / project_id)["status"]
        == "VERIFIED"
    )
    cq_report_path.write_bytes(original_cq_report)

    deferred = service.defer_ontology_publication(
        project_id=project_id,
        decided_by="test-operator",
        reason="先复核交付范围，本轮暂不发布。",
    )
    assert deferred["project_status"] == "RELEASE_DEFERRED"
    assert deferred["stage_statuses"]["S7"] == "DEFERRED"
    assert not list((tmp_path / project_id / "07-release").glob("ontology-engineering-package-*"))
    deferred_report = tmp_path / project_id / "07-release/release-decision-report.html"
    assert deferred_report.exists()
    assert "确认暂不发布" not in deferred_report.read_text(encoding="utf-8")
    assert "暂不发布" in deferred_report.read_text(encoding="utf-8")
    resumed = service.resume_ontology_publication(
        project_id=project_id,
        resumed_by="test-operator",
        reason="交付范围已复核，重新进入发布审批。",
    )
    assert resumed["current_stage"] == "S7"
    assert resumed["stage_statuses"]["S7"] == "RUNNING"
    resumed_report = tmp_path / project_id / "07-release/release-resumption-report.html"
    assert resumed_report.exists()
    assert "恢复发布评审" in resumed_report.read_text(encoding="utf-8")

    real_copy2 = shutil.copy2
    copy_count = 0

    def interrupted_copy(source: Path, target: Path) -> Path:
        nonlocal copy_count
        copy_count += 1
        if copy_count == 2:
            raise OSError("injected package build interruption")
        return real_copy2(source, target)

    monkeypatch.setattr(shutil, "copy2", interrupted_copy)
    with pytest.raises(OSError, match="injected package build interruption"):
        service.publish_ontology_package(
            project_id=project_id,
            release_version="1.0.0",
            approval_decision="APPROVED",
            approved_by="test-operator",
            release_notes="供应链本体第一版正式发布。",
            expected_revision=resumed["revision"],
        )
    stage_dir = tmp_path / project_id / "07-release"
    assert not (stage_dir / "ontology-engineering-package-1.0.0").exists()
    assert not list(stage_dir.glob(".ontology-engineering-package-1.0.0.tmp-*"))
    assert not (stage_dir / "publication.json").exists()
    monkeypatch.setattr(shutil, "copy2", real_copy2)

    published = service.publish_ontology_package(
        project_id=project_id,
        release_version="1.0.0",
        approval_decision="APPROVED",
        approved_by="test-operator",
        release_notes="供应链本体第一版正式发布。",
        expected_revision=resumed["revision"],
    )
    assert published["project_status"] == "PACKAGE_PUBLISHED"
    assert published["realtime_deployment"]["state"] == "DEPLOYMENT_QUEUED"
    assert deployment.calls == [(tmp_path / project_id, "1.0.0")]
    publish_summary = OrionWorkflowTools._summary(
        "publish_ontology_package",
        published,
    )
    assert "Ontop 部署：**DEPLOYMENT_QUEUED**" in publish_summary
    assert "运行时握手 `false`" in publish_summary
    assert published["release_contract"]["status"] == "SEMANTIC_CONTRACT_RECORDED"
    assert published["release_contract"]["contract_version"] == "cq-answer-v2"
    assert published["current_stage"] == "S7"
    assert published["stage_statuses"]["S7"] == "RUNNING"

    deployment.mark_failed(
        project_dir=tmp_path / project_id,
        release_version="1.0.0",
    )
    runtime_blocked = service.get_status(project_id)
    assert runtime_blocked["project_status"] == "PACKAGE_READY_RUNTIME_BLOCKED"
    assert runtime_blocked["stage_statuses"]["S7"] == "RUNNING"
    assert (tmp_path / project_id / "07-release/publication.json").exists()
    assert "仅重试运行时部署与验证" in runtime_blocked["resume_point"]

    retried = service.retry_release_runtime_deployment(
        project_id=project_id,
        requested_by="test-operator",
    )
    assert retried["reused_approval"] is True
    assert retried["release_version"] == "1.0.0"
    assert retried["realtime_deployment"]["state"] == "DEPLOYMENT_QUEUED"
    assert deployment.calls == [(tmp_path / project_id, "1.0.0")] * 2
    events_text = (tmp_path / project_id / "events/agent-trace.jsonl").read_text(encoding="utf-8")
    assert "REALTIME_DEPLOYMENT_RETRY_REQUESTED" in events_text
    retry_summary_tools = OrionWorkflowTools(service)
    with pytest.raises(WorkflowError, match="缺少必填参数"):
        retry_summary_tools.call("publish_ontology_package", {"project_id": project_id})
    with pytest.raises(WorkflowError, match="不支持的参数"):
        retry_summary_tools.call(
            "retry_release_runtime_deployment",
            {"project_id": project_id, "release_version": "1.0.0"},
        )

    deployment.mark_ready(
        project_dir=tmp_path / project_id,
        release_version="1.0.0",
    )
    ready_state = service._read_state(tmp_path / project_id)
    published = {
        **published,
        **service._status_payload(tmp_path / project_id, ready_state),
    }
    assert published["project_status"] == "PUBLISHED"
    assert published["current_stage"] is None
    assert all(
        published["stage_statuses"][stage] == "PASSED"
        for stage in ("S1", "S2", "S3", "S4", "S5", "S6", "S7")
    )
    package = tmp_path / project_id / "07-release/ontology-engineering-package-1.0.0"
    assert (package / "打开查看.html").exists()
    assert (package / "发布说明.md").exists()
    assert (package / "01-本体模型/ontology.owl").exists()
    assert (package / "01-本体模型/ontology.ttl").exists()
    assert (package / "01-本体模型/shapes.ttl").exists()
    assert (package / "02-工程定义/mapping.yaml").exists()
    assert (package / "02-工程定义/ontology-design.yaml").exists()
    assert (package / "03-质量结论/quality-summary.json").exists()
    assert (package / "03-质量结论/competency-question-report.json").exists()
    assert (package / "03-质量结论/hermit-report.json").exists()
    assert (package / "04-发布信息/publication.json").exists()
    assert (package / "04-发布信息/audit-reference.json").exists()
    assert (package / "04-发布信息/release-snapshot.json").exists()
    assert (package / "04-发布信息/owl-dl-review-contract.json").exists()
    assert (package / "04-发布信息/Protégé-HermiT复核说明.md").exists()
    assert (package / "05-运行时/realtime-runtime.json").exists()
    assert (package / "05-运行时/mapping.obda").exists()
    assert (package / "05-运行时/queries/suppliers_live.rq").exists()
    assert (package / "05-运行时/queries/orion_deployment_identity.rq").exists()
    release_report = tmp_path / project_id / "07-release/release-report.html"
    release_html = release_report.read_text(encoding="utf-8")
    assert "S7 评审与发布总结报告" in release_html
    assert "S0 至 S7 阶段成果汇总" in release_html
    assert "S1 阶段总结" in release_html
    assert "01-data-understanding/data-understanding-report.html" not in release_html
    assert not (package / "materialized.ttl").exists()
    assert not (package / "evidence").exists()
    assert not (package / "reports").exists()
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    package_files = {
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    assert manifest["file_count"] == len(package_files)
    assert {item["path"] for item in manifest["files"]} == package_files
    for item in manifest["files"]:
        assert item["sha256"] == (
            "sha256:" + hashlib.sha256((package / item["path"]).read_bytes()).hexdigest()
        )
    if joint_design_enabled:
        assert manifest["package_profile"] == "ENGINEERING_DELIVERY"
        assert manifest["package_contract"] == "package-contract.json"
        contract = json.loads((package / "package-contract.json").read_text(encoding="utf-8"))
        assert contract["lifecycle_contract_version"] == stage_contract_version
        assert contract["project_id"] == project_id
        assert contract["release_version"] == "1.0.0"
        assert contract["engineering_package"]["profile"] == "ENGINEERING_DELIVERY"
        assert contract["runtime_release_package"]["status"] == "PACKAGED"
        assert (
            contract["runtime_release_package"]["deployment_status"] == "REQUIRES_EXTERNAL_READBACK"
        )
        assert contract["data_policy"]["credentials"] == "EXTERNAL_CONFIGURATION_ONLY"
        assert contract["data_policy"]["source_data_mode"] == "REFERENCES_ONLY"
        assert {item["stage"] for item in contract["stage_deliverables"]} == {
            f"S{index}" for index in range(8)
        }
        assert contract["stage_deliverables"][-1]["status_at_capture"] == "RUNNING"
        expected_engineering_assets = {
            "工程包与发布包说明.md",
            "06-工程追溯/阶段产物索引.json",
            "06-工程追溯/00-document-evidence/source-scope.json",
            "06-工程追溯/04-ontology-design/joint-design-baseline.json",
            "02-工程定义/规则目录/rule-catalog.json",
            "02-工程定义/规则目录/业务规则目录.md",
            "02-工程定义/规则目录/规则执行与Protégé复核说明.md",
        }
        assert expected_engineering_assets <= package_files
        baseline = json.loads(
            (package / "06-工程追溯/04-ontology-design/joint-design-baseline.json").read_text(
                encoding="utf-8"
            )
        )
        assert baseline["status"] == "APPROVED"
        assert baseline["approved_by"] == "test-operator"
        assert baseline["joint_design_fingerprint"] == s4["joint_design_fingerprint"]
        assert not any("joint-design-draft" in item for item in package_files)
        assert not any(item.endswith("-report.html") for item in package_files)
        for view in ("engineering_package", "runtime_release_package"):
            for item in contract[view]["artifacts"]:
                assert item["sha256"] == (
                    "sha256:" + hashlib.sha256((package / item["path"]).read_bytes()).hexdigest()
                )
        release_index = (package / "打开查看.html").read_text(encoding="utf-8")
        assert "package-contract.json" in release_index
        assert "06-工程追溯/阶段产物索引.json" in release_index
    else:
        assert manifest["file_count"] == 19
        assert manifest["package_profile"] == "MODEL_DELIVERY"
        assert not (package / "package-contract.json").exists()
        assert not (package / "06-工程追溯").exists()
    assert manifest["instance_data_included"] is False
    assert manifest["document_evidence_included"] is False
    assert manifest["audit_bundle_included"] is False
    assert manifest["realtime_query_capability"] == "PACKAGED_ARTIFACT_VERIFIED"
    binding = ReleaseBindingLoader().load(tmp_path / project_id)
    assert binding.ontop_deployment_id == f"ontop-{project_id}-1.0.0"
    assert binding.ontop_query_names == {"suppliers_live"}
    assert binding.database_access_mode == "READ_ONLY"
    assert binding.release_fingerprint == (
        "sha256:" + hashlib.sha256((package / "manifest.json").read_bytes()).hexdigest()
    )
    release_snapshot = json.loads(
        (package / "04-发布信息/release-snapshot.json").read_text(encoding="utf-8")
    )
    packaged_cq_report = json.loads(
        (package / "03-质量结论/competency-question-report.json").read_text(encoding="utf-8")
    )
    assert packaged_cq_report["schema_version"] == 3
    assert packaged_cq_report["validation_mode"] == "SERVER_EXECUTED_SEMANTIC_ANSWER_CONTRACT"
    assert packaged_cq_report["results"][0]["row_count"] == 1
    assert "result_sample" not in packaged_cq_report["results"][0]
    assert packaged_cq_report["results_sha256"].startswith("sha256:")
    assert set(release_snapshot["formal_stage_fingerprints"]) == {
        "S0",
        "S1",
        "S2",
        "S3",
        "S4",
        "S5",
        "S6",
    }
    assert release_snapshot["pre_publish_chain"]["last_event_hash"].startswith("sha256:")
    assert release_snapshot["competency_question_lineage"]["status"] == "VERIFIED"
    assert release_snapshot["competency_question_lineage"]["execution_results_sha256"].startswith(
        "sha256:"
    )
    audit_reference = json.loads(
        (package / "04-发布信息/audit-reference.json").read_text(encoding="utf-8")
    )
    assert audit_reference["immutable_release_snapshot"]["sha256"].startswith("sha256:")
    assert "project_manifest" not in audit_reference
    assert "event_trace" not in audit_reference
    workspace_publication = json.loads((stage_dir / "publication.json").read_text(encoding="utf-8"))
    events = [
        json.loads(line)
        for line in (tmp_path / project_id / "events/agent-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    package_published_event = next(
        item for item in events if item["event_type"] == "ONTOLOGY_PACKAGE_PUBLISHED"
    )
    assert (
        package_published_event["details"]["package_manifest_sha256"]
        == workspace_publication["package_manifest_sha256"]
    )
    assert events[-1]["event_type"] == "REALTIME_DEPLOYMENT_READY"
    gate_results = json.loads((stage_dir / "gate-results.json").read_text(encoding="utf-8"))
    semantica_gate = next(
        item for item in gate_results["gates"] if item["id"] == "G-S7-SEMANTICA-SYNC"
    )
    assert semantica_gate["status"] == "PASSED"
    assert semantica_gate["registry_verified"] is True
    semantica_receipt = json.loads((stage_dir / "semantica-sync.json").read_text(encoding="utf-8"))
    assert semantica_receipt["synced_by"] == "ORION_S7_PLATFORM_GATE"
    assert all(item["sha256"].startswith("sha256:") for item in manifest["files"])
    original_manifest = (package / "manifest.json").read_text(encoding="utf-8")

    published_preview = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S6",
        requested_by="test-operator",
    )
    assert published_preview["commit_allowed"] is False
    assert published_preview["release_impact"]["requires_release_action"] is True
    assert published_preview["release_impact"]["release_version"] == "1.0.0"
    with pytest.raises(WorkflowError, match="必须先明确撤回"):
        service.reopen_stage_for_correction(
            project_id=project_id,
            stage="S6",
            reason="不得原地覆盖正式资产",
            requested_by="test-operator",
            preview_token=published_preview["preview_token"],
            project_revision=published_preview["project_revision"],
        )

    workspace_owl = tmp_path / project_id / "05-ontology-build/ontology.owl"
    original_workspace_owl = workspace_owl.read_bytes()
    workspace_owl.write_text("mutable workspace drift\n", encoding="utf-8")
    exported = service.export_published_delivery_package(
        project_id=project_id,
        release_version="1.0.0",
        exported_by="test-operator",
    )
    delivery = tmp_path / project_id / exported["package_path"]
    assert exported["file_count"] == 11
    assert (delivery / "打开查看.html").exists()
    assert (delivery / "01-本体模型/ontology.owl").exists()
    assert not (delivery / "materialized.ttl").exists()
    assert not (delivery / "reports").exists()
    assert not (delivery / "package-contract.json").exists()
    assert "package-contract.json" not in (delivery / "打开查看.html").read_text(encoding="utf-8")
    assert (delivery / "01-本体模型/ontology.owl").read_bytes() == (
        package / "01-本体模型/ontology.owl"
    ).read_bytes()
    assert (delivery / "01-本体模型/ontology.owl").read_bytes() != (workspace_owl.read_bytes())
    delivery_audit_reference = json.loads(
        (delivery / "04-发布信息/audit-reference.json").read_text(encoding="utf-8")
    )
    assert delivery_audit_reference["source_package_manifest_sha256"].startswith("sha256:")
    assert "source_project_manifest_sha256" not in delivery_audit_reference
    assert "source_event_trace_sha256" not in delivery_audit_reference
    workspace_owl.write_bytes(original_workspace_owl)

    concurrent_preview = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S6",
        requested_by="test-operator",
    )
    revoke_entered = Event()
    allow_revoke = Event()
    original_revoke = service._revoke_ontology_release_unlocked

    def held_revoke(**arguments: object) -> dict:
        revoke_entered.set()
        assert allow_revoke.wait(timeout=5)
        return original_revoke(**arguments)

    service._revoke_ontology_release_unlocked = held_revoke  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=2) as executor:
        revoke_future = executor.submit(
            service.revoke_ontology_release,
            project_id=project_id,
            release_version="1.0.0",
            revoked_by="test-operator",
            reason="该版本停止继续使用，后续由修订项目产生新版本。",
            expected_revision=published["revision"],
        )
        assert revoke_entered.wait(timeout=5)
        rollback_future = executor.submit(
            service.reopen_stage_for_correction,
            project_id=project_id,
            stage="S6",
            reason="撤回进行中不得并发回退。",
            requested_by="test-operator",
            preview_token=concurrent_preview["preview_token"],
            project_revision=concurrent_preview["project_revision"],
        )
        allow_revoke.set()
        revoked = revoke_future.result(timeout=5)
        with pytest.raises(WorkflowError, match="状态|撤回|preview"):
            rollback_future.result(timeout=5)
    service._revoke_ontology_release_unlocked = original_revoke  # type: ignore[method-assign]
    assert revoked["project_status"] == "RELEASE_REVOKED"
    assert revoked["stage_statuses"]["S7"] == "REVOKED"
    assert (tmp_path / project_id / "07-release/release-revocation.json").exists()
    revocation_report = tmp_path / project_id / "07-release/release-revocation-report.html"
    assert revocation_report.exists()
    assert "撤回已发布版本" in revocation_report.read_text(encoding="utf-8")
    assert (package / "manifest.json").read_text(encoding="utf-8") == original_manifest
    with pytest.raises(WorkflowError, match="工程状态已变化"):
        service.create_revision_from_release(
            project_id=project_id,
            release_version="1.0.0",
            target_stage="S4",
            reason="使用撤回前的旧 revision 发起修订。",
            requested_by="test-operator",
            expected_revision=published["revision"],
            request_id="acceptance:revision:stale",
        )
    with pytest.raises(WorkflowError, match="只有已生成发布包的项目"):
        service.revoke_ontology_release(
            project_id=project_id,
            release_version="1.0.0",
            revoked_by="test-operator",
            reason="重复撤回必须被拒绝。",
            expected_revision=revoked["revision"],
        )

    revision_arguments = {
        "project_id": project_id,
        "release_version": "1.0.0",
        "target_stage": "S4",
        "reason": "补充新的业务问题后发布下一版本。",
        "requested_by": "test-operator",
        "expected_revision": revoked["revision"],
        "request_id": "acceptance:revision:s4",
    }
    revision = service.create_revision_from_release(**revision_arguments)
    revision_replay = service.create_revision_from_release(**revision_arguments)
    assert revision["project_id"] != project_id
    assert revision_replay["project_id"] == revision["project_id"]
    assert revision_replay["idempotent_replay"] is True
    assert revision["current_stage"] == "S4"
    assert revision["suggested_release_version"] == "1.0.1"
    assert revision["stage_statuses"]["S3"] == "PASSED"
    assert revision["stage_statuses"]["S4"] == "RUNNING"
    assert revision["stage_contract_version"] == stage_contract_version
    revision_dir = tmp_path / revision["project_id"]
    assert (revision_dir / "03-mapping-review/mapping.yaml").exists()
    assert (revision_dir / "based-on-release.json").exists()
    assert (
        json.loads((revision_dir / "based-on-release.json").read_text(encoding="utf-8"))[
            "suggested_release_version"
        ]
        == "1.0.1"
    )
    trace = (tmp_path / project_id / "events/agent-trace.jsonl").read_text(encoding="utf-8")
    assert "MODEL_DELIVERY_PACKAGE_EXPORTED" in trace
    assert "ONTOLOGY_PUBLICATION_DEFERRED" in trace
    assert "ONTOLOGY_PUBLICATION_RESUMED" in trace
    assert "ONTOLOGY_RELEASE_REVOKED" in trace


def test_write_sql_fails_closed_and_can_be_retried(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(project_name="测试本体", domain="test-domain")["project_id"]
    _record_s0(service, project_id)

    with pytest.raises(WorkflowGateError, match="不是只读"):
        service.record_data_understanding(
            project_id=project_id,
            datasource_inventory={"selected": "test"},
            schema_snapshot={"tables": [{"name": "t"}]},
            data_profile={},
            relation_candidates=[],
            evidence_sql=[{"id": "SQL-001", "sql": "UPDATE t SET value = 1"}],
        )

    failed = service.get_status(project_id)
    assert failed["project_status"] == "QA_FAILED"
    assert failed["stage_statuses"]["S1"] == "FAILED"
    assert failed["last_error"]["gate"] == "G-S1-READONLY"
    assert failed["stage_execution_policy"] == {
        "stages": ["S1", "S2", "S3", "S4", "S5", "S6", "S7"],
        "mode": "ATOMIC_GATE",
        "partial_success_supported": False,
        "failure_effect": "STAGE_FAILED",
        "diagnostic_artifacts_policy": "PRESERVE_NON_CURRENT",
        "current_artifacts_require_stage_pass": True,
        "preflight_commit": "TOKENIZED_SNAPSHOT",
        "diagnostics": "COLLECT_ALL_STRUCTURAL_THEN_FORMAL_GATE",
        "rollback_scope": "COMPONENT_DEPENDENCY_WHEN_DECLARED",
    }
    events = [
        json.loads(line)
        for line in (tmp_path / project_id / "events/agent-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    failure = next(event for event in events if event["event_type"] == "STAGE_FAILED")
    assert failure["details"]["execution_mode"] == "ATOMIC_GATE"
    assert failure["details"]["partial_success_supported"] is False
    assert failure["details"]["current_artifacts_promoted"] is False

    retried = service.retry_failed_stage(project_id=project_id)
    assert retried["project_status"] == "IN_PROGRESS"
    assert retried["stage_statuses"]["S1"] == "RUNNING"


def test_s4_review_validation_failure_is_atomic_and_retryable(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="S4 原子失败验收",
        domain="s4-atomic-failure",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    _complete_s3(service, project_id)

    with pytest.raises(WorkflowGateError):
        service.prepare_ontology_design_review(
            project_id=project_id,
            ontology_design={"ontology_iri": "not-an-iri", "competency_questions": []},
        )

    failed = service.get_status(project_id)
    assert failed["current_stage"] == "S4"
    assert failed["stage_statuses"]["S4"] == "FAILED"
    assert failed["project_status"] == "QA_FAILED"
    assert not (tmp_path / project_id / "04-ontology-design/ontology-design-draft.yaml").exists()
    retried = service.retry_failed_stage(project_id=project_id)
    assert retried["stage_statuses"]["S4"] == "RUNNING"


def test_s3_rejects_more_than_three_blocking_decisions(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(project_name="测试本体", domain="test-domain")["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)

    template = {
        "title": "高影响业务语义",
        "business_question": "该业务关系应采用哪一种含义？",
        "evidence": {
            "database_facts": [{"summary": "存在数据库关系证据。", "source_refs": ["REL-001"]}],
            "ai_inference": "存在两个合理解释。",
        },
        "confidence": 0.5,
        "options": [
            {
                "id": "option-a",
                "label": "方案 A",
                "summary": "采用第一种含义。",
                "impact": "影响业务关系解释。",
                "recommended": True,
            },
            {
                "id": "option-b",
                "label": "方案 B",
                "summary": "采用第二种含义。",
                "impact": "影响后续推理结果。",
                "recommended": False,
            },
        ],
        "affected_mapping_ids": ["MAP-SUPPLIER"],
    }
    with pytest.raises(WorkflowGateError, match="最多 2 项"):
        service.prepare_mapping_review(
            project_id=project_id,
            mapping_draft={
                "mappings": [
                    {
                        "id": "MAP-SUPPLIER",
                        "source": "sc_suppliers",
                        "target": "Supplier",
                        "mapping_type": "TABLE_TO_CLASS",
                        "source_refs": ["table:sc_suppliers"],
                    }
                ]
            },
            confirmations=[{"id": f"CONF-{index}", **template} for index in range(4)],
            automatic_decisions=[],
            realtime_runtime=_default_realtime_runtime(),
        )


def test_s3_always_requires_overall_mapping_review(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(project_name="总体评审测试", domain="test-domain")[
        "project_id"
    ]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)

    blocked = service.prepare_mapping_review(
        project_id=project_id,
        mapping_draft={
            "mapping_version": "0.1.0-draft",
            "mappings": [
                {
                    "id": "MAP-SUPPLIER",
                    "source": "sc_suppliers",
                    "target": "Supplier",
                    "mapping_type": "TABLE_TO_CLASS",
                    "source_refs": ["table:sc_suppliers"],
                }
            ],
        },
        confirmations=[],
        automatic_decisions=[],
        realtime_runtime=_default_realtime_runtime(),
    )

    assert blocked["project_status"] == "BLOCKED_HUMAN"
    assert blocked["confirmation_progress"] == {"current": 1, "total": 1, "resolved": 0}
    assert blocked["next_confirmation"]["id"] == "S3-OVERALL-MAPPING-REVIEW"
    assert "是否确认当前数据库到本体的全部映射" in blocked["next_confirmation"]["question"]
    with pytest.raises(WorkflowError, match="人工确认|人工决定"):
        service.archive_project(
            project_id=project_id,
            archived_by="test-operator",
            reason="不得跳过待确认决定直接归档",
            expected_revision=blocked["revision"],
        )


def test_s3_rejection_requires_current_revision_and_preserves_history(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(project_name="S3 退回测试", domain="s3-reject")[
        "project_id"
    ]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    blocked = service.prepare_mapping_review(
        project_id=project_id,
        mapping_draft={
            "mapping_version": "0.1.0-draft",
            "mappings": [
                {
                    "id": "MAP-SUPPLIER",
                    "source": "sc_suppliers",
                    "target": "Supplier",
                    "mapping_type": "TABLE_TO_CLASS",
                    "source_refs": ["table:sc_suppliers"],
                }
            ],
        },
        confirmations=[],
        automatic_decisions=[],
        realtime_runtime=_default_realtime_runtime(),
    )

    with pytest.raises(WorkflowError, match="rationale"):
        service.resolve_confirmation_option(
            project_id=project_id,
            confirmation_id="S3-OVERALL-MAPPING-REVIEW",
            selected_option_id="RETURN-TO-SEMANTICS",
            decided_by="test-operator",
            rationale="",
            expected_revision=blocked["revision"],
        )
    with pytest.raises(WorkflowError, match="工程状态已变化"):
        service.resolve_confirmation_option(
            project_id=project_id,
            confirmation_id="S3-OVERALL-MAPPING-REVIEW",
            selected_option_id="RETURN-TO-SEMANTICS",
            decided_by="test-operator",
            rationale="旧页面提交应被拒绝。",
            expected_revision=blocked["revision"] - 1,
        )

    rejected = service.resolve_confirmation_option(
        project_id=project_id,
        confirmation_id="S3-OVERALL-MAPPING-REVIEW",
        selected_option_id="RETURN-TO-SEMANTICS",
        decided_by="test-operator",
        rationale="当前关系证据不足，退回补充语义来源。",
        expected_revision=blocked["revision"],
    )
    assert rejected["current_stage"] == "S2"
    assert rejected["stage_statuses"]["S2"] == "RUNNING"
    assert rejected["stage_statuses"]["S3"] == "INVALIDATED"
    lifecycle = rejected["artifact_lifecycle"]
    assert lifecycle["03-mapping-review/mapping-draft.yaml"]["status"] == "INVALIDATED"
    assert (
        tmp_path
        / project_id
        / lifecycle["03-mapping-review/mapping-draft.yaml"]["historical_snapshot"]
    ).exists()
    with pytest.raises(WorkflowError, match="工程状态已变化"):
        service.resolve_confirmation_option(
            project_id=project_id,
            confirmation_id="S3-OVERALL-MAPPING-REVIEW",
            selected_option_id="RETURN-TO-SEMANTICS",
            decided_by="test-operator",
            rationale="重复提交应被拒绝。",
            expected_revision=blocked["revision"],
        )

    _record_s2(service, project_id)
    reblocked = _prepare_s3(service, project_id)
    assert reblocked["stage_statuses"]["S3"] == "BLOCKED_HUMAN"
    assert "03-mapping-review/mapping-draft.yaml" not in reblocked["artifact_lifecycle"]
    assert (tmp_path / project_id / "revisions").is_dir()


def test_s4_rejection_requires_current_revision_and_invalidates_downstream(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(project_name="S4 退回测试", domain="s4-reject")[
        "project_id"
    ]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    _complete_s3(service, project_id)
    design = {
        "ontology_iri": "https://example.com/s4-reject",
        "version": "0.1.0",
        "classes": [
            {
                "name": "Supplier",
                "iri": "https://example.com/s4-reject#Supplier",
                "source_mapping_ids": ["MAP-SUPPLIER"],
            },
            {
                "name": "PurchaseOrder",
                "iri": "https://example.com/s4-reject#PurchaseOrder",
                "source_mapping_ids": ["MAP-SUPPLIER-RELATION"],
                "label_zh": "采购订单",
                "comment_zh": "向供应商发出的采购订单业务对象。",
            },
        ],
        "object_properties": [
            {
                "name": "orderedFrom",
                "iri": "https://example.com/s4-reject#orderedFrom",
                "domain": "https://example.com/s4-reject#PurchaseOrder",
                "range": "https://example.com/s4-reject#Supplier",
                "source_mapping_ids": ["MAP-SUPPLIER-RELATION"],
            }
        ],
        "data_properties": [],
        "constraints": [],
        "competency_questions": [
            {
                "id": "CQ-001",
                "question": "有哪些供应商？",
                "sparql": "SELECT ?supplier WHERE { ?supplier a <https://example.com/s4-reject#Supplier> . }",
                "expected": "返回供应商。",
                "answer_contract": {
                    "result_assertions": [
                        {"binding": "supplier", "operator": "NE", "expected": ""}
                    ],
                    "boundary_assertions": [
                        {"binding": "supplier", "operator": "NE", "expected": ""}
                    ],
                },
            }
        ],
        "logical_axioms": [
            {
                "id": "AX-ORDER-SUPPLIER-DISJOINT",
                "axiom_type": "DISJOINT_WITH",
                "class": "https://example.com/s4-reject#PurchaseOrder",
                "other": "https://example.com/s4-reject#Supplier",
                "source_refs": ["MAP-SUPPLIER", "MAP-SUPPLIER-RELATION"],
            }
        ],
    }
    blocked = service.prepare_ontology_design_review(
        project_id=project_id,
        ontology_design=design,
    )
    with pytest.raises(WorkflowError, match="工程状态已变化"):
        service.resolve_competency_question_review(
            project_id=project_id,
            questions=design["competency_questions"],
            decision="RETURN_TO_S3",
            decided_by="test-operator",
            rationale="旧页面提交。",
            expected_revision=blocked["revision"] - 1,
        )
    rejected = service.resolve_competency_question_review(
        project_id=project_id,
        questions=design["competency_questions"],
        decision="RETURN_TO_S3",
        decided_by="test-operator",
        rationale="业务问题尚未覆盖供应商风险判断，退回重新核对。",
        expected_revision=blocked["revision"],
    )
    assert rejected["current_stage"] == "S3"
    assert rejected["stage_statuses"]["S3"] == "RUNNING"
    assert rejected["stage_statuses"]["S4"] == "INVALIDATED"
    assert rejected["stage_statuses"]["S5"] == "PENDING"
    lifecycle = rejected["artifact_lifecycle"]
    draft = lifecycle["04-ontology-design/ontology-design-draft.yaml"]
    assert draft["status"] == "INVALIDATED"
    assert (tmp_path / project_id / draft["historical_snapshot"]).exists()

    reblocked = _prepare_s3(service, project_id)
    assert "03-mapping-review/mapping-draft.yaml" not in reblocked["artifact_lifecycle"]
    assert (
        reblocked["artifact_lifecycle"]["03-mapping-review/mapping.yaml"]["status"] == "INVALIDATED"
    )
    pending = json.loads(
        (tmp_path / project_id / "03-mapping-review/pending-confirmations.json").read_text(
            encoding="utf-8"
        )
    )
    assert all(item["id"] != "CONF-001" for item in pending)
    decisions = json.loads(
        (tmp_path / project_id / "03-mapping-review/automatic-decisions.json").read_text(
            encoding="utf-8"
        )
    )
    assert any(
        item.get("status") == "REUSED_ACCEPTED" and item.get("selected_option_id") == "ordered-from"
        for item in decisions
    )
    reused_mapping = yaml.safe_load(
        (tmp_path / project_id / "03-mapping-review/mapping-draft.yaml").read_text(encoding="utf-8")
    )
    reused_relation = next(
        item for item in reused_mapping["mappings"] if item["id"] == "MAP-SUPPLIER-RELATION"
    )
    assert reused_relation["target"] == "orderedFrom"
    overall = service.get_status(project_id)
    remapped = service.resolve_confirmation_option(
        project_id=project_id,
        confirmation_id="S3-OVERALL-MAPPING-REVIEW",
        selected_option_id="APPROVE-MAPPING",
        decided_by="test-operator",
        rationale="重新核对后的 Mapping 可以进入 S4。",
        expected_revision=overall["revision"],
    )
    assert remapped["stage_statuses"]["S4"] == "PENDING"
    rereview = service.prepare_ontology_design_review(
        project_id=project_id,
        ontology_design=design,
        expected_revision=remapped["revision"],
    )
    assert rereview["stage_statuses"]["S4"] == "BLOCKED_HUMAN"
    assert "04-ontology-design/ontology-design-draft.yaml" not in rereview["artifact_lifecycle"]


def test_project_archive_and_restore_preserve_audit_history(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    created = service.create_project(project_name="可归档工程", domain="archive-test")
    project_id = created["project_id"]

    archived = service.archive_project(
        project_id=project_id,
        archived_by="test-operator",
        reason="暂停当前工程，保留资料等待后续继续。",
        expected_revision=created["revision"],
    )
    assert archived["project_status"] == "ARCHIVED"
    assert (tmp_path / project_id / "workflow-state.json").exists()
    assert (tmp_path / project_id / "artifact-manifest.json").exists()
    with pytest.raises(WorkflowError, match="已归档"):
        _record_s0(service, project_id)

    restored = service.restore_project(
        project_id=project_id,
        restored_by="test-operator",
        reason="资料已准备完成，继续原阶段。",
        expected_revision=archived["revision"],
    )
    assert restored["project_status"] == "IN_PROGRESS"
    assert restored["current_stage"] == "S0"
    events = [
        json.loads(line)
        for line in (tmp_path / project_id / "events" / "agent-trace.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [event["event_type"] for event in events][-2:] == [
        "PROJECT_ARCHIVED",
        "PROJECT_RESTORED",
    ]
    assert events[-1]["previous_event_hash"] == events[-2]["event_hash"]


def test_revision_creation_uses_root_then_project_lock_without_root_reentry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    source = service.create_project(
        project_name="修订锁序源工程",
        domain="revision-lock-order",
    )
    source_dir = tmp_path / source["project_id"]
    service._write_json(
        source_dir / "07-release/publication.json",
        {"project_id": source["project_id"], "release_version": "1.0.0"},
    )
    lock_order: list[str] = []
    original_root_lock = service._root_operation_lock
    original_project_lock = service._project_operation_lock

    @contextmanager
    def observed_root_lock():
        lock_order.append("root")
        with original_root_lock():
            yield

    @contextmanager
    def observed_project_lock(project_dir: Path):
        lock_order.append("project")
        with original_project_lock(project_dir):
            yield

    monkeypatch.setattr(service, "_root_operation_lock", observed_root_lock)
    monkeypatch.setattr(service, "_project_operation_lock", observed_project_lock)

    revision = service.create_revision_from_release(
        project_id=source["project_id"],
        release_version="1.0.0",
        target_stage="S0",
        reason="验证修订创建只按统一锁序执行。",
        requested_by="pytest",
        expected_revision=source["revision"],
        request_id="revision-lock-order-test",
    )

    assert revision["project_id"] != source["project_id"]
    assert lock_order[0] == "root"
    assert lock_order.count("root") == 1
    assert lock_order.count("project") >= 2
    assert all(name == "project" for name in lock_order[1:])
    revision_source = inspect.getsource(
        OntologyWorkflowService._create_revision_from_release_unlocked
    )
    assert "self._create_project_unlocked(" in revision_source
    assert "self.create_project(" not in revision_source


@pytest.mark.parametrize(
    ("source_name", "expected_name"),
    [
        ("档案合规义务与责任本体", "档案合规义务与责任本体 · 修订"),
        ("档案合规义务与责任本体 · 修订", "档案合规义务与责任本体 · 修订"),
        ("档案合规义务与责任本体 · 修订 · 修订", "档案合规义务与责任本体 · 修订"),
        ("档案合规义务与责任本体（修订）", "档案合规义务与责任本体 · 修订"),
    ],
)
def test_revision_project_name_does_not_accumulate_suffixes(
    source_name: str,
    expected_name: str,
) -> None:
    from services.ontology_engineering.workflow import _revision_project_name

    assert _revision_project_name(source_name) == expected_name


def test_release_identity_blocks_same_ontology_iri_and_version_across_projects(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    original = service.create_project(
        project_name="版本唯一性原工程",
        domain="release-identity-original",
    )
    original_dir = tmp_path / original["project_id"]
    service._write_yaml(
        original_dir / "04-ontology-design/ontology-design.yaml",
        {"ontology_iri": "https://example.test/ontology", "version": "1.0.0"},
    )
    service._write_json(
        original_dir / "07-release/publication.json",
        {
            "project_id": original["project_id"],
            "ontology_iri": "https://example.test/ontology",
            "release_version": "1.0.0",
        },
    )

    revision = service.create_project(
        project_name="版本唯一性修订工程",
        domain="release-identity-revision",
    )
    revision_dir = tmp_path / revision["project_id"]
    service._write_yaml(
        revision_dir / "04-ontology-design/ontology-design.yaml",
        {"ontology_iri": "https://example.test/ontology", "version": "1.0.0"},
    )
    state = service._read_state(revision_dir)
    state["current_stage"] = "S7"
    state["project_status"] = "IN_PROGRESS"
    state["stage_statuses"]["S7"] = "RUNNING"
    service._write_json(revision_dir / "workflow-state.json", state)

    with pytest.raises(WorkflowGateError) as blocked:
        service.publish_ontology_package(
            project_id=revision["project_id"],
            release_version="1.0.0",
            approval_decision="APPROVED",
            approved_by="tester",
            release_notes="同一身份不同内容必须被版本门禁拒绝。",
            expected_revision=state["revision"],
        )

    assert blocked.value.gate_id == "G-S7-RELEASE-IDENTITY"
    assert "版本号不可复用" in str(blocked.value)
    assert "建议改为 1.0.1" in str(blocked.value)
    service._assert_release_identity_available(
        project_dir=revision_dir,
        release_version="1.0.1",
    )


def test_revision_initialization_preserves_s0_cq_and_blocks_partial_writes(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    source = service.create_project(
        project_name="业务问题修订源工程",
        domain="revision-cq-source",
        cq_mode="USER_PROVIDED",
        initial_competency_questions=[
            {
                "id": "CQ-INTAKE-001",
                "question": "哪些供应商影响关键订单？",
                "expected": "返回供应商和受影响订单。",
            }
        ],
    )
    source_dir = tmp_path / source["project_id"]
    service._write_json(
        source_dir / "07-release/publication.json",
        {"project_id": source["project_id"], "release_version": "1.0.0"},
    )
    source_intake = service._read_json(source_dir / "00-document-evidence/cq-intake.json")

    revision = service.create_revision_from_release(
        project_id=source["project_id"],
        release_version="1.0.0",
        target_stage="S0",
        reason="补充资料但继续使用原业务问题。",
        requested_by="tester",
        request_id="revision-cq-s0-test",
    )
    revision_dir = tmp_path / revision["project_id"]
    revision_intake = service._read_json(revision_dir / "00-document-evidence/cq-intake.json")
    revision_project = service._read_json(revision_dir / "project.json")

    assert revision["project_status"] == "IN_PROGRESS"
    assert revision["current_stage"] == "S0"
    assert revision_intake == source_intake
    assert revision["cq_mode"] == "USER_PROVIDED"
    assert revision["initial_competency_question_count"] == 1
    assert revision_project["cq_mode"] == "USER_PROVIDED"
    assert (revision_dir / ".revision-initialization-complete.json").exists()

    partial = service.create_project(
        project_name="初始化中修订保护工程",
        domain="revision-initializing-guard",
        _initial_project_status="INITIALIZING",
    )
    with pytest.raises(WorkflowError, match="仍在.*初始化"):
        _record_s0(service, partial["project_id"])


def test_archive_rejects_real_active_document_job_and_stale_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workflow_home = tmp_path / "workflows"
    input_root = tmp_path / "inputs"
    service = OntologyWorkflowService(workflow_home)
    created = service.create_project(project_name="活动任务保护工程", domain="archive-guard")
    project_id = created["project_id"]
    job_dir = input_root / ".orion-s0-jobs" / "JOB-ACTIVE"
    job_dir.mkdir(parents=True)
    (job_dir / "request.json").write_text(
        json.dumps({"project_id": project_id}, ensure_ascii=False),
        encoding="utf-8",
    )
    (job_dir / "status.json").write_text(
        json.dumps(
            {"job_id": "JOB-ACTIVE", "project_id": project_id, "status": "RUNNING"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("ORION_DOCUMENT_INPUT_ROOT", str(input_root))

    with pytest.raises(WorkflowError, match="JOB-ACTIVE.*RUNNING"):
        service.archive_project(
            project_id=project_id,
            archived_by="test-operator",
            reason="不应静默归档活动任务",
            expected_revision=created["revision"],
        )
    unchanged = service.get_status(project_id)
    assert unchanged["project_status"] == "IN_PROGRESS"
    assert unchanged["revision"] == created["revision"]

    (job_dir / "status.json").write_text(
        json.dumps(
            {"job_id": "JOB-ACTIVE", "project_id": project_id, "status": "FAILED"},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    archived = service.archive_project(
        project_id=project_id,
        archived_by="test-operator",
        reason="活动任务已经结束，可以归档",
        expected_revision=created["revision"],
    )
    assert archived["project_status"] == "ARCHIVED"
    with pytest.raises(WorkflowError, match="revision"):
        service.restore_project(
            project_id=project_id,
            restored_by="test-operator",
            reason="过期页面提交",
            expected_revision=created["revision"],
        )


def test_mcp_create_requires_page_confirmation_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = OntologyWorkflowService(tmp_path)
    tools = OrionWorkflowTools(service)
    monkeypatch.setenv("ORION_REQUIRE_UI_CREATE_CONFIRMATION", "1")

    with pytest.raises(WorkflowGateError, match="确认创建工程"):
        tools.call(
            "create_ontology_project",
            {
                "project_name": "禁止越权创建测试",
                "domain": "测试",
                "intake_mode": "DOCUMENT_ONLY",
                "intake_rationale": "确认卡尚未点击",
                "request_id": "ui-gate-test-001",
            },
        )

    assert service.list_projects()["count"] == 0


def test_mcp_read_only_mode_hides_and_rejects_mutating_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = OntologyWorkflowService(tmp_path)
    server = McpServer(OrionWorkflowTools(service))
    monkeypatch.setenv("ORION_MCP_READ_ONLY", "1")

    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response is not None
    assert {tool["name"] for tool in response["result"]["tools"]} == {
        "query_source_evidence",
        "list_source_connections",
        "get_document_ingestion_job",
        "get_managed_stage_execution",
        "get_runtime_capability",
        "get_project_artifact",
        "get_ontology_workflow_status",
        "get_next_workflow_action",
        "get_stage_input_contract",
        "get_stage_draft",
        "get_business_quality",
        "get_business_preview",
        "get_cq_semantic_review",
        "get_design_workspace",
        "get_revision_reuse_plan",
        "list_ontology_projects",
        "get_workflow_storage_status",
        "get_project_revision_history",
        "verify_ontology_project_integrity",
        "preflight_stage_submission",
        "preflight_workspace_snapshot",
        "get_message_attachment_candidates",
    }

    blocked = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "create_ontology_project",
                "arguments": {"project_name": "不得创建", "domain": "测试"},
            },
        }
    )
    assert blocked is not None
    assert blocked["error"]["code"] == -32000
    assert "只读模式" in blocked["error"]["message"]
    assert blocked["error"]["data"]["gate"] == "G-MCP-READ-ONLY"
    assert blocked["error"]["data"]["technical_input_owner"] == ("PLATFORM_OR_ENGINEERING_AGENT")
    assert blocked["error"]["data"]["ask_business_user_for_internal_contract"] is False
    assert service.list_projects()["count"] == 0


def test_mcp_returns_internal_error_without_dropping_transport(tmp_path: Path) -> None:
    server = McpServer(OrionWorkflowTools(OntologyWorkflowService(tmp_path)))

    def fail_call(_name: str, _arguments: dict[str, Any]) -> tuple[str, Any]:
        raise AttributeError("unexpected report failure")

    server.tools.call = fail_call  # type: ignore[method-assign]
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "record_data_understanding", "arguments": {}},
        }
    )

    assert response is not None
    assert response["error"]["code"] == -32603
    assert "AttributeError" in response["error"]["message"]


def test_snapshot_workspace_sources_is_scoped_idempotent_and_auditable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "Downloads"
    source = workspace / "政策资料" / "准入制度.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 准入制度\n\n供应商必须通过准入。\n", encoding="utf-8")
    ingestion = tmp_path / "controlled-intake"
    monkeypatch.setenv("ORION_WORKSPACE_REFERENCE_ROOTS", str(workspace))
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(ingestion))
    tools = OrionWorkflowTools(OntologyWorkflowService(tmp_path / "workflows"))

    summary, first = tools.call(
        "snapshot_workspace_sources",
        {"workspace_root": str(workspace), "references": ["@政策资料/"]},
    )
    _, second = tools.call(
        "snapshot_workspace_sources",
        {"workspace_root": str(workspace), "references": ["政策资料/"]},
    )

    assert "1 份 @ 引用资料" in summary
    assert first["reference_id"] == second["reference_id"]
    assert first["source_path"].startswith(".orion-s0-uploads/REFERENCE-")
    snapshot = Path(first["snapshot_root"])
    assert (snapshot / "政策资料" / "准入制度.md").read_text(encoding="utf-8") == source.read_text(
        encoding="utf-8"
    )
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"][0]["sha256"].startswith("sha256:")
    assert manifest["workspace_root"] == str(workspace)

    with pytest.raises(WorkflowError, match="相对路径"):
        tools.call(
            "snapshot_workspace_sources",
            {"workspace_root": str(workspace), "references": ["../outside.md"]},
        )


def test_preflight_workspace_snapshot_routes_large_table_to_hybrid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "Downloads"
    workspace.mkdir()
    (workspace / "明细.csv").write_text(
        "id,value\n" + "\n".join(f"{index},x" for index in range(10_050)),
        encoding="utf-8",
    )
    (workspace / "说明.md").write_text("# 业务说明\n", encoding="utf-8")
    ingestion = tmp_path / "controlled-intake"
    monkeypatch.setenv("ORION_WORKSPACE_REFERENCE_ROOTS", str(workspace))
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(ingestion))
    tools = OrionWorkflowTools(OntologyWorkflowService(tmp_path / "workflows"))

    _, snapshot = tools.call(
        "snapshot_workspace_sources",
        {"workspace_root": str(workspace), "references": ["@明细.csv", "@说明.md"]},
    )
    summary, result = tools.call(
        "preflight_workspace_snapshot",
        {"source_path": snapshot["source_path"]},
    )

    assert "建议 HYBRID / IMPORT" in summary
    assert result["requires_structured_import"] is True
    assert result["recommended_intake_mode"] == "HYBRID"
    assert result["tabular_file_count"] == 1
    assert result["narrative_file_count"] == 1

    with pytest.raises(WorkflowError, match="受控上传快照"):
        tools.call("preflight_workspace_snapshot", {"source_path": "other/source"})


def test_snapshot_workspace_sources_rejects_unapproved_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    approved = tmp_path / "approved"
    unapproved = tmp_path / "unapproved"
    approved.mkdir()
    unapproved.mkdir()
    (unapproved / "资料.md").write_text("不应读取", encoding="utf-8")
    monkeypatch.setenv("ORION_WORKSPACE_REFERENCE_ROOTS", str(approved))
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(tmp_path / "controlled"))
    tools = OrionWorkflowTools(OntologyWorkflowService(tmp_path / "workflows"))

    with pytest.raises(WorkflowError, match="允许的 @ 引用根目录"):
        tools.call(
            "snapshot_workspace_sources",
            {"workspace_root": str(unapproved), "references": ["资料.md"]},
        )


@pytest.mark.parametrize("tamper_kind", ["content", "manifest"])
def test_snapshot_workspace_sources_rejects_tampered_existing_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper_kind: str,
) -> None:
    workspace = tmp_path / "Downloads"
    source = workspace / "政策资料" / "准入制度.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 准入制度\n\n供应商必须通过准入。\n", encoding="utf-8")
    ingestion = tmp_path / "controlled-intake"
    monkeypatch.setenv("ORION_WORKSPACE_REFERENCE_ROOTS", str(workspace))
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(ingestion))
    tools = OrionWorkflowTools(OntologyWorkflowService(tmp_path / "workflows"))
    _, first = tools.call(
        "snapshot_workspace_sources",
        {"workspace_root": str(workspace), "references": ["@政策资料/"]},
    )
    snapshot = Path(first["snapshot_root"])
    snapshot.chmod(0o700)
    (snapshot / "政策资料").chmod(0o700)

    if tamper_kind == "content":
        target = snapshot / "政策资料" / "准入制度.md"
        target.chmod(0o600)
        target.write_text("内容已被篡改", encoding="utf-8")
    else:
        manifest_path = snapshot / "manifest.json"
        manifest_path.chmod(0o600)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = "sha256:" + "0" * 64
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(WorkflowError, match="受控资料快照完整性校验失败"):
        tools.call(
            "snapshot_workspace_sources",
            {"workspace_root": str(workspace), "references": ["政策资料/"]},
        )


def test_snapshot_workspace_sources_rejects_source_change_between_hash_and_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "Downloads"
    source = workspace / "政策资料" / "准入制度.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 固化前内容\n", encoding="utf-8")
    ingestion = tmp_path / "controlled-intake"
    monkeypatch.setenv("ORION_WORKSPACE_REFERENCE_ROOTS", str(workspace))
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(ingestion))
    tools = OrionWorkflowTools(OntologyWorkflowService(tmp_path / "workflows"))
    original_copy = OrionWorkflowTools._copy_workspace_source
    changed = False

    def change_source_then_copy(*args: Any, **kwargs: Any) -> None:
        nonlocal changed
        if not changed:
            changed = True
            source.write_text("# 哈希后发生变化\n", encoding="utf-8")
        original_copy(*args, **kwargs)

    monkeypatch.setattr(
        OrionWorkflowTools,
        "_copy_workspace_source",
        staticmethod(change_source_then_copy),
    )

    with pytest.raises(WorkflowError, match="哈希后、固化前发生变化"):
        tools.call(
            "snapshot_workspace_sources",
            {"workspace_root": str(workspace), "references": ["政策资料/"]},
        )

    snapshot_parent = ingestion / ".orion-s0-uploads"
    assert not list(snapshot_parent.glob("REFERENCE-*"))
    assert not list(snapshot_parent.glob(".REFERENCE-*"))


def test_snapshot_workspace_sources_rejects_symlink_swap_before_secure_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "Downloads"
    source = workspace / "政策资料" / "准入制度.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 批准范围内内容\n", encoding="utf-8")
    outside = tmp_path / "outside-secret.md"
    outside.write_text("# 批准范围外内容不得固化\n", encoding="utf-8")
    ingestion = tmp_path / "controlled-intake"
    monkeypatch.setenv("ORION_WORKSPACE_REFERENCE_ROOTS", str(workspace))
    monkeypatch.setenv("ORION_DOCUMENT_INGESTION_ROOT", str(ingestion))
    tools = OrionWorkflowTools(OntologyWorkflowService(tmp_path / "workflows"))
    original_hash = OrionWorkflowTools._hash_workspace_source
    swapped = False

    def swap_then_hash(workspace_path: Path, relative_path: str):
        nonlocal swapped
        if not swapped:
            swapped = True
            source.unlink()
            source.symlink_to(outside)
        return original_hash(workspace_path, relative_path)

    monkeypatch.setattr(
        OrionWorkflowTools,
        "_hash_workspace_source",
        staticmethod(swap_then_hash),
    )

    with pytest.raises(WorkflowError, match="符号链接|安全读取"):
        tools.call(
            "snapshot_workspace_sources",
            {"workspace_root": str(workspace), "references": ["政策资料/"]},
        )

    snapshot_parent = ingestion / ".orion-s0-uploads"
    assert not snapshot_parent.exists() or not list(snapshot_parent.glob("REFERENCE-*"))


def test_mcp_catalog_and_stdio_protocol(tmp_path: Path) -> None:
    expected_names = {
        "get_stage_input_contract",
        "get_stage_draft",
        "fork_s2_draft_from_history",
        "fork_s3_draft_from_history",
        "get_business_quality",
        "get_business_preview",
        "start_business_preview",
        "save_stage_submission",
        "generate_mapping_skeleton",
        "compile_mapping_runtime",
        "replace_document_sources",
        "patch_stage_submission",
        "preflight_design_patch",
        "query_source_evidence",
        "reconcile_document_source_identities",
        "amend_competency_questions",
        "start_document_ingestion_job",
        "get_document_ingestion_job",
        "commit_document_ingestion_job",
        "create_ontology_project",
        "snapshot_workspace_sources",
        "get_message_attachment_candidates",
        "snapshot_message_attachments",
        "preflight_workspace_snapshot",
        "record_document_evidence",
        "record_s0_scope_decision",
        "record_data_understanding",
        "record_document_understanding",
        "list_source_connections",
        "capture_database_snapshot",
        "record_data_understanding_from_datasets",
        "record_semantic_candidates",
        "prepare_mapping_review",
        "resolve_mapping_confirmation",
        "resolve_mapping_option",
        "get_ontology_workflow_status",
        "get_next_workflow_action",
        "get_cq_semantic_review",
        "get_design_workspace",
        "get_revision_reuse_plan",
        "list_ontology_projects",
        "get_workflow_storage_status",
        "reconcile_workflow_metadata_outbox",
        "get_project_revision_history",
        "retry_release_runtime_deployment",
        "retry_failed_stage",
        "resume_active_revision",
        "preview_stage_rollback",
        "reopen_stage_for_correction",
        "generate_ontology_design",
        "prepare_ontology_design_review",
        "resolve_competency_question_review",
        "record_ontology_build",
        "start_managed_stage_execution",
        "get_managed_stage_execution",
        "get_runtime_capability",
        "get_project_artifact",
        "preflight_stage_submission",
        "commit_preflight_stage_submission",
        "record_quality_validation",
        "publish_ontology_package",
        "sync_published_ontology_to_semantica",
        "defer_ontology_publication",
        "resume_ontology_publication",
        "revoke_ontology_release",
        "create_revision_from_release",
        "archive_ontology_project",
        "restore_ontology_project",
        "verify_ontology_project_integrity",
    }
    assert {tool["name"] for tool in TOOLS} == expected_names
    prepare_tool = next(tool for tool in TOOLS if tool["name"] == "prepare_mapping_review")
    assert "automatic_decisions" in prepare_tool["inputSchema"]["oneOf"][0]["required"]
    assert prepare_tool["inputSchema"]["oneOf"][1]["required"] == ["payload_file", "expected_revision"]
    resolve_tool = next(tool for tool in TOOLS if tool["name"] == "resolve_mapping_confirmation")
    assert "selected_option_id" in resolve_tool["inputSchema"]["required"]

    server = McpServer(OrionWorkflowTools(OntologyWorkflowService(tmp_path)))
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response is not None
    assert len(response["result"]["tools"]) == len(expected_names)

    env = {**os.environ, "ORION_WORKFLOW_HOME": str(tmp_path / "stdio")}
    messages = "\n".join(
        [
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18"},
                }
            ),
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
        ]
    )
    result = subprocess.run(
        [sys.executable, "-m", "harness.orion_workflow_mcp"],
        input=messages + "\n",
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "orion-ontology-workflow"
    assert len(responses[1]["result"]["tools"]) == len(expected_names)


def test_model_facing_s2_and_s4_contracts_are_not_opaque() -> None:
    s2 = next(tool for tool in TOOLS if tool["name"] == "record_semantic_candidates")
    s4 = next(tool for tool in TOOLS if tool["name"] == "generate_ontology_design")

    ontology_item = s2["inputSchema"]["properties"]["ontology_candidates"]["items"]
    rule_item = s2["inputSchema"]["properties"]["business_rule_candidates"]["items"]
    cq_item = s4["inputSchema"]["properties"]["competency_questions"]["items"]
    dimension_item = cq_item["properties"]["answer_contract"]["properties"][
        "required_business_dimensions"
    ]["items"]

    assert ontology_item["additionalProperties"] is False
    assert {"id", "name", "kind", "status", "source_refs"}.issubset(ontology_item["properties"])
    assert rule_item["properties"]["test_cases"]["items"]["properties"]["case_type"]["enum"] == [
        "POSITIVE",
        "NEGATIVE",
        "BOUNDARY",
    ]
    assert dimension_item["additionalProperties"] is False
    assert {
        "dimension",
        "label_zh",
        "applicability",
        "binding",
        "ontology_term",
        "path",
        "evidence_refs",
    }.issubset(dimension_item["properties"])


def test_next_workflow_action_is_computed_by_platform(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="平台动作规划工程",
        domain="platform-action-planning",
        intake_mode="DATABASE_ONLY",
        intake_rationale="只使用已登记数据库证据。",
    )["project_id"]

    action = service.get_next_action(project_id)

    assert action["current_stage"] == "S0"
    assert action["project_name"] == "平台动作规划工程"
    assert action["intake_mode"] == "DATABASE_ONLY"
    assert action["stage_statuses"] == service.get_status(project_id)["stage_statuses"]
    assert action["recommended_tool"] == "record_s0_scope_decision"
    assert action["input_owner"] == "PLATFORM_TOOL_EXECUTOR"
    assert action["policy"]["model_may_choose_stage"] is False
    assert action["policy"]["model_may_invent_contract_fields"] is False
    assert action["writes_performed"] is False


def test_next_workflow_action_routes_ready_transition_to_s4(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="阶段间进入 S4 工程",
        domain="ready-transition-to-s4",
        intake_mode="DATABASE_ONLY",
        intake_rationale="只使用已登记数据库证据。",
    )["project_id"]
    project_dir = tmp_path / project_id
    state = json.loads((project_dir / "workflow-state.json").read_text(encoding="utf-8"))
    state["project_status"] = "S1_S3_READY"
    state["current_stage"] = None
    state["stage_statuses"].update(
        {"S0": "PASSED", "S1": "PASSED", "S2": "PASSED", "S3": "PASSED", "S4": "PENDING"}
    )
    (project_dir / "workflow-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    action = service.get_next_action(project_id)

    assert action["current_stage"] == "S4"
    assert action["stage_status"] == "PENDING"
    assert action["recommended_tool"] == "generate_ontology_design"
    assert action["input_owner"] == "PLATFORM_COMPILER"
    assert action["writes_performed"] is False


def test_next_workflow_action_detects_legacy_s2_rules_before_s3(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="旧规则契约迁移工程",
        domain="legacy-rule-contract-migration",
    )["project_id"]
    project_dir = tmp_path / project_id
    state = json.loads((project_dir / "workflow-state.json").read_text(encoding="utf-8"))
    state["current_stage"] = "S3"
    state["stage_statuses"]["S0"] = "PASSED"
    state["stage_statuses"]["S1"] = "PASSED"
    state["stage_statuses"]["S2"] = "PASSED"
    state["stage_statuses"]["S3"] = "RUNNING"
    (project_dir / "workflow-state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    rules_dir = project_dir / "02-semantic-recognition"
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / "business-rule-candidates.json").write_text(
        json.dumps(
            [
                {
                    "id": "RULE-LEGACY-001",
                    "status": "DOCUMENT_EVIDENCE",
                    "description": "旧版描述式规则",
                }
            ],
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    action = service.get_next_action(project_id)

    assert action["action"] == "REOPEN_S2_FOR_PRODUCTION_RULE_CONTRACT_UPGRADE"
    assert action["recommended_tool"] == "preview_stage_rollback"
    assert action["input_owner"] == "PLATFORM_MIGRATION"
    assert action["target_stage"] == "S2"
    assert action["legacy_rule_ids"] == ["RULE-LEGACY-001"]


def test_preview_stage_rollback_summary_exposes_one_time_token(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="回退令牌回显工程",
        domain="rollback-token-summary",
    )["project_id"]
    preview = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S0",
        requested_by="test-engineer",
    )

    summary = OrionWorkflowTools._summary("preview_stage_rollback", preview)

    assert f"preview_token={preview['preview_token']}" in summary
    assert f"project_revision={preview['project_revision']}" in summary
    assert f"expires_at={preview['expires_at']}" in summary


def test_mcp_blocks_identical_failed_submission_before_a_second_audit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="重复失败熔断工程",
        domain="duplicate-failure-circuit-breaker",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    tools = OrionWorkflowTools(service)
    invalid_arguments = {
        "project_id": project_id,
        "ontology_candidates": [
            {
                "id": "CLASS-INVALID",
                "name": "InvalidCandidate",
                "kind": "CLASS",
                "status": "MODEL_GUESS",
                "source_refs": ["SQL-001"],
            }
        ],
        "business_rule_candidates": [],
    }

    with pytest.raises(WorkflowGateError, match="必须标注"):
        tools.call("record_semantic_candidates", invalid_arguments)
    tools.call("retry_failed_stage", {"project_id": project_id})
    trace_path = tmp_path / project_id / "events/agent-trace.jsonl"
    event_count_before_duplicate = len(trace_path.read_text(encoding="utf-8").splitlines())
    tools = OrionWorkflowTools(OntologyWorkflowService(tmp_path))

    with pytest.raises(
        WorkflowGateError,
        match="G-REPEATED-FAILED-SUBMISSION|已被同一门禁拒绝|熔断|原样重试",
    ) as error:
        tools.call("record_semantic_candidates", invalid_arguments)

    assert error.value.gate_id == "G-REPEATED-FAILED-SUBMISSION"
    assert len(trace_path.read_text(encoding="utf-8").splitlines()) == (
        event_count_before_duplicate
    )

    monkeypatch.setattr(
        OntologyWorkflowService,
        "_validator_fingerprint",
        staticmethod(lambda: "sha256:" + "f" * 64),
    )
    tools = OrionWorkflowTools(OntologyWorkflowService(tmp_path))
    with pytest.raises(WorkflowGateError, match="必须标注") as repaired_validator_error:
        tools.call("record_semantic_candidates", invalid_arguments)
    assert repaired_validator_error.value.gate_id != "G-REPEATED-FAILED-SUBMISSION"


def test_s2_preflight_rejects_bad_contract_without_mutating_state_or_audit(
    tmp_path: Path,
) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="S2 只读预检工程",
        domain="s2-readonly-preflight",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    trace_path = tmp_path / project_id / "events/agent-trace.jsonl"
    state_path = tmp_path / project_id / "workflow-state.json"
    before_trace = trace_path.read_bytes()
    before_state = state_path.read_bytes()

    result = service.preflight_stage_submission(
        project_id=project_id,
        stage="S2",
        payload={
            "ontology_candidates": [
                {
                    "id": "CLASS-UNSUPPORTED",
                    "name": "UnsupportedGuess",
                    "kind": "CLASS",
                    "status": "MODEL_GUESS",
                    "source_refs": ["SQL-001"],
                }
            ],
            "business_rule_candidates": [],
        },
    )

    assert result["status"] == "FAILED"
    assert result["issues"][0]["gate"] == "G-S2-STATUS"
    assert result["writes_performed"] is False
    assert trace_path.read_bytes() == before_trace
    assert state_path.read_bytes() == before_state


def test_s2_preflight_collects_all_structural_issues_in_one_response(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="S2 批量诊断工程",
        domain="s2-aggregate-preflight",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)

    result = service.preflight_stage_submission(
        project_id=project_id,
        stage="S2",
        payload={
            "ontology_candidates": [
                {"id": "DUP", "status": "INVALID"},
                {"id": "DUP", "name": "Second", "kind": "CLASS"},
            ],
            "business_rule_candidates": [{}],
        },
    )

    assert result["status"] == "FAILED"
    assert len(result["issues"]) >= 8
    assert len({item.get("path") for item in result["issues"]}) >= 6
    assert "preflight_token" not in result
    assert service.get_status(project_id)["current_stage"] == "S2"


def test_preflight_text_summary_exposes_actionable_token() -> None:
    receipt = {
        "stage": "S2", "status": "PASSED", "issues": [],
        "preflight_token": "test-preflight-id.test-one-time-signature",
        "preflight_id": "test-preflight-id",
        "preflight_expires_at": "2026-09-05T13:30:00+08:00",
    }
    summary = OrionWorkflowTools._summary("preflight_stage_submission", receipt)
    assert f"preflight_token={receipt['preflight_token']}" in summary
    assert receipt["preflight_expires_at"] in summary
    failed = OrionWorkflowTools._summary(
        "preflight_stage_submission", {**receipt, "status": "FAILED"}
    )
    assert "preflight_token=" not in failed


def test_preflight_token_commits_snapshot_once_without_resending_payload(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="S2 令牌提交工程",
        domain="s2-tokenized-commit",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    payload = {
        "ontology_candidates": [
            {
                "id": "CLASS-SUPPLIER",
                "name": "Supplier",
                "kind": "CLASS",
                "status": "DATABASE_FACT",
                "confidence": 0.99,
                "source_refs": ["table:sc_suppliers", "SQL-001"],
            },
            {
                "id": "REL-ACTUALLY-SUPPLIES",
                "name": "actuallySupplies",
                "kind": "OBJECT_PROPERTY",
                "status": "NEEDS_HUMAN_CONFIRMATION",
                "confidence": 0.55,
                "source_refs": ["REL-001"],
            },
            {
                "id": "CLASS-SUPPLIER-POLICY",
                "name": "SupplierPolicy",
                "kind": "CLASS",
                "status": "DOCUMENT_EVIDENCE",
                "confidence": 0.96,
                "source_refs": ["EVD-001", "DOC-001#准入条件"],
            },
        ],
        "business_rule_candidates": [],
    }
    state_path = tmp_path / project_id / "workflow-state.json"
    trace_path = tmp_path / project_id / "events/agent-trace.jsonl"
    before_state = state_path.read_bytes()
    before_trace = trace_path.read_bytes()

    preflight = service.preflight_stage_submission(
        project_id=project_id,
        stage="S2",
        payload=payload,
    )

    assert preflight["status"] == "PASSED"
    assert preflight["cache_write_performed"] is True
    assert preflight["formal_state_unchanged"] is True
    assert state_path.read_bytes() == before_state
    assert trace_path.read_bytes() == before_trace

    committed = service.commit_preflight_stage_submission(
        project_id=project_id,
        stage="S2",
        preflight_token=preflight["preflight_token"],
        expected_revision=service.get_status(project_id)["revision"],
    )
    assert committed["preflight_consumed"] is True
    assert committed["stage_statuses"]["S2"] == "PASSED"
    assert committed["current_stage"] == "S3"
    assert service.commit_preflight_stage_submission(
        project_id=project_id,
        stage="S2",
        preflight_token=preflight["preflight_token"],
    ) == committed


def test_s6_preflight_accepts_cq_submitted_for_server_validation(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    reports = {
        "hermit_report": {"status": "PASSED"},
        "mapping_report": {"status": "PASSED"},
        "semantic_quality_report": {"status": "PASSED"},
        "competency_question_report": {
            "status": "SUBMITTED_FOR_SERVER_VALIDATION"
        },
        "semantica_report": {"status": "PASSED"},
    }

    issues = service._collect_stage_preflight_issues(
        project_dir=tmp_path,
        state={"current_stage": "S6"},
        stage="S6",
        payload={"materialized_ttl": "<urn:a> <urn:b> <urn:c> .", **reports},
    )

    assert issues == []


def test_component_scoped_rollback_reuses_unaffected_s5(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(
        project_name="组件级回退工程",
        domain="component-rollback",
    )["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    _complete_s3(service, project_id)
    project_dir = tmp_path / project_id
    state = service._read_state(project_dir)
    state["stage_statuses"].update(
        {"S4": "PASSED", "S5": "PASSED", "S6": "PASSED", "S7": "RUNNING"}
    )
    state["current_stage"] = "S7"
    state["project_status"] = "IN_PROGRESS"
    service._write_json(project_dir / "workflow-state.json", state)

    preview = service.preview_stage_rollback(
        project_id=project_id,
        target_stage="S3",
        requested_by="pytest",
        changed_components=["RUNTIME_RULES"],
    )
    dispositions = {item["stage"]: item["disposition"] for item in preview["affected_downstream"]}
    assert dispositions["S4"] == "INVALIDATED"
    assert dispositions["S5"] == "REUSED_UNCHANGED"
    assert dispositions["S6"] == "INVALIDATED"

    reopened = service.reopen_stage_for_correction(
        project_id=project_id,
        stage="S3",
        reason="只修正运行时规则，不改本体二进制。",
        requested_by="pytest",
        preview_token=preview["preview_token"],
        project_revision=preview["project_revision"],
        changed_components=["RUNTIME_RULES"],
    )
    assert reopened["stage_statuses"]["S5"] == "PASSED"
    assert reopened["stage_statuses"]["S4"] == "INVALIDATED"
    assert reopened["stage_statuses"]["S6"] == "INVALIDATED"
    assert reopened["active_revision"]["required_revalidation_stages"] == [
        "S3",
        "S4",
        "S6",
        "S7",
    ]


@pytest.mark.parametrize("unfinished_status", ["PENDING", "FAILED", "INVALIDATED", "RUNNING"])
def test_runtime_only_repair_cannot_skip_unfinished_design(tmp_path: Path, unfinished_status: str) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(project_name="阶段连续性", domain="stage-continuity")["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    _complete_s3(service, project_id)
    project_dir = tmp_path / project_id
    state = service._read_state(project_dir)
    state["stage_statuses"].update({"S4": unfinished_status, "S5": "PENDING", "S6": "PENDING"})
    state["current_stage"] = "S4"
    service._write_json(project_dir / "workflow-state.json", state)
    preview = service.preview_stage_rollback(
        project_id=project_id, target_stage="S3", requested_by="pytest",
        changed_components=["RUNTIME_MAPPING"],
    )
    reopened = service.reopen_stage_for_correction(
        project_id=project_id, stage="S3", reason="修复查询格式，保留有效上游。",
        requested_by="pytest", preview_token=preview["preview_token"],
        project_revision=preview["project_revision"], changed_components=["RUNTIME_MAPPING"],
    )
    assert reopened["active_revision"]["required_revalidation_stages"] == ["S3", "S4", "S5", "S6", "S7"]
    assert reopened["stage_statuses"]["S2"] == "PASSED"


def test_mapping_finalize_does_not_skip_pending_stage_in_legacy_revision(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_id = service.create_project(project_name="旧修订连续性", domain="legacy-continuity")["project_id"]
    _record_s0(service, project_id)
    _record_s1(service, project_id)
    _record_s2(service, project_id)
    _complete_s3(service, project_id)
    project_dir = tmp_path / project_id
    state = service._read_state(project_dir)
    state["active_revision"] = {
        "status": "IN_PROGRESS", "target_stage": "S3",
        "required_revalidation_stages": ["S3", "S6", "S7"],
    }
    state["stage_statuses"].update({"S4": "PENDING", "S5": "PENDING"})
    service._write_json(project_dir / "workflow-state.json", state)
    service._finalize_mapping(project_dir, state)
    result = service._read_state(project_dir)
    assert result["project_status"] == "S1_S3_READY"
    assert result["current_stage"] is None
    assert result["stage_statuses"]["S6"] != "RUNNING"


def test_core_profile_and_frontend_sync_orion_workflow_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads((root / "contracts/runtime-source-manifest.json").read_text(encoding="utf-8"))
    declared = {row["path"]: row for row in manifest["files"]}
    for path in ("harness/orion_workflow_mcp.py", "harness/orion_workflow_action.py",
                 "harness/contracts/workflow-ui-actions.json", "Makefile",
                 "harness/skills/orion-ontology-engineer/SKILL.md"):
        assert path in declared
        value = (root / path).read_bytes()
        assert len(value) == declared[path]["bytes"]
        assert hashlib.sha256(value).hexdigest() == declared[path]["sha256"]
    assert "harness/orion_workflow_mcp.py" in manifest["required"]
    skill = (root / "harness/skills/orion-ontology-engineer/SKILL.md").read_text(encoding="utf-8")
    metadata = yaml.safe_load(skill.split("---", 2)[1])
    assert metadata["name"] == "orion-ontology-engineer" and metadata["description"]
    service = OntologyWorkflowService(tmp_path)
    tools = OrionWorkflowTools(service)
    monkeypatch.setenv("ORION_MCP_READ_ONLY", "1")
    for tool in ("create_ontology_project", "publish_ontology_package", "record_quality_validation"):
        with pytest.raises(WorkflowGateError, match="只读模式"):
            tools.call(tool, {})
    assert service.list_projects()["count"] == 0
    assert "get_next_workflow_action" in skill
    assert "模型不得自己选择阶段" in skill
    assert "工具 Schema 是字段契约的唯一入口" in skill
    assert "mcp__orion_workflow__record_ontology_design" not in skill
    assert "`record_ontology_design` 是平台内部" in skill
    assert "`generate_ontology_design`" in skill
    assert "当前版本禁止进入 S4～S7" not in skill
    assert "最多三项" in skill
    assert "HTML 阶段报告" in skill
    assert "services/ontology_engineering/reporting.py" in skill
    assert "确定性生成" in skill
    assert "不让模型写重复报告" in skill
    assert "禁止 CDN" in skill


def test_s2_accepts_domain_neutral_closed_world_role_bindings(tmp_path: Path) -> None:
    """A non-government project binds closed-world roles to its own columns.

    The first production ontology here was a government material checklist, so
    the gate used to demand keys literally named case_id / service_item_id /
    material_code.  A manufacturing project has none of those, and forcing them
    pushes meaningless field names into the published ontology.
    """

    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-neutral-role-project"
    project_dir.mkdir()
    payload = _production_s2_rule_payload()
    rule = payload["business_rule_candidates"][0]
    rule.update(
        {
            "rule_type": "CLOSED_WORLD_SET_DIFFERENCE",
            "required_capabilities": ["CLOSED_WORLD_SET_DIFFERENCE_V1"],
            "formal_expression": (
                "IF RequiredMaterial(?case,?material) "
                "AND NOT SubmittedMaterial(?case,?material) "
                "THEN MissingMaterial(?case,?material)"
            ),
            "premise_predicates": ["RequiredMaterial", "SubmittedMaterial"],
            "conclusion_predicate": "MissingMaterial",
            "closed_world_inputs": [
                {
                    "predicate": "SubmittedMaterial",
                    "completeness": "COMPLETE_FOR_CASE_AND_SNAPSHOT",
                    "dataset_type": "PRODUCTION_EVIDENCE",
                    "production_evidence": True,
                    "source_refs": ["table:workorder_task_completion"],
                    "snapshot_sha256": "sha256:" + "a" * 64,
                    "snapshot_version": "2026-09-24T08:00:00+08:00",
                    "dataset_id": "DS-WORKORDER-TASK-001",
                    "source_sha256": "sha256:" + "b" * 64,
                    "row_count": 1,
                    "key_fields": ["workorder_no", "task_code"],
                    "field_bindings": {
                        "record_id": "workorder_no",
                        "classifier_id": "wo_type",
                        "subject_id": "task_code",
                        "status_field": "completion_status",
                        "snapshot_version": "snapshot_version",
                        "source_locator": "source_locator",
                    },
                    "status_filter": ["DONE", "VERIFIED"],
                    "pii_scope": {
                        "mode": "MINIMUM_NECESSARY",
                        "allowed_fields": [
                            "workorder_no",
                            "wo_type",
                            "task_code",
                            "completion_status",
                            "snapshot_version",
                            "source_locator",
                        ],
                        "direct_identifiers_included": False,
                    },
                }
            ],
            "required_set_source": {
                "predicate": "RequiredMaterial",
                "dataset_type": "PRODUCTION_EVIDENCE",
                "production_evidence": True,
                "dataset_id": "DS-WORKORDER-TASK-CATALOG-001",
                "source_sha256": "sha256:" + "c" * 64,
                "snapshot_sha256": "sha256:" + "d" * 64,
                "snapshot_version": "2026-09-24T08:00:00+08:00",
                "row_count": 1,
                "source_refs": ["table:wo_type_required_task"],
                "field_bindings": {
                    "classifier_id": "wo_type",
                    "subject_id": "task_code",
                    "mandatory": "is_required",
                    "source_locator": "source_locator",
                },
            },
            "test_cases": [
                {
                    "id": "positive",
                    "case_type": "POSITIVE",
                    "facts": ["RequiredMaterial(case-1,material-a)"],
                    "expected_outcome": "FIRE",
                },
                {
                    "id": "negative",
                    "case_type": "NEGATIVE",
                    "facts": [
                        "RequiredMaterial(case-1,material-a)",
                        "SubmittedMaterial(case-1,material-a)",
                    ],
                    "expected_outcome": "NO_FIRE",
                },
                {
                    "id": "boundary",
                    "case_type": "BOUNDARY",
                    "facts": ["SubmittedMaterial(case-1,material-a)"],
                    "expected_outcome": "NO_FIRE",
                },
            ],
        }
    )

    service._validate_s2(
        payload,
        project_dir=project_dir,
        intake_mode="DATABASE_ONLY",
    )


def test_s2_rejects_mixed_closed_world_role_spellings(tmp_path: Path) -> None:
    service = OntologyWorkflowService(tmp_path)
    project_dir = tmp_path / "rule-mixed-role-project"
    project_dir.mkdir()
    payload = _production_s2_rule_payload()
    rule = payload["business_rule_candidates"][0]
    rule.update(
        {
            "rule_type": "CLOSED_WORLD_SET_DIFFERENCE",
            "required_capabilities": ["CLOSED_WORLD_SET_DIFFERENCE_V1"],
            "formal_expression": (
                "IF RequiredMaterial(?case,?material) "
                "AND NOT SubmittedMaterial(?case,?material) "
                "THEN MissingMaterial(?case,?material)"
            ),
            "premise_predicates": ["RequiredMaterial", "SubmittedMaterial"],
            "conclusion_predicate": "MissingMaterial",
            "closed_world_inputs": [
                {
                    "predicate": "SubmittedMaterial",
                    "completeness": "COMPLETE_FOR_CASE_AND_SNAPSHOT",
                    "dataset_type": "PRODUCTION_EVIDENCE",
                    "production_evidence": True,
                    "source_refs": ["table:workorder_task_completion"],
                    "snapshot_sha256": "sha256:" + "a" * 64,
                    "snapshot_version": "2026-09-24T08:00:00+08:00",
                    "dataset_id": "DS-WORKORDER-TASK-001",
                    "source_sha256": "sha256:" + "b" * 64,
                    "row_count": 1,
                    "key_fields": ["workorder_no", "task_code"],
                    "field_bindings": {
                        "record_id": "workorder_no",
                        "service_item_id": "wo_type",
                        "subject_id": "task_code",
                        "status_field": "completion_status",
                        "snapshot_version": "snapshot_version",
                        "source_locator": "source_locator",
                    },
                    "status_filter": ["DONE"],
                    "pii_scope": {
                        "mode": "MINIMUM_NECESSARY",
                        "allowed_fields": ["workorder_no", "task_code"],
                        "direct_identifiers_included": False,
                    },
                }
            ],
        }
    )

    with pytest.raises(WorkflowGateError, match="record_id") as exc_info:
        service._validate_s2(
            payload,
            project_dir=project_dir,
            intake_mode="DATABASE_ONLY",
        )
    assert exc_info.value.gate_id == "G-S2-INPUT-INCOMPLETE"
