"""CQ answer semantics shared by engineering validation and released queries.

These functions normalize and validate caller-provided contracts and result rows.
They do not load projects, query data, or perform deployment or Harness operations.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import CQBindingError, WorkflowGateError
from .nullable_bindings import normalize_nullable_bindings
from .sparql_syntax import parse_cq_query, query_type, sparql_query_type

_sparql_query_type = sparql_query_type

CJK_PATTERN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _contains_chinese(value: Any) -> bool:
    return bool(CJK_PATTERN.search(str(value or "")))


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _fingerprint(value: Any) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value)).hexdigest()}"


def _row_scalar(value: Any, *, runtime: bool = False) -> tuple[str, Any]:
    """Tag scalar types: True, 1 and '1' must not become the same cell."""
    if value is None:
        return "null", None
    if type(value) is bool:
        return "boolean", value
    if type(value) is str:
        return "string", value
    if type(value) in (int, float) or (runtime and isinstance(value, Decimal)):
        number = Decimal(str(value))
        if number.is_finite():
            return "number", number
    raise CQBindingError("expected_rows cells require finite JSON scalars; no implicit type conversion")


def normalize_expected_rows(raw: dict[str, Any], *, fields: list[str]) -> dict[str, Any]:
    """An opt-in complete multiset, over explicitly named fields only.

    JSON numbers share a numeric value space; strings and booleans stay distinct.
    Null is explicit; missing runtime keys require a reviewed nullable condition. No projection, first row
    or observed result can supply either the comparison fields or expectations.
    """
    if "expected_rows" not in raw and "expected_row_fields" not in raw:
        return {}
    selected = raw.get("expected_row_fields")
    rows = raw.get("expected_rows")
    if (not isinstance(selected, list) or not selected
            or any(not isinstance(field, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field)
                   for field in selected)
            or len(set(selected)) != len(selected) or not set(selected) <= set(fields)):
        raise CQBindingError("expected_rows requires explicit unique expected_row_fields from result fields")
    if not isinstance(rows, list):
        raise CQBindingError("expected_rows must be an explicit array (use [] for an empty result)")
    kinds: dict[str, str] = {}
    normalized = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != set(selected):
            raise CQBindingError("every expected_rows row must contain exactly expected_row_fields")
        for field in selected:
            kind, _ = _row_scalar(row[field])
            if kind != "null":
                if field in kinds and kinds[field] != kind:
                    raise CQBindingError(f"expected_rows has ambiguous mixed scalar types: {field}")
                kinds[field] = kind
        normalized.append({field: row[field] for field in selected})
    return {"expected_row_fields": list(selected), "expected_rows": normalized}


def validate_cq_expected_rows(
    *, question_id: str, contract: dict[str, Any], rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The same complete-row comparison serves preview cases and formal CQs."""
    try:
        expected = normalize_expected_rows(contract, fields=contract.get("expected_row_fields") or [])
        if not expected:
            return None
        fields = expected["expected_row_fields"]
        nullable = normalize_nullable_bindings(
            contract.get("nullable_bindings"),
            contract.get("required_bindings") or contract.get("expected_fields") or fields,
        )
        wanted = Counter(tuple(_row_scalar(row[field]) for field in fields)
                         for row in expected["expected_rows"])
        observed = Counter()
        for row in rows:
            if not isinstance(row, dict):
                raise CQBindingError("actual result row must be an object")
            for field in set(fields) - row.keys():
                if not cq_binding_allows_null(contract={"nullable_bindings": nullable}, field=field, row=row):
                    raise CQBindingError("actual row is missing an expected_row_fields binding without a reviewed nullable condition")
            observed[tuple(_row_scalar(row.get(field), runtime=True) for field in fields)] += 1
    except CQBindingError as exc:
        raise WorkflowGateError("G-S6-CQ-SEMANTIC", f"能力问题 {question_id} 的完整行验收无效：{exc}") from exc
    if wanted != observed:
        raise WorkflowGateError(
            "G-S6-CQ-SEMANTIC",
            f"能力问题 {question_id} 的完整行元组不满足 expected_rows（无序多重集合）："
            f"预期 {len(expected['expected_rows'])} 行，实际 {len(rows)} 行；"
            f"缺少 {sum((wanted - observed).values())} 行，多出 {sum((observed - wanted).values())} 行。",
        )
    return {"comparison": "UNORDERED_MULTISET", "fields": fields,
            "expected_row_count": len(expected["expected_rows"]), "actual_row_count": len(rows),
            "status": "PASSED"}


