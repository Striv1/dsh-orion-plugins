"""Read-only revision impact and byte-verified artifact reuse proposals.

These proofs never advance workflow stages, inherit approvals or modify releases.
Caller-supplied component input inventories must be complete platform contracts;
missing components fail closed. Declared dependencies cannot narrow stage policy.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .dependency_graph import COMPONENT_STAGE_IMPACT, component_revalidation_plan

SOURCE_MODES = {"DOCUMENT_ONLY", "DATABASE_ONLY", "HYBRID"}
REASONS = {
    "SOURCE_EVIDENCE": "来源或来源授权输入变化，全部下游成果需要重新验证。",
    "MAPPING": "映射变化，需要重新检查设计、构建和全量验收。",
    "RUNTIME_RULES": "规则变化，需要规则评审、联合设计及正反例与问答回归。",
    "COMPETENCY_QUESTIONS": "CQ 新增、删除或验收口径变化，需要联合设计与新旧 CQ 回归。",
}


def _file(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or any(p in {"..", "."} for p in path.parts):
        raise ValueError("UNSAFE_INPUT_PATH")
    current = root.resolve()
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("SYMLINK_INPUT_PATH")
    if not current.is_file():
        raise ValueError("MISSING_INPUT_FILE")
    return current


def _hash(root: Path, relative: str) -> str:
    with _file(root, relative).open("rb") as stream:
        return "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()


def _fingerprint(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                                separators=(",", ":")).encode()).hexdigest()


def capture_revision_inputs(root: Path, *, source_mode: str,
                            component_paths: dict[str, list[str]]) -> dict[str, Any]:
    """Capture existing local inputs. Empty paths explicitly mean not applicable.

    All component keys remain mandatory; this API does not infer completeness.
    Source/profile evidence cannot be marked not applicable in any source mode.
    """
    if source_mode not in SOURCE_MODES or set(component_paths) != set(COMPONENT_STAGE_IMPACT):
        raise ValueError("INCOMPLETE_COMPONENT_CONTRACT")
    if not component_paths["SOURCE_EVIDENCE"] or not component_paths["DATA_PROFILE"]:
        raise ValueError("MISSING_SOURCE_OR_PROFILE_EVIDENCE")
    components = {key: {path: _hash(root, path) for path in sorted(set(paths))}
                  for key, paths in component_paths.items()}
    return {"source_mode": source_mode, "components": components,
            "input_fingerprint": _fingerprint({"source_mode": source_mode, "components": components})}


def _verify(root: Path, manifest: dict[str, Any]) -> list[str]:
    try:
        expected = capture_project_inputs(root) if manifest.get("inventory_policy") == PROJECT_INPUT_POLICY else capture_revision_inputs(root, source_mode=manifest["source_mode"],
                                           component_paths={k: list(v) for k, v in manifest["components"].items()})
        if expected != manifest:
            return ["INPUT_MANIFEST_OR_BYTES_CHANGED"]
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        return ["INPUT_MANIFEST_UNVERIFIABLE"]
    return []


def prove_revision_reuse(*, before_dir: Path, after_dir: Path,
                        before: dict[str, Any], after: dict[str, Any],
                        artifacts: list[dict[str, str]]) -> dict[str, Any]:
    """Propose reuse only after actual source and candidate artifact read-back.

    Each artifact supplies id, stage, before_path, after_path, artifact_sha256
    and input_fingerprint (the historical stage input fingerprint). Both artifact
    copies must already exist. No files are copied by this function.
    """
    issues = _verify(before_dir, before) + _verify(after_dir, after)
    changed = []
    if not issues:
        changed = [component for component in COMPONENT_STAGE_IMPACT
                   if before["components"][component] != after["components"][component]]
        if before["source_mode"] != after["source_mode"]:
            changed = list(dict.fromkeys(["SOURCE_EVIDENCE", *changed]))
    plan = component_revalidation_plan(changed if not issues else COMPONENT_STAGE_IMPACT)
    plan["candidate_stages_for_reuse"] = [stage for stage in plan.pop("reusable_stages") if stage != "S7"]
    database_source = any(manifest.get("source_mode") in {"DATABASE_ONLY", "HYBRID"}
                          for manifest in (before, after))
    freshness = {
        "status": "UNKNOWN" if issues else "SNAPSHOT_ONLY",
        "verification_scope": "PERSISTED_ARTIFACT_BYTES",
        "current_source_fingerprint_status": "UNKNOWN",
        "requires_live_source_revalidation": database_source or bool(issues),
        "required_gate": "S1_SOURCE_REVALIDATION" if database_source else "FORMAL_SOURCE_SCOPE_REVIEW",
        "reason": "文件快照一致不证明当前数据库数据或结构未变；数据库及混合来源正式复用前必须经原 S1 来源核验。"
                  if database_source else "仅验证已登记的文档快照字节，不扩大外部来源授权或代替正式来源审核。",
    }
    proposals = []
    for artifact in artifacts:
        stage = artifact.get("stage")
        reasons = list(issues)
        required = [key for key, stages in COMPONENT_STAGE_IMPACT.items() if stage in stages]
        if stage not in {f"S{i}" for i in range(8)}:
            reasons.append("UNKNOWN_STAGE")
        if stage == "S7" or any(Path(str(artifact.get(key, ""))).parts[:1] == ("07-release",)
                                for key in ("before_path", "after_path")):
            reasons.append("IMMUTABLE_RELEASE_REQUIRES_NEW_PUBLICATION")
        impacted = sorted(set(required) & set(changed))
        if impacted:
            reasons.append("CHANGED_INPUT_COMPONENTS:" + ",".join(impacted))
        if not issues and required:
            old_inputs = {key: before["components"][key] for key in required}
            new_inputs = {key: after["components"][key] for key in required}
            if artifact.get("input_fingerprint") != _fingerprint(old_inputs) or old_inputs != new_inputs:
                reasons.append("ARTIFACT_INPUT_FINGERPRINT_MISMATCH")
        try:
            expected = artifact["artifact_sha256"]
            if _hash(before_dir, artifact["before_path"]) != expected or _hash(after_dir, artifact["after_path"]) != expected:
                reasons.append("ARTIFACT_BYTES_CHANGED")
        except (KeyError, ValueError, OSError, TypeError):
            reasons.append("ARTIFACT_UNVERIFIABLE")
        proposals.append({"artifact_id": artifact.get("id"), "stage": stage,
                          "decision": "REUSE_CANDIDATE" if not reasons else "REVALIDATE",
                          "reasons": reasons or ["ACTUAL_INPUT_AND_ARTIFACT_FINGERPRINTS_MATCH"],
                          "approval_inherited": False, "stage_passed": False,
                          "formal_reuse_allowed": False,
                          "required_before_formal_reuse": freshness["required_gate"]})
    return {"policy_version": "revision-reuse-proof-v1", "authority": "READ_ONLY_PROPOSAL",
            "status": "UNKNOWN" if issues else "VERIFIED", "issues": sorted(set(issues)),
            "verification_scope": "PERSISTED_ARTIFACT_BYTE_PROOF_ONLY",
            "source_freshness": freshness,
            "requires_live_source_revalidation": freshness["requires_live_source_revalidation"],
            "formal_reuse_allowed": False,
            **plan, "change_explanations": {c: REASONS.get(c, "该组件变化，其声明的下游阶段需要重新验证。") for c in changed},
            "artifacts": proposals, "workflow_mutated": False, "release_mutated": False}


def artifact_input_fingerprint(manifest: dict[str, Any], stage: str) -> str:
    """Bind an artifact receipt to every policy-required component input."""
    if stage not in {f"S{i}" for i in range(8)}:
        raise ValueError("UNKNOWN_STAGE")
    return _fingerprint({key: manifest["components"][key]
                         for key, stages in COMPONENT_STAGE_IMPACT.items() if stage in stages})


def explain_design_changes(*, before_dir: Path, after_dir: Path,
                           before_snapshot: dict[str, Any],
                           after_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Explain stable-ID design deltas after rebuilding both snapshots from disk.

    This supplements byte proofs; even structural equivalence does not grant reuse.
    Separate revision project IDs are allowed, with both actual roots explicit.
    """
    from .design_workspace import build_snapshot, extract_components

    kinds = {"cq": "COMPETENCY_QUESTIONS", "assessment": "COMPETENCY_QUESTIONS",
             "candidate": "SEMANTIC_MODEL", "rule": "RUNTIME_RULES", "mapping": "MAPPING",
             "query": "COMPETENCY_QUESTIONS", **{kind: "ONTOLOGY_SCHEMA" for kind in (
                 "class", "object_property", "data_property", "constraint", "axiom")}}
    try:
        for root, snapshot in ((before_dir, before_snapshot), (after_dir, after_snapshot)):
            if snapshot.get("issues") or build_snapshot(root) != snapshot:
                raise ValueError("DESIGN_SNAPSHOT_NOT_CURRENT")
        old, new = extract_components(before_snapshot), extract_components(after_snapshot)
        changes = []
        for key in sorted(old.keys() | new.keys()):
            previous, current = old.get(key), new.get(key)
            item = current or previous
            component = kinds[item["kind"]]
            if previous and current and previous["content_sha256"] == current["content_sha256"]:
                continue
            changes.append({"id": item["id"], "kind": item["kind"], "component": component,
                            "change": "ADDED" if previous is None else "REMOVED" if current is None else "MODIFIED",
                            "structurally_equivalent": bool(previous and current and previous["semantic_sha256"] == current["semantic_sha256"]),
                            "required_revalidation_stages": list(COMPONENT_STAGE_IMPACT[component]),
                            "reason": REASONS.get(component, "设计项变化，需要其下游阶段重新验证。")})
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        return {"status": "UNKNOWN", "changes": [], "reason": "DESIGN_SNAPSHOT_UNVERIFIABLE",
                "approval_inherited": False}
    return {"status": "VERIFIED", "changes": changes, "approval_inherited": False,
            "scope": "INDEXED_DESIGN_ONLY_USE_BYTE_PROOFS_FOR_ALL_OTHER_INPUTS"}


