"""Wren is isolated from production credentials and receives only verified rows."""
from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from services.realtime_qa.json_values import query_json_scalar
from services.realtime_qa.rdf_results import typed_result_rows
from services.realtime_qa.result_pages import ResultPageRequest, result_page
from services.realtime_qa.wren_models import AnalysisRequest, AnalysisSource

ROOT = Path(__file__).resolve().parents[2]
WREN_VERSION = "0.15.0"
WREN_PYTHON = Path(os.environ.get("ORION_WREN_PYTHON") or ROOT / ".orion-runtime/wren" / WREN_VERSION / ".venv/bin/python")
MAX_SOURCE_ROWS = 1000
MAX_SOURCE_BYTES = 2 * 1024 * 1024
_SLOTS = threading.BoundedSemaphore(2)


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode()).hexdigest()


def load_source(store, runtime, request: AnalysisSource) -> dict:
    """Reuse the existing receipt/session/release checks and bounded pagination."""
    identity = request.model_dump(include=set(AnalysisSource.model_fields))
    identity["query_id"] = identity.pop("source_query_id")
    page_request = ResultPageRequest.model_validate({**identity, "limit": 50})
    rows, analysis_rows, byte_count, first = [], [], 0, None
    datatypes = {}
    while True:
        page = result_page(store, runtime, page_request)
        if page.get("selection_required"):
            raise ValueError("回执含多个结果，请先指定 capability_name 和 source_question_id。")
        if not page["bundle_complete"] or page.get("truncated"):
            raise ValueError("来源结果不完整或已截断，不能进行全量分析；请重新取得完整结果。")
        if page["total"] > MAX_SOURCE_ROWS:
            raise ValueError("第一版分析最多接受 1000 条完整结果；请先用受控查询缩小业务范围。")
        if page.get("reported_row_count") not in {None, page["total"]}:
            raise ValueError("来源声明数量与回执结果数量不一致，不能分析。")
        first = first or {key: value for key, value in page.items() if key not in {"rows", "row_terms"}}
        rows.extend(page["rows"])
        if page.get("rdf_lexical_rows"):
            decoded = typed_result_rows(page)
            analysis_rows.extend(json.loads(json.dumps(decoded, default=query_json_scalar, allow_nan=False)))
            for terms in page["row_terms"]:
                for name, term in terms.items():
                    datatypes.setdefault(name, set()).add(term.get("datatype") or term["type"])
        else:
            analysis_rows.extend(page["rows"])
        byte_count += len(json.dumps([page["rows"], page.get("row_terms")], ensure_ascii=False).encode())
        if byte_count > MAX_SOURCE_BYTES:
            raise ValueError("来源结果超过分析大小限制。")
        if page["next_offset"] is None:
            break
        if page["next_offset"] <= page_request.offset:
            raise ValueError("来源结果分页未前进。")
        page_request = page_request.model_copy(update={"offset": page["next_offset"]})
    columns = describe_columns(analysis_rows)
    return {"rows": rows, "analysis_rows": analysis_rows, "columns": columns, "source": first,
            "rows_sha256": digest(rows), "type_decoding": {
                "basis": "RDF_LITERAL_METADATA" if first.get("rdf_lexical_rows") else "JSON_VALUE_TYPES",
                "datatypes": {key: sorted(value) for key, value in datatypes.items()}}}


def describe_columns(rows: list[dict]) -> list[dict]:
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("仅支持具有命名字段的结果行。")
    names = sorted({key for row in rows for key in row})
    if len(names) > 40 or any(not isinstance(key, str) or not 1 <= len(key) <= 128 or "\x00" in key for key in names):
        raise ValueError("分析结果最多包含 40 个有效字段。")
    columns = []
    for index, name in enumerate(names):
        values = [row[name] for row in rows if row.get(name) is not None]
        kinds = {type(value) for value in values}
        if not kinds:
            kind = "unknown"
        elif kinds == {bool}:
            kind = "boolean"
        elif kinds <= {int, float}:
            if any((isinstance(value, int) and not -(2**63) <= value < 2**63) or not math.isfinite(value) for value in values):
                raise ValueError(f"字段 {name} 含无法精确支持的数值。")
            kind = "double" if float in kinds else "bigint"
        elif kinds == {str}:
            kind = "varchar"
        else:
            raise ValueError(f"字段 {name} 含嵌套值或混合类型，须先明确字段类型。")
        columns.append({"field": name, "column": f"c{index}", "type": kind,
                        "null_count": len(rows) - len(values)})
    return columns


def run_analysis(source: dict, request: AnalysisRequest) -> dict:
    if not WREN_PYTHON.is_file():
        raise RuntimeError("Wren 分析环境未安装，请运行 make wren-runtime。")
    if not _SLOTS.acquire(timeout=1):
        raise RuntimeError("分析任务正在执行，请稍后重试。")
    try:
        # No inherited database/cloud/model credentials or user-selected paths.
        environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR") if key in os.environ}
        environment.update(PYTHONNOUSERSITE="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
        worker = Path(__file__).with_name("wren_worker.py")
        payload = {"rows": source["analysis_rows"], "columns": source["columns"],
                   "plan": request.model_dump(include={"dimensions", "metrics", "filters", "limit"})}
        try:
            result = subprocess.run([str(WREN_PYTHON), "-I", str(worker)],
                input=json.dumps(payload, ensure_ascii=False, allow_nan=False), text=True,
                capture_output=True, timeout=25, env=environment, check=False)
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("Wren 分析超过 25 秒执行预算。") from exc
        if result.returncode:
            raise RuntimeError("Wren 分析进程未成功完成。")
        if len(result.stdout.encode()) > 4 * 1024 * 1024:
            raise ValueError("分析结果超过大小限制。")
        output = json.loads(result.stdout)
        if output.get("error"):
            raise ValueError(output["error"][:1500])
        if output.get("engine_version") != WREN_VERSION:
            raise RuntimeError("Wren 运行版本与固定版本不一致。")
        return output
    finally:
        _SLOTS.release()