def cq_answer_contract(question: dict[str, Any]) -> dict[str, Any]:
    """Freeze query semantics and expected values, not only the answer shape."""

    sparql = str(question.get("sparql") or "").strip()
    expected = str(question.get("expected") or "").strip()
    try:
        parsed = parse_cq_query(sparql)
    except CQBindingError as exc:
        raise WorkflowGateError("G-S4-CQ", str(exc)) from exc
    operation = query_type(parsed)
    if operation is None:
        raise WorkflowGateError("G-S4-CQ", "CQ 查询类型无法识别。")
    provided = question.get("answer_contract") or {}
    if not isinstance(provided, dict):
        raise WorkflowGateError("G-S4-CQ", "CQ answer_contract 必须是对象。")
    provided_type = str(provided.get("query_type") or operation).upper()
    if provided_type != operation:
        raise WorkflowGateError(
            "G-S4-CQ",
            f"CQ answer_contract.query_type={provided_type} 与 SPARQL {operation} 不一致。",
        )
    if operation != "SELECT" and ("expected_rows" in provided or "expected_row_fields" in provided):
        raise WorkflowGateError("G-S4-CQ", "expected_rows 只适用于 SELECT 查询。")

    contract: dict[str, Any] = {
        "contract_version": "cq-answer-v2",
        "query_type": operation,
    }
    answer_mode = str(provided.get("answer_mode") or "FACT_QUERY").strip().upper()
    if answer_mode not in {
        "FACT_QUERY",
        "EVIDENCE_QUERY",
        "RULE_INFERENCE",
        "OWL_INFERENCE",
    }:
        raise WorkflowGateError("G-S4-CQ", "CQ answer_mode 不受支持。")
    reasoning_capability = str(provided.get("reasoning_capability") or "").strip()
    derived_predicates = provided.get("derived_predicates") or []
    if not isinstance(derived_predicates, list) or any(
        not str(value).strip() for value in derived_predicates
    ):
        raise WorkflowGateError("G-S4-CQ", "CQ derived_predicates 必须是字符串数组。")
    contract.update(
        {
            "answer_mode": answer_mode,
            "reasoning_capability": reasoning_capability or None,
            "derived_predicates": list(
                dict.fromkeys(str(value).strip() for value in derived_predicates)
            ),
        }
    )
    # Only a later read-back against the actual S3 binding authorizes this
    # provenance.  Merely supplying these fields cannot bypass a gate.
    for field in ("reviewed_runtime_binding", "answer_scope_zh", "source_refs"):
        if field in provided:
            contract[field] = provided[field]
    if operation == "SELECT":
        # CompValue.get returns the key itself for absent keys, unlike dict.get.
        query_bindings = list(dict.fromkeys(
            str(item["var"] if "var" in item else item["evar"])
            for item in (parsed["projection"] if "projection" in parsed else [])  # noqa: SIM401
        ))
        if not query_bindings:
            raise WorkflowGateError(
                "G-S4-CQ",
                "SELECT CQ 必须显式返回至少一个命名变量，不能使用无变量或 SELECT * 契约。",
            )
        required_bindings = provided.get("required_bindings", query_bindings)
        if (
            not isinstance(required_bindings, list)
            or not required_bindings
            or not all(isinstance(value, str) and value.strip() for value in required_bindings)
        ):
            raise WorkflowGateError(
                "G-S4-CQ",
                "SELECT CQ 的 required_bindings 必须是非空变量名数组。",
            )
        normalized_bindings = list(
            dict.fromkeys(value.strip().removeprefix("?") for value in required_bindings)
        )
        unknown_bindings = set(normalized_bindings) - set(query_bindings)
        if query_bindings and unknown_bindings:
            raise WorkflowGateError(
                "G-S4-CQ",
                f"SELECT CQ 的答案契约引用了查询未返回的变量：{', '.join(sorted(unknown_bindings))}",
            )
        try:
            exact = normalize_expected_rows(provided, fields=query_bindings)
        except CQBindingError as exc:
            raise WorkflowGateError("G-S4-CQ", str(exc)) from exc
        contract.update(exact)
        default_min_rows = 0 if exact or "空集" in expected or "没有实例" in expected else 1
        min_rows = int(provided.get("min_rows", default_min_rows))
        if min_rows < 0:
            raise WorkflowGateError("G-S4-CQ", "SELECT CQ 的 min_rows 不能小于 0。")
        if exact and min_rows > len(exact["expected_rows"]):
            raise WorkflowGateError("G-S4-CQ", "min_rows 与显式 expected_rows 行数矛盾。")
        contract.update(
            {
                "required_bindings": normalized_bindings,
                "min_rows": min_rows,
            }
        )
        if "nullable_bindings" in provided:
            try:
                contract["nullable_bindings"] = normalize_nullable_bindings(
                    provided["nullable_bindings"], normalized_bindings
                )
            except CQBindingError as exc:
                raise WorkflowGateError("G-S4-CQ-BINDING", str(exc)) from exc
        assertions = normalize_cq_result_assertions(
            provided.get("result_assertions"),
            normalized_bindings,
        )
        if not assertions and not exact:
            assertions = derive_cq_result_assertions(
                expected,
                normalized_bindings,
            )
        boundary_assertions = normalize_cq_result_assertions(
            provided.get("boundary_assertions"),
            normalized_bindings,
        )
        contract["result_assertions"] = assertions
        contract["boundary_assertions"] = boundary_assertions
    elif operation == "ASK":
        expected_boolean = provided.get("expected_boolean", True)
        if not isinstance(expected_boolean, bool):
            raise WorkflowGateError("G-S4-CQ", "ASK CQ 的 expected_boolean 必须是布尔值。")
        contract["expected_boolean"] = expected_boolean
    else:
        min_triples = int(provided.get("min_triples", 1))
        if min_triples < 0:
            raise WorkflowGateError("G-S4-CQ", "图查询 CQ 的 min_triples 不能小于 0。")
        contract["min_triples"] = min_triples

    contract["required_business_dimensions"] = (
        normalize_required_business_dimensions(
            provided.get("required_business_dimensions"),
            query_bindings=(
                list(contract.get("required_bindings") or []) if operation == "SELECT" else []
            ),
        )
    )

    required_fragments = cq_required_sparql_fragments(
        question_text=str(question.get("question") or ""),
        expected=expected,
        sparql=sparql,
        provided=provided.get("required_sparql_fragments"),
    )
    forbidden_fragments = provided.get("forbidden_sparql_fragments") or []
    if not isinstance(forbidden_fragments, list) or not all(
        isinstance(value, str) and value.strip() for value in forbidden_fragments
    ):
        raise WorkflowGateError(
            "G-S4-CQ",
            "CQ forbidden_sparql_fragments 必须是非空字符串数组。",
        )
    contract["required_sparql_fragments"] = required_fragments
    contract["forbidden_sparql_fragments"] = list(
        dict.fromkeys(value.strip() for value in forbidden_fragments)
    )
    contract["semantic_assertion_count"] = (
        len(required_fragments)
        + len(contract.get("result_assertions") or [])
        + len(contract.get("boundary_assertions") or [])
        + int("expected_rows" in contract)
    )

    contract.update(
        {
            "question_sha256": _fingerprint(
                {
                    "id": str(question.get("id") or "").strip(),
                    "question": str(question.get("question") or "").strip(),
                    "expected": expected,
                    "source_question_id": question.get("source_question_id"),
                }
            ),
            "sparql_sha256": _fingerprint(sparql),
            "expected_sha256": _fingerprint(expected),
        }
    )
    return contract


