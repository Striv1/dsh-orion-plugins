"""Bounded execution of one prepared business case, without project writes.

The caller owns preparation, fingerprints and the worker's 180s lifetime. Only
the supplied empty candidate directory receives assets. Its typed vocabulary is
a mapping draft, not a reviewed S4/S5 ontology or a publication receipt. The
existing Ontop context manager cleans its container up at normal worker exit.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import psycopg
from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from sqlalchemy.engine import make_url

from scripts.run_quality_validation_stage import (
    pre_release_ontop_endpoint,
    verify_reader_principal_catalog,
)
from services.config import Settings
from services.ontology_contracts.cq_answers import (
    cq_answer_contract,
    cq_assertion_matches,
    validate_cq_expected_rows,
    validate_cq_required_bindings,
    validate_cq_select_semantics,
)
from services.ontology_contracts.errors import WorkflowGateError
from services.ontology_contracts.sparql_syntax import parse_cq_query
from services.ontology_engineering.formal_facts import (
    materialize_formal_fact,
    ontology_materialization_contract,
)
from services.ontology_engineering.ontology_types import normalize_datatype_iri
from services.ontop_client.query_execution import compile_ontop_select
from services.realtime_qa.cq_contract import _has_external_dataset, compile_reviewed_cq
from services.realtime_qa.json_values import query_json_scalar
from services.realtime_qa.query_capabilities import (
    QueryCapabilityError,
    normalize_query_capabilities,
    render_query_parameters,
)
from services.realtime_qa.rdf_results import rdf_term_value as _term_value
from services.realtime_qa.reasoning import SemanticaReasoningClient, facts_from_rows, result_facts
from services.realtime_qa.reasoning_contract import (
    ReasoningCapabilityError,
    normalize_reasoning_capabilities,
)
from services.structured_data.source_evidence import verify_snapshot_evidence_binding

MAX_ROWS = 200
QUERY_TIMEOUT_SECONDS = 15
MAX_RESPONSE_BYTES = 1024 * 1024


class PreviewDiagnostic(Exception):
    def __init__(self, code, message, *, status="FAILED", suffix="", next_step=None):
        super().__init__(message)
        self.code, self.status, self.suffix, self.next_step = code, status, suffix, next_step


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)


def _json_safe(value):
    return json.loads(json.dumps(value, default=query_json_scalar, allow_nan=False))


def _display_receipt(receipt):
    """Bound presentation only, after every assertion used the original values."""
    truncated = receipt.get("returned_row_count", 0) > len(receipt.get("rows", []))
    rule = receipt.get("rule")
    if rule:
        rule["trace_count"] = len(rule["trace"])
        rule["cq_count"] = len(rule["cq_results"])
        truncated |= (len(rule["trace"]) > 5 or len(rule["cq_results"]) > 5
                      or any(item.get("returned_row_count", 0) > len(item.get("rows", []))
                             for item in rule["cq_results"]))
        rule["trace"] = rule["trace"][:5]
        rule["cq_results"] = rule["cq_results"][:5]

    def clip(value, width):
        nonlocal truncated
        if isinstance(value, str) and len(value) > width:
            truncated = True
            return value[:width - 1] + "…"
        if isinstance(value, dict):
            return {key: clip(item, width) for key, item in value.items()}
        if isinstance(value, list):
            return [clip(item, width) for item in value]
        return value

    result = _json_safe(receipt)
    for width in (256, 128, 64, 32):
        result = clip(result, width)
        # Reserve room for the worker's envelope, even with ASCII JSON encoding.
        if len(json.dumps(result, ensure_ascii=True).encode()) <= 48 * 1024:
            break
    result["display_truncated"] = truncated
    return result


def _selected_contract(job):
    name, case = job["plan_id"], job["case"]
    plans = job["mapping_draft"]["business_query_plans"]
    if (not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", name) or len(plans) != 1
            or plans[0]["id"] != name or case["id"] != job["case_id"]):
        raise ValueError("selected plan/case mismatch")
    declared = [item for item in plans[0]["validation_cases"] if item["id"] == case["id"]]
    if len(declared) != 1 or _canonical(declared[0]) != _canonical(case):
        raise ValueError("selected case differs from authored expectations")
    runtime = job["runtime"]
    if runtime["database_access_mode"] != "READ_ONLY" or not runtime["mapping_obda"].strip():
        raise ValueError("read-only mapping is required")
    if (name not in runtime.get("query_capabilities", {})
            or not isinstance(runtime.get("ontop_queries", {}).get(name), str)):
        raise PreviewDiagnostic("QUERY_CAPABILITY_MISSING", "当前计划缺少对应的已编译查询或查询能力合同。",
                                next_step="修正此 business_query_plan 的映射依赖并重新准备运行时。")
    try:
        capability = normalize_query_capabilities(
            {name: runtime["query_capabilities"][name]}, {name}, allow_legacy=False,
        )[name]
    except QueryCapabilityError:
        raise PreviewDiagnostic("QUERY_CAPABILITY_INVALID", "查询能力的参数、结果字段或验收用例合同不兼容。",
                                next_step="核对本计划 parameters、select、validation_cases 与 CQ 绑定后重新编译。") from None
    # The normalized case may contain defaults. Compare its input with the
    # authored case before normalization rather than changing its expectations.
    raw_cases = runtime["query_capabilities"][name]["validation_cases"]
    matches = [item for item in raw_cases if item["id"] == case["id"]]
    if len(matches) != 1 or _canonical(matches[0]) != _canonical(case):
        raise ValueError("runtime case differs from authored expectations")
    selected = next(item for item in capability["validation_cases"] if item["id"] == case["id"])
    reason = None
    if "rule" in plans[0]:
        reason_name = name + "_rule"
        if reason_name not in runtime.get("reasoning_capabilities", {}):
            raise PreviewDiagnostic("RULE_CAPABILITY_MISSING", "当前计划未编译出对应的原生规则能力。",
                                    suffix="/rule", next_step="核对规则候选、前提映射及结论映射后重新编译。")
        try:
            reason = normalize_reasoning_capabilities(
                {reason_name: runtime["reasoning_capabilities"][reason_name]}, {name}, require_rules=True,
            )[reason_name]
        except ReasoningCapabilityError:
            raise PreviewDiagnostic("RULE_CAPABILITY_INVALID", "规则事实绑定、类型、参数或验收合同不兼容。",
                                    suffix="/rule", next_step="核对前提、结论、runtime_validation 与规则 CQ 的验收用例。") from None
        if reason["evidence_query"] != name:
            raise ValueError("rule evidence query mismatch")
        if _canonical(reason["runtime_validation"]["parameters"]) != _canonical(selected["parameters"]):
            raise PreviewDiagnostic(
                "RULE_PARAMETERS_MISMATCH", "规则验收参数与本次证据用例不一致，无法共用此证据执行规则。",
                suffix="/rule/runtime_validation/parameters",
                next_step="选择参数一致的 validation_cases，或明确修正规则验收参数后重新编译。",
            )
    if not isinstance(job["source_bindings"], list) or not job["source_bindings"]:
        raise ValueError("snapshot bindings are required")
    return capability, selected, reason


def _verify_sources(job, reader_url):
    principal = make_url(reader_url).username
    if not principal:
        raise ValueError("reader principal missing")
    verify_reader_principal_catalog(reader_url, principal)
    # Each invocation opens a fresh read-only transaction, including the final
    # check: a repeatable-read transaction spanning the run would hide changes.
    with psycopg.connect(
        reader_url.replace("postgresql+psycopg://", "postgresql://", 1),
        connect_timeout=QUERY_TIMEOUT_SECONDS,
        options="-c default_transaction_read_only=on -c statement_timeout=15000",
    ) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_user, current_setting('transaction_read_only')")
        if cursor.fetchone() != (principal, "on"):
            raise ValueError("reader session mismatch")
        for binding in job["source_bindings"]:
            verify_snapshot_evidence_binding(cursor, project_id=job["project_id"], binding=binding)


def _candidate_assets(job, candidate_dir):
    if candidate_dir.is_symlink() or not candidate_dir.is_dir() or any(candidate_dir.iterdir()):
        raise ValueError("candidate directory must already exist and be empty")
    draft = job["mapping_draft"]
    namespace = draft["namespace"]
    if not re.fullmatch(r'(?:https?://|urn:)[^\s<>"{}\\]+[#/:]', namespace):
        raise ValueError("invalid namespace")
    graph = Graph()
    graph.add((URIRef("urn:orion:business-preview:vocabulary"), RDFS.label,
               Literal("Draft typed vocabulary from mapping_draft; not a formal reviewed OWL ontology")))
    types = {"TABLE_TO_CLASS": OWL.Class, "SQL_TO_CLASS": OWL.Class, "RULE_TO_CLASS": OWL.Class,
             "COLUMN_TO_DATA_PROPERTY": OWL.DatatypeProperty,
             "CANDIDATE_JOIN_TO_OBJECT_PROPERTY": OWL.ObjectProperty}
    for mapping in draft["mappings"]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", mapping["target"]):
            raise ValueError("invalid vocabulary target")
        kind = types[mapping["mapping_type"]]
        subject = URIRef(namespace + mapping["target"])
        graph.add((subject, RDF.type, kind))
        if kind == OWL.DatatypeProperty:
            graph.add((subject, RDFS.range, URIRef(normalize_datatype_iri(mapping["datatype"]))))
        # Domain/range axioms are deliberately absent: this is typed vocabulary,
        # not an inferred substitute for the project's reviewed ontology design.
    ontology_materialization_contract(graph)
    for relative, content in {
        "03-mapping-review/runtime/mapping.obda": job["runtime"]["mapping_obda"],
        "05-ontology-build/ontology.ttl": graph.serialize(format="turtle"),
    }.items():
        path = candidate_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as handle:
            handle.write(content)
    return graph


def _bounded_query(template, case, capability):
    try:
        rendered = render_query_parameters(template, case["parameters"], capability)
    except QueryCapabilityError:
        raise PreviewDiagnostic("QUERY_PARAMETERS_INVALID", "所选用例的查询参数缺失、未声明或类型不符合参数合同。",
                                suffix="/validation_cases", next_step="核对 parameters 的必填项与类型，并修正此 validation_cases.parameters。") from None
    query, execution = compile_ontop_select(rendered)
    # Generated plans have no pagination. Appending a sentinel LIMIT preserves
    # their order; an already paginated or non-SELECT query fails closed here.
    bounded = query.rstrip() + f"\nLIMIT {MAX_ROWS + 1}"
    parsed = parse_cq_query(bounded)
    if parsed.name != "SelectQuery" or _has_external_dataset(parsed):
        raise ValueError("preview requires a standalone SELECT")
    return bounded, {**execution, "executed_query_sha256": "sha256:" + hashlib.sha256(bounded.encode()).hexdigest()}


def _decode_select(data):
    def unique_object(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("duplicate response member")
        return result

    payload = json.loads(data, object_pairs_hook=unique_object)
    if not isinstance(payload, dict) or "boolean" in payload:
        raise ValueError("SELECT object required")
    variables = payload["head"]["vars"]
    bindings = payload["results"]["bindings"]
    if (not isinstance(variables, list) or not variables
            or any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in variables)
            or len(set(variables)) != len(variables) or not isinstance(bindings, list)):
        raise ValueError("malformed SELECT header or bindings")
    rows = []
    for binding in bindings:
        if not isinstance(binding, dict) or set(binding) - set(variables):
            raise ValueError("row disagrees with SELECT header")
        row = {field: _term_value(term) for field, term in binding.items()}
        if any(field.startswith("orion_condition_") and not isinstance(value, bool)
               for field, value in row.items()):
            raise ValueError("rule condition must be an RDF boolean")
        rows.append(row)  # Absent OPTIONAL bindings remain unknown, never false.
    return variables, rows


def _select(endpoint, query):
    started = time.monotonic()
    with httpx.Client(timeout=QUERY_TIMEOUT_SECONDS, trust_env=False) as client, client.stream(
        "POST", endpoint, data={"query": query}, headers={"Accept": "application/sparql-results+json"},
    ) as response:
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() - started > QUERY_TIMEOUT_SECONDS:
                raise httpx.ReadTimeout("preview query deadline")
            if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                raise PreviewDiagnostic(
                    "RESPONSE_LIMIT_EXCEEDED", "Ontop 响应超过 1 MiB，证据不完整，未进行规则推理。",
                    status="INCONCLUSIVE", next_step="缩小此计划的证据范围或减少投影字段后重试。",
                )
            data.extend(chunk)
    try:
        return _decode_select(data)
    except (ValueError, TypeError, KeyError, RuntimeError, OverflowError):
        raise PreviewDiagnostic(
            "ONTOP_MALFORMED_RESPONSE", "Ontop 返回的 SELECT 结构或 RDF 值类型无效，无法验证证据。",
            status="INCONCLUSIVE", next_step="检查候选 Ontop 的响应格式与映射数据类型，再重新预览。",
        ) from None


def _validate_case(case, variables, rows, suffix):
    # A case remains an independent oracle even when no CQ references it.
    valid = (set(case["expected_fields"]) <= set(variables) and len(rows) >= case["min_rows"])
    for field, expected in case["expected_first_row"].items():
        actual = rows[0].get(field) if rows else None
        valid = valid and (actual is expected if isinstance(expected, bool) else
                           cq_assertion_matches(actual, {"operator": "EQ", "expected": expected}))
    try:
        validate_cq_expected_rows(question_id=case["id"], contract=case, rows=rows)
        valid = valid and set(case.get("expected_row_fields", [])) <= set(variables)
    except WorkflowGateError:
        valid = False
    if not valid:
        raise PreviewDiagnostic(
            "CASE_EXPECTATION_FAILED", "实际返回字段、行数、首行或完整结果未满足独立声明的验收用例。",
            suffix=suffix, next_step="核对来源映射与此用例的 expected_fields/min_rows/expected_first_row/expected_row_fields/expected_rows；来源明确未核定时，用 nullable_bindings 声明有来源的同行条件并在完整 expected_rows 中保留该行的 null，不删行或删减比较字段；不要从结果自动改写预期。",
        )


def _validate_cqs(name, capability, case, template, variables, rows, suffix):
    for question_id, binding in capability.get("cq_bindings", {}).items():
        if binding["validation_case_id"] != case["id"]:
            continue
        try:
            compiled = compile_reviewed_cq(question_id=question_id, query_name=name,
                                            query=template, capability=capability)
            contract = cq_answer_contract(compiled)
            validate_cq_required_bindings(question_id=question_id, contract=contract,
                                          variables=variables, rows=rows)
            validate_cq_select_semantics(question_id=question_id, sparql=compiled["sparql"],
                                         contract=contract, rows=rows)
        except TimeoutError:
            raise
        except Exception:
            raise PreviewDiagnostic(
                "CQ_CONTRACT_FAILED", "所选用例对应的 CQ 必填绑定或业务边界断言未通过。",
                suffix=suffix + "/cq_bindings/" + question_id.replace("~", "~0").replace("/", "~1"),
                next_step="核对此 CQ 的 validation_case_id、expected_rows、nullable_bindings 和 boundary_assertions。",
            ) from None


def _run_rule(job, reason, case, evidence, graph, receipt, progress):
    symbols = {}
    facts = facts_from_rows(evidence, case["parameters"], reason["fact_bindings"], symbols)
    kinds, datatypes = ontology_materialization_contract(graph)
    progress({"phase": "RULE_EXECUTION", "message": "使用完整查询证据执行原生只读规则推理。"})
    client = SemanticaReasoningClient(Settings.from_env().semantica_url, timeout=QUERY_TIMEOUT_SECONDS)
    run = client.run_forward(facts=facts, rules=[rule["expression"] for rule in reason["rules"]],
                             closed_world_predicates=reason.get("closed_world_predicates"))
    if (run.get("mutated") or run.get("added_edges") or run.get("updated_properties")
            or not isinstance(run.get("inferred_facts"), list) or not isinstance(run.get("trace"), list)
            or type(run.get("rules_fired")) is not int or run["rules_fired"] < 0):
        raise ValueError("invalid read-only reasoning response")
    selected = result_facts(run["inferred_facts"], reason["result_predicates"])
    receipt["backend_identity"]["semantica"] = {
        "engine_build_sha256": run.get("engine_build_sha256", "UNKNOWN"),
        "apply_to_graph": False,
    }
    receipt["rule"] = {
        "input_fact_count": len(facts), "result_fact_count": len(selected), "rules_fired": run["rules_fired"],
        "trace": [{key: item[key] for key in ("rule_id", "rule_text", "premises", "conclusion") if key in item}
                  for item in run["trace"]], "cq_results": [],
    }
    validation = reason["runtime_validation"]
    if (len(facts) < validation["min_input_facts"] or len(selected) < validation["min_result_facts"]
            or (validation["require_rules_fired"] and not run["rules_fired"])
            or (selected and not run["trace"])
            or (validation["expected_live_outcome"] == "POSITIVE" and not selected)
            or (validation["expected_live_outcome"] == "NEGATIVE" and selected)):
        raise PreviewDiagnostic(
            "RULE_EXPECTATION_FAILED", "真实规则输入、结论数量、触发记录或正反例结果未满足验收约定。",
            suffix="/rule/runtime_validation", next_step="核对规则前提、源数据中的未知值与 runtime_validation 的独立预期。",
        )
    for fact in dict.fromkeys([*facts, *run["inferred_facts"]]):
        materialize_formal_fact(graph, fact, reason["ontology_terms"], symbols,
                                term_kinds=kinds, term_datatypes=datatypes, strict_types=True)
    progress({"phase": "RULE_CQ_VALIDATION", "message": "对原生推理事实执行规则 CQ 并验证已声明预期。"})
    name = job["plan_id"] + "_rule"
    for question_id, binding in reason.get("cq_bindings", {}).items():
        rule_case = next(item for item in reason["validation_cases"] if item["id"] == binding["validation_case_id"])
        if _canonical(rule_case["parameters"]) != _canonical(case["parameters"]):
            raise PreviewDiagnostic("RULE_CQ_PARAMETERS_MISMATCH", "规则 CQ 用例参数与本次证据参数不一致。",
                                    suffix="/rule/validation_cases", next_step="为规则 CQ 和证据用例声明相同参数。")
        compiled = compile_reviewed_cq(question_id=question_id, query_name=name,
                                        query=binding["cq_sparql"], capability=reason)
        result = graph.query(compiled["sparql"])
        variables = [str(field) for field in result.vars]
        rows = [{str(key): value.toPython() for key, value in row.asdict().items()} for row in result]
        receipt["rule"]["cq_results"].append({
            "question_id": question_id, "case_id": rule_case["id"], "result_fields": variables,
            "rows": _json_safe(rows[:5]), "returned_row_count": len(rows),
        })
        _validate_case(rule_case, variables, rows, "/rule/validation_cases")
        _validate_cqs(name, reason, rule_case, binding["cq_sparql"], variables, rows, "/rule")


def execute_preview(job: dict, candidate_dir: Path, progress: callable) -> dict:
    """Execute a prepared plan/case and return a safe partial receipt.

    ``rows`` always contains the first five evidence rows; rule CQ answers live
    in ``rule.cq_results``. A returned row count of 201 is a lower bound and
    produces INCONCLUSIVE, with no inference over that truncated evidence.
    """
    receipt: dict[str, Any] = {
        "status": "INCONCLUSIVE", "message": "尚未取得完整的运行证据。",
        "result_fields": [], "rows": [], "returned_row_count": 0,
        "source_refs": [], "diagnostics": [],
        "backend_identity": {"vocabulary_kind": "DRAFT_TYPED_VOCABULARY", "ontop": {}},
    }
    path = "/mapping_draft/business_query_plans"
    if isinstance(job.get("plan_pointer"), str) and re.fullmatch(
        r"/mapping_draft/business_query_plans/(?:0|[1-9][0-9]*)", job["plan_pointer"],
    ):
        path = job["plan_pointer"]
    phase, verified, timed_out = "PREPARATION", False, False
    reader_url = os.getenv("ORION_SOURCE_DATA_READER_URL", "").strip()

    def fail(error):
        receipt.update(status=error.status, message=str(error))
        receipt["diagnostics"].append({"code": error.code, "message": str(error),
                                       "repair_path": path + error.suffix,
                                       "next_step": error.next_step or "修正计划或候选运行条件后重新预览。"})

    try:
        capability, case, reason = _selected_contract(job)
        if not reader_url:
            raise PreviewDiagnostic("READER_NOT_CONFIGURED", "未配置专用来源只读连接，无法执行真实预览。",
                                    status="INCONCLUSIVE", next_step="配置 ORION_SOURCE_DATA_READER_URL 后重试；不使用写入连接回退。")
        receipt["source_evidence"] = [
            {"source_ref": b.get("source_ref") or f"snapshot:{b['snapshot_set_id']}:dataset:{b['dataset_id']}:table:{b['source_table']}",
             "source_sha256": b["dataset"]["source_sha256"], "manifest_sha256": b["dataset"].get("manifest_sha256")}
            for b in job["source_bindings"]
        ]
        receipt["source_refs"] = [item["source_ref"] for item in receipt["source_evidence"]]
        phase = "SOURCE_VERIFICATION"
        progress({"phase": phase, "message": "验证专用只读账号及所选快照的实时目录身份。"})
        _verify_sources(job, reader_url)
        verified = True
        phase = "CANDIDATE_ASSETS"
        graph = _candidate_assets(job, Path(candidate_dir))
        phase = "QUERY_PREPARATION"
        template = job["runtime"]["ontop_queries"][job["plan_id"]]
        query, execution = _bounded_query(template, case, capability)
        receipt["query_execution"] = execution
        phase = "ONTOP_STARTUP"
        progress({"phase": phase, "message": "启动只读候选 Ontop，加载现有 OBDA 和草稿类型词表。"})
        with pre_release_ontop_endpoint(
            project_dir=Path(candidate_dir), database_url=reader_url,
            candidate_id="business-preview-" + hashlib.sha256(str(job["input_fingerprint"]).encode()).hexdigest()[:20],
            backend_identity=receipt["backend_identity"]["ontop"],
            resource_journal=Path(candidate_dir) / "candidate-resource.json",
        ) as endpoint:
            phase = "ONTOP_QUERY"
            progress({"phase": phase, "message": "执行所选证据用例；上限 200 行、15 秒、1 MiB。"})
            variables, rows = _select(endpoint, query)
            receipt.update(result_fields=variables, rows=_json_safe(rows[:5]), returned_row_count=len(rows))
            if len(rows) > MAX_ROWS:
                receipt["truncated"] = True
                raise PreviewDiagnostic("ROW_LIMIT_EXCEEDED", "查询超过 200 行，结果不完整，未进行规则推理或验收。",
                                        status="INCONCLUSIVE", next_step="按已审业务范围缩小证据查询；规则证据不可按结论条件裁剪。")
            phase = "CASE_VALIDATION"
            progress({"phase": phase, "message": "对照独立预期核对实际查询行与 CQ 断言。"})
            _validate_case(case, variables, rows, "/validation_cases")
            _validate_cqs(job["plan_id"], capability, case, template, variables, rows, "")
            if reason is not None:
                # Catch source identity changes before inference, and again at
                # completion. These checks never mutate a source or graph.
                phase = "SOURCE_VERIFICATION"
                _verify_sources(job, reader_url)
                phase = "RULE_EXECUTION"
                _run_rule(job, reason, case, rows, graph, receipt, progress)
        checked = ["查询样例"]
        if reason is not None:
            checked.append("规则推理")
        if (any(binding["validation_case_id"] == case["id"] for binding in capability.get("cq_bindings", {}).values())
                or receipt.get("rule", {}).get("cq_results")):
            checked.append("绑定的CQ验证")
        receipt.update(status="PASSED", message="本次已通过：" + "、".join(checked) + "（草稿候选预览）。")
    except PreviewDiagnostic as exc:
        fail(exc)
    except httpx.TimeoutException:
        fail(PreviewDiagnostic("BACKEND_TIMEOUT", "候选查询或原生推理请求超过 15 秒，未取得完整验证结果。", status="INCONCLUSIVE"))
    except httpx.HTTPStatusError:
        fail(PreviewDiagnostic("BACKEND_REJECTED", "候选后端拒绝了此次查询或推理请求。", status="INCONCLUSIVE"))
    except httpx.TransportError:
        fail(PreviewDiagnostic("BACKEND_UNAVAILABLE", "无法连接候选查询或推理后端。", status="INCONCLUSIVE"))
    except TimeoutError:
        timed_out = True
        raise  # The worker owns the whole-job budget and its classification.
    except Exception as exc:
        # The existing candidate context can wrap a startup exception while
        # collecting diagnostics. Preserve a worker alarm even through that
        # wrapper (``raise ... from None`` still retains __context__).
        cause, seen = exc, set()
        while cause is not None and id(cause) not in seen:
            if isinstance(cause, TimeoutError):
                timed_out = True
                raise cause from None
            seen.add(id(cause))
            cause = cause.__cause__ or cause.__context__
        codes = {"PREPARATION": "PREVIEW_INPUT_INVALID", "SOURCE_VERIFICATION": "SOURCE_EVIDENCE_UNVERIFIED",
                 "CANDIDATE_ASSETS": "CANDIDATE_ASSETS_INVALID", "QUERY_PREPARATION": "QUERY_CONTRACT_INVALID",
                 "ONTOP_STARTUP": "ONTOP_STARTUP_FAILED", "ONTOP_QUERY": "ONTOP_EXECUTION_FAILED",
                 "CASE_VALIDATION": "CASE_CONTRACT_INVALID", "RULE_EXECUTION": "RULE_EXECUTION_FAILED"}
        fail(PreviewDiagnostic(codes[phase], "此预览步骤未能取得有效证据；连接信息和后端原始诊断未写入回执。",
                               status="FAILED" if phase in {"PREPARATION", "CANDIDATE_ASSETS", "QUERY_PREPARATION", "CASE_VALIDATION"}
                               else "INCONCLUSIVE",
                               next_step=f"检查 {phase} 步骤的计划配置或候选运行条件后重试。"))
    finally:
        if verified and not timed_out:
            try:
                _verify_sources(job, reader_url)
            except TimeoutError:
                raise
            except Exception:
                fail(PreviewDiagnostic("SOURCE_EVIDENCE_CHANGED_OR_UNAVAILABLE", "执行结束时只读身份或来源快照无法再次验证，本次结果不能判定通过。",
                                       status="INCONCLUSIVE", next_step="重新读取来源快照及只读账号状态，准备新的预览任务。"))
    return _display_receipt(receipt)
