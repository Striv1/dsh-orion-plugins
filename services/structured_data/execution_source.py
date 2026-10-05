"""Resolve reviewed S1 file imports to immutable, catalog-verifiable versions.

File imports and database captures use different physical metadata columns.
Keep that distinction at the source boundary, not in model-authored SQL. This
adapter is pure and never changes an existing S1 checkpoint or current view.
"""
from __future__ import annotations

import copy
import re

FILE_SYSTEM_COLUMNS = {"_orion_dataset_id", "_orion_source_row"}


def execution_schema(schema, inventory, *, project_id):
    result = copy.deepcopy(schema)
    for table in result.get("tables", []):
        if not table.get("physical_version_table"):
            continue  # SnapshotHub already supplies canonical version bindings.
        identity = re.fullmatch(r"dataset:(DS-[A-F0-9]{20}):sheet:(.+)", str(table.get("source_ref") or ""))
        if not identity:
            raise ValueError("文件来源缺少正式 S1 dataset/sheet 身份，不能编译当前视图。")
        dataset_id, sheet = identity.groups()
        datasets = [d for d in (inventory or {}).get("datasets", []) if d.get("dataset_id") == dataset_id]
        if (len(datasets) != 1 or datasets[0].get("project_id") != project_id
                or datasets[0].get("status") != "READY" or not datasets[0].get("document_id")
                or not re.fullmatch(r"(?:sha256:)?[a-f0-9]{64}", str(datasets[0].get("source_sha256") or ""))):
            raise ValueError("文件来源必须匹配当前工程正式 S1 的 READY 数据集、文档身份和 SHA-256。")
        sources = [s for s in (inventory or {}).get("datasources", [])
                   if s.get("id") and s.get("database") == schema.get("database")
                   and s.get("access_mode") == "READ_ONLY_AFTER_IMPORT"]
        if len(sources) != 1:
            raise ValueError("文件来源须匹配唯一的已登记只读导入数据区。")
        source_id = sources[0]["id"]
        logical = table.get("source_table") or table.get("name") or table.get("table")
        physical = table["physical_version_table"]
        if (not isinstance(logical, str) or not re.fullmatch(r"current_[a-z0-9_]{1,55}", logical)
                or not re.fullmatch(r"ds_" + dataset_id[3:15].lower() + r"_\d{3}", str(physical))
                or (table.get("name") or table.get("table")) not in {logical, physical}
                or table.get("schema") != "orion_data" or table.get("source_sheet") != sheet
                or (table.get("source_id") and table["source_id"] != source_id)
                or (table.get("dataset_id") and table["dataset_id"] != dataset_id)):
            raise ValueError("文件版本表、逻辑表、来源与数据集身份不一致；不能读取可变当前视图。")
        columns = [c if isinstance(c, str) else c.get("column_name", c.get("name")) for c in table.get("columns", [])]
        if not columns or any(not isinstance(c, str) for c in columns):
            raise ValueError("文件版本表缺少已登记列。")
        dataset = datasets[0]
        table.update(name=physical, source_table=logical, source_id=source_id, dataset_id=dataset_id,
                     source_kind="STRUCTURED_FILE", dataset_column="_orion_dataset_id",
                     columns=list(dict.fromkeys([*columns, *sorted(FILE_SYSTEM_COLUMNS)])),
                     file_import={"document_id": dataset["document_id"], "source_sheet": sheet,
                                  "source_sha256": dataset["source_sha256"],
                                  "imported_at": dataset.get("imported_at"),
                                  "source_name": dataset.get("source_name")})
    return result


def dataset_column(table):
    """Return only the metadata column belonging to this controlled table kind."""
    name, dataset = str(table.get("name") or ""), table.get("dataset_id")
    if not isinstance(dataset, str) or not re.fullmatch(r"DS-[A-Za-z0-9_-]+", dataset):
        raise ValueError("需要已登记的受控版本表与 dataset_id，不能直接使用业务源表。")
    if name.startswith("ms_") and not table.get("physical_version_table"):
        column = "dataset_id"
    elif (table.get("source_kind") == "STRUCTURED_FILE" and table.get("file_import")
          and re.fullmatch(r"ds_" + dataset[3:15].lower() + r"_\d{3}", name)
          and name == table.get("physical_version_table")):
        column = "_orion_dataset_id"
    else:
        raise ValueError("需要 S1 登记的数据库快照或不可变文件版本；不能读取业务源表或 current_ 视图。")
    if column not in table.get("columns", []):
        raise ValueError("受控版本缺少数据集隔离列。")
    return column
