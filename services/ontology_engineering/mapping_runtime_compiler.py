"""Compile deterministic S3 runtime assets; unresolved semantics remain review work.

Pure compilation: no database access, workflow writes, approvals or invented CQ
expectations. Snapshot bindings are authoritative; arbitrary SQL is never copied.
"""
from __future__ import annotations

import copy
import hashlib
import re
from typing import Any

from services.ontology_contracts.errors import WorkflowError
from services.ontology_engineering.business_source_contract import (
    source_capability,
    source_dependency,
)
from services.ontology_engineering.mapping_preflight import collect_mapping_runtime_issues
from services.ontology_engineering.mapping_runtime_coverage import runtime_uncovered_targets
from services.ontology_engineering.mapping_skeleton import is_business_column
from services.realtime_qa.runtime_release import (
    RuntimeReleaseError,
    _validate_obda,
    normalize_runtime_submission,
)
from services.structured_data.execution_source import dataset_column, execution_schema

CONTRACT_VERSION = "s3-runtime-compiler-v4"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOCAL = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_CASTS = {
    "string": "text", "int": "integer", "integer": "numeric", "long": "bigint",
    "decimal": "numeric", "double": "double precision", "float": "real",
    "boolean": "boolean", "date": "date", "dateTime": "timestamp with time zone",
}
_FILTER_OPS = {"EQ": "=", "NE": "<>", "GT": ">", "GE": ">=", "LT": "<", "LE": "<="}


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("来源列或物理表标识不受支持，需核对快照绑定")
    return '"' + value + '"'


def _table_sql(table: dict) -> str:
    name = str(table.get("name") or "")
    dataset_column(table)
    return "orion_data." + _identifier(name)


def _identity(mapping: dict, table: dict) -> list[str]:
    contract = mapping.get("instance_contract") or {}
    # Only the leading identity clause counts. Later prose often names a
    # secondary verification column, which must not change entity identity.
    clause = re.split(r"[；;。\n]", str(contract.get("identity_rule") or ""), maxsplit=1)[0]
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*\+\s*[A-Za-z_][A-Za-z0-9_]*)*)\s*(?:唯一|组合标识|组合键|联合主键)\s*", clause)
    declared = (mapping.get("derivation") or {}).get("identity_columns")
    columns = declared if declared is not None else ([x.strip() for x in match[1].split("+")] if match else [])
    if not isinstance(columns, list) or any(not isinstance(c, str) for c in columns):
        raise ValueError("derivation.identity_columns 必须为业务列名数组")
    if not columns or len(set(columns)) != len(columns) or any(
        c not in table.get("columns", []) or not is_business_column(c) for c in columns
    ):
        raise ValueError("无法确定业务实例标识；请补 derivation.identity_columns，或在 identity_rule 首句明确“列名 唯一”或“列1 + 列2 组合标识”，不能自动退化为行号")
    return columns


def _entity(target: str, columns: list[str]) -> str:
    # Slash-separated, URI-encoded template components avoid composite-key
    # collisions such as (a-b,c) and (a,b-c).
    return ":" + target + "/" + "/".join("{" + c + "}" for c in columns)


