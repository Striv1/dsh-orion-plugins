"""Regenerate compiler-owned assets, preserving unowned runtime extensions."""
from __future__ import annotations

import copy
import hashlib
import json
import re

from services.ontology_contracts.errors import WorkflowError
from services.ontology_contracts.obda import parse_obda_prefixes, parse_obda_targets
from services.ontology_engineering.stage_submission_validation import DATABASE_CLASS_MAPPING_TYPES

_MANAGED_MAPS = ("ontop_queries", "query_capabilities", "reasoning_capabilities")


def _asset_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def runtime_compiler_manifest(runtime: dict) -> dict:
    """Fingerprint exactly the generated assets, never a merged user runtime."""
    if not isinstance(runtime.get("mapping_obda"), str):
        raise WorkflowError("mapping_obda 必须是文本，不能生成编译资源清单。")
    assets = {"mapping_obda": _asset_hash(runtime["mapping_obda"])}
    for field in _MANAGED_MAPS:
        values = runtime.get(field, {})
        if not isinstance(values, dict) or any(not isinstance(name, str) for name in values):
            raise WorkflowError(f"{field} 必须是以资源名称为键的对象，不能生成编译资源清单。")
        assets[field] = {name: _asset_hash(value) for name, value in values.items()}
    return {"version": 1, "assets": assets}


def _manifest_assets(manifest: object) -> dict:
    """Reject damaged provenance rather than falling back to legacy merging."""
    message = "compiler_manifest 格式或版本无效，无法确认受管资源；请先人工复核编译清单。"
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
            or manifest["version"] != 1 or not isinstance(manifest.get("assets"), dict)):
        raise WorkflowError(message)
    assets = manifest["assets"]
    if set(assets) != {"mapping_obda", *_MANAGED_MAPS}:
        raise WorkflowError(message)
    hashes = [assets["mapping_obda"]]
    for field in _MANAGED_MAPS:
        values = assets[field]
        if not isinstance(values, dict) or any(not isinstance(name, str) for name in values):
            raise WorkflowError(message)
        hashes.extend(values.values())
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
        raise WorkflowError(message)
    return assets


def _conflict(path: str) -> WorkflowError:
    return WorkflowError(
        f"编译冲突：{path} 已被人工修改或删除，当前内容与新编译结果不一致；"
        "请人工合并或复核该资源，未覆盖现有运行内容。"
    )


def _regenerate(runtime: dict, generated: dict, old_assets: dict, new_manifest: dict) -> None:
    new_assets = new_manifest["assets"]
    current_obda = runtime.get("mapping_obda")
    if _asset_hash(current_obda) not in (old_assets["mapping_obda"], new_assets["mapping_obda"]):
        raise _conflict("mapping_obda")
    runtime["mapping_obda"] = generated["mapping_obda"]
    for field in _MANAGED_MAPS:
        current = runtime.setdefault(field, {})
        old, new = old_assets[field], new_assets[field]
        for name in sorted(set(old) | set(new)):
            current_hash = _asset_hash(current[name]) if name in current else None
            if name in old:
                # A manual edit is safe only when the desired compilation has
                # independently converged to it (including the same deletion).
                if current_hash != old[name] and current_hash != new.get(name):
                    raise _conflict(f"{field}.{name}")
            elif name in current:
                raise WorkflowError(
                    f"编译命名冲突：{field}.{name} 是未受管的人工扩展；请先更名或人工复核，不能自动接管。"
                )
            if name in new:
                current[name] = copy.deepcopy(generated[field][name])
            else:
                current.pop(name, None)
    # Do not hash the merged runtime: that would claim ownership of manual extras.
    runtime["compiler_manifest"] = copy.deepcopy(new_manifest)


def _covered(mapping: dict, obda: str, namespace: str) -> bool:
    local = str(mapping.get("target") or "")
    tokens = [re.escape(prefix + ":" + local) for prefix, iri in parse_obda_prefixes(obda).items()
              if iri == namespace]
    tokens.append(re.escape("<" + namespace + local + ">"))
    term = r"(?<![\w:-])(?:" + "|".join(tokens) + r")(?![\w-])"
    if mapping.get("mapping_type") in DATABASE_CLASS_MAPPING_TYPES:
        term = r"(?:\ba|rdf:type)\s+" + term
    else:
        term += r"\s+(?:\{|<|[A-Za-z_]*:)"
    return bool(re.search(term, "\n".join(parse_obda_targets(obda))))


