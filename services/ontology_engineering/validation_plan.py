"""Evidence-backed S6 execution policy; graph persistence belongs to S7."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

POLICY = "capability-directed-v1"
V2 = "s0-s7-stage-contract-v2"


def _read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def uses_capability_validation(project_dir: Path, report: dict[str, Any]) -> bool:
    state_path = project_dir / "workflow-state.json"
    return (
        report.get("validation_execution_policy") == POLICY
        and state_path.is_file()
        and _read(state_path).get("stage_contract_version") == V2
    )


def materialization_fingerprint(project_dir: Path, document_graph_sha256: str) -> str:
    """Exclude unrelated CQ/approval/diagnostic edits from source graph reuse.

    The graph checkpoint separately verifies the live source-content fingerprint.
    Bump the explicit materializer profile if extraction semantics change.
    """
    inputs = {
        "materializer_profile": "ontop-full-construct-document-facts-v2",
        "document_graph_sha256": document_graph_sha256,
        "assets": {
            name: _sha(project_dir / name) if (project_dir / name).is_file() else None
            for name in (
                "03-mapping-review/runtime/mapping.obda",
                "05-ontology-build/ontology.ttl",
            )
        },
    }
    return "sha256:" + hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_s6_validation_plan(
    project_dir: Path, payload: dict[str, Any], quality: dict[str, Any]
) -> dict[str, Any] | None:
    report = payload["semantica_report"]
    if not uses_capability_validation(project_dir, report):
        return None
    build_path = project_dir / "05-ontology-build/protege-build-report.json"
    build = _read(build_path)
    hermit = payload["hermit_report"]
    evidence = hermit.get("reused_evidence") or {}
    if (
        hermit.get("run_id") != build.get("run_id")
        or not build.get("run_id")
        or evidence.get("source_stage") != "S5"
        or evidence.get("report_sha256") != _sha(build_path)
        or evidence.get("ontology_sha256") != _sha(project_dir / "05-ontology-build/ontology.ttl")
        or (build.get("reasoner_report") or {}).get("consistent") is not True
    ):
        raise ValueError("S6 复用的 S5 HermiT 回执或本体指纹不一致。")
    graph_sha = "sha256:" + hashlib.sha256(payload["materialized_ttl"].encode()).hexdigest()
    if report.get("validated_graph_sha256") != graph_sha:
        raise ValueError("S6 本地验证图与执行报告的指纹不一致。")
    reasoning = quality["reasoning_capability_validation"]
    relationships = quality["relationship_validation"]
    conditional_nulls = sum(
        sum((row.get("conditional_null_counts") or {}).values())
        for row in quality.get("competency_question_execution", [])
    )
    checks = []

    def check(
        key: str, name: str, reason: str, *, status: str = "PASSED",
        mode: str = "EXECUTED_IN_RUN", source_stage: str = "S6",
        refs: list[str] | None = None,
    ) -> None:
        checks.append({
            "id": key, "name_zh": name, "status": status, "execution_mode": mode,
            "source_stage": source_stage, "evidence_refs": refs or [], "reason_zh": reason,
        })

    check("SOURCE", "来源与版本核对", "完整来源对账与当前 S0～S5 指纹已核验。",
          refs=["06-quality-validation/semantica-report.json#production_coverage"])
    check("HERMIT", "本体逻辑一致性", "复用同一模型在 S5 的真实 HermiT 回执；未对全量实例重新运行 HermiT。",
          mode="REUSED_PRIOR_STAGE", source_stage="S5",
          refs=["05-ontology-build/protege-build-report.json", "05-ontology-build/ontology.ttl"])
    check("MAPPING", "数据映射", "使用本轮完整来源生成实例图，并核对映射覆盖。",
          refs=["06-quality-validation/mapping-report.json"])
    check("SHACL", "实例数据约束", "服务端对完整候选实例图执行 SHACL 约束检查。",
          refs=["06-quality-validation/shacl-report.ttl"])
    check("CQ", "业务问题验收", f"{quality['competency_question_total']} 个已批准业务问题完成真实答案契约验证。"
          + (f"保留 {conditional_nulls} 个符合已审条件的未知值。" if conditional_nulls else ""),
          status="PASSED_WITH_NOTES" if conditional_nulls else "PASSED",
          refs=["06-quality-validation/competency-question-report.json"])
    applicable = int(reasoning.get("declared", 0)) > 0
    check("RULES", "声明的推理能力", f"实际验证 {reasoning.get('validated', 0)} 项已声明推理能力；无需先向探索器持久化全图。"
          if applicable else str(reasoning.get("not_applicable_reason") or "当前批准设计未声明推理能力。"),
          status="PASSED" if applicable else "NOT_APPLICABLE",
          mode="EXECUTED_IN_RUN" if applicable else "NOT_APPLICABLE",
          refs=["06-quality-validation/semantica-report.json#reasoning_capability_results"])
    applicable = relationships.get("status") != "NOT_APPLICABLE"
    check("RELATIONSHIPS", "业务关系完整性", "在完整实例图上验证关系型业务问题要求的路径、端点类型和来源。"
          if applicable else str(relationships.get("reason") or "没有关系型业务问题。"),
          status="PASSED" if applicable else "NOT_APPLICABLE",
          mode="EXECUTED_IN_RUN" if applicable else "NOT_APPLICABLE")
    return {
        "schema_version": 1, "policy_version": POLICY, "checks": checks,
        "materialized_graph_sha256": graph_sha,
        "deployment_tasks": [{
            "id": "SEMANTICA_MODEL_SYNC", "name_zh": "本体模型与版本标签同步",
            "stage": "S7", "status": "PENDING",
            "reason_zh": "由发布阶段按运行环境配置同步并回读；不作为 S6 数据质量检查。",
        }, {
            "id": "SEMANTICA_INSTANCE_PERSISTENCE", "name_zh": "探索器全实例图持久化",
            "stage": "S7", "status": "NOT_APPLICABLE",
            "reason_zh": "当前查询和规则从已绑定事实源取数；全实例图用于本地质量验证，不默认复制到共享探索器。",
        }],
    }
