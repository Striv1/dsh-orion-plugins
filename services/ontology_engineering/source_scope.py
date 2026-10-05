"""Pure intake-scope checks over existing document and SnapshotHub receipts.

This module neither discovers sources nor grants database access. SourceBinding,
SnapshotHub and ingestion remain responsible for authenticating/reading sources;
these helpers check that their receipts stay inside the project's declared scope.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import PurePosixPath
from typing import Any

SOURCE_SCOPE_VERSION = "orion-source-scope-v1"
MODES = {"DOCUMENT_ONLY", "DATABASE_ONLY", "HYBRID"}
FILE_KINDS = {"DOCUMENT", "STRUCTURED_FILE"}
# A registered read-only connection variable (e.g. ORION_ERP_SOURCE_URL). When
# S0 names it as a DATABASE source_id it identifies the connection, which the
# SourceBinding records as connection_ref=env://<NAME>.
_CONNECTION_ENV_PREFIX = "ORION_"
_CONNECTION_ENV_SUFFIX = "_SOURCE_URL"


def _is_connection_env(value: str) -> bool:
    return (
        value.startswith(_CONNECTION_ENV_PREFIX)
        and value.endswith(_CONNECTION_ENV_SUFFIX)
        and value.replace("_", "").isalnum()
        and value.upper() == value
    )


class SourceScopeError(ValueError):
    def __init__(self, message: str, *, stage: str = "S0") -> None:
        self.gate = f"G-{stage}-SOURCE-SCOPE"
        super().__init__(f"{self.gate}: {message}")


def _strings(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise SourceScopeError(f"{field} 必须为非空字符串列表。")
    return list(dict.fromkeys(v.strip() for v in value))


def _column_map(value: Any, field: str) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SourceScopeError(f"{field} 必须按表声明列范围。")
    if any(not isinstance(k, str) or not k.strip() for k in value):
        raise SourceScopeError(f"{field} 的表名必须为非空字符串。")
    return {k.strip(): _strings(v, field) for k, v in value.items()}


def _table(value: str) -> str:
    # Receipt schemas use both bare identifiers and PostgreSQL quoted targets.
    return str(value).strip().replace('"', "").replace("`", "")


def _table_matches(actual: str, allowed: str, schemas: list[str]) -> bool:
    actual, allowed = _table(actual), _table(allowed)
    if actual == allowed:
        return True
    if len(schemas) == 1:
        return (actual if "." in actual else f"{schemas[0]}.{actual}") == (
            allowed if "." in allowed else f"{schemas[0]}.{allowed}"
        )
    return False


def _path(value: str) -> str:
    value = str(value).strip().replace("\\", "/")
    if ".." in PurePosixPath(value).parts:
        raise SourceScopeError("资料路径不能包含上级目录跳转。")
    # Do not resolve paths or dereference symlinks here; ingestion owns that check.
    return value.rstrip("/")


def normalize_source_scope(
    *,
    intake_mode: str,
    datasource_label: str | None = None,
    table_scope: list[str] | None = None,
    source_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize declarations without inventing a source from labels or empty lists.

    Omitted scope retains old display/table hints and is UNRESOLVED. Validated
    receipts can later describe observed sources, but do not retroactively turn
    these hints into an explicit authorization. Explicit empty scope is fail closed.
    """
    mode = str(intake_mode).strip().upper()
    if mode not in MODES:
        raise SourceScopeError("未知接入模式。")
    if source_scope is not None and not isinstance(source_scope, dict):
        raise SourceScopeError("source_scope 必须为结构化对象。")
    raw = deepcopy(source_scope or {})
    normalized = raw.get("contract_version") == SOURCE_SCOPE_VERSION
    if normalized and raw.get("intake_mode") != mode:
        raise SourceScopeError("来源范围绑定的接入模式不一致。")
    supplied = source_scope is not None and raw.get("declaration") != "LEGACY_COMPATIBLE"
    if raw.get("contract_version") not in {None, SOURCE_SCOPE_VERSION}:
        raise SourceScopeError("不支持的来源范围合同版本。")
    permitted = {
        "sources",
        "excluded_source_refs",
        "contract_version",
        "intake_mode",
        "scope_status",
        "declaration",
        "legacy_hints",
        "unresolved",
        "business_goal",
    }
    if set(raw) - permitted:
        raise SourceScopeError("source_scope 含未知字段，不能静默忽略范围要求。")
    rows = raw.get("sources", [])
    if not isinstance(rows, list):
        raise SourceScopeError("sources 必须为来源列表。")
    sources = []
    seen = set()
    source_ids = set()
    source_fields = {
        "kind",
        "source_id",
        "source_path",
        "path_scope",
        "source_sha256",
        "source_name",
        "datasource_label",
        "database",
        "schemas",
        "authorized_tables",
        "table_scope",
        "authorized_columns",
        "excluded_tables",
        "excluded_columns",
    }
    for row in rows:
        if not isinstance(row, dict) or set(row) - source_fields:
            raise SourceScopeError("来源条目格式错误或含未知范围字段。")
        kind = str(row.get("kind", "")).strip().upper()
        if kind not in FILE_KINDS | {"DATABASE"}:
            raise SourceScopeError("来源 kind 必须为 DOCUMENT、STRUCTURED_FILE 或 DATABASE。")
        if mode == "DATABASE_ONLY" and kind in FILE_KINDS:
            raise SourceScopeError("DATABASE_ONLY 不允许文档或文件来源。")
        if mode == "DOCUMENT_ONLY" and kind == "DATABASE":
            raise SourceScopeError("DOCUMENT_ONLY 不允许外部数据库来源；表格导入应保留文件血缘。")
        file_fields = ("source_path", "path_scope", "source_sha256", "source_name")
        if kind == "DATABASE" and any(row.get(k) for k in file_fields):
            raise SourceScopeError("DATABASE 条目不能混入文件范围字段。")
        if kind in FILE_KINDS and any(
            row.get(k) for k in ("database", "datasource_label", "schemas")
        ):
            raise SourceScopeError("文件来源不能伪装成外部数据库范围。")
        if kind == "DOCUMENT" and any(
            row.get(k)
            for k in (
                "authorized_tables",
                "table_scope",
                "authorized_columns",
                "excluded_tables",
                "excluded_columns",
            )
        ):
            raise SourceScopeError("结构化表范围应声明为 STRUCTURED_FILE。")
        item = {"kind": kind}
        for key in (
            "source_id",
            "source_path",
            "source_sha256",
            "source_name",
            "datasource_label",
            "database",
        ):
            if row.get(key) is not None:
                if not isinstance(row[key], str) or not row[key].strip():
                    raise SourceScopeError(f"{key} 必须为非空字符串。")
                item[key] = row[key].strip()
        if kind in FILE_KINDS:
            if not any(item.get(k) for k in ("source_id", "source_path", "source_sha256")):
                raise SourceScopeError(
                    "文件来源需要受控路径、文档标识或来源哈希，文件名不能单独作为授权。"
                )
            item["path_scope"] = str(row.get("path_scope", "FILE")).upper()
            if item["path_scope"] not in {"FILE", "DIRECTORY"}:
                raise SourceScopeError("path_scope 必须为 FILE 或 DIRECTORY。")
            if item.get("source_path"):
                item["source_path"] = _path(item["source_path"])
                if not item["source_path"]:
                    raise SourceScopeError("不能把空路径或根路径作为资料范围。")
            if item["path_scope"] == "DIRECTORY" and not item.get("source_path"):
                raise SourceScopeError("目录范围必须提供 source_path。")
        elif not any(item.get(k) for k in ("source_id", "datasource_label", "database")):
            raise SourceScopeError("数据库来源缺少已知来源标识或库名，不能凭空构造。")
        item["schemas"] = _strings(row.get("schemas"), "schemas")
        item["authorized_tables"] = _strings(
            row.get("authorized_tables", row.get("table_scope")), "authorized_tables"
        )
        item["excluded_tables"] = _strings(row.get("excluded_tables"), "excluded_tables")
        item["authorized_columns"] = _column_map(
            row.get("authorized_columns"), "authorized_columns"
        )
        item["excluded_columns"] = _column_map(row.get("excluded_columns"), "excluded_columns")
        for name in item["authorized_columns"]:
            if not any(_table_matches(name, t, item["schemas"]) for t in item["authorized_tables"]):
                raise SourceScopeError("列授权引用了未声明的表。")
        identity = tuple(
            item.get(k, "")
            for k in (
                "kind",
                "source_id",
                "source_path",
                "source_sha256",
                "datasource_label",
                "database",
            )
        )
        if identity in seen:
            raise SourceScopeError("来源列表包含重复来源。")
        # One Chat2DB connection may expose several databases. Provider identity
        # is the connection plus database; formal source_id remains project-unique.
        source_identity = item.get("source_id")
        if str(source_identity or "").startswith("chat2db://"):
            source_identity = (source_identity, item.get("database"))
        if source_identity in source_ids:
            raise SourceScopeError("source_id 重复，不能把不同库合并成同一个来源。")
        if item.get("source_id"):
            source_ids.add(source_identity)
        seen.add(identity)
        sources.append(item)
    unresolved = []
    if mode != "DATABASE_ONLY" and not any(s["kind"] in FILE_KINDS for s in sources):
        unresolved.append("DOCUMENT_SOURCES")
    if mode != "DOCUMENT_ONLY" and not any(
        s["kind"] in {"DATABASE", "STRUCTURED_FILE"} for s in sources
    ):
        unresolved.append("STRUCTURED_SOURCES")
    if any(s["kind"] == "DATABASE" and not s["authorized_tables"] for s in sources):
        unresolved.append("DATABASE_TABLE_SCOPE")
    hints = raw.get("legacy_hints", {}) if normalized else {}
    business_goal = raw.get("business_goal")
    if business_goal is not None and (
        not isinstance(business_goal, str) or not business_goal.strip()
    ):
        raise SourceScopeError("business_goal 必须为非空业务目标说明。")
    return {
        "contract_version": SOURCE_SCOPE_VERSION,
        "intake_mode": mode,
        "business_goal": business_goal.strip() if business_goal else None,
        "declaration": "EXPLICIT" if supplied else "LEGACY_COMPATIBLE",
        "scope_status": "UNRESOLVED" if not sources else ("PARTIAL" if unresolved else "DECLARED"),
        "sources": sources,
        "excluded_source_refs": _strings(raw.get("excluded_source_refs"), "excluded_source_refs"),
        "legacy_hints": {
            "datasource_label": datasource_label or hints.get("datasource_label"),
            "table_scope": _strings(
                table_scope if table_scope is not None else hints.get("table_scope"), "table_scope"
            ),
        },
        "unresolved": unresolved,
    }