def normalize_required_business_dimensions(
    raw: Any,
    *,
    query_bindings: list[str],
) -> list[dict[str, Any]]:
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise WorkflowGateError(
            "G-S4-CQ-CONTRACT-COVERAGE",
            "CQ required_business_dimensions 必须是数组。",
        )
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise WorkflowGateError(
                "G-S4-CQ-CONTRACT-COVERAGE",
                f"第 {index} 个业务输出维度必须是对象。",
            )
        dimension = str(item.get("dimension") or "").strip()
        label_zh = str(item.get("label_zh") or "").strip()
        applicability = str(item.get("applicability") or "REQUIRED").upper()
        evidence_refs = item.get("evidence_refs") or []
        if (
            not dimension
            or dimension in seen
            or not _contains_chinese(label_zh)
            or applicability not in {"REQUIRED", "NOT_APPLICABLE"}
            or not isinstance(evidence_refs, list)
            or not evidence_refs
            or any(not str(value).strip() for value in evidence_refs)
        ):
            raise WorkflowGateError(
                "G-S4-CQ-CONTRACT-COVERAGE",
                f"第 {index} 个业务输出维度缺少唯一名称、中文语义、适用性或证据。",
            )
        normalized_item: dict[str, Any] = {
            "dimension": dimension,
            "label_zh": label_zh,
            "applicability": applicability,
            "evidence_refs": list(dict.fromkeys(str(value).strip() for value in evidence_refs)),
        }
        if applicability == "NOT_APPLICABLE":
            reason_zh = str(item.get("reason_zh") or "").strip()
            if not _contains_chinese(reason_zh):
                raise WorkflowGateError(
                    "G-S4-CQ-CONTRACT-COVERAGE",
                    f"业务输出维度 {dimension} 声明不适用时必须提供中文理由。",
                )
            normalized_item["reason_zh"] = reason_zh
        else:
            binding = str(item.get("binding") or "").strip().removeprefix("?")
            ontology_term = str(item.get("ontology_term") or "").strip()
            path = str(item.get("path") or "").strip()
            failures = []
            if binding not in query_bindings:
                available = ", ".join(str(name)[:100] for name in query_bindings[:20]) or "（无）"
                failures.append(f"未绑定 SELECT 输出变量：binding={binding[:100] or '（缺失）'}；当前返回变量为 {available}")
            if not re.match(r"^(?:https?://|urn:)", ontology_term):
                failures.append("ontology_term 缺失或不是完整 IRI；必须引用已声明的本体类或属性，不能用局部名称替代")
            if failures:
                raise WorkflowGateError(
                    "G-S4-CQ-CONTRACT-COVERAGE",
                    f"业务输出维度 {dimension}：{'；'.join(failures)}。REQUIRED 维度必须同时提供 binding 和 ontology_term；不要为过检改成不适用。",
                )
            normalized_item.update(
                {
                    "binding": binding,
                    "ontology_term": ontology_term,
                    "path": path,
                }
            )
        seen.add(dimension)
        normalized.append(normalized_item)
    return normalized


