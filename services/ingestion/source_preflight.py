from __future__ import annotations

import argparse
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

TABULAR_SUFFIXES = frozenset({".xlsx", ".xlsm", ".csv", ".tsv"})
NARRATIVE_SUFFIXES = frozenset(
    {
        ".pdf",
        ".docx",
        ".txt",
        ".md",
        ".html",
        ".htm",
        ".xml",
        ".json",
        ".yaml",
        ".yml",
        ".eml",
        ".png",
        ".jpg",
        ".jpeg",
        ".tif",
        ".tiff",
        ".bmp",
        ".webp",
    }
)
SUPPORTED_SUFFIXES = TABULAR_SUFFIXES | NARRATIVE_SUFFIXES
DEFAULT_MAX_DOCUMENT_ROWS = 10_000
DEFAULT_LARGE_TABULAR_BYTES = 10 * 1024 * 1024
CELL_REFERENCE_ROW = re.compile(r"[A-Z]+([0-9]+)$", re.IGNORECASE)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _collect_files(source: Path) -> list[Path]:
    root = source if source.is_dir() else source.parent
    if source.is_file():
        candidates = [source]
    elif source.is_dir():
        candidates = [path for path in source.rglob("*") if path.is_file()]
    else:
        raise ValueError("资料来源不存在或不可读取。")
    return sorted(
        (
            path
            for path in candidates
            if path.suffix.lower() in SUPPORTED_SUFFIXES
            and path != root / "manifest.json"
            and not any(part.startswith(".") for part in path.relative_to(root).parts)
        ),
        key=lambda path: path.as_posix().lower(),
    )


def _xlsx_dimensions(path: Path) -> tuple[int, int, str]:
    rows = 0
    sheets = 0
    with zipfile.ZipFile(path) as archive:
        worksheet_names = sorted(
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        )
        for name in worksheet_names:
            sheets += 1
            sheet_rows = 0
            with archive.open(name) as stream:
                for _event, element in ElementTree.iterparse(stream, events=("start",)):
                    tag = element.tag.rsplit("}", 1)[-1]
                    if tag == "dimension":
                        reference = str(element.attrib.get("ref") or "")
                        last_cell = reference.split(":")[-1]
                        match = CELL_REFERENCE_ROW.search(last_cell)
                        sheet_rows = int(match.group(1)) if match else 0
                        break
                    if tag == "sheetData":
                        break
            rows += sheet_rows
    return rows, sheets, "OPENXML_WORKSHEET_DIMENSION"


def _delimited_rows(path: Path, limit: int) -> tuple[int, str]:
    row_count = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            row_count += chunk.count(b"\n")
            if row_count > limit:
                return row_count, "LOWER_BOUND_ABOVE_THRESHOLD"
    if path.stat().st_size and row_count == 0:
        row_count = 1
    return row_count, "EXACT_NEWLINE_COUNT"


def inspect_source(
    source: Path,
    *,
    max_document_rows: int = DEFAULT_MAX_DOCUMENT_ROWS,
    large_tabular_bytes: int = DEFAULT_LARGE_TABULAR_BYTES,
) -> dict[str, Any]:
    source = source.resolve()
    files = _collect_files(source)
    root = source if source.is_dir() else source.parent
    inspected: list[dict[str, Any]] = []
    total_tabular_rows = 0
    tabular_count = 0
    narrative_count = 0
    large_tabular_paths: list[str] = []

    for path in files:
        suffix = path.suffix.lower()
        relative_path = path.relative_to(root).as_posix()
        size_bytes = path.stat().st_size
        item: dict[str, Any] = {
            "relative_path": relative_path,
            "source_type": suffix.removeprefix(".").upper(),
            "size_bytes": size_bytes,
            "kind": "TABULAR" if suffix in TABULAR_SUFFIXES else "NARRATIVE",
        }
        if suffix in TABULAR_SUFFIXES:
            tabular_count += 1
            try:
                if suffix in {".xlsx", ".xlsm"}:
                    rows, sheets, method = _xlsx_dimensions(path)
                    item["sheet_count"] = sheets
                else:
                    rows, method = _delimited_rows(path, max_document_rows)
                    item["sheet_count"] = 1
                item["row_count_estimate"] = rows
                item["row_count_method"] = method
                total_tabular_rows += rows
                is_large = rows > max_document_rows or size_bytes > large_tabular_bytes
            except (OSError, ValueError, zipfile.BadZipFile, ElementTree.ParseError) as error:
                item["inspection_error"] = str(error)
                is_large = size_bytes > large_tabular_bytes
            item["requires_structured_import"] = is_large
            if is_large:
                large_tabular_paths.append(relative_path)
        else:
            narrative_count += 1
        inspected.append(item)

    requires_import = bool(large_tabular_paths)
    if requires_import:
        recommended_mode = "HYBRID"
        recommended_action = "IMPORT"
        reasons = [
            "检测到超出 S0 文档样本上限或大文件阈值的表格，必须进入 PostgreSQL 结构化数据区并执行 S1。"
        ]
        if narrative_count:
            reasons.append(
                "同一批次还包含 Word/PDF 等叙述资料，文档证据与结构化数据需要在 S2 汇合。"
            )
    else:
        recommended_mode = "DOCUMENT_ONLY"
        recommended_action = "DOCUMENT_ONLY"
        reasons = ["未检测到需要全量结构化导入的大表；当前批次可以仅作为 S0 文档证据处理。"]

    return {
        "schema_version": 1,
        "inspected_at": _now(),
        "source": source.name,
        "file_count": len(inspected),
        "tabular_file_count": tabular_count,
        "narrative_file_count": narrative_count,
        "total_tabular_rows_estimate": total_tabular_rows,
        "large_tabular_paths": large_tabular_paths,
        "requires_structured_import": requires_import,
        "recommended_intake_mode": recommended_mode,
        "recommended_structured_data_action": recommended_action,
        "reasons": reasons,
        "thresholds": {
            "max_document_rows_per_sheet": max_document_rows,
            "large_tabular_bytes": large_tabular_bytes,
        },
        "files": inspected,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="ORION S0 数据源预检与接入路线判定")
    parser.add_argument("--source", required=True)
    parser.add_argument("--max-document-rows", type=int, default=DEFAULT_MAX_DOCUMENT_ROWS)
    parser.add_argument("--large-tabular-bytes", type=int, default=DEFAULT_LARGE_TABULAR_BYTES)
    arguments = parser.parse_args()
    result = inspect_source(
        Path(arguments.source),
        max_document_rows=max(1, arguments.max_document_rows),
        large_tabular_bytes=max(1, arguments.large_tabular_bytes),
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