def _normalized(scope: dict[str, Any]) -> dict[str, Any]:
    return normalize_source_scope(intake_mode=scope.get("intake_mode", ""), source_scope=scope)


def reconcile_file_source_ids(
    scope: dict[str, Any], verified_sources: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove only invented IDs after proving unchanged, exact file selection."""
    normalized_scope = _normalized(scope)
    rows = scope.get("sources") or []
    keys = ("source_path", "source_sha256", "source_name", "kind")
    if not rows or len(rows) != len(verified_sources):
        raise SourceScopeError("现有文件声明必须与验证批次完整一对一，不能增加或删减来源。")
    observed = {item["source_path"]: item for item in verified_sources}
    if len(observed) != len(verified_sources):
        raise SourceScopeError("验证批次存在重复文件路径。")
    revised = deepcopy(scope)
    differences = []
    matched = set()
    for row in revised["sources"]:
        if row.get("kind") not in FILE_KINDS or row.get("path_scope") != "FILE":
            raise SourceScopeError("仅允许精确 FILE 声明的编号修复，数据库和目录范围不能变更。")
        actual = observed.get(row.get("source_path"))
        if (
            not actual
            or any(not row.get(key) or row[key] != actual[key] for key in keys)
            or row["source_path"] in matched
        ):
            raise SourceScopeError("文件 path、sha256、name、kind 必须完整且与验证清单一对一一致。")
        matched.add(row["source_path"])
        actual_id = f"DOC-{actual['source_sha256'].removeprefix('sha256:')[:16].upper()}"
        previous_id = row.get("source_id")
        if previous_id and previous_id != actual_id:
            _excluded(normalized_scope, {"source_id": previous_id}, "S0")
            row.pop("source_id")
            differences.append(
                {
                    "source_path": row["source_path"],
                    "previous_source_id": previous_id,
                    "source_id": None,
                    "verified_document_id": actual_id,
                    "source_sha256": row["source_sha256"],
                    "operation": "REMOVE_UNVERIFIED_SOURCE_ID",
                }
            )
    documents = [
        {**item, "document_id": f"DOC-{item['source_sha256'].removeprefix('sha256:')[:16].upper()}"}
        for item in verified_sources
    ]
    validate_document_sources(revised, documents)
    if not differences:
        raise SourceScopeError("没有需要修复的来源编号；现有声明保持不变。")
    return revised, differences


def _rows(value: Any, label: str, stage: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, dict) for v in value):
        raise SourceScopeError(f"{label} 必须为真实回执对象列表。", stage=stage)
    return value


def _file_refs(item: dict[str, Any]) -> set[str]:
    return {
        str(item[k])
        for k in (
            "source_id",
            "document_id",
            "s0_document_id",
            "source_path",
            "original_source_uri",
            "source_uri",
            "source_sha256",
        )
        if item.get(k)
    }


def _excluded(scope: dict[str, Any], item: dict[str, Any], stage: str) -> None:
    refs = _file_refs(item) | {
        str(item[k]) for k in ("database", "datasource_label", "id", "label", "connection_ref") if item.get(k)
    }
    for excluded in scope["excluded_source_refs"]:
        if excluded in refs or (
            excluded.endswith("/") and any(r.startswith(excluded) for r in refs)
        ):
            raise SourceScopeError("实际来源命中明确排除项。", stage=stage)


def _file_matches(declared: dict[str, Any], observed: dict[str, Any]) -> bool:
    for key in ("source_id", "source_sha256", "source_name"):
        if declared.get(key):
            values = (
                {
                    str(observed[k])
                    for k in ("source_id", "document_id", "s0_document_id")
                    if observed.get(k)
                }
                if key == "source_id"
                else {str(observed.get(key, ""))}
            )
            if declared[key] not in values:
                return False
    if declared.get("source_path"):
        paths = [
            _path(str(observed[k]))
            for k in ("source_path", "original_source_uri", "source_uri")
            if observed.get(k)
        ]
        expected = declared["source_path"]
        if not any(
            p == expected
            or (declared["path_scope"] == "DIRECTORY" and p.startswith(expected + "/"))
            for p in paths
        ):
            return False
    return True


def _match_one(
    scope: dict[str, Any],
    item: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    stage: str,
    file: bool,
) -> int:
    _excluded(scope, item, stage)
    matches = []
    for i, candidate in enumerate(candidates):
        if file and candidate.get("kind") not in FILE_KINDS:
            continue
        if not file and candidate.get("kind") != "DATABASE":
            continue
        if file:
            matched = _file_matches(candidate, item)
        else:
            # Identity keys decide the match; the datasource label is a display
            # name and only identifies a source when nothing stronger is declared.
            identity = [k for k in ("source_id", "database") if candidate.get(k)]
            keys = identity or (["datasource_label"] if candidate.get("datasource_label") else [])
            matched = bool(keys)
            for key in keys:
                if candidate.get(key):
                    values = {str(item.get(key, ""))}
                    if key == "datasource_label":
                        values |= {str(item.get(k, "")) for k in ("id", "label", "name")}
                    if key == "source_id" and _is_connection_env(str(candidate[key])):
                        # Declared by connection variable: the binding's
                        # connection_ref is the same identity.
                        ref = str(item.get("connection_ref") or "")
                        if ref == f"env://{candidate[key]}":
                            values.add(candidate[key])
                    if (key == "source_id" and str(candidate[key]).startswith("chat2db://community/")
                            and item.get("connection_ref") == candidate[key]):
                        values.add(candidate[key])
                    matched &= candidate[key] in values
        if matched:
            matches.append(i)
    if len(matches) != 1:
        declared = [
            {k: c.get(k) for k in ("source_id", "database", "datasource_label") if c.get(k)}
            for c in candidates
            if (c.get("kind") in FILE_KINDS) == file
        ]
        observed = {
            k: item.get(k)
            for k in ("source_id", "database", "connection_ref", "datasource_label", "source_path")
            if item.get(k)
        }
        raise SourceScopeError(
            "实际来源未被唯一授权或存在来源范围歧义。"
            f"已声明={declared}；实际={observed}。"
            "处理：capture 时 source_id 使用 S0 已声明的 source_id，"
            "或在 S0 以 database 库名/连接变量名声明该来源。",
            stage=stage,
        )
    return matches[0]


def validate_document_sources(
    scope: dict[str, Any],
    documents: Any,
    *,
    processing_trace: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Check S0 scope after ingestion's provenance/quality validation has passed."""
    scope = _normalized(scope)
    if isinstance(documents, dict):
        documents = documents.get("documents")
    rows = _rows(documents, "documents", "S0")
    if scope["intake_mode"] == "DATABASE_ONLY":
        if rows:
            raise SourceScopeError("DATABASE_ONLY 禁止提交文档来源。")
        return {"status": "NOT_APPLICABLE", "observed_sources": []}
    if not rows:
        raise SourceScopeError("S0 资料来源尚未实际闭合。")
    rows = list(rows)
    if processing_trace is not None:
        if not isinstance(processing_trace, dict):
            raise SourceScopeError("processing_trace 必须为平台执行回执对象。")
        invocations = _rows(processing_trace.get("tool_invocations"), "tool_invocations", "S0")
        for invocation in invocations:
            if invocation.get("tool") != "orion__batch__sha256_deduplicate":
                continue
            original = _path(str(invocation.get("reused_from") or ""))
            alias = _path(str(invocation.get("input") or ""))
            sha256 = invocation.get("source_sha256")
            matches = [
                d
                for d in rows
                if d.get("source_path") == original and d.get("source_sha256") == sha256
            ]
            if (
                invocation.get("status") != "SUCCEEDED"
                or not original
                or not alias
                or not sha256
                or len(matches) != 1
            ):
                raise SourceScopeError("去重别名缺少成功执行、原路径或同哈希的已登记资料依据。")
            existing_alias = [d for d in rows if d.get("source_path") == alias]
            if existing_alias:
                if any(d.get("source_sha256") != sha256 for d in existing_alias):
                    raise SourceScopeError("去重路径与已登记资料哈希冲突。")
                continue
            rows.append(
                {
                    **matches[0],
                    "source_path": alias,
                    "source_name": PurePosixPath(alias).name,
                    "alias_of": original,
                }
            )
    candidates = [s for s in scope["sources"] if s["kind"] in FILE_KINDS]
    matched = set()
    observed = []
    for row in rows:
        if not row.get("source_sha256") or not _file_refs(row) - {str(row.get("source_sha256"))}:
            raise SourceScopeError("资料缺少来源身份或哈希，不能由模型补造。")
        _excluded(scope, row, "S0")
        if scope["declaration"] == "EXPLICIT":
            matched.add(_match_one(scope, row, candidates, stage="S0", file=True))
        observed.append(
            {
                k: row[k]
                for k in (
                    "source_id",
                    "document_id",
                    "source_path",
                    "source_name",
                    "source_sha256",
                    "original_source_uri",
                    "alias_of",
                )
                if row.get(k)
            }
        )
    if scope["declaration"] == "EXPLICIT" and len(matched) != len(candidates):
        raise SourceScopeError("已声明的资料来源尚未全部处理。")
    return {
        "status": "PASSED",
        "scope_status": "VERIFIED" if scope["declaration"] == "EXPLICIT" else "OBSERVED",
        "observed_sources": observed,
    }


def _columns(table: dict[str, Any]) -> list[str]:
    raw = table.get("columns") or []
    return [
        str(c.get("name") or c.get("column_name") or c.get("source_name") or "")
        if isinstance(c, dict)
        else str(c)
        for c in raw
    ]


def _check_tables(declared: dict[str, Any], tables: list[dict[str, Any]]) -> None:
    schemas = declared["schemas"]
    allowed = declared["authorized_tables"]
    excluded = declared["excluded_tables"]
    if not allowed:
        raise SourceScopeError("数据库表范围尚未声明，空列表不代表全部授权。", stage="S1")
    names = set()
    for table in tables:
        name = str(table.get("source_table") or table.get("table") or table.get("name") or "")
        if not table.get("source_table") and table.get("schema") and "." not in _table(name):
            name = f"{table['schema']}.{name}"
        if not name:
            raise SourceScopeError("来源表缺少身份。", stage="S1")
        if "." in _table(name) and schemas and _table(name).rsplit(".", 1)[0] not in schemas:
            raise SourceScopeError("实际来源表超出 schema 范围。", stage="S1")
        if any(_table_matches(name, t, schemas) for t in excluded) or not any(
            _table_matches(name, t, schemas) for t in allowed
        ):
            raise SourceScopeError("实际来源表超出授权或命中排除项。", stage="S1")
        names.add(name)
        columns = set(_columns(table))
        for field in ("authorized_columns", "excluded_columns"):
            for scoped_table, scoped_columns in declared[field].items():
                if not _table_matches(name, scoped_table, schemas):
                    continue
                if not columns:
                    raise SourceScopeError("列范围校验缺少真实列画像。", stage="S1")
                if field == "authorized_columns" and not columns.issubset(scoped_columns):
                    raise SourceScopeError("实际来源列超出授权。", stage="S1")
                if field == "excluded_columns" and columns.intersection(scoped_columns):
                    raise SourceScopeError("实际来源列命中排除项。", stage="S1")
    effective = [t for t in allowed if not any(_table_matches(t, e, schemas) for e in excluded)]
    if not effective or any(
        not any(_table_matches(n, t, schemas) for n in names) for t in effective
    ):
        raise SourceScopeError("已声明库表范围尚未全部闭合。", stage="S1")


def validate_database_capture_scope(
    scope: dict[str, Any], binding: dict[str, Any], profile: dict[str, Any] | None = None,
) -> None:
    """Check one proposed capture before source registration or materialization.

    The first call checks identities/tables before reading source metadata; the
    second checks the actual retained columns after exclusions and profiling.
    This validates one source without requiring other sources to be captured in
    the same call. The existing S1 handoff still checks complete project coverage.
    """
    scope = _normalized(scope)
    _excluded(scope, binding, "S1")
    if scope["intake_mode"] == "DOCUMENT_ONLY":
        raise SourceScopeError("DOCUMENT_ONLY 禁止采集外部数据库。", stage="S1")
    if scope["declaration"] != "EXPLICIT":
        return  # Preserve legacy intake; do not pretend it is explicitly approved.
    candidates = [s for s in scope["sources"] if s["kind"] == "DATABASE"]
    index = _match_one(scope, binding, candidates, stage="S1", file=False)
    declared = candidates[index]
    if not declared.get("source_id") or declared.get("database") != binding.get("database"):
        raise SourceScopeError("正式采集必须同时声明来源编号（或 Chat2DB 引用）和数据库，名称标签不足以授权。", stage="S1")
    schemas = binding.get("schemas") or []
    if declared["schemas"] and not set(schemas).issubset(declared["schemas"]):
        raise SourceScopeError("采集来源超出已声明 Schema。", stage="S1")
    table_scope = {**declared, "schemas": declared["schemas"] or schemas}
    tables = [{"table": table} for table in binding.get("authorized_tables", [])]
    if profile is None:
        _check_tables({**table_scope, "authorized_columns": {}, "excluded_columns": {}}, tables)
        return
    for key in ("project_id", "source_id", "engine", "database"):
        if profile.get(key) != binding.get(key):
            raise SourceScopeError("真实画像与拟登记来源身份不一致。", stage="S1")
    actual = _rows(profile.get("tables"), "capture profile tables", "S1")
    if (set(t.get("table") for t in actual) != set(binding.get("authorized_tables", []))
            or len(actual) != len(tables)):
        raise SourceScopeError("真实画像与拟采集表范围不一致。", stage="S1")
    for table in actual:
        if set(_columns(table)) != set(binding.get("authorized_columns", {}).get(table["table"], [])):
            raise SourceScopeError("采集字段与实际保留的来源列不一致。", stage="S1")
    _check_tables(table_scope, actual)


def validate_database_sources(
    scope: dict[str, Any],
    data_understanding: dict[str, Any],
    *,
    documents: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Check S1 original-source coverage, not the SnapshotHub storage database.

    Pass S0 document-register as ``documents`` when a file-derived S1 inventory
    only retains a document ID/hash. Existing S1 gates still verify access mode,
    registered datasets, receipt authenticity, counts, and snapshot promotion.
    """
    scope = _normalized(scope)
    if not isinstance(data_understanding, dict):
        raise SourceScopeError("S1 载荷必须为对象。", stage="S1")
    payload = data_understanding.get("s1", data_understanding)
    if not isinstance(payload, dict):
        raise SourceScopeError("S1 handoff 必须为对象。", stage="S1")
    inventory = payload.get("datasource_inventory") or {}
    schema = payload.get("schema_snapshot") or {}
    if not isinstance(inventory, dict) or not isinstance(schema, dict | list):
        raise SourceScopeError("来源清单或 Schema 格式错误。", stage="S1")
    table_rows = _rows(
        schema if isinstance(schema, list) else schema.get("tables"), "schema tables", "S1"
    )
    profile = payload.get("data_profile") or {}
    if not isinstance(profile, dict):
        raise SourceScopeError("数据画像格式错误。", stage="S1")
    bindings = _rows(inventory.get("source_bindings"), "source_bindings", "S1")
    datasets = _rows(inventory.get("datasets"), "datasets", "S1")
    receipts = _rows(data_understanding.get("receipts"), "receipts", "S1")
    file_datasets = [d for d in datasets if d.get("document_id") or d.get("s0_document_id")]
    candidates = [s for s in scope["sources"] if s["kind"] in {"DATABASE", "STRUCTURED_FILE"}]
    matched = set()
    observed = []
    file_targets: set[str] = set()
    for dataset in file_datasets:
        if scope["intake_mode"] == "DATABASE_ONLY":
            raise SourceScopeError("DATABASE_ONLY 禁止混入文件导入数据。", stage="S1")
        source = dict(dataset)
        lineage = [*receipts, *(documents or [])]
        compatible = [
            d
            for d in lineage
            if d.get("source_sha256") == dataset.get("source_sha256")
            and (
                str(dataset.get("s0_document_id") or dataset.get("document_id")) in _file_refs(d)
                or d.get("dataset_id") == dataset.get("dataset_id")
            )
        ]
        if compatible:
            source = {**compatible[0], **{k: v for k, v in dataset.items() if v is not None}}
        if not source.get("source_sha256") or dataset.get("status") != "READY":
            raise SourceScopeError("文件导入缺少 READY 来源哈希回执。", stage="S1")
        _excluded(scope, source, "S1")
        if scope["declaration"] == "EXPLICIT":
            index = _match_one(scope, source, candidates, stage="S1", file=True)
            if candidates[index]["kind"] != "STRUCTURED_FILE":
                raise SourceScopeError("该来源未声明结构化文件导入。", stage="S1")
            matched.add(index)
        selected = [
            t
            for t in table_rows
            if t.get("dataset_id") == dataset.get("dataset_id")
            or str(t.get("source_ref", "")).startswith(f"dataset:{dataset['dataset_id']}:sheet:")
        ]
        if not selected:
            raise SourceScopeError("文件数据集缺少对应表和血缘。", stage="S1")
        file_targets.update(_table(t.get("table") or t.get("name") or "") for t in selected)
        if scope["declaration"] == "EXPLICIT" and candidates[index]["authorized_tables"]:
            # File table names refer to source sheets, not generated storage views.
            file_profiles = _rows(profile.get("tables"), "file table profiles", "S1")
            originals = []
            for table in selected:
                target = table.get("table") or table.get("name")
                matches = [p for p in file_profiles if (p.get("table") or p.get("name")) == target]
                original = matches[0] if len(matches) == 1 else table
                if (
                    original.get("source_sheet")
                    and table.get("source_sheet")
                    and original["source_sheet"] != table["source_sheet"]
                ):
                    raise SourceScopeError("文件工作表画像与 Schema 血缘不一致。", stage="S1")
                columns = [
                    str(c.get("source_name") or c.get("name") or c.get("column_name") or "")
                    if isinstance(c, dict)
                    else str(c)
                    for c in original.get("columns", [])
                ]
                originals.append(
                    {
                        **table,
                        "source_table": table.get("source_sheet") or table.get("source_table"),
                        "columns": columns,
                    }
                )
            _check_tables(
                candidates[index],
                originals,
            )
        observed.append(
            {
                "kind": "STRUCTURED_FILE",
                **{
                    k: source[k]
                    for k in (
                        "dataset_id",
                        "document_id",
                        "s0_document_id",
                        "source_path",
                        "source_sha256",
                    )
                    if source.get(k)
                },
            }
        )
    raw_sources = bindings or _rows(inventory.get("datasources"), "datasources", "S1")
    if not raw_sources and inventory.get("selected"):
        raw_sources = [
            {
                "datasource_label": str(inventory["selected"]),
                "database": schema.get("database") if isinstance(schema, dict) else None,
            }
        ]
    source_profiles = _rows(profile.get("source_profiles"), "source_profiles", "S1")
    external = []
    for raw in raw_sources:
        if (
            file_datasets
            and not bindings
            and raw.get("database") == "orion_source_data"
            and raw.get("id") == "orion_source_data"
        ):
            continue  # Proven file lineage, not an additional external source.
        source = {
            **raw,
            "source_id": raw.get("source_id") or raw.get("id"),
            "datasource_label": raw.get("datasource_label") or raw.get("label") or raw.get("name"),
        }
        _excluded(scope, source, "S1")
        external.append(source)
    if external and scope["intake_mode"] == "DOCUMENT_ONLY":
        raise SourceScopeError("DOCUMENT_ONLY 禁止外部数据库来源。", stage="S1")
    if any(
        p.get("source_id") not in {s.get("source_id") for s in external} for p in source_profiles
    ):
        raise SourceScopeError("源画像混入未登记的数据库来源。", stage="S1")
    covered_targets = set(file_targets)
    for source in external:
        if not any(source.get(k) for k in ("source_id", "datasource_label", "database")):
            raise SourceScopeError("数据库回执缺少可追溯来源身份。", stage="S1")
        index = None
        if scope["declaration"] == "EXPLICIT":
            index = _match_one(scope, source, candidates, stage="S1", file=False)
            matched.add(index)
            declared_schemas = candidates[index]["schemas"]
            if (
                declared_schemas
                and source.get("schemas")
                and not set(source["schemas"]).issubset(declared_schemas)
            ):
                raise SourceScopeError("SourceBinding 超出已声明的 schema 范围。", stage="S1")
        sid = source.get("source_id")
        selected = [t for t in table_rows if sid and t.get("source_id") == sid]
        if not selected and len(external) == 1:
            selected = [
                t
                for t in table_rows
                if not t.get("source_id")
                and _table(t.get("table") or t.get("name") or "") not in file_targets
            ]
        if not selected:
            raise SourceScopeError("来源没有对应表，或多库表缺少 source_id 血缘。", stage="S1")
        covered_targets.update(_table(t.get("table") or t.get("name") or "") for t in selected)
        originals = [
            t
            for p in source_profiles
            if p.get("source_id") == sid
            for t in _rows(p.get("tables"), "source profile tables", "S1")
        ]
        if index is not None:
            # Source profiles carry original columns; generated snapshot columns
            # (dataset_id,row_ordinal) must not be treated as business columns.
            tables = originals or selected
            candidate = {
                **candidates[index],
                "schemas": candidates[index]["schemas"] or source.get("schemas", []),
            }
            _check_tables(candidate, tables)
            # Every actually materialized table must also be within the allowlist.
            _check_tables({**candidate, "authorized_columns": {}, "excluded_columns": {}}, selected)
        observed.append(
            {
                "kind": "DATABASE",
                **{
                    k: source[k]
                    for k in ("source_id", "datasource_label", "database")
                    if source.get(k)
                },
                "tables": [
                    str(t.get("source_table") or t.get("table") or t.get("name")) for t in selected
                ],
            }
        )
    if table_rows and any(
        _table(t.get("table") or t.get("name") or "") not in covered_targets for t in table_rows
    ):
        raise SourceScopeError("存在无法归属到已登记来源的表。", stage="S1")
    if scope["declaration"] == "EXPLICIT":
        # Query receipts must not quietly read a table omitted from the schema
        # and therefore omitted from the per-source validation above.
        query_tables = [
            str(t)
            for e in _rows(payload.get("evidence_sql"), "evidence_sql", "S1")
            for t in e.get("source_tables", [])
        ]
        reported_tables = [*inventory.get("business_tables_scope", []), *query_tables]
        snapshot_schemas = schema.get("schemas", []) if isinstance(schema, dict) else []
        target_refs = set(covered_targets)
        target_refs.update(
            f"{t['schema']}.{_table(t.get('table') or t.get('name') or '')}"
            for t in table_rows
            if t.get("schema")
        )
        if any(
            not any(_table_matches(str(t), ref, snapshot_schemas) for ref in target_refs)
            for t in reported_tables
        ):
            raise SourceScopeError("清单或 SQL 回执引用了范围外或无血缘的表。", stage="S1")
    if not observed and scope["intake_mode"] != "DOCUMENT_ONLY":
        raise SourceScopeError("S1 结构化来源尚未实际闭合。", stage="S1")
    if scope["declaration"] == "EXPLICIT" and len(matched) != len(candidates):
        raise SourceScopeError("已声明的数据库或结构化文件来源尚未全部闭合。", stage="S1")
    return {
        "status": "PASSED" if observed else "NOT_APPLICABLE",
        "scope_status": "VERIFIED" if scope["declaration"] == "EXPLICIT" else "OBSERVED",
        "observed_sources": observed,
    }
