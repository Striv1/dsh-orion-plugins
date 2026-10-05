"""Bind a human design decision to the exact ontology, mapping and rule inputs."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import yaml


def _digest(value: Any) -> str:
    return (
        "sha256:"
        + hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def canonicalize_design(design: dict[str, Any]) -> dict[str, Any]:
    """Hash the representation actually persisted as the review draft."""
    return yaml.safe_load(yaml.safe_dump(design, allow_unicode=True, sort_keys=False))


def _checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _changed_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        paths = []
        for key in sorted(before.keys() | after.keys()):
            path = f"{prefix}.{key}" if prefix else str(key)
            paths.extend(_changed_paths(before.get(key), after.get(key), path))
        return paths
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [path for index, (left, right) in enumerate(zip(before, after, strict=True))
                for path in _changed_paths(left, right, f"{prefix}[{index}]")]
    return [prefix]


def _design_projection(path: Path, fields: list[str]) -> dict[str, Any]:
    """Read the approved fields without rewriting terms or business semantics."""
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or not fields
        or any(field not in document for field in fields)
    ):
        raise ValueError("本体施工图缺少已批准设计字段。")
    extra = set(document) - set(fields) - {"workflow_version", "design_status", "generated_at"}
    if extra:
        raise ValueError("本体施工图含未经批准的设计字段。")
    return {field: document[field] for field in fields}


def _require_private_snapshot(project_dir: Path, frozen_dir: Path) -> None:
    if frozen_dir.is_symlink() or not frozen_dir.resolve().is_relative_to(project_dir.resolve()):
        raise ValueError("联合设计冻结目录不能是符号链接或位于工程外。")


def _inputs(project_dir: Path) -> dict[str, str]:
    names = (
        "00-document-evidence/source-scope.json",
        "00-document-evidence/cq-intake.json",
        "00-document-evidence/document-register.json",
        "00-document-evidence/evidence-index.json",
        "01-data-understanding/source-understanding.json",
        "01-data-understanding/data-profile.json",
        "01-data-understanding/schema-snapshot.json",
        "01-data-understanding/datasource-inventory.json",
        "02-semantic-recognition/ontology-candidates.yaml",
        "02-semantic-recognition/business-rule-candidates.json",
        "02-semantic-recognition/capability-plan.json",
        "03-mapping-review/mapping.yaml",
        "03-mapping-review/realtime-runtime.json",
    )
    paths = [project_dir / name for name in names if (project_dir / name).is_file()]
    runtime = project_dir / "03-mapping-review/runtime"
    if runtime.exists():
        paths.extend(path for path in runtime.rglob("*") if path.is_file())
    result = {}
    for path in sorted(paths):
        if path.is_symlink() or not path.resolve().is_relative_to(project_dir.resolve()):
            raise ValueError("联合设计输入必须位于当前工程内。")
        result[path.relative_to(project_dir).as_posix()] = _checksum(path)
    if "03-mapping-review/mapping.yaml" not in result:
        raise ValueError("联合设计缺少已审候选映射。")
    return result


def _runtime_review_summary(project_dir: Path, inputs: dict[str, str]) -> dict[str, Any]:
    runtime_dir = project_dir / "03-mapping-review/runtime"
    source_path = runtime_dir / "runtime-source.json"
    if not source_path.is_file():
        return {"status": "NOT_PRESENT", "queries": [], "reasoning_capabilities": []}
    source = json.loads(source_path.read_text(encoding="utf-8"))

    def artifact(relative: str, expected: str | None = None) -> dict[str, Any]:
        path = runtime_dir / relative
        if (
            not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
            or "\\" in relative
            or not path.resolve().is_relative_to(runtime_dir.resolve())
        ):
            raise ValueError("联合设计运行资产路径越界。")
        name = path.relative_to(project_dir).as_posix()
        if name not in inputs or (expected and inputs[name] != expected):
            raise ValueError("联合设计运行资产未进入当前输入指纹或已漂移。")
        return {
            "project_path": name,
            "sha256": inputs[name],
            "content": path.read_text(encoding="utf-8"),
        }

    queries = []
    for name, entry in sorted((source.get("ontop_queries") or {}).items()):
        if not isinstance(entry, dict):
            raise ValueError("联合设计查询必须引用经过评审的查询资产。")
        query = artifact(str(entry.get("path") or ""), entry.get("sha256"))
        queries.append(
            {
                "query_name": name,
                "project_path": query["project_path"],
                "sha256": query["sha256"],
                "sparql": query["content"],
                "query_capability": (source.get("query_capabilities") or {}).get(name),
            }
        )
    rules = []
    for name, entry in sorted((source.get("reasoning_capabilities") or {}).items()):
        rule = artifact(str(entry.get("rule_artifact") or ""), entry.get("rule_sha256"))
        rule_payload = json.loads(rule["content"])
        rules.append(
            {
                "capability_name": name,
                "description_zh": entry.get("description_zh"),
                "engine": entry.get("engine"),
                "evidence_query": entry.get("evidence_query"),
                "source_rule_ids": entry.get("source_rule_ids") or [],
                "project_path": rule["project_path"],
                "sha256": rule["sha256"],
                "rules": rule_payload.get("rules") or [],
                "fact_bindings": entry.get("fact_bindings") or [],
                "ontology_terms": entry.get("ontology_terms") or {},
                "runtime_validation": entry.get("runtime_validation"),
                "closed_world_inputs": entry.get("closed_world_inputs") or [],
                "required_set_source": entry.get("required_set_source"),
            }
        )
    mapping_artifact = source.get("mapping_artifact")
    return {
        "status": source.get("review_status") or "UNDECLARED",
        "runtime_mode": source.get("runtime_mode"),
        "structured_query_enabled": source.get("structured_query_enabled"),
        "reasoning_requirement": source.get("reasoning_requirement"),
        "reasoning_not_applicable_reason": source.get("reasoning_not_applicable_reason"),
        "mapping": artifact(str(mapping_artifact), source.get("mapping_sha256"))
        if mapping_artifact
        else None,
        "queries": queries,
        "reasoning_capabilities": rules,
        "document_query_capabilities": source.get("document_query_capabilities") or [],
        "execution_status": "DESIGN_ONLY_SEE_S6_EVIDENCE",
    }


def prepare_joint_design(project_dir: Path, design: dict[str, Any]) -> dict[str, Any]:
    inputs = _inputs(project_dir)
    basis = {"design_sha256": _digest(design), "source_artifacts": inputs}
    rules_path = project_dir / "02-semantic-recognition/business-rule-candidates.json"
    rules = json.loads(rules_path.read_text()) if rules_path.is_file() else []
    pending_rule_review_ids = sorted(
        str(rule.get("id") or "")
        for rule in rules
        if isinstance(rule, dict) and rule.get("review_required")
    )
    mapping = yaml.safe_load((project_dir / "03-mapping-review/mapping.yaml").read_text())
    scope_path = project_dir / "00-document-evidence/source-scope.json"
    scope = json.loads(scope_path.read_text()) if scope_path.is_file() else {}
    return {
        "schema_version": 1,
        "status": "PENDING",
        "review_scope": "JOINT_DESIGN",
        **basis,
        "joint_design_fingerprint": _digest(basis),
        "joint_design_summary": {
            "business_goal": scope.get("business_goal"),
            "source_scope": scope,
            "ontology_iri": design.get("ontology_iri"),
            "version": design.get("version") or design.get("ontology_version"),
            "classes": design.get("classes", []),
            "object_properties": design.get("object_properties", []),
            "data_properties": design.get("data_properties", []),
            "logical_axioms": design.get("logical_axioms", []),
            "logical_axiom_applicability": design.get("logical_axiom_applicability"),
            "constraints": design.get("constraints", []),
            "business_rules": rules,
            "pending_rule_review_ids": pending_rule_review_ids,
            "runtime_design": _runtime_review_summary(project_dir, inputs),
            "mappings": mapping.get("mappings", []),
            "competency_questions": design.get("competency_questions", []),
            "approval_meaning": "确认本体、映射、业务规则与验收预期组成的完整设计；上线发布另行批准。",
        },
    }


def verify_joint_review(
    project_dir: Path, design: dict[str, Any], review: dict[str, Any]
) -> dict[str, Any]:
    current = prepare_joint_design(project_dir, design)
    if review.get("review_scope") != "JOINT_DESIGN":
        raise ValueError("联合设计评审范围无效，请重新生成并确认联合设计。")
    basis = review
    if "design_sha256" not in basis or "source_artifacts" not in basis:
        draft_path = project_dir / "04-ontology-design/joint-design-draft.json"
        if draft_path.is_file():
            candidate = json.loads(draft_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and candidate.get("joint_design_fingerprint") == review.get("joint_design_fingerprint"):
                basis = candidate
    if basis.get("design_sha256") != current["design_sha256"]:
        draft_path = project_dir / "04-ontology-design/ontology-design-draft.yaml"
        saved = (yaml.safe_load(draft_path.read_text(encoding="utf-8")) or {}) if draft_path.is_file() else {}
        saved_hash = _digest(saved)
        fields = ("ontology_iri", "version", "classes", "object_properties", "data_properties",
                  "logical_axioms", "constraints", "competency_questions")
        summary = basis.get("joint_design_summary") or {}
        changed = []
        for field in fields:
            value = (design.get("version") or design.get("ontology_version")) if field == "version" else design.get(field)
            if field not in ("ontology_iri", "version"):
                value = value or []
            if field in summary and summary[field] != value:
                changed.append(field)
        if saved_hash == current["design_sha256"]:
            reason = "已保存草案与评审指纹不一致"
        else:
            changed.extend(path for path in _changed_paths(saved, design) if path not in changed)
            reason = "恢复输入与已保存草案不一致"
        raise ValueError(
            "本体施工图内容与待批准版本不同：" + reason
            + f"；评审={basis.get('design_sha256')}，草案={saved_hash}，当前={current['design_sha256']}"
            + ("；差异字段=" + "、".join(changed[:6]) if changed else "；已展示业务字段无差异")
        )
    before = basis.get("source_artifacts") or {}
    after = current["source_artifacts"]
    if not isinstance(before, dict):
        raise ValueError("联合设计来源资产索引无效，请重新生成并确认联合设计。")
    if before != after:
        changed = sorted(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        )
        raise ValueError(
            "联合设计来源资产已改变，请重新生成并确认联合设计。变化路径："
            + "、".join(changed[:4])
        )
    if review.get("joint_design_fingerprint") != current["joint_design_fingerprint"]:
        raise ValueError("联合设计摘要指纹与当前来源不一致，请重新生成并确认联合设计。")
    return current


def freeze_joint_design(
    project_dir: Path, design: dict[str, Any], review: dict[str, Any]
) -> dict[str, Any]:
    current = validate_joint_approval(project_dir, design, review)
    stage_dir = project_dir / "04-ontology-design"
    frozen_dir = stage_dir / "joint-design-inputs"
    _require_private_snapshot(project_dir, frozen_dir)
    approved_fields = sorted(design)
    if (
        _digest(_design_projection(stage_dir / "ontology-design.yaml", approved_fields))
        != current["design_sha256"]
    ):
        raise ValueError("本体施工图与负责人已批准的联合设计不一致。")
    # Only regenerate this module's private snapshot; historical revision copies
    # and published packages are managed by the workflow and never touched here.
    if frozen_dir.exists():
        shutil.rmtree(frozen_dir)
    for relative, expected in current["source_artifacts"].items():
        source = project_dir / relative
        if _checksum(source) != expected:
            raise ValueError(f"联合设计输入在冻结期间改变：{relative}")
        target = frozen_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if target.is_symlink() or _checksum(target) != expected or _checksum(source) != expected:
            raise ValueError(f"联合设计输入在冻结复制期间改变：{relative}")
    if _inputs(project_dir) != current["source_artifacts"]:
        raise ValueError("联合设计输入集合在冻结期间改变，请重新评审。")
    if (
        _digest(_design_projection(stage_dir / "ontology-design.yaml", approved_fields))
        != current["design_sha256"]
    ):
        raise ValueError("本体施工图在冻结期间改变，请重新评审。")
    baseline = {
        **current,
        "status": "APPROVED",
        "approved_by": review["decided_by"],
        "approved_at": review["decided_at"],
        "rationale": review["rationale"],
        "approved_design_fields": approved_fields,
        "ontology_design_artifact_sha256": _checksum(stage_dir / "ontology-design.yaml"),
        "frozen_inputs_directory": "04-ontology-design/joint-design-inputs",
    }
    (stage_dir / "joint-design-baseline.json").write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return baseline


def validate_joint_approval(
    project_dir: Path, design: dict[str, Any], review: dict[str, Any]
) -> dict[str, Any]:
    """Validate the exact approval before changing review or workflow state."""
    current = verify_joint_review(project_dir, design, review)
    if review.get("status") != "APPROVED" or review.get("approval_mode") != "HUMAN":
        raise ValueError("联合设计尚未获得负责人明确批准。")
    if any(
        not str(review.get(key) or "").strip() for key in ("decided_by", "decided_at", "rationale")
    ):
        raise ValueError("联合设计批准缺少负责人、时间或决定依据。")
    pending_rules = current["joint_design_summary"]["pending_rule_review_ids"]
    unreviewed = [rule_id for rule_id in pending_rules if rule_id not in review["rationale"]]
    if unreviewed:
        raise ValueError("联合设计批准理由未逐项确认待评审规则：" + "、".join(unreviewed))
    return current


def verify_joint_baseline(project_dir: Path) -> dict[str, Any]:
    stage_dir = project_dir / "04-ontology-design"
    path = stage_dir / "joint-design-baseline.json"
    if not path.is_file():
        raise ValueError("缺少 S4 已批准联合设计基线。")
    baseline = json.loads(path.read_text())
    if baseline.get("status") != "APPROVED" or not baseline.get("approved_by"):
        raise ValueError("联合设计基线没有明确批准。")
    if _inputs(project_dir) != baseline.get("source_artifacts"):
        raise ValueError("联合设计批准后的来源、映射或规则已漂移，必须重新评审。")
    if _checksum(stage_dir / "ontology-design.yaml") != baseline.get(
        "ontology_design_artifact_sha256"
    ):
        raise ValueError("联合设计批准后的本体施工图已漂移。")
    fields = baseline.get("approved_design_fields")
    if not isinstance(fields, list) or not all(isinstance(field, str) for field in fields):
        raise ValueError("联合设计基线缺少已批准的设计字段。")
    if _digest(_design_projection(stage_dir / "ontology-design.yaml", fields)) != baseline.get(
        "design_sha256"
    ):
        raise ValueError("本体施工图与批准设计的语义指纹不一致。")
    frozen_dir = stage_dir / "joint-design-inputs"
    _require_private_snapshot(project_dir, frozen_dir)
    for relative, expected in baseline["source_artifacts"].items():
        frozen = frozen_dir / relative
        if (
            frozen.is_symlink()
            or not frozen.resolve().is_relative_to(frozen_dir.resolve())
            or not frozen.is_file()
            or _checksum(frozen) != expected
        ):
            raise ValueError(f"联合设计冻结输入缺失或已改变：{relative}")
    basis = {key: baseline[key] for key in ("design_sha256", "source_artifacts")}
    if _digest(basis) != baseline.get("joint_design_fingerprint"):
        raise ValueError("联合设计基线指纹不一致。")
    return baseline