PROJECT_INPUT_POLICY = "platform-stage-inventory-v1"


def _stage_files(project_dir: Path) -> dict[str, list[str]]:
    # Import lazily: workflow calls this module without a module-load cycle.
    import os

    from .workflow import STAGE_FOLDERS

    inventory = {}
    for stage, folder in STAGE_FOLDERS.items():
        directory = project_dir / folder
        if directory.is_symlink():
            raise ValueError("SYMLINK_STAGE_DIRECTORY")
        paths = []
        if directory.exists():
            if not directory.is_dir():
                raise ValueError("INVALID_STAGE_DIRECTORY")
            for base, dirs, files in os.walk(directory, followlinks=False):
                for name in dirs + files:
                    path = Path(base) / name
                    if path.is_symlink():
                        raise ValueError("SYMLINK_STAGE_INPUT")
                    if name in files:
                        if not path.is_file():
                            raise ValueError("NON_REGULAR_STAGE_INPUT")
                        paths.append(path.relative_to(project_dir).as_posix())
        inventory[stage] = sorted(paths)
    return inventory


def _component_for(stage: str, relative: str) -> str:
    path = Path(relative)
    name = path.name
    # Known CQ/acceptance and rule contracts have narrower semantics than their
    # containing stage. Everything else, including newly added unknown files,
    # inherits that stage's conservative component classification.
    if (stage == "S0" and path.parts == ("00-document-evidence", "cq-intake.json")) or (stage == "S2" and name == "capability-plan.json"):
        return "COMPETENCY_QUESTIONS"
    if stage == "S2" and name == "business-rule-candidates.json":
        return "RUNTIME_RULES"
    if stage == "S3" and "runtime" in path.parts:
        return "RUNTIME_RULES" if "rules" in path.parts else "RUNTIME_MAPPING"
    return {"S0": "SOURCE_EVIDENCE", "S1": "DATA_PROFILE", "S2": "SEMANTIC_MODEL",
            "S3": "MAPPING", "S4": "ONTOLOGY_SCHEMA", "S5": "ONTOLOGY_BINARY",
            "S6": "VALIDATION_EVIDENCE", "S7": "DEPLOYMENT_CONFIG"}[stage]


