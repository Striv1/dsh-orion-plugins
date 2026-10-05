"""Freeze one business capability against its exact draft and source identities.

The preview is a design aid. It never prepares a formal stage commit or reuses
an approved ontology from a different revision to make a draft look valid.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

from services.ontology_contracts.business_query_plan import NAME
from services.ontology_contracts.errors import WorkflowError
from services.ontology_engineering.business_mapping_scope import resolve_business_mapping_scope
from services.ontology_engineering.semantic_artifact import (
    ONTOLOGY_CANDIDATES_PATH,
    parse_ontology_candidates,
)
from services.structured_data.execution_source import execution_schema
from services.structured_data.source_evidence import snapshot_evidence_binding

SOURCE_FILES = {
    "snapshot": "01-data-understanding/schema-snapshot.json",
    "inventory": "01-data-understanding/datasource-inventory.json",
    "rules": "02-semantic-recognition/business-rule-candidates.json",
    "candidates": ONTOLOGY_CANDIDATES_PATH,
    "cq": "00-document-evidence/cq-intake.json",
}
MAX_INPUT_BYTES = 8 * 1024 * 1024
PLAN_ID = re.compile(NAME["pattern"])
COMPILER_FILES = (
    "business_preview_inputs.py", "business_preview_execution.py", "business_preview_diagnostics.py",
    "business_preview.py", "business_mapping_scope.py", "business_source_contract.py", "semantic_artifact.py",
    "mapping_runtime_compiler.py", "business_query_compiler.py", "business_answer_contract.py", "rule_condition_binding.py",
    "../ontology_contracts/rule_conditions.py", "../ontology_contracts/business_query_plan.py",
    "../realtime_qa/rdf_results.py", "../ontology_contracts/nullable_bindings.py",
    "../structured_data/execution_source.py", "../structured_data/source_evidence.py", "../ontology_contracts/cq_answers.py", "../realtime_qa/cq_contract.py", "../realtime_qa/query_capabilities.py",
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def read_context(tools, project_id):
    root = tools.service._resolve_project(project_id)
    state = tools.service._read_state(root)
    result = {"project_id": project_id, "revision": state["revision"], "stage": "S3",
              "current_stage": state["current_stage"], "intake_mode": state.get("intake_mode"),
              "payload_file": None, "plans": [], "sources": {}, "source_hashes": {}}
    if state["current_stage"] != "S3":
        result["message"] = "业务能力试运行用于当前 S3 设计草稿；正式阶段验收仍按工程流程执行。"
        return result
    if state.get("intake_mode") not in {"DATABASE_ONLY", "HYBRID"}:
        result["message"] = "当前试运行支持已登记数据库快照的业务查询与规则；纯文档能力暂未接入此入口。"
        return result
    reference = tools._get_stage_draft(project_id=project_id, stage="S3").get("payload_file")
    if not reference:
        result["message"] = "先保存当前 S3 业务设计草稿，再逐项试运行；无需先提交整个阶段。"
        return result
    payload, _ = tools._read_submission_payload(root, reference)
    mapping = payload.get("mapping_draft") or {}
    plans = mapping.get("business_query_plans", []) if isinstance(mapping, dict) else []
    if (not isinstance(plans, list) or len(plans) > 32
            or any(not isinstance(p, dict) or not PLAN_ID.fullmatch(str(p.get("id", ""))) for p in plans)
            or len({p["id"] for p in plans}) != len(plans)):
        raise WorkflowError("业务计划须有不同的合法 ID，且一次草稿最多 32 项；请局部修订业务计划。")
    result.update(payload_file=reference, mapping_draft=mapping, plans=plans)
    if not plans:
        result["message"] = "草稿尚未声明业务能力；先围绕一项 CQ 明确对象、关系、条件和独立验收期望。"
        return result
    for name, relative in SOURCE_FILES.items():
        if state.get("artifact_lifecycle", {}).get(relative, {}).get("status") == "INVALIDATED":
            raise WorkflowError(f"试运行来源已失效，须先修复当前来源：{relative}")
        with tools._open_workspace_source(root, relative) as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise WorkflowError("试运行来源文件超过 8 MiB，请先缩小设计范围。")
        try:
            source = parse_ontology_candidates(raw) if name == "candidates" else json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise WorkflowError(f"试运行来源文件不是有效 JSON：{relative}") from exc
        if not isinstance(source, list if name in {"rules", "candidates"} else dict):
            raise WorkflowError(f"试运行来源文件结构不正确：{relative}")
        result["sources"][name] = source
        result["source_hashes"][name] = hashlib.sha256(raw).hexdigest()
    result["compiler_hash"] = digest({name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                      for name in COMPILER_FILES})
    result["runtime_hash"] = digest({"build": tools.service._validator_fingerprint(), "configuration": {
        name: os.getenv(name, "") for name in ("ORION_SOURCE_DATA_READER_URL", "ORION_DOCKER_NETWORK",
                                               "ORION_ONTOP_IMAGE", "SEMANTICA_API_URL", "SEMANTICA_API_KEY")
    }})
    return result


def input_fingerprint(context, plan_id, case_id):
    # The host still requires the latest project revision and payload reference
    # before starting a worker. Reuse the *result* when another capability in
    # the same revision changes but this plan and its mapping closure do not.
    # Resolve against the full draft so a new same-target producer invalidates
    # the old result. A broken closure hashes the full draft and cannot reuse a
    # previous successful fingerprint; prepare_input reports the actual error.
    plans = [p for p in context.get("plans", []) if p.get("id") == plan_id]
    draft = context.get("mapping_draft") or {}
    try:
        selected = scoped_mapping(draft, plans[0]) if len(plans) == 1 else {"unresolved": draft}
    except WorkflowError:
        selected = {"unresolved": draft}
    return digest({key: context.get(key) for key in (
        "project_id", "revision", "source_hashes", "compiler_hash", "runtime_hash", "intake_mode",
    )} | {"plan_id": plan_id, "case_id": case_id, "selected_draft": selected})


def select_plan(context, plan_id, case_id=None):
    plans = [p for p in context["plans"] if p["id"] == plan_id]
    if len(plans) != 1:
        raise WorkflowError("业务计划不属于当前草稿，请刷新后再选择。")
    plan = plans[0]
    cases = plan.get("validation_cases") or []
    if not isinstance(cases, list) or not cases or any(not isinstance(c, dict) for c in cases):
        raise WorkflowError("业务计划需先填写独立验收用例，不能把实际查询结果自动当作期望。")
    selected_id = case_id if case_id is not None else cases[0].get("id")
    selected = [c for c in cases if c.get("id") == selected_id]
    if len(selected) != 1 or not isinstance(selected_id, str):
        raise WorkflowError("验收用例不存在或 ID 重复，请局部修订。")
    return plan, selected[0]


def scoped_mapping(mapping, plan):
    """Keep only this capability's mapping dependency closure, not every CQ."""
    all_mappings = mapping.get("mappings")
    try:
        refs = resolve_business_mapping_scope(all_mappings, plan)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc
    return {"namespace": mapping.get("namespace"),
            "mappings": [copy.deepcopy(m) for m in all_mappings
                         if isinstance(m.get("id"), str) and m["id"] in refs],
            "business_query_plans": [copy.deepcopy(plan)]}


