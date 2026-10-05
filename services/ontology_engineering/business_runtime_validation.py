"""Prevent approving stale generated execution after business-plan edits."""
from __future__ import annotations

import json
from pathlib import Path

from services.ontology_contracts.errors import WorkflowError
from services.realtime_qa.runtime_release import RuntimeReleaseError, normalize_runtime_submission

from .semantic_artifact import ONTOLOGY_CANDIDATES_PATH, parse_ontology_candidates


def business_runtime_issues(project_dir: Path, mapping: dict, runtime: dict, *, intake_mode: str) -> list[dict]:
    if not mapping.get("business_query_plans"):
        source = project_dir / "02-semantic-recognition/business-rule-candidates.json"
        if source.is_file():
            try:
                rules = json.loads(source.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                rules = []  # The existing source-integrity gates own invalid files.
            if isinstance(rules, list) and any(isinstance(r, dict) and "condition_contract" in r for r in rules):
                return [{"gate": "G-S3-BUSINESS-COMPILATION", "path": "/mapping_draft/business_query_plans",
                         "reason_code": "DECLARED_RULE_PLAN_REQUIRED", "repair_action": "PATCH_BUSINESS_PLAN",
                         "owner": "ENGINEERING_AGENT", "contract_section": "mapping_draft.business_query_plans",
                         "message": "S2已声明可编译业务条件；必须通过业务计划绑定和平台编译核对，不能删除计划后手工填写运行规则绕过语义校验。"}]
        return []
    from .mapping_preflight import normalize_known_literal_datatypes
    from .mapping_runtime_compiler import compile_mapping_runtime

    def issue(message, *, path="/mapping_draft/business_query_plans",
              reason_code="BUSINESS_COMPILATION_FAILED", repair_action="REVIEW_COMPILATION_SOURCE",
              owner="PLATFORM_COMPILER", **guidance):
        return {"gate": "G-S3-BUSINESS-COMPILATION", "message": message,
                "path": path, "owner": owner, "reason_code": reason_code,
                "repair_action": repair_action, "contract_section": "mapping_draft.business_query_plans",
                **guidance}

    try:
        def read(relative):
            return json.loads((project_dir / relative).read_text(encoding="utf-8"))

        snapshot = read("01-data-understanding/schema-snapshot.json")
        inventory = (read("01-data-understanding/datasource-inventory.json")
                     if any(t.get("physical_version_table") for t in snapshot.get("tables", [])) else None)
        result = compile_mapping_runtime(
            mapping, snapshot, project_id=project_dir.name,
            intake_mode=intake_mode, datasource_inventory=inventory,
            rule_candidates=read("02-semantic-recognition/business-rule-candidates.json"),
            cq_questions=read("00-document-evidence/cq-intake.json").get("questions", []),
            ontology_candidates=parse_ontology_candidates((project_dir / ONTOLOGY_CANDIDATES_PATH).read_bytes()))
        problems = [item for item in result["open_items"] if item["kind"] == "BUSINESS_PLAN_REVIEW_REQUIRED"]
        if problems:
            return [issue("业务计划未完成编译：" + item["detail"],
                          path=item.get("path", "/mapping_draft/business_query_plans"),
                          reason_code=item.get("reason_code", "BUSINESS_PLAN_COMPILATION_FAILED"),
                          repair_action="REVIEW_UPSTREAM_RULE" if item.get("upstream_source") else "PATCH_BUSINESS_PLAN",
                          owner="ENGINEERING_AGENT",
                          **{key: item[key] for key in ("missing_fields", "allowed_fields", "expected", "upstream_source") if key in item})
                    for item in problems]
        expected = normalize_runtime_submission(result["runtime"], intake_mode=intake_mode, require_explicit_capabilities=True)
        actual = normalize_runtime_submission(runtime, intake_mode=intake_mode, require_explicit_capabilities=True)
        if actual is None:
            return [issue("业务计划缺少对应运行内容，请调用 compile_mapping_runtime。",
                          path="/realtime_runtime", reason_code="BUSINESS_RUNTIME_MISSING",
                          repair_action="RECOMPILE_RUNTIME")]
        for field in ("ontop_queries", "query_capabilities", "reasoning_capabilities"):
            for name, value in expected[field].items():
                if actual[field].get(name) != value:
                    pointer = "/realtime_runtime/" + field + "/" + name.replace("~", "~0").replace("/", "~1")
                    return [issue(f"{field}.{name} 与当前业务计划或来源不一致；先 compile_mapping_runtime，再完整预检。",
                                  path=pointer, reason_code="BUSINESS_RUNTIME_STALE",
                                  repair_action="RECOMPILE_RUNTIME")]
        expected_obda = normalize_known_literal_datatypes(expected["mapping_obda"])[0]
        actual_obda = normalize_known_literal_datatypes(actual["mapping_obda"])[0]
        if expected_obda.strip() != actual_obda.strip():
            return [issue("业务计划使用的映射与当前编译来源不一致；先重新编译。有人工 OBDA 扩展时须明确拆分评审，不能沿用旧来源。",
                          path="/realtime_runtime/mapping_obda", reason_code="BUSINESS_MAPPING_STALE",
                          repair_action="RECOMPILE_RUNTIME")]
    except (OSError, ValueError, TypeError, KeyError, AttributeError, WorkflowError, RuntimeReleaseError) as exc:
        return [issue("无法核对业务计划编译结果：" + str(exc))]
    return []
