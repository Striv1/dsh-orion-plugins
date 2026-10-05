"""Versioned business-first contracts; absent version always means legacy behavior."""
from __future__ import annotations

from collections import Counter
from typing import Any

from rdflib import RDF

VERSION = "business-first-v1"
ROLES = {"BUSINESS_OBJECT", "BUSINESS_RECORD", "DICTIONARY", "DERIVED_CLASSIFICATION", "ASSESSMENT_RESULT", "ABSTRACT", "INTERNAL_EVIDENCE"}
MODES = {"SOURCE_MAPPING", "DOCUMENT_FACTS", "DICTIONARY_ITEMS", "RULE_DERIVED", "SUBCLASS_MEMBERS", "INTERNAL"}
EMPTY = {"REQUIRE_NONEMPTY", "ALLOW_EMPTY", "NO_DIRECT_INSTANCES"}

def _references(value: Any, *, field: str, name: str, required: bool = False) -> list[str]:
    if value is None and not required:
        return []
    if (not isinstance(value, list) or (required and not value)
            or any(not isinstance(ref, str) or not ref.strip() for ref in value)):
        raise ValueError(f"{name}: {field} 必须是非空字符串组成的列表。")
    return value


def _mapping_index(mappings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result = {}
    for item in mappings:
        key = str(item.get("id") or "").strip()
        if not key or key in result:
            raise ValueError("映射 id 缺失或重复。")
        result[key] = item
    return result


def enabled(state: dict[str, Any]) -> bool:
    return state.get("business_modeling_contract_version") == VERSION

def validate_contract(item: dict[str, Any], *, executable: bool = False) -> dict[str, Any]:
    name = str(item.get("iri") or item.get("target") or item.get("name") or item.get("id"))
    contract = item.get("instance_contract")
    if not isinstance(contract, dict):
        raise ValueError(f"{name} 缺少 instance_contract：必须说明一个实例是什么及如何生成。")
    for key, allowed in (("business_role", ROLES), ("generation_mode", MODES), ("empty_policy", EMPTY)):
        if not isinstance(contract.get(key), str) or contract[key] not in allowed:
            raise ValueError(f"{name}: {key} 必须为 {sorted(allowed)} 之一。")
    for key in ("instance_meaning", "identity_rule", "empty_reason"):
        if not str(contract.get(key) or "").strip():
            raise ValueError(f"{name}: 缺少 {key}。")
    compatible = {
        "DICTIONARY": {"DICTIONARY_ITEMS"}, "ABSTRACT": {"SUBCLASS_MEMBERS"},
        "INTERNAL_EVIDENCE": {"INTERNAL"},
        "DERIVED_CLASSIFICATION": {"SOURCE_MAPPING", "DOCUMENT_FACTS", "RULE_DERIVED", "SUBCLASS_MEMBERS"},
        "BUSINESS_OBJECT": {"SOURCE_MAPPING", "DOCUMENT_FACTS"},
        "BUSINESS_RECORD": {"SOURCE_MAPPING", "DOCUMENT_FACTS"},
        "ASSESSMENT_RESULT": {"SOURCE_MAPPING", "DOCUMENT_FACTS", "RULE_DERIVED"},
    }
    if contract["generation_mode"] not in compatible[contract["business_role"]]:
        raise ValueError(f"{name}: business_role 与 generation_mode 不相容。")
    if contract["business_role"] == "INTERNAL_EVIDENCE" and contract.get("default_business_exploration") is not False:
        raise ValueError(f"{name}: 内部计算证据不能默认作为业务类展示。")
    if contract["empty_policy"] == "NO_DIRECT_INSTANCES" and contract["business_role"] not in {"ABSTRACT", "DERIVED_CLASSIFICATION"}:
        raise ValueError(f"{name}: 只有抽象类或派生分类可声明不要求直接成员。")
    if contract["generation_mode"] == "SUBCLASS_MEMBERS" and contract["empty_policy"] != "NO_DIRECT_INSTANCES":
        raise ValueError(f"{name}: SUBCLASS_MEMBERS 必须使用 NO_DIRECT_INSTANCES；子类成员需单独查询，不要求直接类型成员。")
    if executable or "mapping_refs" in contract:
        _references(contract.get("mapping_refs"), field="mapping_refs", name=name,
                    required=executable and contract["generation_mode"] not in {"SUBCLASS_MEMBERS", "INTERNAL"})
    return contract

def validate_candidates(candidates: list[dict[str, Any]]) -> None:
    for item in candidates:
        if str(item.get("kind", "")).upper() in {"CLASS", "OWL:CLASS", "CONCEPT", "ENTITY", "BUSINESS_OBJECT", "类", "实体"}:
            validate_contract(item)

def validate_mapping(mappings: list[dict[str, Any]]) -> None:
    ids = set(_mapping_index(mappings))
    for item in mappings:
        if str(item.get("mapping_type", "")).upper().endswith("TO_CLASS"):
            contract = validate_contract(item, executable=True)
            unknown = set(contract.get("mapping_refs") or []) - ids
            if unknown:
                raise ValueError(f"{item.get('id')}: mapping_refs 引用了不存在的映射 {sorted(unknown)}。")
            # Formal rule execution materializes result predicates as class
            # assertions. A premise-only RULE_TO_CLASS mapping is an input fact,
            # unless another mapping can explicitly produce the same class.
            derivation = item.get("derivation")
            if (item.get("mapping_type") == "RULE_TO_CLASS"
                    and isinstance(derivation, dict) and derivation.get("premise_predicate")
                    and contract["empty_policy"] == "REQUIRE_NONEMPTY"
                    and not any(
                        other is not item and other.get("target") == item.get("target")
                        and str(other.get("mapping_type", "")).upper().endswith("TO_CLASS")
                        and not (other.get("mapping_type") == "RULE_TO_CLASS"
                                 and isinstance(other.get("derivation"), dict)
                                 and other["derivation"].get("premise_predicate"))
                        for other in mappings
                    )):
                raise ValueError(
                    f"{item.get('id')}: 规则前提类 {item.get('target')} 没有独立显式实例生成路径，"
                    "不能要求 REQUIRE_NONEMPTY；若仅作为运行期前提，请声明 NO_DIRECT_INSTANCES，"
                    "并用实际规则输入、轨迹和结论验收事实。"
                )

def attach_contracts(classes: list[dict[str, Any]], mappings: list[dict[str, Any]]) -> None:
    by_id = _mapping_index(mappings)
    for cls in classes:
        refs = _references(cls.get("source_mapping_ids"), field="source_mapping_ids", name=str(cls.get("name")))
        unknown = set(refs) - by_id.keys()
        if unknown:
            raise ValueError(f"{cls.get('name')}: source_mapping_ids 引用了不存在的映射 {sorted(unknown)}。")
        contracts = [by_id[ref]["instance_contract"] for ref in refs if "instance_contract" in by_id[ref]]
        if contracts:
            if any(contract != contracts[0] for contract in contracts[1:]):
                raise ValueError(f"{cls.get('name')}: 同类来源映射的实例合同不一致。")
            cls["instance_contract"] = dict(contracts[0])

def validate_design(classes: list[dict[str, Any]], mappings: list[dict[str, Any]] | None = None) -> None:
    ids = set(_mapping_index(mappings)) if mappings is not None else None
    for cls in classes:
        value = validate_contract(cls, executable=True)
        refs = _references(cls.get("source_mapping_ids"), field="source_mapping_ids", name=str(cls.get("name")))
        if ids is not None:
            for field, references in (("mapping_refs", value.get("mapping_refs") or []), ("source_mapping_ids", refs)):
                if set(references) - ids:
                    raise ValueError(f"{cls.get('name')}: {field} 不属于 S3 已审映射。")

def validate_instances(classes: list[dict[str, Any]], graph: Any) -> dict[str, Any]:
    """One traversal of type assertions. Counts direct membership, never claims inference."""
    validate_design(classes)
    counts = Counter(str(obj) for _, _, obj in graph.triples((None, RDF.type, None)))
    rows = []
    for cls in classes:
        contract = cls["instance_contract"]
        count = counts[str(cls["iri"])]
        status = "FAILED" if not count and contract["empty_policy"] == "REQUIRE_NONEMPTY" else "PASSED"
        rows.append({"class_iri": cls["iri"], "label": cls.get("label_zh") or cls.get("name"), "direct_instance_count": count, "membership_scope": "EXPLICIT_TYPES_IN_S6_SNAPSHOT", "generation_binding_status": "DECLARED_REFERENCES_ONLY", "subclass_membership_status": "NOT_COMPUTED" if contract["generation_mode"] == "SUBCLASS_MEMBERS" else "NOT_REQUESTED", "status": status, "instance_contract": contract})
    return {"version": VERSION, "status": "FAILED" if any(row["status"] == "FAILED" for row in rows) else "PASSED", "classes": rows}