def normalize_cq_result_assertions(
    raw: Any,
    bindings: list[str],
) -> list[dict[str, Any]]:
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        raise WorkflowGateError(
            "G-S4-CQ",
            "CQ result_assertions/boundary_assertions 必须是数组。",
        )
    allowed_operators = {"EQ", "NE", "GT", "GTE", "LT", "LTE", "APPROX", "CONTAINS"}
    allowed_rows = {"FIRST", "ANY", "ALL"}
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise WorkflowGateError("G-S4-CQ", f"第 {index} 个结果断言必须是对象。")
        binding = str(item.get("binding") or "").strip().removeprefix("?")
        operator = str(item.get("operator") or "EQ").strip().upper()
        row = str(item.get("row") or "FIRST").strip().upper()
        if binding not in bindings:
            raise WorkflowGateError(
                "G-S4-CQ",
                f"结果断言引用了查询未返回的变量：{binding or '空变量'}",
            )
        if operator not in allowed_operators or row not in allowed_rows:
            raise WorkflowGateError("G-S4-CQ", "CQ 结果断言的 operator 或 row 不受支持。")
        if "expected" not in item:
            raise WorkflowGateError("G-S4-CQ", "CQ 结果断言缺少 expected。")
        assertion = {
            "binding": binding,
            "operator": operator,
            "expected": item["expected"],
            "row": row,
        }
        if operator == "APPROX":
            tolerance = item.get("tolerance")
            if tolerance is None:
                raise WorkflowGateError("G-S4-CQ", "APPROX 结果断言必须提供 tolerance。")
            try:
                decimal_tolerance = Decimal(str(tolerance))
                if not decimal_tolerance.is_finite() or decimal_tolerance < 0:
                    raise InvalidOperation
            except (InvalidOperation, ValueError) as exc:
                raise WorkflowGateError("G-S4-CQ", "APPROX tolerance 必须是非负数。") from exc
            assertion["tolerance"] = tolerance
        normalized.append(assertion)
    return normalized


