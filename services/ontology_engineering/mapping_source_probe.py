"""Check OBDA SQL sources against platform snapshot rules and the live catalog.

Static checks need no database. The live probe runs EXPLAIN for each source
inside a read-only transaction with the read-only principal, so a missing
table or column is reported with its mapping id before Ontop is started.
Neither check changes mapping semantics.
"""
from __future__ import annotations

import os
import re
from typing import Any

from services.ontology_contracts.errors import WorkflowGateError
from services.ontology_contracts.obda import (
    normalize_obda_block_separation,
    obda_structure_issues,
    parse_obda_blocks,
)

from .mapping_preflight import normalize_known_literal_datatypes

SNAPSHOT_SCHEMA = "orion_data"
_SNAPSHOT_REF = re.compile(
    r"\b(?:FROM|JOIN)\s+(?P<ref>(?:\"?[A-Za-z_]\w*\"?\s*\.\s*)?\"?ms_[A-Za-z0-9_]+\"?)",
    re.IGNORECASE,
)
_QUALIFIED = re.compile(r'^"?' + SNAPSHOT_SCHEMA + r'"?\s*\.')
_DATASET_FILTER = re.compile(r"\bdataset_id\s*=\s*'DS-[A-Za-z0-9_-]+'", re.IGNORECASE)


def snapshot_source_issues(mapping_obda: str) -> list[dict[str, str]]:
    """Structural and Snapshot Hub isolation defects, keyed by mapping id."""
    issues = list(obda_structure_issues(mapping_obda))
    for block in parse_obda_blocks(mapping_obda):
        sql = block.get("source") or ""
        mapping_id = block.get("mapping_id") or "(missing mappingId)"
        refs = [match.group("ref") for match in _SNAPSHOT_REF.finditer(sql)]
        if not refs:
            continue
        unqualified = [ref for ref in refs if not _QUALIFIED.match(ref)]
        if unqualified:
            issues.append({
                "mapping_id": mapping_id, "code": "SNAPSHOT_TABLE_UNQUALIFIED",
                "message": f"快照表必须写成 {SNAPSHOT_SCHEMA}.<表名>："
                + ", ".join(sorted(set(unqualified))),
            })
        if len(_DATASET_FILTER.findall(sql)) < len(refs):
            issues.append({
                "mapping_id": mapping_id, "code": "SNAPSHOT_DATASET_FILTER_MISSING",
                "message": "每个引用的快照表都需要 dataset_id='DS-…' 过滤，否则会混入其他数据集版本的行。",
            })
    return issues


def probe_obda_sources(mapping_obda: str, reader_url: str, *, timeout_ms: int = 15000) -> list[dict[str, str]]:
    """EXPLAIN every source with the read-only principal; return failures."""
    import psycopg

    url = str(reader_url or "").replace("postgresql+psycopg://", "postgresql://", 1)
    failures: list[dict[str, str]] = []
    with psycopg.connect(url, connect_timeout=10) as connection:
        for block in parse_obda_blocks(mapping_obda):
            sql = (block.get("source") or "").strip().rstrip(";")
            if not sql:
                continue
            try:
                with connection.transaction(), connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(f"SET LOCAL statement_timeout = {int(timeout_ms)}")
                    cursor.execute("EXPLAIN " + sql)
            except psycopg.Error as exc:
                primary = getattr(getattr(exc, "diag", None), "message_primary", None)
                message = str(primary or exc).splitlines()[0][:300]
                failures.append({
                    "mapping_id": block.get("mapping_id") or "(missing mappingId)",
                    "code": "SOURCE_SQL_REJECTED",
                    "sqlstate": exc.sqlstate or "unknown",
                    "message": message,
                })
    return failures