def _dependency_tables(compiled, snapshot):
    dependencies = compiled.get("mapping_source_dependencies")
    compiled_ids = (compiled.get("summary") or {}).get("compiled_mapping_ids", [])
    if (not isinstance(dependencies, dict) or not dependencies
            or set(dependencies) != set(compiled_ids)):
        raise WorkflowError("编译结果缺少完整 mapping_source_dependencies，不能按裸表名推导试运行范围。")
    identities = {}
    for mapping_id, sources in dependencies.items():
        if not isinstance(sources, list) or not sources:
            raise WorkflowError(f"映射 {mapping_id} 缺少实际执行来源依赖。")
        for source in sources:
            keys = ("source_id", "source_table", "snapshot_table", "dataset_id")
            if (not isinstance(source, dict)
                    or any(not isinstance(source.get(key), str) or not source[key].strip() for key in keys)):
                raise WorkflowError(f"映射 {mapping_id} 的来源依赖缺少完整四元身份。")
            identity = tuple(source[key] for key in keys)
            matches = [table for table in snapshot.get("tables", []) if isinstance(table, dict)
                       and tuple(table.get(key) for key in ("source_id", "source_table", "name", "dataset_id")) == identity]
            if len(matches) != 1:
                raise WorkflowError(f"映射 {mapping_id} 的来源依赖须精确匹配唯一登记快照：{identity}。")
            identities[identity] = matches[0]
    if not identities or len(identities) > 8:
        raise WorkflowError("单项能力最多试运行 8 个已登记快照表，请缩小业务范围。")
    return [identities[key] for key in sorted(identities)]


def prepare_input(context, plan, case):
    from services.ontology_engineering.mapping_runtime_compiler import compile_mapping_runtime

    mapping = scoped_mapping(context["mapping_draft"], plan)
    sources = context["sources"]
    rule_id = plan.get("rule", {}).get("rule_id")
    cq_ids = set(plan.get("cq_bindings", {})) | set(plan.get("rule", {}).get("cq_bindings", {}))
    compiled = compile_mapping_runtime(
        mapping, sources["snapshot"], project_id=context["project_id"], intake_mode=context["intake_mode"],
        datasource_inventory=sources["inventory"],
        rule_candidates=[r for r in sources["rules"] if r.get("id") == rule_id],
        ontology_candidates=sources["candidates"],
        cq_questions=[q for q in sources["cq"].get("questions", []) if q.get("id") in cq_ids],
    )
    if compiled["open_items"]:
        details = "; ".join(str(item.get("detail", "映射未就绪")) for item in compiled["open_items"][:3])
        raise WorkflowError("当前业务能力尚不能编译：" + details[:1200])
    runtime = compiled["runtime"]
    if plan["id"] not in runtime["ontop_queries"]:
        raise WorkflowError("未编译出本项业务查询，不能借用实例列表替代。")
    # Every generated mapping query declares its table dependencies, including
    # joins used only inside SQL_TO_CLASS. Verify all, not just selected fields.
    snapshot = execution_schema(sources["snapshot"], sources["inventory"], project_id=context["project_id"])
    tables = _dependency_tables(compiled, snapshot)
    bindings = [snapshot_evidence_binding(project_id=context["project_id"], schema_snapshot=snapshot,
                                          datasource_inventory=sources["inventory"], selected=table) for table in tables]
    # Do not copy datasource connection configuration into durable worker input.
    for binding in bindings:
        binding["dataset"] = {k: binding["dataset"][k] for k in ("source_sha256", "manifest_sha256") if k in binding["dataset"]}
    return {"project_id": context["project_id"], "revision": context["revision"],
            "payload_file": context["payload_file"], "plan_id": plan["id"], "case_id": case["id"],
            "plan_pointer": f"/mapping_draft/business_query_plans/{context['plans'].index(plan)}",
            "input_fingerprint": input_fingerprint(context, plan["id"], case["id"]),
            "mapping_draft": mapping, "runtime": runtime, "case": case, "source_bindings": bindings}