def _query_name(target: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", target).lower().replace("-", "_")
    return "list_" + snake[:45] + "_" + hashlib.sha256(target.encode()).hexdigest()[:8]


def _literal(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float) and not isinstance(value, bool):
        if not re.fullmatch(r"-?\d+(?:\.\d+)?", str(value)):
            raise ValueError("过滤值不是有限数值")
        return str(value)
    if isinstance(value, str) and len(value) <= 512:
        return "'" + value.replace("'", "''") + "'"
    raise ValueError("过滤值只支持有界字符串、布尔值或有限数值")


def _filtered_class_sql(mapping: dict, base: dict, ids: list[str], tables: list[dict]) -> tuple[str, list[dict]]:
    """Compile a bounded snapshot-only SELECT for one derived class membership."""
    derivation = mapping.get("derivation") or {}
    aliases = {str(base["source_table"]): ("t0", base)}
    joins = derivation.get("joins") or []
    if not isinstance(joins, list) or len(joins) > 3:
        raise ValueError("derivation.joins 最多支持 3 条显式快照关联")
    join_sql = []
    used_columns = {str(base["source_table"]): set(ids)}
    for index, join in enumerate(joins, 1):
        if not isinstance(join, dict):
            raise ValueError("derivation.joins 条目必须为对象")
        name = str(join.get("source_table") or "")
        source_id = str(join.get("source_id") or "")
        matching = [t for t in tables if t.get("source_table") == name
                    and (not source_id or t.get("source_id") == source_id)]
        if name in aliases or len(matching) != 1:
            raise ValueError("关联表重复、未登记或存在同名来源歧义；请指定 source_id")
        right = matching[0]
        _table_sql(right)
        left_name = str(join.get("left_table") or base["source_table"])
        if left_name not in aliases:
            raise ValueError("关联左表必须是基础表或前序已关联表")
        left_alias, left = aliases[left_name]
        left_col, right_col = join.get("left_column"), join.get("right_column")
        if (left_col not in left.get("columns", []) or right_col not in right.get("columns", [])
                or not is_business_column(left_col) or not is_business_column(right_col)):
            raise ValueError("关联列须为两侧已登记快照的业务列")
        alias = f"t{index}"
        join_sql.append(f"JOIN {_table_sql(right)} {alias} ON {left_alias}.{_identifier(left_col)}={alias}.{_identifier(right_col)}"
                        f" AND {alias}.{dataset_column(right)}={_literal(right['dataset_id'])}")
        aliases[name] = (alias, right)
        used_columns[left_name].add(left_col)
        used_columns[name] = {right_col}

    def column(table_name: Any, column_name: Any) -> str:
        name = str(table_name or base["source_table"])
        if name not in aliases:
            raise ValueError("过滤字段必须来自基础表或显式关联表")
        alias, table = aliases[name]
        if column_name not in table.get("columns", []) or not is_business_column(column_name):
            raise ValueError("过滤字段不是已登记快照业务列")
        used_columns[name].add(column_name)
        return f"{alias}.{_identifier(column_name)}"

    filter_nodes = 0

    def predicate(node: Any, depth: int = 0) -> str:
        nonlocal filter_nodes
        filter_nodes += 1
        if filter_nodes > 64:
            raise ValueError("derivation.filter 条件节点不能超过 64 个")
        if not isinstance(node, dict) or depth > 4:
            raise ValueError("derivation.filter 必须是深度不超过 4 的结构化条件")
        combinators = [name for name in ("all", "any") if name in node]
        if combinators:
            if len(combinators) != 1 or len(node) != 1:
                raise ValueError("过滤组只能选择 all 或 any")
            children = node[combinators[0]]
            if not isinstance(children, list) or not 1 <= len(children) <= 12:
                raise ValueError("过滤组必须包含 1 到 12 个条件")
            separator = " AND " if combinators[0] == "all" else " OR "
            return "(" + separator.join(predicate(child, depth + 1) for child in children) + ")"
        if set(node) - {"table", "column", "op", "value", "other_table", "other_column", "datatype"}:
            raise ValueError("过滤条件含未支持字段")
        left = column(node.get("table"), node.get("column"))
        datatype = node.get("datatype")
        if datatype is not None:
            if datatype != "decimal":
                raise ValueError("过滤数值转换当前只支持显式 decimal")
            left = f"CAST(NULLIF(CAST({left} AS text), '') AS numeric)"
        op = str(node.get("op") or "").upper()
        if op in {"IS_NULL", "IS_NOT_NULL"}:
            if "value" in node or "other_column" in node:
                raise ValueError("空值过滤不能再提供比较值")
            return f"{left} IS {'NOT ' if op == 'IS_NOT_NULL' else ''}NULL"
        if op == "IN":
            values = node.get("value")
            if not isinstance(values, list) or not 1 <= len(values) <= 20 or "other_column" in node:
                raise ValueError("IN 条件必须提供 1 到 20 个常量")
            return f"{left} IN ({', '.join(_literal(value) for value in values)})"
        if op not in _FILTER_OPS or ("value" in node) == ("other_column" in node):
            raise ValueError("过滤比较必须指定受支持运算符及恰好一个常量或快照字段")
        right = (column(node.get("other_table"), node["other_column"])
                 if "other_column" in node else _literal(node["value"]))
        if datatype is not None and "other_column" in node:
            right = f"CAST(NULLIF(CAST({right} AS text), '') AS numeric)"
        return f"{left} {_FILTER_OPS[op]} {right}"

    if "filter" not in derivation:
        raise ValueError("SQL_TO_CLASS 必须提供结构化 derivation.filter；不能直接执行 source SQL")
    where = [f"t0.{dataset_column(base)}={_literal(base['dataset_id'])}"]
    where.extend(f"t0.{_identifier(c)} IS NOT NULL" for c in ids)
    where.append(predicate(derivation["filter"]))
    selected = ", ".join(f"t0.{_identifier(c)} AS subject_key_{i}" for i, c in enumerate(ids))
    sql = (f"SELECT DISTINCT {selected} FROM {_table_sql(base)} t0 "
           + " ".join(join_sql) + " WHERE " + " AND ".join(where))
    return sql, [source_dependency(table, used_columns[name]) for name, (_, table) in aliases.items()]


def compile_mapping_runtime(mapping_draft: dict, schema_snapshot: dict, *, project_id: str,
                            intake_mode: str, rule_candidates: list[dict],
                            cq_questions: list[dict], ontology_candidates: list[dict] | None = None,
                            datasource_inventory: dict | None = None) -> dict[str, Any]:
    if intake_mode not in {"HYBRID", "DATABASE_ONLY"}:
        raise WorkflowError("当前编译器用于数据库或混合工程；纯资料工程应使用文档事实与证据查询合同。")
    namespace = str(mapping_draft.get("namespace") or "")
    if not re.fullmatch(r"(?:https?://|urn:)[^\s<>\"{}\\]+[#/:]", namespace):
        raise WorkflowError("mapping_draft.namespace 必须是以 #、/ 或 : 结尾的绝对 IRI。")
    mappings = mapping_draft.get("mappings")
    if not isinstance(mappings, list) or not mappings or any(not isinstance(m, dict) for m in mappings):
        raise WorkflowError("当前 S3 草稿缺少有效 mappings。")
    schema_snapshot = execution_schema(schema_snapshot, datasource_inventory, project_id=project_id)
    tables = schema_snapshot.get("tables") or []
    open_items: list[dict] = []
    blocks: list[str] = []
    compiled: list[str] = []
    compiled_blocks: list[dict] = []
    mapping_sources: dict[str, list[dict]] = {}
    queries: dict[str, str] = {}
    capabilities: dict[str, dict] = {}
    classes: dict[str, tuple[dict, dict, list[str]]] = {}

    def issue(mapping: dict, message: str, kind: str = "MAPPING_REVIEW_REQUIRED") -> None:
        open_items.append({"kind": kind, "mapping_id": mapping.get("id"), "detail": message})

    def table_for(mapping: dict) -> dict:
        derivation = mapping.get("derivation") or {}
        name = derivation.get("from_snapshot_table")
        candidates = [t for t in tables if name in {t.get("source_table"), t.get("name")}]
        source_id = derivation.get("source_id")
        if source_id:
            candidates = [t for t in candidates if t.get("source_id") == source_id]
        if len(candidates) != 1:
            raise ValueError("来源表缺失或存在同名表歧义；请核对 derivation.from_snapshot_table 和 source_id。S1 登记身份："
                             + "; ".join(f"{t.get('source_id')} / {t.get('source_table')}" for t in tables[:8]))
        _table_sql(candidates[0])
        return candidates[0]

    def add(mapping: dict, target: str, sql: str, dependencies: list[dict]) -> None:
        blocks.append(f"mappingId generated_{len(blocks) + 1}\ntarget {target} .\nsource {sql}")
        compiled.append(str(mapping.get("id") or ""))
        mapping_sources[compiled[-1]] = dependencies
        compiled_blocks.append({"mapping_id": str(mapping.get("id") or ""),
                                "target": mapping.get("target"), "block": blocks[-1]})

    invalid_targets = {str(m.get("target") or "") for m in mappings
                       if not _LOCAL.fullmatch(str(m.get("target") or ""))}
    for m in mappings:
        if m.get("mapping_type") != "TABLE_TO_CLASS":
            continue
        try:
            target = m.get("target")
            if target in invalid_targets or sum(x.get("target") == target for x in mappings) != 1:
                raise ValueError("类目标名不受支持或重复，需消除歧义")
            table = table_for(m)
            ids = _identity(m, table)
            classes[target] = (m, table, ids)
            selected = ", ".join(_identifier(c) for c in ids)
            where = f"{dataset_column(table)}='{table['dataset_id']}'" + "".join(f" AND {_identifier(c)} IS NOT NULL" for c in ids)
            add(m, f"{_entity(target, ids)} a :{target}", f"SELECT {selected} FROM {_table_sql(table)} WHERE {where}",
                [source_dependency(table, ids)])
            name = _query_name(target)
            queries[name] = f"PREFIX : <{namespace}> SELECT ?entity WHERE {{ ?entity a :{target} . }} ORDER BY ?entity LIMIT 100"
            question = f"列出{m.get('target_label_zh') or target}实例"
            capabilities[name] = {
                "description_zh": question, "parameters": {}, "result_fields": ["entity"],
                "question_examples": [question], "query_mode": "SNAPSHOT_ONLY",
                **source_capability(mapping_sources[m["id"]]),
                "validation_cases": [{"id": "list_instances", "question": question,
                                      "parameters": {}, "expected_fields": ["entity"],
                                      "min_rows": 1 if (m.get("instance_contract") or {}).get("empty_policy") == "REQUIRE_NONEMPTY" else 0,
                                      "expected_first_row": {}}],
            }
        except (ValueError, KeyError) as exc:
            issue(m, str(exc))

    for m in mappings:
        if m.get("mapping_type") != "SQL_TO_CLASS":
            continue
        try:
            target = str(m.get("target") or "")
            if target in invalid_targets or sum(x.get("target") == target for x in mappings) != 1:
                raise ValueError("派生类目标名不受支持或重复，需消除歧义")
            derivation = m.get("derivation") or {}
            subject_class = str(derivation.get("subject_class") or "")
            if subject_class not in classes:
                raise ValueError("SQL_TO_CLASS 必须引用已编译 TABLE_TO_CLASS 的 derivation.subject_class，复用同一业务实例 IRI")
            _, base, base_ids = classes[subject_class]
            if table_for(m) != base or _identity(m, base) != base_ids:
                raise ValueError("派生类来源表和业务实例标识必须与 subject_class 基础类完全一致")
            sql, dependencies = _filtered_class_sql(m, base, base_ids, tables)
            aliases = [f"subject_key_{i}" for i in range(len(base_ids))]
            add(m, f"{_entity(subject_class, aliases)} a :{target}", sql, dependencies)
            name = _query_name(target)
            queries[name] = f"PREFIX : <{namespace}> SELECT ?entity WHERE {{ ?entity a :{target} . }} ORDER BY ?entity LIMIT 100"
            question = f"列出{m.get('target_label_zh') or target}实例"
            capabilities[name] = {
                "description_zh": question, "parameters": {}, "result_fields": ["entity"],
                "question_examples": [question], "query_mode": "SNAPSHOT_ONLY",
                **source_capability(dependencies),
                "validation_cases": [{"id": "list_instances", "question": question,
                                      "parameters": {}, "expected_fields": ["entity"],
                                      "min_rows": 1 if (m.get("instance_contract") or {}).get("empty_policy") == "REQUIRE_NONEMPTY" else 0,
                                      "expected_first_row": {}}],
            }
        except (ValueError, KeyError) as exc:
            issue(m, str(exc))

    for m in mappings:
        kind = str(m.get("mapping_type") or "")
        if kind in {"TABLE_TO_CLASS", "SQL_TO_CLASS", "RULE_TO_CLASS"} or kind.startswith("EVIDENCE_TO_"):
            continue
        try:
            target = m.get("target")
            if target in invalid_targets:
                raise ValueError("目标名不是受支持的本地名称，需明确目标 IRI")
            if kind == "COLUMN_VALUE_TO_CLASS":
                raise ValueError(
                    "COLUMN_VALUE_TO_CLASS 目前不能由平台静态编译为 OBDA；需针对已登记快照评审并补齐可执行的类实例映射。"
                    "类实例 IRI 必须与业务主键及关联映射一致；不能把设备编码映射成设备型号实例，"
                    "也不能把 source 中的任意 SQL 直接复制进运行时。"
                )
            if kind not in {"COLUMN_TO_DATA_PROPERTY", "CANDIDATE_JOIN_TO_OBJECT_PROPERTY"}:
                raise ValueError("计算列、桥接表或数组展开需评审专门的 SQL 与快照类型；不会复制任意 source SQL")
            if m.get("domain") not in classes:
                raise ValueError("domain 缺少已编译且标识明确的类映射")
            _, table, ids = classes[m["domain"]]
            if table_for(m) != table:
                raise ValueError("属性来源与 domain 的快照不一致")
            col = (m.get("derivation") or {}).get("from_snapshot_column")
            if col not in table.get("columns", []) or not is_business_column(col):
                raise ValueError("derivation.from_snapshot_column 缺失或不是当前快照业务列；请读取映射合同后显式绑定真实列，不能仅写在 source 描述中")
            qcol = _identifier(col)
            if kind == "COLUMN_TO_DATA_PROPERTY":
                datatype = str(m.get("datatype") or "xsd:string")
                dtype = datatype.removeprefix("xsd:").removeprefix("http://www.w3.org/2001/XMLSchema#")
                if dtype not in _CASTS:
                    raise ValueError("数据类型缺少显式 SQL 转换规则")
                # Separate property blocks preserve identity even when a key is
                # also a numeric property. Bad values fail visibly at runtime.
                expression = f"CAST({qcol} AS text)" if dtype == "string" else f"CAST(NULLIF(CAST({qcol} AS text), '') AS {_CASTS[dtype]})"
                aliases = [f"subject_key_{i}" for i in range(len(ids))]
                selected = ", ".join(f"{_identifier(c)} AS {a}" for c, a in zip(ids, aliases, strict=True))
                where = f"{dataset_column(table)}='{table['dataset_id']}'" + "".join(f" AND {_identifier(c)} IS NOT NULL" for c in ids)
                sql = f"SELECT {selected}, {expression} AS compiled_value FROM {_table_sql(table)} WHERE {where}"
                add(m, f"{_entity(m['domain'], aliases)} :{target} {{compiled_value}}^^xsd:{dtype}", sql,
                    [source_dependency(table, [*ids, col])])
            else:
                if m.get("range") not in classes:
                    raise ValueError("range 缺少已编译的类映射")
                _, right, right_ids = classes[m["range"]]
                join = str((m.get("derivation") or {}).get("join_target") or "")
                if len(right_ids) != 1 or join != f"{right['source_table']}.{right_ids[0]}":
                    raise ValueError("连接目标与 range 业务标识不一致；复合键关系须显式评审")
                aliases = [f"subject_key_{i}" for i in range(len(ids))]
                selected = [f"l.{_identifier(c)} AS {a}" for c, a in zip(ids, aliases, strict=True)]
                selected.append(f"r.{_identifier(right_ids[0])} AS object_key")
                sql = (f"SELECT {', '.join(selected)} FROM {_table_sql(table)} l JOIN {_table_sql(right)} r "
                       f"ON l.{qcol}=r.{_identifier(right_ids[0])} "
                       f"WHERE l.{dataset_column(table)}='{table['dataset_id']}' AND r.{dataset_column(right)}='{right['dataset_id']}'" +
                       "".join(f" AND l.{_identifier(c)} IS NOT NULL" for c in ids))
                add(m, f"{_entity(m['domain'], aliases)} :{target} {_entity(m['range'], ['object_key'])}", sql,
                    [source_dependency(table, [*ids, col]), source_dependency(right, right_ids)])
        except (ValueError, KeyError) as exc:
            issue(m, str(exc))

    obda = (f"[PrefixDeclaration]\n: {namespace}\nxsd: http://www.w3.org/2001/XMLSchema#\n"
            "rdfs: http://www.w3.org/2000/01/rdf-schema#\n\n[MappingDeclaration] @collection [[\n" +
            "\n\n".join(blocks) + "\n]]\n")
    reasons = {}
    for index, rule in enumerate(rule_candidates, 1):
        name = f"rule_{index}"
        reasons[name] = {
            "description_zh": rule.get("description") or rule.get("name"),
            "source_rule_ids": [rule.get("id")], "result_predicates": [rule.get("conclusion_predicate")],
            "execution_scope": "FULL_QUERY_RESULT", "evidence_query": "", "fact_bindings": [],
            "ontology_terms": {}, "question_examples": [], "runtime_validation": {},
            "closed_world_inputs": copy.deepcopy(rule.get("closed_world_inputs") or []),
            "required_set_source": copy.deepcopy(rule.get("required_set_source")),
            "rules": [{"rule_id": rule.get("id"), "description_zh": rule.get("description") or rule.get("name"),
                       "expression": rule.get("formal_expression"), "confidence": rule.get("confidence", 1)}],
        }
        open_items.append({"kind": "REASONING_BINDING_REQUIRED", "path": f"/realtime_runtime/reasoning_capabilities/{name}",
                           "source_rule_id": rule.get("id"), "detail": "需补事实查询、fact_bindings、ontology_terms、问题示例与真实 runtime_validation；候选规则尚未成为可执行推理。"})
    for cq in cq_questions:
        open_items.append({"kind": "CQ_BINDING_REQUIRED", "question_id": cq.get("id"), "question": cq.get("question"),
                           "detail": "在对应 query_capabilities、reasoning_capabilities 或 document_fact_queries 的 cq_bindings 绑定原业务问题和真实行验收；通用列表查询不能代替业务问答。"})
    runtime = {
        "ontop_deployment_id": "ontop-" + hashlib.sha256(project_id.encode()).hexdigest()[:16],
        "prepared_by": "ORION_PLATFORM_COMPILER", "database_access_mode": "READ_ONLY",
        "mapping_obda": obda, "ontop_queries": queries, "query_capabilities": capabilities,
        "document_query_capabilities": ["current_full_text_search", "reviewed_entity_evidence"] if intake_mode == "HYBRID" else [],
        "document_fact_queries": {}, "reasoning_requirement": "REQUIRED" if reasons else "NOT_APPLICABLE",
        "reasoning_capabilities": reasons,
    }
    if not reasons:
        runtime["reasoning_not_applicable_reason"] = "当前已登记语义候选没有业务推理规则，仅提供映射事实查询；业务问题仍需逐项评审绑定。"
    from services.ontology_engineering.business_query_compiler import compile_business_query_plans
    from services.ontology_engineering.mapping_runtime_extension import runtime_compiler_manifest

    business_queries, business_caps, business_rules, bound_rules, plan_issues = compile_business_query_plans(
        mapping_draft, compiled_ids=compiled, rules=rule_candidates, cq_questions=cq_questions,
        ontology_candidates=ontology_candidates or [], mapping_source_dependencies=mapping_sources)
    collisions = set(business_queries) & set(queries)
    if collisions:
        raise WorkflowError("业务计划 ID 与平台实例查询重名，请使用独立 ID：" + ", ".join(sorted(collisions)))
    queries.update(business_queries)
    capabilities.update(business_caps)
    for name in list(reasons):
        if set(reasons[name]["source_rule_ids"]) <= bound_rules:
            del reasons[name]
    if set(reasons) & set(business_rules):
        raise WorkflowError("业务计划的规则名称与已有规则草稿重名，请修改计划 ID。")
    reasons.update(business_rules)
    bound_cqs = {qid for cap in [*business_caps.values(), *business_rules.values()] for qid in cap.get("cq_bindings", {})}
    open_items = [item for item in open_items
                  if not (item.get("kind") == "REASONING_BINDING_REQUIRED" and item.get("source_rule_id") in bound_rules)
                  and not (item.get("kind") == "CQ_BINDING_REQUIRED" and item.get("question_id") in bound_cqs)]
    open_items.extend(plan_issues)
    runtime["compiler_manifest"] = runtime_compiler_manifest(runtime)
    checks = check_mapping_runtime(mappings, runtime, intake_mode=intake_mode)
    return {"contract_version": CONTRACT_VERSION, "runtime": runtime, "open_items": open_items,
            "self_check": checks, "compiled_blocks": compiled_blocks,
            "mapping_source_dependencies": mapping_sources, "summary": {"mapping_count": len(mappings), "compiled_mapping_count": len(compiled),
                "compiled_mapping_ids": compiled, "query_count": len(queries), "rule_count": len(reasons),
                "business_query_count": len(business_queries), "compiled_business_rule_count": len(bound_rules),
                "open_item_count": len(open_items)}}


def check_mapping_runtime(mappings: list[dict], runtime: dict, *, intake_mode: str) -> dict:
    obda = str(runtime.get("mapping_obda") or "")
    checks: dict[str, Any] = {"runtime_verified": False}
    try:
        _validate_obda(obda)
        checks["obda"] = "PASSED"
    except RuntimeReleaseError as exc:
        checks["obda"] = {"status": "FAILED", "message": str(exc)}
    checks["uncovered_targets"] = runtime_uncovered_targets(mappings, obda)
    checks["datatype_issues"] = collect_mapping_runtime_issues(mappings, obda)
    try:
        normalize_runtime_submission(runtime, intake_mode=intake_mode, require_explicit_capabilities=True)
        checks["runtime_contract"] = "PASSED"
    except RuntimeReleaseError as exc:
        checks["runtime_contract"] = {"status": "PENDING_REVIEW", "message": str(exc)}
    return checks
