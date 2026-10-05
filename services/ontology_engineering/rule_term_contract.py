"""Check whether approved rule conclusions can inhabit their S4 ontology terms."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from services.ontology_contracts.rule_syntax import parse_atom


def rule_conclusion_type_issues(
    runtime_dir: Path, capabilities: dict[str, Any], term_kinds: dict[str, str],
) -> list[str]:
    issues: list[str] = []
    root = runtime_dir.resolve()
    for name, capability in capabilities.items():
        rules = capability.get("rules") or []
        artifact = capability.get("rule_artifact")
        if artifact:
            relative = Path(str(artifact))
            candidate = runtime_dir / relative
            path = candidate.resolve()
            if (relative.is_absolute() or ".." in relative.parts or root not in path.parents
                    or any(item.is_symlink() for item in (candidate, *candidate.parents)
                           if item != runtime_dir and runtime_dir in item.parents)
                    or not path.is_file()):
                raise ValueError(f"推理能力 {name} 的正式规则包不存在或路径不安全。")
            data = path.read_bytes()
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if digest != capability.get("rule_sha256"):
                raise ValueError(f"推理能力 {name} 的正式规则包校验值不一致。")
            package = json.loads(data)
            if (not isinstance(package, dict) or package.get("capability_name") != name
                    or not isinstance(package.get("rules"), list)):
                raise ValueError(f"推理能力 {name} 的正式规则包身份或内容无效。")
            rules = package["rules"]
        terms = capability.get("ontology_terms") or {}
        for rule in rules:
            if not isinstance(rule, dict):
                raise ValueError(f"推理能力 {name} 的正式规则条目无效。")
            expression = str(rule.get("expression") or "")
            matched = re.fullmatch(r"IF\s+.+?\s+THEN\s+(.+)", expression, re.I)
            if matched is None:
                raise ValueError(f"推理能力 {name} 的正式规则表达式无效。")
            predicate, arguments = parse_atom(matched[1])
            iri = str(terms.get(predicate) or "")
            if term_kinds.get(iri) in {"DATA_PROPERTY", "OBJECT_PROPERTY"} and len(arguments) != 2:
                issues.append(f"{name}: 结论 {predicate}({len(arguments)} 参数) → {iri} 是二元属性")
            elif term_kinds.get(iri) == "CLASS" and len(arguments) != 1:
                issues.append(f"{name}: 结论 {predicate}({len(arguments)} 参数) → {iri} 是一元业务类")
    return issues
