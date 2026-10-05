"""Mechanically derive an S3 mapping skeleton from stored S1/S2 artefacts.

The skeleton is a review starting point, not an approved mapping: every entry
carries its derivation and stays SKELETON_PENDING_REVIEW until a reviewer
confirms it. Unresolvable parts become open_items instead of guessed values,
so no mapping silently invents a source column, join or datatype.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

SKELETON_CONTRACT_VERSION = "s3-mapping-skeleton-v1"
SNAPSHOT_TECHNICAL_COLUMNS = ("dataset_id", "row_ordinal", "row_sha256")
REVIEW_STATE = "SKELETON_PENDING_REVIEW"

_DATATYPE_BY_SQL_TYPE = {
    "bigint": "xsd:long",
    "integer": "xsd:int",
    "int": "xsd:int",
    "smallint": "xsd:int",
    "numeric": "xsd:decimal",
    "decimal": "xsd:decimal",
    "double precision": "xsd:double",
    "real": "xsd:double",
    "boolean": "xsd:boolean",
    "date": "xsd:date",
    "timestamp": "xsd:dateTime",
    "timestamp without time zone": "xsd:dateTime",
    "timestamp with time zone": "xsd:dateTime",
    "text": "xsd:string",
    "character varying": "xsd:string",
    "varchar": "xsd:string",
    "uuid": "xsd:string",
    "json": "xsd:string",
    "jsonb": "xsd:string",
}
_FALLBACK_DATATYPE = "xsd:string"
_DECLARED_TYPE_PATTERN_TEMPLATE = r"{column}[^〈（(]{{0,16}}[（(]\s*([A-Za-z][A-Za-z ]*?)\s*(?:可空|非空|[,，)）])"
_JOIN_PATTERN = re.compile(r"([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\s*(?:\u2192|->)\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)")


def datatype_for_sql_type(sql_type: str | None) -> str:
    key = str(sql_type or "").strip().lower()
    if key.startswith("timestamp"):
        return "xsd:dateTime"
    return _DATATYPE_BY_SQL_TYPE.get(key, _FALLBACK_DATATYPE)


def is_known_sql_type(sql_type: str | None) -> bool:
    key = str(sql_type or "").strip().lower()
    return key.startswith("timestamp") or key in _DATATYPE_BY_SQL_TYPE


def is_business_column(column: str | None) -> bool:
    return bool(str(column or "").strip()) and str(column).strip() not in SNAPSHOT_TECHNICAL_COLUMNS


def source_tables(candidate: dict[str, Any]) -> list[str]:
    """Structured binding first; table: refs stay a compatible fallback."""
    tables: list[str] = []
    binding = candidate.get("source_binding") or {}
    if isinstance(binding, dict):
        join = binding.get("join")
        for name in (binding.get("table"), join.get("from_table") if isinstance(join, dict) else None):
            value = str(name or "").strip()
            if value and value not in tables:
                tables.append(value)
    if tables:
        return tables
    for ref in candidate.get("source_refs") or []:
        value = str(ref or "").strip()
        if value.lower().startswith("table:"):
            name = value.split(":", 1)[1].strip()
            if name and name not in tables:
                tables.append(name)
    return tables


def binding_column(candidate: dict[str, Any]) -> str:
    binding = candidate.get("source_binding") or {}
    return str(binding.get("column") or "").strip() if isinstance(binding, dict) else ""


def binding_join(candidate: dict[str, Any]) -> tuple[str, str, str, str] | None:
    binding = candidate.get("source_binding") or {}
    join = binding.get("join") if isinstance(binding, dict) else None
    if not isinstance(join, dict):
        return None
    parts = tuple(str(join.get(key) or "").strip() for key in ("from_table", "from_column", "to_table", "to_column"))
    return parts if all(parts) else None  # type: ignore[return-value]


def declared_sql_type(candidate: dict[str, Any], column: str) -> str:
    """Structured binding first; fall back to the type S2 noted in prose."""
    binding = candidate.get("source_binding") or {}
    if isinstance(binding, dict):
        declared = str(binding.get("declared_sql_type") or "").strip().lower()
        if declared:
            return declared if is_known_sql_type(declared) else ""
    description = str(candidate.get("description") or "")
    match = re.search(_DECLARED_TYPE_PATTERN_TEMPLATE.format(column=re.escape(column)), description)
    value = (match.group(1).strip().lower() if match else "")
    return value if is_known_sql_type(value) else ""


_CJK = re.compile(r"[\u4e00-\u9fff]")


def _label_zh(candidate: dict[str, Any], target: str) -> str:
    """Prefer an explicit Chinese label; an English candidate name is not a business label."""
    for key in ("label_zh", "name_zh", "business_name_zh", "name"):
        value = str(candidate.get(key) or "").strip()
        if value and _CJK.search(value):
            return value
    return ""


def _ascii_token(value: str | None) -> str:
    return re.sub(r"[^0-9A-Za-z]+", " ", str(value or "")).strip()


def _pascal(value: str | None) -> str:
    return "".join(part[:1].upper() + part[1:] for part in _ascii_token(value).split(" ") if part)


def _camel(value: str | None) -> str:
    token = _pascal(value)
    return token[:1].lower() + token[1:] if token else ""


def _class_target(table: dict[str, Any]) -> str:
    return _pascal(_table_name(table))


def _table_name(table: dict[str, Any]) -> str:
    """The lookup and every downstream reference must use the same S1 table key."""
    return str(table.get("source_table") or table.get("name") or "")


def _source_name(table: dict[str, Any], column: str = "") -> str:
    return ".".join(str(value) for value in (table.get("source_id"), _table_name(table), column) if value)


def _candidate_target(candidate: dict[str, Any], fallback: str) -> str:
    name = str(candidate.get("name") or "").strip()
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) else fallback


def _property_target(prefix: str, ordinal: int, column: str | None) -> str:
    token = _camel(column)
    return token or f"{prefix}{ordinal:02d}"


def _unique(target: str, used: dict[str, str], owner: str, fallback_prefix: str) -> str:
    if used.get(target) in (None, owner):
        used[target] = owner
        return target
    qualified = _camel(fallback_prefix) + _pascal(target)
    if used.get(qualified) in (None, owner):
        used[qualified] = owner
        return qualified
    index = 2
    while f"{qualified}{index:02d}" in used:
        index += 1
    final = f"{qualified}{index:02d}"
    used[final] = owner
    return final


def _business_columns(table: dict[str, Any]) -> list[str]:
    return [column for column in table.get("columns") or [] if is_business_column(column)]


def _column_from_description(candidate: dict[str, Any], table: dict[str, Any]) -> str:
    """Only accept an unambiguous single column match; otherwise return ""."""
    description = str(candidate.get("description") or "")
    physical = _table_name(table)
    columns = _business_columns(table)
    qualified = [column for column in columns if f"{physical}.{column}" in description]
    if len(qualified) == 1:
        return qualified[0]
    if qualified:
        return ""
    bare = [column for column in columns if re.search(rf"(?<![\w.]){re.escape(column)}(?![\w])", description)]
    return bare[0] if len(bare) == 1 else ""


def _join_columns(candidate: dict[str, Any]) -> tuple[str, str, str, str] | None:
    match = _JOIN_PATTERN.search(str(candidate.get("description") or ""))
    return (match.group(1), match.group(2), match.group(3), match.group(4)) if match else None


def _derivation(candidate: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "contract_version": SKELETON_CONTRACT_VERSION,
        "from_candidate": candidate.get("id"),
        "review_state": REVIEW_STATE,
        **{key: value for key, value in extra.items() if value},
    }


def _refs(candidate: dict[str, Any], table: dict[str, Any] | None, extra_tables: Iterable[str] = ()) -> list[str]:
    refs: list[str] = []
    for name in ([_table_name(table)] if table else []) + list(extra_tables):
        ref = f"table:{name}"
        if name and ref not in refs:
            refs.append(ref)
    for ref in candidate.get("source_refs") or []:
        value = str(ref or "").strip()
        if value and value not in refs:
            refs.append(value)
    candidate_id = str(candidate.get("id") or "").strip()
    if candidate_id and candidate_id not in refs:
        refs.append(candidate_id)
    return refs


def build_mapping_skeleton(*, namespace: str, schema_snapshot: dict[str, Any], candidates: Iterable[dict[str, Any]]) -> dict[str, Any]:
    tables = list(schema_snapshot.get("tables") or [])
    by_table = {_table_name(table): table for table in tables}
    items = [item for item in candidates if isinstance(item, dict)]
    open_items: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    class_target_by_table: dict[str, str] = {}
    ambiguous_class_tables: set[str] = set()
    used_class_targets: dict[str, str] = {}
    used_property_targets: dict[str, str] = {}

    def add_open_item(kind: str, candidate: dict[str, Any], detail: str, **extra: Any) -> None:
        open_items.append({
            "kind": kind,
            "candidate_id": candidate.get("id"),
            "candidate_name": candidate.get("name"),
            "detail": detail,
            **extra,
        })

    for ordinal, candidate in enumerate((item for item in items if item.get("kind") == "CLASS"), start=1):
        contract = dict(candidate.get("instance_contract") or {})
        generation_mode = contract.get("generation_mode")
        if generation_mode and generation_mode != "SOURCE_MAPPING":
            add_open_item("CLASS_GENERATION_REQUIRES_BINDING", candidate,
                          "候选实例合同不允许直接整表生成；source_refs 是业务依据，不是成员生成条件。"
                          "规则派生类需绑定已审 S2 规则及其推理能力，其他模式需对应的生成合同。",
                          generation_mode=generation_mode,
                          target=_candidate_target(candidate, ""),
                          required_mapping_type="RULE_TO_CLASS" if generation_mode == "RULE_DERIVED" else None)
            continue
        names = source_tables(candidate)
        table = next((by_table[name] for name in names if name in by_table), None)
        if table is None:
            add_open_item("CLASS_WITHOUT_SNAPSHOT_TABLE", candidate,
                          "候选类的 source_refs 没有指向快照中的表，需人工指定来源表或改为文档映射。",
                          referenced_tables=names)
            continue
        target = _candidate_target(candidate, _class_target(table))
        if not target:
            add_open_item("CLASS_TARGET_UNRESOLVED", candidate,
                          "无法从表名生成 ASCII 目标类名，需人工指定 target。",
                          source_table=_table_name(table))
            continue
        target = _unique(target, used_class_targets, f"class:{candidate.get('id')}", "Business")
        if not contract:
            add_open_item("CLASS_WITHOUT_INSTANCE_CONTRACT", candidate,
                          "S2 候选未带实例合同，business-first 合同要求人工补齐后才能提交。",
                          source_table=_table_name(table))
        mapping_id = f"MAP-TC-{ordinal:02d}"
        row_count = table.get("row_count")
        mapping = {
            "id": mapping_id,
            "mapping_type": "TABLE_TO_CLASS",
            "target": target,
            "target_label_zh": _label_zh(candidate, target) or target,
            "target_comment_zh": candidate.get("description") or "",
            "source": _source_name(table) + (f"（{row_count} 行）" if row_count is not None else ""),
            "source_refs": _refs(candidate, table),
            "instance_contract": contract,
            "derivation": _derivation(candidate, from_snapshot_table=_table_name(table), source_id=table.get("source_id")),
        }
        mappings.append(mapping)
        name = _table_name(table)
        if name in class_target_by_table or name in ambiguous_class_tables:
            class_target_by_table.pop(name, None)
            ambiguous_class_tables.add(name)
            add_open_item("CLASS_TABLE_DOMAIN_AMBIGUOUS", candidate,
                          "同一来源表对应多个直接来源类，属性与关系的 domain/range 需明确绑定，不能按候选顺序覆盖。",
                          source_table=name)
        else:
            class_target_by_table[name] = target

    for ordinal, candidate in enumerate((item for item in items if item.get("kind") == "DATA_PROPERTY"), start=1):
        names = source_tables(candidate)
        table = next((by_table[name] for name in names if name in by_table), None)
        if table is None:
            add_open_item("DATA_PROPERTY_WITHOUT_SNAPSHOT_TABLE", candidate,
                          "候选数据属性没有指向快照中的表，需人工指定来源表。", referenced_tables=names)
            continue
        declared_binding = binding_column(candidate)
        if declared_binding and not is_business_column(declared_binding):
            add_open_item("BINDING_COLUMN_IS_TECHNICAL", candidate,
                          f"结构化绑定指向技术列 {declared_binding}，快照技术列没有业务含义，需改绑业务列。",
                          source_table=_table_name(table), source_column=declared_binding)
            continue
        if declared_binding and declared_binding not in _business_columns(table):
            add_open_item("BINDING_COLUMN_NOT_IN_SNAPSHOT", candidate,
                          f"结构化绑定的列 {declared_binding} 不在授权快照表中，需核对来源范围。",
                          source_table=_table_name(table), source_column=declared_binding,
                          available_columns=_business_columns(table))
            continue
        column = declared_binding or _column_from_description(candidate, table)
        column_source = "SOURCE_BINDING" if declared_binding else "DESCRIPTION_FALLBACK"
        if not column:
            add_open_item("DATA_PROPERTY_COLUMN_UNRESOLVED", candidate,
                          "候选未填 source_binding.column，描述里也没有唯一可识别的来源列；"
                          "请在 S2 补结构化绑定或人工指定列名。",
                          source_table=_table_name(table),
                          available_columns=_business_columns(table))
            continue
        if not declared_binding:
            add_open_item("COLUMN_INFERRED_FROM_DESCRIPTION", candidate,
                          f"列名 {column} 由中文描述推断，未经结构化绑定确认，需人工核对。",
                          source_table=_table_name(table), source_column=column)
        domain = class_target_by_table.get(_table_name(table))
        sql_type = (table.get("column_types") or {}).get(column)
        declared = declared_sql_type(candidate, column)
        snapshot_datatype = datatype_for_sql_type(sql_type)
        if not is_known_sql_type(sql_type):
            add_open_item("DATATYPE_UNRESOLVED", candidate,
                          f"S1 来源列 SQL 类型 {sql_type!r} 缺失或不受支持，不能默认映射为字符串。"
                          "请补齐受控来源的实际列类型；S2 声明仅供核对，不能替代物理类型证据。",
                          source_table=_table_name(table), source_column=column,
                          declared_sql_type=declared or None)
            continue
        elif declared and datatype_for_sql_type(declared) != snapshot_datatype:
            add_open_item("DATATYPE_SNAPSHOT_WIDENED", candidate,
                          f"S2 记录来源列为 {declared}（对应 {datatype_for_sql_type(declared)}），但快照物理列是 {sql_type}；"
                          f"骨架按快照填 {snapshot_datatype} 以保证 OBDA 与真实列一致，数值比较需在规则或查询里显式转换。",
                          source_table=_table_name(table), source_column=column,
                          declared_sql_type=declared, snapshot_sql_type=sql_type,
                          declared_datatype=datatype_for_sql_type(declared),
                          skeleton_datatype=snapshot_datatype)
        owner = f"data:{candidate.get('id')}"
        target = _unique(_candidate_target(candidate, _property_target("dataProperty", ordinal, column)),
                         used_property_targets, owner, domain or "")
        mapping = {
            "id": f"MAP-DP-{ordinal:02d}",
            "mapping_type": "COLUMN_TO_DATA_PROPERTY",
            "target": target,
            "target_label_zh": _label_zh(candidate, target) or target,
            "target_comment_zh": candidate.get("description") or "",
            "datatype": datatype_for_sql_type(sql_type),
            "source": _source_name(table, column),
            "source_refs": _refs(candidate, table),
            "derivation": _derivation(candidate, from_snapshot_table=_table_name(table),
                                      from_snapshot_column=column, binding_source=column_source,
                                      source_id=table.get("source_id")),
        }
        if domain:
            mapping["domain"] = domain
        else:
            add_open_item("DATA_PROPERTY_WITHOUT_SNAPSHOT_TABLE", candidate,
                          "来源表没有唯一的类映射，需先明确类绑定再确定 domain。",
                          source_table=_table_name(table))
        mappings.append(mapping)

    for ordinal, candidate in enumerate((item for item in items if item.get("kind") == "OBJECT_PROPERTY"), start=1):
        structured_join = binding_join(candidate)
        binding = candidate.get("source_binding")
        has_declared_join = isinstance(binding, dict) and "join" in binding
        join = structured_join if has_declared_join else _join_columns(candidate)
        join_source = "SOURCE_BINDING" if structured_join else "DESCRIPTION_FALLBACK"
        if join is None:
            add_open_item("OBJECT_PROPERTY_JOIN_UNRESOLVED", candidate,
                          "候选未填 source_binding.join，描述里也没有可解析的关联式；"
                          "多对多关系请指向桥接表并分别登记两段关系。",
                          referenced_tables=source_tables(candidate))
            continue
        if not structured_join:
            add_open_item("JOIN_INFERRED_FROM_DESCRIPTION", candidate,
                          "关联列由中文描述推断，未经结构化绑定确认，需人工核对方向与基数。",
                          join=f"{join[0]}.{join[1]} \u2192 {join[2]}.{join[3]}")
        left_table, left_column, right_table, right_column = join
        domain = class_target_by_table.get(left_table)
        target_range = class_target_by_table.get(right_table)
        if not domain or not target_range:
            add_open_item("OBJECT_PROPERTY_ENDPOINTS_UNRESOLVED", candidate,
                          "关联两端至少一侧没有类映射，需先补类映射再确定 domain/range。",
                          join=f"{left_table}.{left_column} \u2192 {right_table}.{right_column}",
                          resolved_domain=domain, resolved_range=target_range)
            continue
        invalid_columns = [f"{name}.{column}" for name, column in
                           ((left_table, left_column), (right_table, right_column))
                           if column not in _business_columns(by_table[name])]
        if invalid_columns:
            add_open_item("OBJECT_PROPERTY_COLUMN_NOT_IN_SNAPSHOT", candidate,
                          "关联必须使用两端授权来源表中已登记的业务列，不能绑定技术列或缺失列。",
                          invalid_columns=invalid_columns)
            continue
        owner = f"object:{candidate.get('id')}"
        target = _unique(_candidate_target(candidate, f"has{target_range}"), used_property_targets, owner, domain)
        mappings.append({
            "id": f"MAP-OP-{ordinal:02d}",
            "mapping_type": "CANDIDATE_JOIN_TO_OBJECT_PROPERTY",
            "target": target,
            "target_label_zh": _label_zh(candidate, target) or target,
            "target_comment_zh": candidate.get("description") or "",
            "domain": domain,
            "range": target_range,
            "source": f"{left_table}.{left_column} = {right_table}.{right_column}",
            "source_refs": _refs(candidate, by_table.get(left_table), [right_table]),
            "derivation": _derivation(candidate, from_snapshot_table=left_table,
                                      from_snapshot_column=left_column,
                                      join_target=f"{right_table}.{right_column}",
                                      binding_source=join_source,
                                      source_id=by_table[left_table].get("source_id")),
        })

    for mapping in mappings:
        if mapping["mapping_type"] != "TABLE_TO_CLASS":
            continue
        contract = mapping.get("instance_contract")
        if not isinstance(contract, dict) or not contract:
            continue
        refs = [mapping["id"]] + [
            other["id"] for other in mappings
            if other is not mapping and other.get("domain") == mapping["target"]
        ]
        contract["mapping_refs"] = refs

    for mapping in mappings:
        if not _CJK.search(str(mapping.get("target_label_zh") or "")):
            open_items.append({
                "kind": "LABEL_ZH_MISSING",
                "candidate_id": (mapping.get("derivation") or {}).get("from_candidate"),
                "candidate_name": mapping.get("target"),
                "mapping_id": mapping["id"],
                "detail": "S2 候选只有英文 name，没有中文业务名称；提交 S3 前必须在 target_label_zh 填写业务人员能理解的中文名称（G-S3-CHINESE）。",
            })

    mapped_tables = class_target_by_table.keys() | ambiguous_class_tables
    unmapped = [name for name in by_table if name not in mapped_tables]
    for name in unmapped:
        open_items.append({
            "kind": "SNAPSHOT_TABLE_NOT_MAPPED",
            "candidate_id": None,
            "candidate_name": None,
            "detail": "快照表没有任何类映射，需确认是被有意排除还是漏建类。",
            "source_table": name,
        })

    coverage = {
        "snapshot_set_id": schema_snapshot.get("snapshot_set_id"),
        "snapshot_tables": len(by_table),
        "tables_with_class_mapping": len(mapped_tables),
        "tables_without_class_mapping": sorted(unmapped),
        "class_mappings": sum(1 for item in mappings if item["mapping_type"] == "TABLE_TO_CLASS"),
        "data_property_mappings": sum(1 for item in mappings if item["mapping_type"] == "COLUMN_TO_DATA_PROPERTY"),
        "object_property_mappings": sum(1 for item in mappings if item["mapping_type"] == "CANDIDATE_JOIN_TO_OBJECT_PROPERTY"),
        "candidates_seen": len(items),
        "open_item_count": len(open_items),
        "label_zh_missing": sum(1 for item in open_items if item["kind"] == "LABEL_ZH_MISSING"),
    }
    review_note = (
        "这是由平台从 S1 快照与 S2 候选机械生成的 S3 映射骨架，状态为待评审："
        "每条映射都带 derivation，需逐条复核目标命名、中文业务定义、datatype 与关系方向；"
        f"未解决项 {len(open_items)} 条必须先处理或明确排除，处理完再提交映射预检。"
    )
    return {
        "contract_version": SKELETON_CONTRACT_VERSION,
        "mapping_draft": {"namespace": namespace, "mappings": mappings},
        "open_items": open_items,
        "coverage": coverage,
        "review_note": review_note,
    }
