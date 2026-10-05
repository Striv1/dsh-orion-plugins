"""Bounded readback of a committed S3 runtime capability for platform repair."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

KINDS = ("query_capabilities", "reasoning_capabilities", "document_fact_queries")


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def read_runtime_capability(
    service: Any, *, project_id: str, capability_name: str | None = None,
) -> dict[str, Any]:
    project = service._resolve_project(project_id)
    root = project / "03-mapping-review/runtime"
    manifest = root / "runtime-source.json"
    if (root.is_symlink() or not root.resolve().is_relative_to(project.resolve())
            or manifest.is_symlink() or not manifest.is_file()):
        raise ValueError("当前工程没有可回读的 S3 正式运行能力索引。")
    runtime = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(runtime, dict):
        raise ValueError("S3 正式运行能力索引格式无效。")
    state = service._read_state(project)
    lifecycle = (state.get("artifact_lifecycle") or {}).get(
        "03-mapping-review/runtime/runtime-source.json", {},
    )
    base = {
        "project_id": project_id,
        "stage": "S3",
        "stage_status": (state.get("stage_statuses") or {}).get("S3"),
        "artifact_lifecycle": lifecycle.get("status", "CURRENT"),
        "runtime_sha256": _sha256(manifest),
    }
    if capability_name is None:
        return {**base, "capabilities": [
            {"kind": kind, "name": name,
             "business_question_ids": cap.get("business_question_ids") or [],
             "fact_binding_count": len(cap.get("fact_bindings") or [])}
            for kind in KINDS
            for name, cap in (runtime.get(kind) or {}).items()
            if isinstance(cap, dict)
        ]}
    matches = [(kind, cap) for kind in KINDS
               if isinstance(cap := (runtime.get(kind) or {}).get(capability_name), dict)]
    if len(matches) != 1:
        raise ValueError("运行能力名称不存在或不唯一；先列出当前工程的能力清单。")
    kind, capability = matches[0]
    result = {**base, "kind": kind, "name": capability_name, "capability": capability}
    relative = capability.get("rule_artifact")
    if relative:
        part = Path(str(relative))
        candidate = root / part
        target = candidate.resolve()
        if (part.is_absolute() or ".." in part.parts or root.resolve() not in target.parents
                or any(path.is_symlink() for path in (candidate, *candidate.parents)
                       if path != root and root in path.parents)
                or not target.is_file()):
            raise ValueError("S3 规则包路径不安全或不存在。")
        package_bytes = target.read_bytes()
        actual = "sha256:" + hashlib.sha256(package_bytes).hexdigest()
        if actual != capability.get("rule_sha256"):
            raise ValueError("S3 规则包与正式索引校验值不一致。")
        result["rule_package_sha256"] = actual
        result["rule_package"] = json.loads(package_bytes)
    return result
