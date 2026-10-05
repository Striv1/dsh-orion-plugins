"""Reviewable engineering assets added before the existing S7 manifest is sealed.

These helpers never execute rules, approve, or deploy. Release assets remain
immutable; the explicitly scoped S5 regenerator archives replaced working assets.
Callers hold project locks and decide whether a project uses the v2 contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from services.realtime_qa.reasoning_contract import (
    CAPABILITY_NAME,
    ReasoningCapabilityError,
    normalize_rule_package,
    validate_ontology_term_binding,
)

from .stage_contracts import stage_contract

DELIVERY_CONTRACT_VERSION = "s0-s7-delivery-package-v1"
RULE_CATALOG_VERSION = "orion-rule-review-v1"
ENGINEERING_DIRECTORY = "06-工程追溯"
RULE_DIRECTORY = "02-工程定义/规则目录"

STAGE_FOLDERS = (
    "00-document-evidence",
    "01-data-understanding",
    "02-semantic-recognition",
    "03-mapping-review",
    "04-ontology-design",
    "05-ontology-build",
    "06-quality-validation",
    "07-release",
)
# Explicit copies contain engineering definitions, never connection configuration,
# profile samples, raw documents, or unrestricted audit/event payloads.
COPY_ASSETS = {
    "S0": ("source-scope.json", "cq-intake.json", "scope-decision.json"),
    "S1": (
        "source-understanding.json",
        "schema-snapshot.json",
        "data-contract.json",
        "scope-decision.json",
    ),
    "S2": ("ontology-candidates.yaml", "business-rule-candidates.json", "capability-plan.json"),
    "S3": ("pending-confirmations.json",),
    "S4": ("joint-design-baseline.json",),
    "S5": ("runtime-assembly.json",),
}
REFERENCE_ASSETS = {
    "S0": ("document-register.json", "evidence-index.json", "ingestion-quality-report.json"),
    "S1": ("datasource-inventory.json", "data-profile.json", "evidence-sql.json"),
    "S2": (),
    "S3": ("mapping.yaml", "automatic-decisions.json", "decisions.jsonl"),
    "S4": ("ontology-design.yaml", "mapping.yaml", "realtime-runtime.json"),
    "S5": ("ontology.owl", "ontology.ttl", "shapes.ttl"),
    "S6": ("quality-summary.json", "competency-question-report.json", "hermit-report.json"),
    "S7": (),
}


class DeliveryPackageError(ValueError):
    pass


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _safe_path(root: Path, relative: str) -> Path:
    parsed = PurePosixPath(relative)
    path = root / relative
    if not relative or parsed.is_absolute() or ".." in parsed.parts or "\\" in relative:
        raise DeliveryPackageError("交付资产路径越界。")
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise DeliveryPackageError("交付资产不能通过符号链接访问目录外文件。")
    return path


def _record(root: Path, path: Path) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    data = _safe_path(root, relative).read_bytes()
    return {"path": relative, "sha256": _sha256(data), "bytes": len(data)}


def _assert_no_credential_fields(data: bytes, filename: str) -> None:
    """Fail closed on explicit credential fields; do not log their values."""
    if Path(filename).suffix not in {".json", ".yaml", ".yml"}:
        return
    text = data.decode("utf-8")
    if re.search(r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", text, re.I):
        raise DeliveryPackageError(f"交付文件包含连接凭据：{filename}")
    payload = json.loads(text) if filename.endswith(".json") else yaml.safe_load(text)
    secret_keys = {
        "password",
        "passwd",
        "jdbcpassword",
        "dbpassword",
        "accesstoken",
        "refreshtoken",
        "apikey",
        "clientsecret",
        "secretaccesskey",
        "privatekey",
    }

    def inspect(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                key = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if key in secret_keys and child not in (None, "", False):
                    raise DeliveryPackageError(f"交付文件包含凭据字段：{filename}")
                inspect(child)
        elif isinstance(value, list):
            for child in value:
                inspect(child)

    inspect(payload)


def _write_assets(target_dir: Path, assets: dict[str, bytes]) -> list[dict[str, Any]]:
    # Validate everything before writing; allow exact idempotent read-back only.
    for name, data in assets.items():
        target = _safe_path(target_dir, name)
        _assert_no_credential_fields(data, name)
        if target.exists() and target.read_bytes() != data:
            raise DeliveryPackageError(f"交付资产已存在且内容不同：{name}")
    for name, data in assets.items():
        target = _safe_path(target_dir, name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if target.read_bytes() != data:
            raise DeliveryPackageError(f"交付资产回读失败：{name}")
    return [_record(target_dir, target_dir / name) for name in sorted(assets)]


def write_rule_review_assets(
    source_dir: Path,
    target_dir: Path,
    *,
    project_id: str,
    release_version: str | None = None,
    source_rules_path: Path | None = None,
) -> dict[str, Any]:
    """Copy verified formal rules and write readable review assets.

    source_dir is either a reviewed runtime directory (runtime-source.json), or
    the S7 staging root (05-运行时/realtime-runtime.json, package-relative paths).
    Runtime cases are specifications; this helper never reports them as executed.
    """
    reviewed_path = _safe_path(source_dir, "runtime-source.json")
    packaged_path = _safe_path(source_dir, "05-运行时/realtime-runtime.json")
    source_path = reviewed_path if reviewed_path.is_file() else packaged_path
    source = _read_json(source_path) if source_path.is_file() else {}
    if source_path == reviewed_path and source and source.get("review_status") != "REVIEWED":
        raise DeliveryPackageError("规则目录只能从已评审的运行设计生成。")
    if (
        source_path == packaged_path
        and source
        and (
            source.get("project_id") != project_id
            or source.get("release_version") != release_version
        )
    ):
        raise DeliveryPackageError("规则目录与发布运行时身份不一致。")
    source_rules = (
        _read_json(source_rules_path) if source_rules_path and source_rules_path.is_file() else []
    )
    if isinstance(source_rules, dict):
        source_rules = (
            source_rules.get("rules") or source_rules.get("business_rule_candidates") or []
        )
    source_index = {
        str(rule.get("rule_id") or rule.get("id") or ""): rule
        for rule in source_rules
        if isinstance(rule, dict)
    }
    assets: dict[str, bytes] = {}
    capabilities = []
    for name, capability in sorted((source.get("reasoning_capabilities") or {}).items()):
        if not CAPABILITY_NAME.fullmatch(name) or not isinstance(capability, dict):
            raise DeliveryPackageError("正式规则能力名称无效。")
        rule_path = _safe_path(source_dir, str(capability.get("rule_artifact") or ""))
        rule_bytes = rule_path.read_bytes()
        if _sha256(rule_bytes) != capability.get("rule_sha256"):
            raise DeliveryPackageError(f"正式规则 SHA-256 与评审合同不一致：{name}")
        try:
            normalized = normalize_rule_package(json.loads(rule_bytes), capability_name=name)
            validate_ontology_term_binding(name, capability, normalized["rules"])
        except ReasoningCapabilityError as exc:
            raise DeliveryPackageError(f"正式规则合同无效：{name}：{exc}") from exc
        rule_ids = {rule["rule_id"] for rule in normalized["rules"]}
        if rule_ids != set(capability.get("source_rule_ids") or []):
            raise DeliveryPackageError(f"正式规则与来源规则 ID 不一致：{name}")
        engine = str(capability.get("engine") or "UNDECLARED")
        destination = f"formal-rules/{name}.json"
        assets[destination] = rule_bytes
        rules = []
        for rule in normalized["rules"]:
            candidate = source_index.get(rule["rule_id"]) or {}
            rules.append(
                {
                    **rule,
                    "source_refs": candidate.get("source_refs") or [],
                    "source_record_status": "CAPTURED" if candidate else "NOT_CAPTURED",
                    "source_formal_expression": candidate.get("formal_expression"),
                    "business_question_ids": candidate.get("business_question_ids") or [],
                    "test_cases": candidate.get("test_cases") or [],
                    "validation_cases": candidate.get("validation_cases") or [],
                }
            )
        capabilities.append(
            {
                "capability_name": name,
                "description_zh": capability.get("description_zh"),
                "engine": engine,
                "execution_scope": capability.get("execution_scope"),
                "source_rule_ids": sorted(rule_ids),
                "formal_rule_artifact": destination,
                "rule_sha256": _sha256(rule_bytes),
                "rules": rules,
                "evidence_query": capability.get("evidence_query"),
                "fact_bindings": capability.get("fact_bindings") or [],
                "ontology_terms": capability.get("ontology_terms") or {},
                "result_predicates": capability.get("result_predicates") or [],
                "runtime_validation": capability.get("runtime_validation"),
                "validation_status": "SPECIFICATION_ONLY_SEE_S6_EVIDENCE",
                "closed_world_inputs": capability.get("closed_world_inputs") or [],
                "required_set_source": capability.get("required_set_source"),
                "swrl": {
                    "status": "NOT_SERIALIZED",
                    "plugin_executable": False,
                    "reason": "正式规则使用已声明执行器的 IF/THEN 合同；未转换或验证为 SWRL。",
                },
            }
        )
    catalog = {
        "schema_version": 1,
        "contract_version": RULE_CATALOG_VERSION,
        "project_id": project_id,
        "release_version": release_version,
        "status": "PACKAGED"
        if capabilities
        else (
            "NOT_APPLICABLE"
            if source.get("reasoning_requirement") == "NOT_APPLICABLE"
            else "NOT_DECLARED"
        ),
        "reasoning_not_applicable_reason": source.get("reasoning_not_applicable_reason"),
        "source_runtime": _record(source_dir, source_path) if source_path.is_file() else None,
        "capability_count": len(capabilities),
        "rule_count": sum(len(item["rules"]) for item in capabilities),
        "capabilities": capabilities,
        "execution_performed": False,
    }
    lines = [
        "# 业务规则目录",
        "",
        "本目录按正式规则合同生成，中文说明和原始表达式一同保留。验收用例是待执行或已执行报告的输入规范，生成目录不会执行规则。",
        "",
    ]
    for capability in capabilities:
        lines += [
            f"## {capability['description_zh']}（{capability['capability_name']}）",
            "",
            f"- 执行器：`{capability['engine']}`",
            f"- 事实查询：`{capability['evidence_query']}`",
            f"- 正式规则：[JSON 文件]({capability['formal_rule_artifact']})",
            "",
        ]
        for rule in capability["rules"]:
            lines += [
                f"### {rule['rule_id']} · {rule['description_zh']}",
                "",
                "```text",
                rule["expression"],
                "```",
                "",
                "来源："
                + (
                    "；".join(map(str, rule["source_refs"]))
                    or "来源记录未随本目录捕获；按 rule_id 回到 S2 规则台账核对。"
                ),
                "",
            ]
            if rule["test_cases"]:
                lines += [
                    "S2 正式规则台账中的正例、反例与边界用例：",
                    "",
                    "```json",
                    json.dumps(rule["test_cases"], ensure_ascii=False, indent=2),
                    "```",
                    "",
                ]
            if rule["validation_cases"]:
                lines += [
                    "来源台账中的验证用例：",
                    "",
                    "```json",
                    json.dumps(rule["validation_cases"], ensure_ascii=False, indent=2),
                    "```",
                    "",
                ]
        lines += [
            "验证参数与结果要求：",
            "",
            "```json",
            json.dumps(capability["runtime_validation"], ensure_ascii=False, indent=2),
            "```",
            "",
        ]
        if capability["closed_world_inputs"]:
            lines += [
                "该能力含封闭世界判断，必须同时核对完整来源集合、截止时点和反连接输入；不能把未查到直接解释成不存在。",
                "",
            ]
    if not capabilities:
        lines += [
            "本合同未打包正式推导规则。状态与不适用原因见 rule-catalog.json；本目录不据此推断工程具备推理能力。",
            "",
        ]
    assets["rule-catalog.json"] = _json_bytes(catalog)
    assets["业务规则目录.md"] = "\n".join(lines).encode("utf-8")
    assets["规则执行与Protégé复核说明.md"] = (
        "# 规则执行与 Protégé 复核说明\n\n"
        "1. 复制发布包中的 ontology.owl 到独立目录后用 Protégé 打开，核对类、对象属性、数据属性和公理；不要保存覆盖不可变发布包。\n"
        "2. 对照 rule-catalog.json 的 ontology_terms 和来源定位，核对规则使用的业务术语和事实查询。\n"
        "3. 本目录的 formal-rules/*.json 是 ORION 正式规则合同，使用清单声明的执行器；没有被序列化为 SWRL，不能直接粘贴到 SWRLTab 或 SQWRL Query Tab 执行。\n"
        "4. 规则运行链路为：当前版本的只读事实查询 → fact_bindings → 声明的规则执行器 → result_predicates → 结果与证据回读。外部数据源、凭据、规则服务与运行时注册需由部署环境提供。\n"
        "5. 核对 S6 中真实执行结果、正例/反例/边界及完整来源范围。runtime_validation、test_cases 和 validation_cases 是验证规范，不等于通过记录。HermiT 的本体一致性结论也不等于业务规则已执行。\n\n"
        "未来接入 SWRL/SQWRL 时应另行完成语法转换、执行器支持和语义一致性验收；本包未声明该接入完成。\n"
    ).encode()
    return {"files": _write_assets(target_dir, assets), "rule_catalog": catalog}


def regenerate_s5_rule_review_assets(
    source_dir: Path,
    target_dir: Path,
    *,
    project_id: str,
    source_rules_path: Path | None = None,
) -> dict[str, Any]:
    """Stage verified S5 derivatives, retaining the previous working copy in audit history.

    This path is deliberately separate from immutable release asset writers.
    Callers hold the project mutation lock and have validated the S5 design.
    """
    if (target_dir.name != "rule-review" or target_dir.parent.name != "05-ontology-build"
            or target_dir.is_symlink() or target_dir.parent.is_symlink()
            or any(parent.name == "07-release" for parent in target_dir.parents)):
        raise DeliveryPackageError("规则重生成仅允许 S5 工作目录，不能覆盖发布资产。")
    project_dir = target_dir.parent.parent
    if project_dir.name != project_id:
        raise DeliveryPackageError("S5 规则重生成工程身份不一致。")
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    archive: Path | None = None
    with tempfile.TemporaryDirectory(prefix=".rule-review-staging-", dir=target_dir.parent) as tmp:
        staged = Path(tmp) / "rule-review"
        result = write_rule_review_assets(
            source_dir, staged, project_id=project_id, source_rules_path=source_rules_path
        )
        if target_dir.exists():
            archive = project_dir / "revisions" / "rule-review-rebuilds" / uuid.uuid4().hex / "before"
            archive.parent.mkdir(parents=True, exist_ok=False)
            os.replace(target_dir, archive)
        try:
            os.replace(staged, target_dir)
        except BaseException:
            if archive is not None and not target_dir.exists():
                os.replace(archive, target_dir)
            raise
    return {
        **result,
        "previous_assets_archive": archive.relative_to(project_dir).as_posix() if archive else None,
        "current_assets_path": target_dir.relative_to(project_dir).as_posix(),
    }


def write_engineering_delivery_assets(
    project_dir: Path,
    package_dir: Path,
    *,
    project_id: str,
    release_version: str,
    stage_statuses: dict[str, str],
    lifecycle_contract_version: str,
) -> dict[str, Any]:
    """Add v2 engineering/release views inside new S7 staging before manifest."""
    if (package_dir / "manifest.json").exists() or (package_dir / "package-contract.json").exists():
        raise DeliveryPackageError("已封存的发布包不能补写工程交付资产。")
    publication = _read_json(_safe_path(package_dir, "04-发布信息/publication.json"))
    if (
        publication.get("project_id") != project_id
        or publication.get("release_version") != release_version
    ):
        raise DeliveryPackageError("交付目录与发布身份不一致。")
    if publication.get("approval_decision") != "APPROVED":
        raise DeliveryPackageError("工程交付目录必须绑定正式批准的发布快照。")
    baseline = _safe_path(project_dir, "04-ontology-design/joint-design-baseline.json")
    if not baseline.is_file():
        raise DeliveryPackageError("新版工程包缺少正式联合设计基线。")
    baseline_payload = _read_json(baseline)
    if (
        baseline_payload.get("status") != "APPROVED"
        or not str(baseline_payload.get("approved_by") or "").strip()
    ):
        raise DeliveryPackageError("工程包的联合设计基线必须记录明确批准和批准人。")
    additions: dict[str, bytes] = {}
    stages = []
    for index, folder in enumerate(STAGE_FOLDERS):
        stage = f"S{index}"
        artifacts = []
        names = list(
            dict.fromkeys(
                (*COPY_ASSETS.get(stage, ()), *REFERENCE_ASSETS[stage], "gate-results.json")
            )
        )
        stage_dir = _safe_path(project_dir, folder)
        if stage_dir.exists():
            names += [path.name for path in sorted(stage_dir.glob("*-report.html"))]
        for name in dict.fromkeys(names):
            source = _safe_path(project_dir, f"{folder}/{name}")
            item: dict[str, Any] = {
                "project_path": f"{folder}/{name}",
                "delivery_mode": "NOT_CAPTURED",
            }
            if source.is_file():
                item.update(_record(project_dir, source), delivery_mode="REFERENCE_ONLY")
                if name in COPY_ASSETS.get(stage, ()):
                    target = f"{ENGINEERING_DIRECTORY}/{folder}/{name}"
                    additions[target] = source.read_bytes()
                    item.update(delivery_mode="COPIED", package_path=target)
            artifacts.append(item)
        stages.append(
            {
                "stage": stage,
                "title": stage_contract(stage, lifecycle_contract_version)["name"],
                "status_at_capture": stage_statuses.get(stage, "UNKNOWN"),
                "artifacts": artifacts,
            }
        )
    audit_refs = []
    for relative in (
        "artifact-manifest.json",
        "events/agent-trace.jsonl",
        "03-mapping-review/decisions.jsonl",
    ):
        source = _safe_path(project_dir, relative)
        if source.is_file():
            audit_refs.append({**_record(project_dir, source), "delivery_mode": "REFERENCE_ONLY"})
    for relative in (
        "04-发布信息/publication.json",
        "04-发布信息/release-snapshot.json",
        "04-发布信息/audit-reference.json",
    ):
        source = _safe_path(package_dir, relative)
        if source.is_file():
            stages[-1]["artifacts"].append(
                {
                    **_record(package_dir, source),
                    "package_path": relative,
                    "delivery_mode": "PACKAGE_ASSET",
                }
            )
    rule_result = write_rule_review_assets(
        package_dir,
        package_dir / RULE_DIRECTORY,
        project_id=project_id,
        release_version=release_version,
        source_rules_path=project_dir / "02-semantic-recognition/business-rule-candidates.json",
    )
    additions[f"{ENGINEERING_DIRECTORY}/阶段产物索引.json"] = _json_bytes(
        {"stages": stages, "audit_references": audit_refs}
    )
    additions["工程包与发布包说明.md"] = (
        "# 工程包与发布包\n\n"
        "工程视图用于审阅、追溯和后续修订：包含本体/映射设计、正式联合设计基线、规则中文目录、验收结论与 S0-S7 产物引用。阶段报告和完整审计以工程相对路径和 SHA-256 定位。\n\n"
        "发布视图用于装配已经评审的运行资产：01-本体模型、05-运行时（如存在）和04-发布信息；查询、映射、规则及其依赖以 realtime-runtime.json 为准，完整性以外层 manifest.json 为准。工程包完整归档由现有发布/下载流程生成。\n\n"
        "复现需要解析来源引用，获取同一数据/文档快照，准备外部连接凭据和声明的执行器，再按 S6 规范执行和回读。包生成时的阶段状态会被原样记录；部署后的运行接入状态不在此预先宣称完成。\n\n"
        "源数据、原始文档和完整审计默认保留在工程；经评审的 document-facts 规则输入快照如已进入运行包，会在 package-contract.json 中明确列出。生产连接凭据由部署环境提供。\n"
    ).encode()
    written = _write_assets(package_dir, additions)
    files = [
        _record(package_dir, path)
        for path in sorted(package_dir.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    ]
    runtime_contract_path = _safe_path(package_dir, "05-运行时/realtime-runtime.json")
    runtime = _read_json(runtime_contract_path) if runtime_contract_path.is_file() else None
    runtime_files = (
        [
            item
            for item in files
            if item["path"].split("/", 1)[0] in {"01-本体模型", "05-运行时", "04-发布信息"}
        ]
        if runtime is not None
        else []
    )
    fact_snapshots = [
        item for item in runtime_files if item["path"].startswith("05-运行时/document-facts/")
    ]
    contract = {
        "schema_version": 1,
        "contract_version": DELIVERY_CONTRACT_VERSION,
        "project_id": project_id,
        "release_version": release_version,
        "lifecycle_contract_version": lifecycle_contract_version,
        "stage_deliverables": stages,
        "engineering_package": {
            "profile": "ENGINEERING_DELIVERY",
            "status": "PACKAGED",
            "artifacts": files,
            "audit_references": audit_refs,
        },
        "runtime_release_package": {
            "profile": "RUNTIME_RELEASE",
            "status": "PACKAGED" if runtime is not None else "NOT_PACKAGED",
            "deployment_status": "REQUIRES_EXTERNAL_READBACK"
            if runtime is not None
            else "NOT_APPLICABLE",
            "artifacts": runtime_files,
            "runtime_contract": "05-运行时/realtime-runtime.json" if runtime is not None else None,
            "dependencies": {
                "engines": sorted(
                    {item["engine"] for item in rule_result["rule_catalog"]["capabilities"]}
                ),
                "structured_query_enabled": bool(
                    runtime and runtime.get("structured_query_enabled") is not False
                ),
                "document_query_capabilities": runtime.get("document_query_capabilities") or []
                if runtime
                else [],
                "connection_credentials": "EXTERNAL_CONFIGURATION_ONLY",
            },
        },
        "rule_catalog": f"{RULE_DIRECTORY}/rule-catalog.json",
        "data_policy": {
            "source_data_mode": "REFERENCES_AND_REVIEWED_FACT_SNAPSHOTS"
            if fact_snapshots
            else "REFERENCES_ONLY",
            "raw_documents_included": False,
            "full_instance_graph_included": False,
            "full_audit_journal_included": False,
            "reviewed_fact_snapshots": fact_snapshots,
            "credentials": "EXTERNAL_CONFIGURATION_ONLY",
        },
        "integrity": {
            "manifest": "manifest.json",
            "capture_boundary": "BEFORE_PUBLICATION_RUNTIME_READBACK",
        },
    }
    written += _write_assets(package_dir, {"package-contract.json": _json_bytes(contract)})
    for item in files:
        if _record(package_dir, _safe_path(package_dir, item["path"])) != item:
            raise DeliveryPackageError(f"工程交付资产回读失败：{item['path']}")
    return {
        "files": written
        + [{**item, "path": f"{RULE_DIRECTORY}/{item['path']}"} for item in rule_result["files"]],
        "contract": contract,
    }