def derive_cq_result_assertions(
    expected: str,
    bindings: list[str],
) -> list[dict[str, Any]]:
    assertions: list[dict[str, Any]] = []
    for binding in bindings:
        match = re.search(
            rf"(?:\?{re.escape(binding)}|\b{re.escape(binding)}\b)\s*=\s*"
            r"(?P<approx>约\s*)?(?P<value>true|false|-?\d+(?:\.\d+)?|\"[^\"]+\"|'[^']+')",
            expected,
            re.IGNORECASE,
        )
        if match is None:
            continue
        raw_value = match.group("value")
        if raw_value.lower() in {"true", "false"}:
            value: Any = raw_value.lower() == "true"
        elif raw_value[:1] in {'"', "'"}:
            value = raw_value[1:-1]
        else:
            value = float(raw_value) if "." in raw_value else int(raw_value)
        assertion: dict[str, Any] = {
            "binding": binding,
            "operator": "APPROX" if match.group("approx") else "EQ",
            "expected": value,
            "row": "FIRST",
        }
        if assertion["operator"] == "APPROX":
            assertion["tolerance"] = max(abs(float(value)) * 0.001, 1e-9)
        assertions.append(assertion)
    return assertions


def cq_required_sparql_fragments(
    *,
    question_text: str,
    expected: str,
    sparql: str,
    provided: Any,
) -> list[str]:
    if provided in (None, []):
        fragments: list[str] = []
    elif isinstance(provided, list) and all(
        isinstance(value, str) and value.strip() for value in provided
    ):
        fragments = [value.strip() for value in provided]
    else:
        raise WorkflowGateError(
            "G-S4-CQ",
            "CQ required_sparql_fragments 必须是非空字符串数组。",
        )

    semantic_text = f"{question_text}\n{expected}"
    # Infer only an explicit positive NG selection. Mentions in examples or
    # non-equivalence clauses do not override the reviewed answer contract.
    if cq_explicitly_requires_ng(semantic_text):
        if not re.search(r"checkResult", sparql, re.IGNORECASE) or not re.search(
            r"['\"]NG['\"]",
            sparql,
            re.IGNORECASE,
        ):
            raise WorkflowGateError(
                "G-S4-CQ-SEMANTIC",
                "业务问题要求 NG 结果，但 SPARQL 没有同时限定 checkResult 和 NG。",
            )
        fragments.extend(["checkResult", "NG"])
    if "参数值" in semantic_text and "parameterValue" not in sparql:
        raise WorkflowGateError(
            "G-S4-CQ-SEMANTIC",
            "业务问题要求参数值判断，但 SPARQL 未使用 parameterValue。",
        )
    if "参数值" in semantic_text:
        fragments.append("parameterValue")

    comparison_patterns = (
        (r"(?:不超过|小于等于)\s*(\d+(?:\.\d+)?)", "<="),
        (r"(?:不少于|大于等于)\s*(\d+(?:\.\d+)?)", ">="),
        (r"(?:大于|(?<!不)超过)\s*(\d+(?:\.\d+)?)", ">"),
        (r"(?<!不)(?:小于)\s*(\d+(?:\.\d+)?)", "<"),
    )
    for pattern, operator in comparison_patterns:
        match = re.search(pattern, question_text)
        if match is None:
            continue
        threshold = match.group(1)
        if not re.search(
            rf"{re.escape(operator)}\s*{re.escape(threshold)}(?:\D|$)",
            sparql,
        ):
            raise WorkflowGateError(
                "G-S4-CQ-SEMANTIC",
                f"业务问题要求边界 {operator} {threshold}，但 SPARQL 未保留该条件。",
            )
        fragments.append(f"{operator}{threshold}")
        break

    filters = re.findall(r"\bFILTER\s*\(([^\n]+?)\)", sparql, re.IGNORECASE)
    fragments.extend(f"FILTER({value.strip()})" for value in filters)
    iris = re.findall(r"<([^>]+)>", sparql)
    fragments.extend(
        f"<{value}>"
        for value in iris
        if value
        not in {
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
        }
    )
    return list(dict.fromkeys(fragments))


