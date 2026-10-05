from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .joint_design import verify_joint_baseline
from .mapping_preflight import collect_mapping_runtime_issues
from .stage_contracts import STAGE_CONTRACT_VERSION, project_stage_contract_version

CONTRACT_CHAIN_VERSION = "s0-s7-contract-chain-v1"
STAGES = tuple(f"S{index}" for index in range(8))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _issue(
    code: str,
    message: str,
    *,
    stages: list[str],
    severity: str = "ERROR",
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "stages": stages,
        "message": message,
    }


def validate_project_contract_chain(project_dir: Path) -> dict[str, Any]:
    """Validate persisted cross-stage invariants without changing project state."""

    state = _read_json(project_dir / "workflow-state.json")
    statuses = dict(state.get("stage_statuses") or {})
    fingerprints = dict(state.get("stage_fingerprints") or {})
    issues: list[dict[str, Any]] = []
    active_revision = state.get("active_revision") or {}
    revision_required = set(active_revision.get("required_revalidation_stages") or [])
    revision_in_progress = active_revision.get("status") == "IN_PROGRESS"

    reached_gap = False
    for stage in STAGES:
        status = str(statuses.get(stage) or "PENDING").upper()
        complete = status in {"PASSED", "NOT_APPLICABLE"}
        reusable_revision_stage = (
            revision_in_progress and stage not in revision_required and status == "PASSED"
        )
        if reached_gap and complete and not reusable_revision_stage:
            issues.append(
                _issue(
                    "CHAIN-STAGE-ORDER",
                    f"{stage} 已完成，但更早阶段尚未闭合。",
                    stages=[stage],
                )
            )
        if not complete:
            reached_gap = True
        if status == "PASSED" and not str((fingerprints.get(stage) or {}).get("output") or ""):
            issues.append(
                _issue(
                    "CHAIN-FINGERPRINT-MISSING",
                    f"{stage} 已通过但缺少正式输出指纹。",
                    stages=[stage],
                )
            )

    current_stage = str(state.get("current_stage") or "").upper()
    project_status = str(state.get("project_status") or "").upper()
    s4_ready_from_review = (
        not current_stage
        and str(statuses.get("S3") or "").upper() == "PASSED"
        and str(statuses.get("S4") or "").upper() == "PENDING"
    )
    if not current_stage and (
        project_status in {"PUBLISHED", "RELEASED", "COMPLETED", "ARCHIVED", "REVOKED"}
        or s4_ready_from_review
    ):
        pass
    elif current_stage not in STAGES:
        issues.append(
            _issue(
                "CHAIN-CURRENT-STAGE",
                f"current_stage 无效：{current_stage or 'EMPTY'}。",
                stages=[],
            )
        )
    elif str(statuses.get(current_stage) or "").upper() == "PASSED" and project_status not in {
        "PUBLISHED",
        "RELEASED",
        "COMPLETED",
    }:
        issues.append(
            _issue(
                "CHAIN-CURRENT-STAGE-PASSED",
                f"current_stage={current_stage}，但该阶段已标记 PASSED。",
                stages=[current_stage],
            )
        )

    cq_path = project_dir / "00-document-evidence/cq-intake.json"
    plan_path = project_dir / "02-semantic-recognition/capability-plan.json"
    rules_path = project_dir / "02-semantic-recognition/business-rule-candidates.json"
    if str(statuses.get("S2") or "").upper() == "PASSED":
        if not plan_path.is_file():
            gate_path = project_dir / "02-semantic-recognition/gate-results.json"
            gate_ids = (
                {
                    str(item.get("id") or "")
                    for item in (_read_json(gate_path).get("gates") or [])
                    if isinstance(item, dict)
                }
                if gate_path.is_file()
                else set()
            )
            enforced = "G-S2-PLATFORM-CAPABILITY" in gate_ids
            issues.append(
                _issue(
                    "CHAIN-S2-CAPABILITY-PLAN",
                    (
                        "S2 已通过但缺少 capability-plan.json。"
                        if enforced
                        else "该工程早于平台能力计划合同；下次 S2 修订时自动补齐。"
                    ),
                    stages=["S2", "S3"],
                    severity="ERROR" if enforced else "WARNING",
                )
            )
        else:
            plan = _read_json(plan_path)
            plan_status = str(plan.get("status") or "").upper()
            if plan_status not in {"READY", "SUPPORTED"}:
                issues.append(
                    _issue(
                        "CHAIN-S2-CAPABILITY-BLOCKED",
                        f"S2 已通过但能力计划状态为 {plan_status or 'EMPTY'}。",
                        stages=["S2", "S3", "S4", "S6"],
                    )
                )
            if plan_status == "SUPPORTED":
                issues.append(
                    _issue(
                        "CHAIN-S2-CAPABILITY-LEGACY",
                        "能力计划使用旧状态 SUPPORTED；下次 S2 修订时将升级为 READY。",
                        stages=["S2"],
                        severity="WARNING",
                    )
                )
            questions = _read_json(cq_path).get("questions") or [] if cq_path.is_file() else []
            question_ids = {str(item.get("id") or "") for item in questions}
            rules = _read_json(rules_path) if rules_path.is_file() else []
            rule_ids = {str(item.get("id") or "") for item in rules if isinstance(item, dict)}
            for route in plan.get("routes") or []:
                question_id = str(route.get("question_id") or "")
                if question_id not in question_ids:
                    issues.append(
                        _issue(
                            "CHAIN-CQ-IDENTITY",
                            f"能力计划引用了 S0 中不存在的业务问题 {question_id or 'EMPTY'}。",
                            stages=["S0", "S2"],
                        )
                    )
                unknown_rules = sorted(
                    set(str(value) for value in route.get("bound_rule_ids") or []) - rule_ids
                )
                if unknown_rules:
                    issues.append(
                        _issue(
                            "CHAIN-RULE-IDENTITY",
                            "能力计划引用了不存在的正式规则：" + ", ".join(unknown_rules),
                            stages=["S2", "S3"],
                        )
                    )

    mapping_path = project_dir / "03-mapping-review/mapping.yaml"
    runtime_path = project_dir / "03-mapping-review/runtime/runtime-source.json"
    if str(statuses.get("S3") or "").upper() == "PASSED":
        if not mapping_path.is_file() or not runtime_path.is_file():
            issues.append(
                _issue(
                    "CHAIN-S3-RUNTIME-ASSETS",
                    "S3 已通过但缺少正式 Mapping 或 runtime-source。",
                    stages=["S3", "S4", "S6"],
                )
            )
        else:
            mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}
            runtime = _read_json(runtime_path)
            for datatype_issue in collect_mapping_runtime_issues(
                list(mapping.get("mappings") or []),
                str(runtime.get("mapping_obda") or ""),
            ):
                issues.append(
                    _issue(
                        "CHAIN-S3-RUNTIME-DATATYPE",
                        datatype_issue["message"],
                        stages=["S3", "S5", "S6"],
                    )
                )

    if (
        project_stage_contract_version(state) == STAGE_CONTRACT_VERSION
        and str(statuses.get("S4") or "").upper() == "PASSED"
    ):
        try:
            verify_joint_baseline(project_dir)
        except (ValueError, OSError) as exc:
            issues.append(
                _issue(
                    "CHAIN-S4-JOINT-DESIGN",
                    str(exc),
                    stages=["S4", "S5", "S6", "S7"],
                )
            )

    if str(statuses.get("S5") or "").upper() == "PASSED":
        required_build_assets = (
            "05-ontology-build/ontology.owl",
            "05-ontology-build/ontology.ttl",
            "05-ontology-build/shapes.ttl",
            "05-ontology-build/protege-build-report.json",
        )
        missing = [name for name in required_build_assets if not (project_dir / name).is_file()]
        if missing:
            issues.append(
                _issue(
                    "CHAIN-S5-BUILD-ASSETS",
                    "S5 已通过但缺少构建制品：" + ", ".join(missing),
                    stages=["S5", "S6", "S7"],
                )
            )

    if str(statuses.get("S6") or "").upper() == "PASSED":
        summary_path = project_dir / "06-quality-validation/quality-summary.json"
        if not summary_path.is_file():
            issues.append(
                _issue(
                    "CHAIN-S6-EVIDENCE",
                    "S6 已通过但缺少 quality-summary.json。",
                    stages=["S6", "S7"],
                )
            )
        else:
            summary = _read_json(summary_path)
            coverage = summary.get("production_coverage") or {}
            bound = dict(coverage.get("source_stage_fingerprints") or {})
            drifted = [
                stage
                for stage in ("S0", "S1", "S2", "S3", "S4", "S5")
                if str(bound.get(stage) or "")
                != str((fingerprints.get(stage) or {}).get("output") or "")
            ]
            if drifted:
                issues.append(
                    _issue(
                        "CHAIN-S6-FINGERPRINT-DRIFT",
                        (
                            "S6 生产覆盖证据未绑定当前阶段指纹：" + ", ".join(drifted)
                            if str(summary.get("production_gate_policy_version") or "")
                            == "production-gates-v2"
                            else "该发布早于 S6 全阶段指纹绑定合同；历史制品保持可读。"
                        ),
                        stages=[*drifted, "S6", "S7"],
                        severity=(
                            "ERROR"
                            if str(summary.get("production_gate_policy_version") or "")
                            == "production-gates-v2"
                            else "WARNING"
                        ),
                    )
                )

    errors = [issue for issue in issues if issue["severity"] == "ERROR"]
    warnings = [issue for issue in issues if issue["severity"] == "WARNING"]
    return {
        "schema_version": 1,
        "contract_version": CONTRACT_CHAIN_VERSION,
        "project_id": project_dir.name,
        "status": "FAILED" if errors else "PASSED_WITH_WARNINGS" if warnings else "PASSED",
        "error_count": len(errors),
        "warning_count": len(warnings),
        "issues": issues,
    }