def mapping_source_report(mapping_obda: str, reader_url: str | None) -> dict[str, Any]:
    issues = snapshot_source_issues(mapping_obda)
    live: dict[str, Any] = {"status": "SKIPPED_NO_READER"}
    if reader_url:
        try:
            failures = probe_obda_sources(mapping_obda, reader_url)
            live = {"status": "PASSED" if not failures else "FAILED", "failures": failures}
            issues.extend(failures)
        except Exception as exc:  # connection problems are reported, never hidden as success
            live = {"status": "UNAVAILABLE", "message": str(exc).splitlines()[0][:300]}
    return {"issues": issues, "live_probe": live,
            "failing_mapping_ids": sorted({str(item["mapping_id"]) for item in issues})}


def format_source_issues(issues: list[dict[str, str]], limit: int = 12) -> str:
    parts = [f"{item['mapping_id']}[{item['code']}]: {item['message']}" for item in issues[:limit]]
    if len(issues) > limit:
        parts.append(f"… 另有 {len(issues) - limit} 项")
    return "；".join(parts)


def failing_source_lines(mapping_obda: str, mapping_ids: list[str], limit: int = 8) -> list[dict[str, str]]:
    """Exact source text of failing blocks, usable as replace_text 'old' values.

    The agent can then edit /realtime_runtime/mapping_obda with a unique
    replace_text instead of paging through the whole OBDA document.
    """
    wanted = set(mapping_ids)
    text = str(mapping_obda or "")
    result: list[dict[str, str]] = []
    blocks: list[list[str]] = []
    for line in text.split("\n"):
        if re.match(r"^\s*mappingId\s", line) or not blocks:
            blocks.append([])
        blocks[-1].append(line)
    for lines in blocks:
        head = lines[0]
        if not re.match(r"^\s*mappingId\s", head):
            continue
        mapping_id = re.sub(r"^\s*mappingId\s+", "", head).strip()
        if mapping_id not in wanted:
            continue
        start = next((i for i, line in enumerate(lines) if re.match(r"^\s*source\s", line)), None)
        if start is None:
            continue
        tail: list[str] = []
        for line in lines[start:]:
            if not line.strip() or line.strip() == "]]":
                break
            tail.append(line)
        source_text = "\n".join(tail)
        result.append({
            "mapping_id": mapping_id,
            "occurrences": str(text.count(source_text)),
            "source_text": source_text[:1200],
        })
        if len(result) >= limit:
            break
    return result


def normalize_s3_obda(mapping_obda: str, *, check_sources: bool = False) -> tuple[str, list[dict[str, str]]]:
    """Apply the purely syntactic S3 OBDA compilers, then optionally gate SQL sources."""
    separated, separated_ids = normalize_obda_block_separation(mapping_obda)
    normalized, decisions = normalize_known_literal_datatypes(separated)
    if separated_ids:
        decisions = [*decisions, {
            "compiler": "OBDA_BLOCK_SEPARATION",
            "mapping_ids": ",".join(separated_ids),
            "occurrence_count": str(len(separated_ids)),
        }]
    if check_sources:
        require_s3_source_sql(normalized)
    return normalized, decisions


def require_s3_source_sql(mapping_obda: str) -> None:
    """Reject S3 SQL sources that Ontop would fail on at S6, naming each mappingId."""
    report = mapping_source_report(
        mapping_obda, os.getenv("ORION_SOURCE_DATA_READER_URL", "").strip() or None
    )
    if report["issues"]:
        sources = failing_source_lines(mapping_obda, report["failing_mapping_ids"])
        hint = ""
        if sources:
            hint = (
                "。修正方式：patch_stage_submission 对 path=/realtime_runtime/mapping_obda 使用 replace_text，"
                "old 取下列原文（occurrences=1 时可直接唯一替换），new 为修正后的 source 行："
                + "；".join(
                    f"{item['mapping_id']} (occurrences={item['occurrences']}) ⇒ {item['source_text']}"
                    for item in sources
                )
            )
        raise WorkflowGateError(
            "G-S3-SOURCE-SQL",
            "映射 SQL 未通过快照来源校验，请按 mappingId 修正后重新预检："
            + format_source_issues(report["issues"]) + hint,
            path="/realtime_runtime/mapping_obda",
            reason_code="SNAPSHOT_SOURCE_SQL",
        )