def extend_mapping_runtime(existing: dict, compilation: dict, mapping_draft: dict, *, intake_mode: str) -> dict:
    # The compiler also calls runtime_compiler_manifest; keep this import lazy
    # so it can share ownership hashing without a circular module import.
    from services.ontology_engineering.mapping_runtime_compiler import check_mapping_runtime

    runtime = copy.deepcopy(existing)
    for field in (*_MANAGED_MAPS, "document_fact_queries"):
        if field in runtime and not isinstance(runtime[field], dict):
            raise WorkflowError(f"{field} 必须是对象，不能在结构损坏时增量编译。")
    generated = compilation["runtime"]
    new_manifest = runtime_compiler_manifest(generated)
    if ("compiler_manifest" in generated
            and _manifest_assets(generated["compiler_manifest"]) != new_manifest["assets"]):
        raise WorkflowError("新编译的 compiler_manifest 与生成内容不一致，不能更新运行内容。")
    obda = str(runtime.get("mapping_obda") or "")
    mapping_by_id = {m.get("id"): m for m in mapping_draft["mappings"]}
    if "compiler_manifest" in runtime:
        old_assets = _manifest_assets(runtime["compiler_manifest"])
        added = [entry["mapping_id"] for entry in compilation["compiled_blocks"]
                 if not _covered(mapping_by_id[entry["mapping_id"]], obda, mapping_draft["namespace"])]
        _regenerate(runtime, generated, old_assets, new_manifest)
    else:
        added = _append_legacy(runtime, compilation, mapping_draft)
    if runtime.get("reasoning_capabilities"):
        runtime["reasoning_requirement"] = "REQUIRED"
        runtime.pop("reasoning_not_applicable_reason", None)
    else:
        runtime["reasoning_requirement"] = "NOT_APPLICABLE"
        if "reasoning_not_applicable_reason" in generated:
            runtime.setdefault("reasoning_not_applicable_reason", generated["reasoning_not_applicable_reason"])
    # Keep only genuinely outstanding generated review tasks. Full contract
    # validation still checks the user's existing rules and CQ bindings.
    bound_cqs = {qid for field in ("query_capabilities", "reasoning_capabilities", "document_fact_queries")
                 for cap in (runtime.get(field) or {}).values() if isinstance(cap, dict)
                 for qid in (cap.get("cq_bindings") or {})}
    bound_rules = {rid for cap in (runtime.get("reasoning_capabilities") or {}).values()
                   if isinstance(cap, dict) and all(cap.get(key) for key in (
                       "evidence_query", "fact_bindings", "ontology_terms", "runtime_validation", "question_examples"))
                   for rid in cap.get("source_rule_ids", [])}
    remaining = []
    for item in compilation["open_items"]:
        mapping = mapping_by_id.get(item.get("mapping_id"))
        if mapping and _covered(mapping, runtime["mapping_obda"], mapping_draft["namespace"]):
            continue
        if item.get("kind") == "CQ_BINDING_REQUIRED" and item.get("question_id") in bound_cqs:
            continue
        if item.get("kind") == "REASONING_BINDING_REQUIRED" and item.get("source_rule_id") in bound_rules:
            continue
        remaining.append(copy.deepcopy(item))
    return {"runtime": runtime, "added_mapping_ids": added, "runtime_changed": runtime != existing,
            "open_items": remaining,
            "self_check": check_mapping_runtime(mapping_draft["mappings"], runtime, intake_mode=intake_mode)}


def _append_legacy(runtime: dict, compilation: dict, mapping_draft: dict) -> list[str]:
    """Append unambiguous assets without claiming ownership of reviewed work."""
    obda = str(runtime.get("mapping_obda") or "")
    if parse_obda_prefixes(obda).get("") != mapping_draft["namespace"]:
        raise WorkflowError("当前运行映射命名空间与草稿不一致，不能自动合并；请先复核命名空间变更。")
    if not re.search(r"\]\]\s*$", obda):
        raise WorkflowError("当前 OBDA 集合不完整，不能增量合并；请先修复映射格式。")
    mapping_by_id = {m.get("id"): m for m in mapping_draft["mappings"]}
    added = []
    appended = []
    coverage_obda = obda
    for entry in compilation["compiled_blocks"]:
        if _covered(mapping_by_id[entry["mapping_id"]], coverage_obda, mapping_draft["namespace"]):
            continue  # Existing reviewed/custom blocks are never rewritten.
        block_id = "compiler_" + hashlib.sha256(entry["mapping_id"].encode()).hexdigest()[:16]
        if re.search(rf"(?m)^mappingId\s+{block_id}\s*$", obda):
            raise WorkflowError("已有编译块 ID 与新增目标冲突，需局部评审现有映射，不能覆盖。")
        appended.append(re.sub(r"^mappingId\s+\S+", "mappingId " + block_id, entry["block"], count=1))
        added.append(entry["mapping_id"])
        coverage_obda += "\n" + entry["block"]
    if appended:
        prefixes = parse_obda_prefixes(obda)
        if any("^^xsd:" in block for block in appended):
            if "xsd" in prefixes and prefixes["xsd"] != "http://www.w3.org/2001/XMLSchema#":
                raise WorkflowError("现有 xsd 前缀与标准数据类型冲突，不能增量合并。")
            if "xsd" not in prefixes:
                obda = obda.replace("[PrefixDeclaration]", "[PrefixDeclaration]\nxsd: http://www.w3.org/2001/XMLSchema#", 1)
        runtime["mapping_obda"] = re.sub(r"\]\]\s*$", lambda _: "\n\n" + "\n\n".join(appended) + "\n]]\n", obda)
    for field in _MANAGED_MAPS:
        current = runtime.setdefault(field, {})
        for name, value in compilation["runtime"].get(field, {}).items():
            if name in current and _asset_hash(current[name]) != _asset_hash(value):
                raise WorkflowError(
                    f"编译冲突：{field}.{name} 与新生成内容不同，旧运行内容没有 compiler_manifest；"
                    "无法证明安全更新，请人工合并或更名，未覆盖现有运行内容。"
                )
            current[name] = copy.deepcopy(value)
    return added