def cq_explicitly_requires_ng(semantic_text: str) -> bool:
    """Recognize explicit NG selection, not every occurrence of the label."""
    ng = r"(?<![A-Za-z0-9_/])NG(?![A-Za-z0-9_/])"
    for clause in re.split(r"[。；;，,\n!?！？]", semantic_text):
        if not re.search(ng, clause, re.IGNORECASE):
            continue
        # These clauses explain excluded criteria or sample observations;
        # a separate positive selection clause is still checked below.
        if re.search(
            r"不等于|不等同|不代表|不意味着|不替代|(?:不能|不得|不应)替代|"
            rf"(?:不能|不得|不应|禁止|无需|不需|不要求|不要)[^。；;，,\n]*{ng}|"
            r"并非|不是|非\s*NG|"
            r"样例|示例|举例|例如|比如|历史样本|!=|<>",
            clause,
            re.IGNORECASE,
        ):
            continue
        if re.search(
            r"(?:check_?result|检测结果|检查结果|检验结果|判定结果)\s*"
            rf"(?:为|是|等于|=)\s*['\"]?{ng}|"
            r"(?:查询|筛选|统计|计数|列出|返回|找出|选择|限定|只看|只要|"
            rf"仅看|仅要|仅保留|哪些|多少|所有|全部)\s*[^。；;，,\n]{{0,40}}?{ng}|"
            rf"{ng}\s*(?:的)?\s*(?:检测|检查|检验|判定)?\s*"
            r"(?:记录|结果|电芯|样本|对象)[^。；;，,\n]{0,20}?"
            r"(?:有哪些|有多少|数量|总数)",
            clause,
            re.IGNORECASE,
        ):
            return True
    return False


def cq_assertion_matches(actual: Any, assertion: dict[str, Any]) -> bool:
    operator = str(assertion.get("operator") or "EQ").upper()
    expected = assertion.get("expected")
    if actual is None:
        return False
    if isinstance(expected, bool):
        boolean_lexical = str(actual).strip().lower()
        if boolean_lexical not in {"true", "false", "1", "0"}:
            return False
        actual_value: Any = boolean_lexical in {"true", "1"}
    else:
        # Local S6 rows carry RDF ISO lexical strings; released RDF queries
        # return Python date/datetime values. Preserve the same ISO syntax
        # (including T and timezone), rather than datetime.__str__'s space.
        actual_value = actual.isoformat() if isinstance(actual, date) else str(actual)
    if operator == "CONTAINS":
        return str(expected) in str(actual_value)
    if operator in {"EQ", "NE"}:
        equal = actual_value == expected or str(actual_value) == str(expected)
        if not equal and not isinstance(expected, bool):
            # RDF decimal values are value-equal even when their lexical forms
            # differ (for example ``1E+4``, ``10000`` and ``10000.00``).
            # Keep identifiers such as ``001`` lexical by accepting only the
            # JSON-number form, which deliberately rejects leading-zero codes.
            numeric_lexical = re.compile(r"^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?$")
            actual_text = str(actual).strip()
            expected_text = str(expected).strip()
            if numeric_lexical.fullmatch(actual_text) and numeric_lexical.fullmatch(
                expected_text
            ):
                try:
                    equal = Decimal(actual_text) == Decimal(expected_text)
                except InvalidOperation:
                    equal = False
        return equal if operator == "EQ" else not equal
    try:
        actual_number = Decimal(str(actual))
        expected_number = Decimal(str(expected))
    except (InvalidOperation, ValueError):
        return False
    if operator == "GT":
        return actual_number > expected_number
    if operator == "GTE":
        return actual_number >= expected_number
    if operator == "LT":
        return actual_number < expected_number
    if operator == "LTE":
        return actual_number <= expected_number
    if operator == "APPROX":
        try:
            tolerance = Decimal(str(assertion.get("tolerance")))
        except (InvalidOperation, ValueError):
            return False
        return abs(actual_number - expected_number) <= tolerance
    return False


