"""Detect CQ graph patterns that can never match the S6/S7 answer graph.

A reasoning capability's fact_bindings premises are consumed from evidence
rows inside the rule engine; unless the premise class is also mapped in OBDA
or produced as a result predicate, it is never asserted in the combined graph.
A CQ that *requires* such a class (`?x a :Premise` outside OPTIONAL/NOT
EXISTS) is guaranteed to return zero rows.  This module is read-only and
returns preflight issues; it never relaxes or rewrites a contract.
"""

from __future__ import annotations

import re
from typing import Any

from rdflib import RDF, URIRef
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery


def obda_asserted_classes(mapping_obda: str) -> set[str]:
    """Class IRIs asserted by any OBDA `target ... a :Class` line."""
    prefixes: dict[str, str] = {}
    head = mapping_obda.split("[MappingDeclaration]", 1)[0]
    for line in head.splitlines():
        match = re.match(r"^\s*([A-Za-z0-9_-]*):\s+(\S+)\s*$", line)
        if match:
            prefixes[match.group(1)] = match.group(2)
    classes: set[str] = set()
    for line in mapping_obda.splitlines():
        if not line.strip().startswith("target"):
            continue
        for term in re.findall(r"\ba\s+(<[^>]+>|[A-Za-z0-9_-]*:[A-Za-z0-9_.-]+)", line):
            if term.startswith("<"):
                classes.add(term[1:-1])
            else:
                prefix, _, local = term.partition(":")
                if prefix in prefixes:
                    classes.add(prefixes[prefix] + local)
    return classes