def capture_project_inputs(project_dir: Path) -> dict[str, Any]:
    """Enumerate the platform-owned complete stage inventory, never model paths."""
    state_path = _file(project_dir, "workflow-state.json")
    state_before = state_path.read_bytes()
    state = json.loads(state_before)
    inventory = _stage_files(project_dir)
    components = {key: [] for key in COMPONENT_STAGE_IMPACT}
    for stage, paths in inventory.items():
        for path in paths:
            components[_component_for(stage, path)].append(path)
    result = capture_revision_inputs(project_dir, source_mode=state["intake_mode"], component_paths=components)
    if inventory != _stage_files(project_dir) or state_before != state_path.read_bytes():
        raise ValueError("PROJECT_INPUTS_CHANGED_DURING_CAPTURE")
    result["inventory_policy"] = PROJECT_INPUT_POLICY
    return result


def capture_artifact_receipts(project_dir: Path, *, inputs: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """Capture current baseline bytes, not an assertion of historical approval.

    Every stage file is included, even diagnostics or files unknown to the model.
    The frozen snapshot must remain available for later actual-byte verification.
    """
    inputs = inputs or capture_project_inputs(project_dir)
    if inputs != capture_project_inputs(project_dir):
        raise ValueError("PROJECT_INPUTS_CHANGED_BEFORE_ARTIFACT_CAPTURE")
    inventory = _stage_files(project_dir)
    result = [{"id": path, "stage": stage, "before_path": path, "after_path": path,
               "artifact_sha256": _hash(project_dir, path),
               "input_fingerprint": artifact_input_fingerprint(inputs, stage),
               "receipt_origin": "CURRENT_BASELINE_CAPTURE_NOT_HISTORICAL_APPROVAL"}
              for stage, paths in inventory.items() for path in paths]
    if inputs != capture_project_inputs(project_dir):
        raise ValueError("PROJECT_INPUTS_CHANGED_DURING_ARTIFACT_CAPTURE")
    return result


def get_revision_reuse_plan(before_dir: Path, after_dir: Path) -> dict[str, Any]:
    """Safe workflow/MCP wrapper: roots only, no caller-declared dependencies."""
    try:
        before, after = capture_project_inputs(before_dir), capture_project_inputs(after_dir)
        receipts = capture_artifact_receipts(before_dir, inputs=before)
        result = prove_revision_reuse(before_dir=before_dir, after_dir=after_dir,
                                     before=before, after=after, artifacts=receipts)
        from .design_workspace import build_snapshot
        result["design_changes"] = explain_design_changes(
            before_dir=before_dir, after_dir=after_dir,
            before_snapshot=build_snapshot(before_dir), after_snapshot=build_snapshot(after_dir))
        result["baseline_kind"] = "CURRENT_BYTES_NOT_HISTORICAL_APPROVAL"
        old_paths = {p for paths in before["components"].values() for p in paths}
        new_paths = {p for paths in after["components"].values() for p in paths}
        result["added_files"] = sorted(new_paths - old_paths)
        result["removed_files"] = sorted(old_paths - new_paths)
        return result
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        return {"policy_version": PROJECT_INPUT_POLICY, "authority": "READ_ONLY_PROPOSAL",
                "status": "UNKNOWN", "issues": ["PROJECT_INPUT_CONTRACT_UNVERIFIABLE"],
                "required_revalidation_stages": [f"S{i}" for i in range(8)],
                "candidate_stages_for_reuse": [], "artifacts": [],
                "source_freshness": {"status": "UNKNOWN", "current_source_fingerprint_status": "UNKNOWN"},
                "requires_live_source_revalidation": True, "formal_reuse_allowed": False,
                "workflow_mutated": False, "release_mutated": False}


def verify_revision_origin(service, source_dir: Path, reference: dict[str, Any]) -> dict[str, str]:
    """Verify the frozen origin, not merely its unchanged publication pointer."""
    publication = json.loads(_file(source_dir, '07-release/publication.json').read_text())
    version = reference.get('source_release_version')
    if not isinstance(version, str) or not version or '/' in version or '\\' in version or version in {'.', '..'}:
        raise ValueError('INVALID_SOURCE_RELEASE_VERSION')
    expected_package = f'07-release/ontology-engineering-package-{version}'
    if publication.get('release_version') != version or publication.get('package_path') != expected_package:
        raise ValueError('SOURCE_RELEASE_IDENTITY_MISMATCH')
    # Full inventory rejects symlinks before the generic package validator reads
    # anything; the latter validates exact membership and individual hashes.
    _stage_files(source_dir)
    manifest_path = expected_package + '/manifest.json'
    manifest_hash = _hash(source_dir, manifest_path)
    if (manifest_hash != reference.get('source_manifest_sha256')
            or manifest_hash != publication.get('package_manifest_sha256')
            or _hash(source_dir, '07-release/publication.json') != reference.get('source_publication_sha256')):
        raise ValueError('SOURCE_RELEASE_RECEIPT_MISMATCH')
    manifest = service._verify_release_package(source_dir / expected_package)
    if manifest.get('project_id') != source_dir.name or manifest.get('release_version') != version:
        raise ValueError('SOURCE_PACKAGE_IDENTITY_MISMATCH')
    return {'source_project_id': source_dir.name, 'source_release_version': version,
            'source_manifest_sha256': manifest_hash, 'status': 'VERIFIED'}