def cq_binding_allows_null(*, contract: dict[str, Any], field: str, row: dict[str, Any]) -> bool:
    """Use the reviewed same-row condition consistently across CQ gates."""
    condition = (contract.get("nullable_bindings") or {}).get(field)
    return bool(condition and condition.get("when") and all(
        row.get(other) is not None
        and cq_assertion_matches(row[other], {"operator": "EQ", "expected": expected})
        for other, expected in condition["when"].items()
    ))


def cq_business_dimension_values(
    *, question_id: Any, contract: dict[str, Any], dimension: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[Any]:
    """Return a required dimension's real values; absence needs reviewed same-row conditions.

    SPARQL defines GROUP_CONCAT over an empty group as "", so an empty string is
    treated exactly like an unbound value: it never counts as a real answer and
    is accepted only where nullable_bindings authorizes absence on that row.
    """
    binding = str(dimension.get("binding") or "")
    values = [row[binding] for row in rows if row.get(binding) not in (None, "")]
    absent = [row for row in rows if row.get(binding) in (None, "")]
    probe = [{**row, binding: None} for row in absent]
    unauthorized = [
        row for row in probe
        if not cq_binding_allows_null(contract=contract, field=binding, row=row)
    ]
    if values and not unauthorized:
        return values
    detail = f"（非空 {len(values)}/{len(rows)} 行，未获条件允许的空值 {len(unauthorized)} 行）"
    hint = ""
    if values and unauthorized:
        required = list(dict.fromkeys([*(contract.get("required_bindings") or []), binding]))
        hint = _nullable_condition_hints(
            required=required, contract=contract, unbound={binding: len(unauthorized)},
            rows=[{**row, binding: None} if row.get(binding) in (None, "") else row for row in rows],
        )
    raise WorkflowGateError(
        "G-S6-CQ-BUSINESS-SEMANTIC",
        f"能力问题 {question_id} 的业务维度 {dimension.get('dimension')} 没有非空实际答案"
        + detail + ("；" + hint if hint else "") + "。",
    )


def validate_cq_required_bindings(
    *, question_id: str, contract: dict[str, Any],
    variables: list[str], rows: list[dict[str, Any]],
) -> dict[str, int]:
    required = list(contract["required_bindings"])
    missing = sorted((set(required) | set(contract.get("expected_row_fields", []))) - set(variables))
    conditional_nulls: dict[str, int] = {}
    unbound: dict[str, int] = {}
    for field in required:
        for row in rows:
            if row.get(field) is not None:
                continue
            if cq_binding_allows_null(contract=contract, field=field, row=row):
                conditional_nulls[field] = conditional_nulls.get(field, 0) + 1
            else:
                unbound[field] = unbound.get(field, 0) + 1
    minimum = int(contract["min_rows"])
    reasons = []
    if len(rows) < minimum:
        reasons.append(f"实际 {len(rows)} 行，小于契约要求 {minimum} 行")
    if missing:
        reasons.append("缺少返回变量 " + ", ".join(missing))
    if unbound:
        # Keep the field list first (stable, grep-able), then give the row
        # counts so the fix can be judged against source evidence at once.
        reasons.append(
            "必填变量存在未获条件允许的空值 " + ", ".join(unbound)
            + "（" + "，".join(f"{field} 空 {count}/{len(rows)} 行" for field, count in unbound.items()) + "）"
        )
        hints = _nullable_condition_hints(required=required, contract=contract, unbound=unbound, rows=rows)
        if hints:
            reasons.append(hints)
    if reasons:
        raise WorkflowGateError(
            "G-S6-CQ-ANSWER", f"能力问题 {question_id} 的答案未满足契约：{'；'.join(reasons)}。"
        )
    return conditional_nulls


def _nullable_condition_hints(
    *, required: list[str], contract: dict[str, Any],
    unbound: dict[str, int], rows: list[dict[str, Any]],
) -> str:
    """Suggest same-row discriminators so a reviewer can declare real absence.

    A candidate is another required field that is non-null in every offending
    row, takes one scalar value there, and is not declared nullable itself.
    This is advisory only: it never relaxes the contract.
    """
    declared = set((contract.get("nullable_bindings") or {}).keys())
    parts = []
    for field in unbound:
        offending = [row for row in rows if row.get(field) is None]
        candidates = []
        for other in required:
            if other == field or other in declared or other in unbound:
                continue
            values = {row.get(other) for row in offending if isinstance(row.get(other), str | int | float | bool)}
            if len(values) == 1 and all(row.get(other) is not None for row in offending):
                value = next(iter(values))
                if any(row.get(other) != value for row in rows if row.get(field) is not None):
                    candidates.append(f"{other}={value!r}")
        if candidates:
            parts.append(f"{field} 空值行可用同行条件 " + " 或 ".join(candidates[:3]))
    if not parts:
        return ""
    return (
        "若空值是来源真实缺失，可在 S3 cq_bindings.nullable_bindings 中声明"
        "（{字段:{when:{条件字段:值},reason_zh,source_refs}}）："
        + "；".join(parts)
    )


def validate_cq_select_semantics(
    *,
    question_id: str,
    sparql: str,
    contract: dict[str, Any],
    rows: list[dict[str, Any]],
    typed_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    normalized_sparql = re.sub(r"\s+", "", sparql).casefold()
    required_fragments = list(contract.get("required_sparql_fragments") or [])
    forbidden_fragments = list(contract.get("forbidden_sparql_fragments") or [])
    missing_fragments = [
        fragment
        for fragment in required_fragments
        if re.sub(r"\s+", "", str(fragment)).casefold() not in normalized_sparql
    ]
    forbidden_present = [
        fragment
        for fragment in forbidden_fragments
        if re.sub(r"\s+", "", str(fragment)).casefold() in normalized_sparql
    ]
    if missing_fragments or forbidden_present:
        reasons = []
        if missing_fragments:
            reasons.append("缺少业务过滤/路径 " + ", ".join(missing_fragments))
        if forbidden_present:
            reasons.append("包含禁止条件 " + ", ".join(forbidden_present))
        raise WorkflowGateError(
            "G-S6-CQ-SEMANTIC",
            f"能力问题 {question_id} 的查询语义未满足契约：{'；'.join(reasons)}。",
        )

    # RDF executors keep lexical rows for legacy assertions and JSON receipts;
    # only the explicit complete-row contract uses RDF-to-Python values.
    exact_result = validate_cq_expected_rows(
        question_id=question_id, contract=contract,
        rows=typed_rows if typed_rows is not None else rows,
    )
    assertion_results: list[dict[str, Any]] = []
    assertions = [
        *(contract.get("result_assertions") or []),
        *(contract.get("boundary_assertions") or []),
    ]
    for assertion in assertions:
        binding = str(assertion.get("binding") or "")
        row_mode = str(assertion.get("row") or "FIRST").upper()
        values = [row.get(binding) for row in rows]
        matches = [cq_assertion_matches(value, assertion) for value in values]
        passed = (
            bool(matches) and matches[0]
            if row_mode == "FIRST"
            else any(matches)
            if row_mode == "ANY"
            else bool(matches) and all(matches)
        )
        assertion_results.append(
            {
                **assertion,
                "actual": values[0] if row_mode == "FIRST" and values else None,
                "status": "PASSED" if passed else "FAILED",
            }
        )
        if not passed:
            # Name the observed values so the agent can tell a data defect from
            # a query or contract defect without rerunning the whole stage.
            observed = values[:1] if row_mode == "FIRST" else values[:5]
            sample = [
                {key: row.get(key) for key in list(row)[:6]} for row in rows[:3]
            ]
            raise WorkflowGateError(
                "G-S6-CQ-SEMANTIC",
                f"能力问题 {question_id} 的 {binding} 未满足 {assertion.get('operator')} "
                f"{assertion.get('expected')} 边界/预期值（行模式 {row_mode}，共 {len(rows)} 行，"
                f"实际值 {json.dumps(observed, ensure_ascii=False, default=str)}；"
                f"前 3 行 {json.dumps(sample, ensure_ascii=False, default=str)}）。",
            )
    return {
        "required_sparql_fragment_count": len(required_fragments),
        "forbidden_sparql_fragment_count": len(forbidden_fragments),
        "result_assertion_count": len(contract.get("result_assertions") or []),
        "boundary_assertion_count": len(contract.get("boundary_assertions") or []),
        "assertions": assertion_results,
        **({"expected_rows_validation": exact_result} if exact_result is not None else {}),
    }
