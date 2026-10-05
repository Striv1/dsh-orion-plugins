from __future__ import annotations

import zipfile
from pathlib import Path

from services.ingestion.source_preflight import inspect_source


def write_xlsx_with_dimension(path: Path, dimension: str) -> None:
    worksheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="{dimension}"/><sheetData/></worksheet>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/worksheets/sheet1.xml", worksheet)


def test_large_excel_and_narrative_files_require_hybrid_import(tmp_path: Path) -> None:
    write_xlsx_with_dimension(tmp_path / "批次数据.xlsx", "A1:R150630")
    (tmp_path / "需求说明.docx").write_bytes(b"placeholder")
    (tmp_path / "判定流程.pdf").write_bytes(b"placeholder")

    result = inspect_source(tmp_path)

    assert result["recommended_intake_mode"] == "HYBRID"
    assert result["recommended_structured_data_action"] == "IMPORT"
    assert result["requires_structured_import"] is True
    assert result["total_tabular_rows_estimate"] == 150630
    assert result["tabular_file_count"] == 1
    assert result["narrative_file_count"] == 2
    assert result["large_tabular_paths"] == ["批次数据.xlsx"]


def test_small_excel_can_remain_document_only(tmp_path: Path) -> None:
    write_xlsx_with_dimension(tmp_path / "小型参数表.xlsx", "A1:F120")

    result = inspect_source(tmp_path)

    assert result["recommended_intake_mode"] == "DOCUMENT_ONLY"
    assert result["recommended_structured_data_action"] == "DOCUMENT_ONLY"
    assert result["requires_structured_import"] is False


def test_large_csv_stops_after_proving_threshold(tmp_path: Path) -> None:
    source = tmp_path / "明细.csv"
    source.write_text("id,value\n" + "\n".join(f"{index},x" for index in range(10_050)))

    result = inspect_source(tmp_path)
    inspected = result["files"][0]

    assert result["requires_structured_import"] is True
    assert inspected["row_count_estimate"] > 10_000
    assert inspected["row_count_method"] == "LOWER_BOUND_ABOVE_THRESHOLD"