def required_type_classes(query: str) -> set[str]:
    """rdf:type objects in mandatory BGPs (OPTIONAL right side and FILTER NOT EXISTS excluded)."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        name = getattr(node, "name", None)
        if name == "BGP":
            for _s, p, o in node.get("triples", []):
                if p == RDF.type and isinstance(o, URIRef):
                    found.add(str(o))
            return
        keys = ("p", "p1") if name in {"LeftJoin", "Minus"} else ("p", "p1", "p2")
        if name == "Union":
            return  # a union branch alone is not mandatory
        for key in keys:
            if key in node:
                walk(node[key])

    try:
        walk(translateQuery(parseQuery(query)).algebra)
    except Exception:
        return set()
    return found


def union_unbound(questions: list[dict[str, Any]], runtime: dict[str, Any]) -> list[dict[str, Any]]:
    from .cq_binding_guarantee import unbound_required_binding_issues

    return unbound_required_binding_issues(questions, runtime)


def unmaterialized_premise_classes(runtime: dict[str, Any], mapping_obda: str) -> dict[str, str]:
    """Premise-only class IRI -> capability name, for premises absent from the answer graph."""
    raw = runtime.get("reasoning_capabilities")
    capabilities = {name: cap for name, cap in (raw.items() if isinstance(raw, dict) else [])
                    if isinstance(cap, dict) and isinstance(cap.get("ontology_terms") or {}, dict)
                    and isinstance(cap.get("result_predicates") or [], list)
                    and isinstance(cap.get("fact_bindings") or [], list)}
    produced = {
        str((cap.get("ontology_terms") or {}).get(str(predicate)) or "")
        for cap in capabilities.values() for predicate in cap.get("result_predicates") or []
    }
    asserted = obda_asserted_classes(mapping_obda)
    premises: dict[str, str] = {}
    for name, cap in capabilities.items():
        terms = cap.get("ontology_terms") or {}
        for binding in cap.get("fact_bindings") or []:
            if not isinstance(binding, dict):
                continue
            iri = str(terms.get(str(binding.get("predicate") or "")) or "")
            if iri and iri not in produced and iri not in asserted:
                premises.setdefault(iri, name)
    return premises


def reviewed_binding_source_path(runtime: dict[str, Any], binding: dict[str, Any] | None) -> str | None:
    """S3 location that owns a compiled CQ query (S4 CQs are compiled from S3 cq_bindings)."""
    if not isinstance(binding, dict):
        return None
    name, qid = str(binding.get("query_name") or ""), str(binding.get("question_id") or "")
    for kind in ("reasoning_capabilities", "document_fact_queries", "query_capabilities"):
        group = runtime.get(kind)
        cap = group.get(name) if isinstance(group, dict) else None
        if not isinstance(cap, dict) or qid not in (cap.get("cq_bindings") or {}):
            continue
        if (cap["cq_bindings"].get(qid) or {}).get("cq_sparql"):
            return f"realtime_runtime.{kind}.{name}.cq_bindings.{qid}.cq_sparql"
        if kind == "query_capabilities":
            return f"realtime_runtime.ontop_queries.{name}"
        return f"realtime_runtime.{kind}.{name}.sparql"
    return None


def unreachable_cq_class_issues(
    questions: list[dict[str, Any]], runtime: dict[str, Any], mapping_obda: str,
) -> list[dict[str, Any]]:
    premises = unmaterialized_premise_classes(runtime, mapping_obda)
    if not premises:
        return []
    issues = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        contract = question.get("answer_contract") or {}
        for iri in sorted(required_type_classes(str(question.get("sparql") or "")) & set(premises)):
            local = re.split(r"[#/]", iri)[-1]
            derived = ", ".join(contract.get("derived_predicates") or []) or "规则结论类"
            issues.append({
                "gate": "G-S4-CQ-UNREACHABLE-CLASS",
                "message": (
                    f"能力问题 {question.get('id')} 的 SPARQL 必需匹配 `a :{local}`，但该类只是推理能力 "
                    f"{premises[iri]} 的 fact_bindings 前提：没有 OBDA 映射、也不是任何规则的 result_predicates，"
                    "S6/S7 答案图中永远不存在该类型，查询必然 0 行。请删除此类型约束，改为查询规则结论"
                    f"（{derived}）与业务字段——前提条件已由规则保证；如确需在图中查询前提，须在 S3 为其补充 OBDA 映射。"
                ),
                "path": f"competency_questions[{index}].sparql",
                "owner": "ENGINEERING_AGENT",
                "question_id": question.get("id"),
                "reason_code": "UNMATERIALIZED_PREMISE_CLASS",
                "premise_class": iri,
                "reasoning_capability": premises[iri],
            })
            upstream = reviewed_binding_source_path(runtime, contract.get("reviewed_runtime_binding"))
            if upstream:
                issues[-1]["upstream_source"] = {
                    "stage": "S3", "path": upstream, "changed_components": ["RUNTIME_RULES"],
                    "instruction": (
                        "该 CQ 查询由 S3 已审 cq_bindings 编译，S4 载荷里改不动：用 preview_stage_rollback"
                        "(target_stage=S3, changed_components=[RUNTIME_RULES]) → reopen_stage_for_correction → "
                        "fork_s3_draft_from_history → 在 path 处 replace_text 删除该类型约束 → S3 预检/提交；"
                        "未变化的 S5 由指纹复用。"
                    ),
                }
    return issues


def s3_runtime_unreachable_cq_issues(runtime: Any) -> list[dict[str, Any]]:
    """Same check at S3 submit time, against the inline cq_bindings queries."""
    if not isinstance(runtime, dict):
        return []
    questions = []
    for kind in ("reasoning_capabilities", "document_fact_queries", "query_capabilities"):
        for name, cap in (runtime.get(kind) or {}).items() if isinstance(runtime.get(kind), dict) else []:
            if not isinstance(cap, dict) or not isinstance(cap.get("cq_bindings"), dict):
                continue
            for qid, binding in cap["cq_bindings"].items():
                if not isinstance(binding, dict):
                    continue
                ontop = runtime.get("ontop_queries") if isinstance(runtime.get("ontop_queries"), dict) else {}
                query = binding.get("cq_sparql") or (ontop.get(name) if kind == "query_capabilities" else cap.get("sparql"))
                case = next((c for c in cap.get("validation_cases") or [] if isinstance(c, dict)
                             and c.get("id") == binding.get("validation_case_id")), {})
                questions.append({"id": qid, "sparql": str(query or ""), "answer_contract": {
                    "derived_predicates": binding.get("derived_predicates") or [],
                    "required_bindings": case.get("expected_fields") or [],
                    "nullable_bindings": binding.get("nullable_bindings") or {},
                    "reviewed_runtime_binding": {"query_name": name, "question_id": qid}}})
    issues = unreachable_cq_class_issues(questions, runtime, str(runtime.get("mapping_obda") or ""))
    issues += union_unbound(questions, runtime)
    for issue in issues:
        upstream = issue.pop("upstream_source", None) or {}
        issue.update(gate=issue["gate"].replace("G-S4-", "G-S3-"), path=upstream.get("path") or "realtime_runtime",
                     source_question_id=issue.pop("question_id", None))
    from services.realtime_qa.evidence_absence import evidence_absence_issues

    from .ontop_boolean_bind import nullable_boolean_bind_issues
    from .rule_premise_selectivity import tautological_rule_premise_issues

    return (issues + tautological_rule_premise_issues(runtime)
            + nullable_boolean_bind_issues(runtime) + evidence_absence_issues(runtime))


def project_unreachable_cq_class_issues(project_dir: Any, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read S3 runtime artifacts from a project directory; absent artifacts yield no issues."""
    import json
    from pathlib import Path

    runtime_dir = Path(project_dir) / "03-mapping-review/runtime"
    runtime_path, obda_path = runtime_dir / "runtime-source.json", runtime_dir / "mapping.obda"
    if not runtime_path.is_file() or not obda_path.is_file():
        return []
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    return (unreachable_cq_class_issues(questions, runtime, obda_path.read_text(encoding="utf-8"))
            + union_unbound(questions, runtime))
