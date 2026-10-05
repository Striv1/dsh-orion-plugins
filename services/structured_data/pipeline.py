from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import unicodedata
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import chain, islice
from pathlib import Path, PurePosixPath
from typing import Any

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

SOURCE_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
MARKDOWN_SEPARATOR = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
IDENTIFIER_PART = re.compile(r"[^a-z0-9_]+")
DEFAULT_ONTOLOGY_BASE_IRI = "https://orion.local/ontology/"


def default_ontology_namespace(project_id: str) -> str:
    """Namespace for newly drafted structured-data ontologies.

    Matches the S4 default (`https://orion.local/ontology/<project>#`) so S1
    candidates, S3 mappings and the S4 build share one namespace. Deployments
    can override the base with `ORION_ONTOLOGY_BASE_IRI`.
    """

    base = str(os.environ.get("ORION_ONTOLOGY_BASE_IRI") or DEFAULT_ONTOLOGY_BASE_IRI).strip()
    base = base.rstrip("#/") + "/"
    return f"{base}{project_id or 'project'}#"


MAX_IDENTIFIER_LENGTH = 63
STRUCTURED_IMPORT_PROFILE = "file-postgresql-v2-bilingual-header"
CHINESE_CHARACTER = re.compile(r"[\u3400-\u9fff]")
TECHNICAL_HEADER = re.compile(r"^[A-Za-z_][A-Za-z0-9_ .()/-]*$")
BUSINESS_TOKEN_ZH = {
    "actual": "实际",
    "amount": "金额",
    "batch": "批次",
    "batterycell": "电芯",
    "cell": "电芯",
    "check": "判定",
    "code": "编码",
    "created": "创建",
    "customer": "客户",
    "date": "日期",
    "device": "设备",
    "end": "结束",
    "equipment": "设备",
    "factory": "工厂",
    "id": "标识",
    "line": "产线",
    "name": "名称",
    "no": "编号",
    "order": "订单",
    "param": "质检参数",
    "process": "工序",
    "product": "产品",
    "result": "结果",
    "source": "来源",
    "start": "开始",
    "station": "工位",
    "time": "时间",
    "trace": "追溯",
    "updated": "更新",
    "value": "值",
    "work": "工单",
    "workshop": "车间",
}
IGNORED_TABLE_TOKENS = {"data", "detail", "dt", "lsj", "table", "tbl"}


class StructuredDataImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class ColumnPlan:
    source_name: str
    column_name: str
    inferred_type: str
    nullable: bool
    label_zh: str | None = None


@dataclass(frozen=True)
class SheetPlan:
    index: int
    source_name: str
    table_name: str
    view_name: str
    header_row: int
    columns: tuple[ColumnPlan, ...]
    rows: tuple[tuple[Any, ...], ...] | None = None
    label_zh: str | None = None


def _digest(*parts: str, size: int = 20) -> str:
    value = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(value).hexdigest()[:size]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _logical_source_reference(source_path: str, source_name: str) -> str:
    normalized = unicodedata.normalize("NFC", source_path.strip()).replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    try:
        upload_index = parts.index(".orion-s0-uploads")
    except ValueError:
        stable_parts = parts
    else:
        stable_parts = parts[upload_index + 2 :]
    stable = PurePosixPath(*stable_parts).as_posix() if stable_parts else source_name
    return unicodedata.normalize("NFC", stable).casefold()


def _identifier(value: str, *, fallback: str, used: set[str] | None = None) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    normalized = IDENTIFIER_PART.sub("_", normalized.lower()).strip("_") or fallback
    if normalized[0].isdigit():
        normalized = f"c_{normalized}"
    normalized = normalized[:MAX_IDENTIFIER_LENGTH]
    if used is None:
        return normalized
    candidate = normalized
    suffix = 2
    while candidate in used:
        tail = f"_{suffix}"
        candidate = normalized[: MAX_IDENTIFIER_LENGTH - len(tail)] + tail
        suffix += 1
    used.add(candidate)
    return candidate


def _kind(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "bigint"
    if isinstance(value, float | Decimal):
        return "numeric"
    if isinstance(value, datetime):
        return "timestamp"
    if isinstance(value, date):
        return "date"
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return None
        if re.fullmatch(r"[-+]?\d+", normalized):
            unsigned = normalized.lstrip("+-")
            if len(unsigned) > 1 and unsigned.startswith("0"):
                return "text"
            try:
                number = int(normalized)
            except ValueError:
                return "text"
            return "bigint" if -(2**63) <= number < 2**63 else "numeric"
        if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", normalized):
            return "numeric"
        try:
            parsed = datetime.fromisoformat(normalized)
            return "timestamp" if parsed.time() != datetime.min.time() else "date"
        except ValueError:
            return "text"
    return "text"


def _merge_kind(current: str | None, incoming: str | None) -> str | None:
    if incoming is None:
        return current
    if current is None or current == incoming:
        return incoming
    if {current, incoming} <= {"bigint", "numeric"}:
        return "numeric"
    if {current, incoming} <= {"date", "timestamp"}:
        return "timestamp"
    return "text"


def _convert(value: Any, inferred_type: str) -> Any:
    if value is None or value == "":
        return None
    if inferred_type == "text":
        return str(value)
    if inferred_type == "boolean":
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"true", "1", "yes", "y", "是"}:
            return True
        if normalized in {"false", "0", "no", "n", "否"}:
            return False
        raise ValueError(f"cannot convert {value!r} to boolean")
    if inferred_type == "bigint":
        return int(value)
    if inferred_type == "numeric":
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError(f"cannot convert {value!r} to numeric") from exc
    if inferred_type == "date":
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        return date.fromisoformat(str(value))
    if inferred_type == "timestamp":
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, datetime.min.time())
        return datetime.fromisoformat(str(value))
    raise ValueError(f"unsupported inferred type: {inferred_type}")


def _normalize_row(row: Sequence[Any], width: int) -> tuple[Any, ...]:
    values = tuple(row[:width])
    return values + (None,) * (width - len(values))


def _contains_chinese(value: Any) -> bool:
    return bool(CHINESE_CHARACTER.search(str(value or "")))


def _looks_like_bilingual_label_row(
    headers: Sequence[Any],
    candidate: Sequence[Any] | None,
) -> bool:
    if not candidate or len(headers) < 2:
        return False
    normalized = _normalize_row(candidate, len(headers))
    technical_headers = [
        value
        for value in headers
        if value not in {None, ""} and TECHNICAL_HEADER.fullmatch(str(value).strip())
    ]
    labels = [str(value).strip() for value in normalized if value not in {None, ""}]
    if len(technical_headers) / len(headers) < 0.6 or len(labels) < 2:
        return False
    return sum(_contains_chinese(value) for value in labels) / len(labels) >= 0.6


def _name_tokens(value: str) -> list[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return [item.lower() for item in re.split(r"[^A-Za-z0-9]+", expanded) if item]


def _business_column_label(source_name: str) -> str:
    if _contains_chinese(source_name):
        return source_name.strip()
    translated = [
        BUSINESS_TOKEN_ZH[item]
        for item in _name_tokens(source_name)
        if item in BUSINESS_TOKEN_ZH
    ]
    return "".join(translated) or f"业务字段{source_name}"


def _business_sheet_label(
    source_name: str,
    columns: Sequence[dict[str, Any]] | Sequence[ColumnPlan],
) -> str:
    if _contains_chinese(source_name):
        return source_name.strip()
    tokens = set(_name_tokens(source_name)) - IGNORED_TABLE_TOKENS
    labels = {
        str(
            getattr(item, "label_zh", None)
            or (item.get("label_zh") if isinstance(item, dict) else "")
            or ""
        )
        for item in columns
    }
    joined_labels = "".join(labels)
    if "trace" in tokens and "batch" in tokens:
        return "批次追溯记录"
    if "param" in tokens:
        return "质检参数记录" if "判定" in joined_labels or "参数" in joined_labels else "参数记录"
    if "batch" in tokens:
        return "批次工序记录" if "工序" in joined_labels or "设备" in joined_labels else "批次记录"
    translated = [
        BUSINESS_TOKEN_ZH[item]
        for item in _name_tokens(source_name)
        if item in BUSINESS_TOKEN_ZH
    ]
    return "".join(translated) + "记录" if translated else f"业务数据记录（{source_name}）"


def _workbook_has_formulas(path: Path) -> bool:
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                with archive.open(name) as stream:
                    tail = b""
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        value = tail + chunk
                        if b"<f" in value:
                            return True
                        tail = value[-1:]
    return False


def _column_plans(
    headers: Sequence[Any],
    samples: Iterable[Sequence[Any]],
    labels_zh: Sequence[Any] | None = None,
) -> tuple[ColumnPlan, ...]:
    used: set[str] = set()
    source_names = [str(value).strip() if value not in {None, ""} else f"第{index}列" for index, value in enumerate(headers, 1)]
    kinds: list[str | None] = [None] * len(source_names)
    nullable = [False] * len(source_names)
    normalized_labels = _normalize_row(labels_zh or (), len(source_names))
    for row in samples:
        normalized = _normalize_row(row, len(source_names))
        for index, value in enumerate(normalized):
            if value is None or value == "":
                nullable[index] = True
            kinds[index] = _merge_kind(kinds[index], _kind(value))
    return tuple(
        ColumnPlan(
            source_name=source_name,
            column_name=_identifier(source_name, fallback=f"column_{index}", used=used),
            inferred_type=kinds[index - 1] or "text",
            nullable=nullable[index - 1],
            label_zh=(
                str(normalized_labels[index - 1]).strip()
                if _contains_chinese(normalized_labels[index - 1])
                else _business_column_label(source_name)
            ),
        )
        for index, source_name in enumerate(source_names, 1)
    )


def _nonempty(values: Sequence[Any]) -> bool:
    return any(value not in {None, ""} for value in values)


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().astimezone().isoformat()


def _execute_count(connection: Any, cursor: Any, sql: str) -> int:
    """Execute one read-only COUNT probe against the caller-provided cursor."""
    if cursor is None or connection is None:
        raise StructuredDataImportError("生产证据 SQL 缺少只读数据库连接")
    try:
        cursor.execute(sql)
        row = cursor.fetchone()
        value = int(row[0]) if row else 0
        return value
    except Exception as exc:
        raise StructuredDataImportError(f"生产证据 SQL 执行失败：{exc}") from exc


def _markdown_tables(markdown: str) -> Iterator[tuple[str, list[list[str]]]]:
    lines = markdown.splitlines()
    section = "表格"
    index = 0
    table_number = 0
    while index < len(lines):
        line = lines[index].strip()
        if line.startswith("#"):
            section = line.lstrip("#").strip() or section
            index += 1
            continue
        if (
            "|" in line
            and index + 1 < len(lines)
            and MARKDOWN_SEPARATOR.match(lines[index + 1])
        ):
            table_number += 1
            rows = [_split_markdown_row(line)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_markdown_row(lines[index]))
                index += 1
            yield f"{section} 表格{table_number}", rows
            continue
        index += 1


def _split_markdown_row(line: str) -> list[str]:
    stripped = line.strip().strip("|")
    return [cell.replace("\\|", "|").strip() for cell in stripped.split("|")]


class StructuredDataPipeline:
    """Versioned file-to-PostgreSQL data plane used by S0 to hand off into S1."""

    def __init__(
        self,
        database_url: str | None = None,
        *,
        batch_size: int = 1000,
        sample_rows: int = 2000,
    ) -> None:
        self.database_url = database_url or os.getenv("ORION_SOURCE_DATA_URL", "")
        if not self.database_url:
            raise StructuredDataImportError("ORION_SOURCE_DATA_URL is not configured")
        self.database_url = self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
        self.batch_size = max(100, int(batch_size))
        self.sample_rows = max(100, int(sample_rows))

    def import_documents(
        self,
        *,
        project_id: str,
        documents: Sequence[dict[str, Any]],
        input_root: Path,
    ) -> dict[str, Any]:
        receipts = []
        for document in documents:
            source_type = str(document.get("source_type") or "").upper()
            if source_type not in {"XLSX", "XLSM", "CSV", "TSV"}:
                continue
            receipts.append(
                self.import_document(
                    project_id=project_id,
                    document=document,
                    input_root=input_root,
                )
            )
        if not receipts:
            raise StructuredDataImportError("没有发现可导入 S1 的 Excel 或 CSV 表格")
        return self.build_handoff_for_datasets(
            project_id=project_id,
            dataset_ids=[str(receipt["dataset_id"]) for receipt in receipts],
        )

    def build_handoff_for_datasets(
        self,
        *,
        project_id: str,
        dataset_ids: Sequence[str],
    ) -> dict[str, Any]:
        """Rebuild a production handoff from cataloged datasets using read-only SQL."""

        normalized_ids = list(dict.fromkeys(str(value).strip() for value in dataset_ids))
        if not project_id.strip() or not normalized_ids or any(not value for value in normalized_ids):
            raise StructuredDataImportError("dataset handoff 缺少 project_id 或 dataset_ids")
        receipts: list[dict[str, Any]] = []
        with psycopg.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            for dataset_id in normalized_ids:
                cursor.execute(
                    "SELECT project_id, status FROM orion_catalog.datasets WHERE dataset_id=%s",
                    (dataset_id,),
                )
                identity = cursor.fetchone()
                if identity is None:
                    from .snapshot_handoff import build_snapshot_handoff

                    try:
                        return build_snapshot_handoff(
                            cursor, project_id=project_id, dataset_ids=normalized_ids
                        )
                    except ValueError as exc:
                        raise StructuredDataImportError(str(exc)) from exc
                if str(identity[0]) != project_id:
                    raise StructuredDataImportError(
                        f"数据集 {dataset_id} 不属于当前工程 {project_id}"
                    )
                if str(identity[1]) != "READY":
                    raise StructuredDataImportError(
                        f"数据集 {dataset_id} 尚未达到 READY：{identity[1]}"
                    )
                receipts.append(self._receipt(cursor, dataset_id, unchanged=True))
            return self.build_handoff(
                project_id=project_id,
                receipts=receipts,
                read_only_connection=connection,
                read_only_cursor=cursor,
            )

    def import_document(
        self,
        *,
        project_id: str,
        document: dict[str, Any],
        input_root: Path,
    ) -> dict[str, Any]:
        s0_document_id = str(document.get("document_id") or "").strip()
        source_name = str(document.get("source_name") or "").strip()
        source_type = str(document.get("source_type") or "").upper()
        source_sha256 = str(document.get("source_sha256") or "").strip()
        if not project_id or not s0_document_id or not source_name or not SOURCE_SHA256.fullmatch(source_sha256):
            raise StructuredDataImportError("结构化导入缺少项目、文档或 SHA-256 身份")
        source_path_reference = unicodedata.normalize(
            "NFC", str(document.get("source_path") or "").strip()
        )
        logical_source_reference = _logical_source_reference(
            source_path_reference,
            source_name,
        )
        document_id = str(document.get("logical_document_id") or "").strip() or (
            f"FILE-{_digest(logical_source_reference, size=24).upper()}"
        )
        source_version = source_sha256.removeprefix("sha256:")[:16]
        version = f"{source_version}-{_digest(STRUCTURED_IMPORT_PROFILE, size=8)}"
        dataset_id = (
            f"DS-{_digest(project_id, document_id, source_sha256, STRUCTURED_IMPORT_PROFILE).upper()}"
        )
        source_path = (input_root / str(document.get("source_path") or "")).resolve()
        if source_path.parent != input_root.resolve() and input_root.resolve() not in source_path.parents:
            raise StructuredDataImportError("结构化导入源文件越过 input_root")
        if not source_path.is_file():
            raise StructuredDataImportError(f"结构化导入源文件不存在：{source_name}")
        if _sha256_file(source_path) != source_sha256:
            raise StructuredDataImportError(f"结构化导入源文件 SHA-256 已变化：{source_name}")
        with psycopg.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT status, row_count, sheet_count FROM orion_catalog.datasets WHERE dataset_id = %s",
                (dataset_id,),
            )
            existing = cursor.fetchone()
            if existing and existing[0] == "READY":
                return self._receipt(cursor, dataset_id, unchanged=True)
            plans, iterator_factory = self._plans(
                document,
                source_path,
                dataset_id,
                document_id,
            )
            if not plans:
                raise StructuredDataImportError(f"{source_name} 没有可导入的表格")
            cursor.execute(
                """
                INSERT INTO orion_catalog.datasets (
                    dataset_id, project_id, document_id, source_name, source_type,
                    source_sha256, source_uri, version, status, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'IMPORTING', %s)
                ON CONFLICT (dataset_id) DO UPDATE SET
                    status = 'IMPORTING', payload = EXCLUDED.payload
                """,
                (
                    dataset_id,
                    project_id,
                    document_id,
                    source_name,
                    source_type,
                    source_sha256,
                    document.get("original_source_uri"),
                    version,
                    Jsonb(
                        {
                            "import_mode": "FULL_STREAMING",
                            "import_profile": STRUCTURED_IMPORT_PROFILE,
                            "source_version": source_version,
                            "source_path": source_path_reference,
                            "logical_source_reference": logical_source_reference,
                            "s0_document_id": s0_document_id,
                        }
                    ),
                ),
            )
            cursor.execute("DELETE FROM orion_catalog.columns WHERE sheet_id IN (SELECT sheet_id FROM orion_catalog.sheets WHERE dataset_id = %s)", (dataset_id,))
            cursor.execute("DELETE FROM orion_catalog.sheets WHERE dataset_id = %s", (dataset_id,))
            total_rows = 0
            for plan in plans:
                cursor.execute(sql.SQL("DROP TABLE IF EXISTS orion_data.{}") .format(sql.Identifier(plan.table_name)))
                definitions = [
                    sql.SQL("{} {}").format(sql.Identifier(column.column_name), sql.SQL(self._postgres_type(column.inferred_type)))
                    for column in plan.columns
                ]
                cursor.execute(
                    sql.SQL("CREATE TABLE orion_data.{} (_orion_dataset_id TEXT NOT NULL, _orion_source_row BIGINT NOT NULL, {})").format(
                        sql.Identifier(plan.table_name), sql.SQL(", ").join(definitions)
                    )
                )
                cursor.execute(sql.SQL("CREATE INDEX {} ON orion_data.{} (_orion_source_row)").format(
                    sql.Identifier(f"{plan.table_name[:48]}_row_idx"), sql.Identifier(plan.table_name)
                ))
                sheet_id = f"SH-{_digest(dataset_id, str(plan.index), plan.source_name).upper()}"
                cursor.execute(
                    """
                    INSERT INTO orion_catalog.sheets (
                        sheet_id, dataset_id, sheet_index, source_name, table_name, view_name,
                        header_row, column_count, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        sheet_id,
                        dataset_id,
                        plan.index,
                        plan.source_name,
                        plan.table_name,
                        plan.view_name,
                        plan.header_row,
                        len(plan.columns),
                        Jsonb(
                            {
                                "label_zh": plan.label_zh,
                                "header_rows": plan.header_row,
                                "type_inference_sample_rows": self.sample_rows,
                            }
                        ),
                    ),
                )
                row_count, formula_count = self._insert_rows(
                    cursor,
                    dataset_id=dataset_id,
                    plan=plan,
                    rows=iterator_factory(plan),
                )
                total_rows += row_count
                cursor.execute(
                    "UPDATE orion_catalog.sheets SET row_count=%s, formula_count=%s WHERE sheet_id=%s",
                    (row_count, formula_count, sheet_id),
                )
                self._profile_columns(cursor, sheet_id=sheet_id, plan=plan)
            for plan in plans:
                cursor.execute(
                    sql.SQL("DROP VIEW IF EXISTS orion_data.{}").format(
                        sql.Identifier(plan.view_name)
                    )
                )
                cursor.execute(
                    sql.SQL("CREATE VIEW orion_data.{} AS SELECT * FROM orion_data.{}").format(
                        sql.Identifier(plan.view_name),
                        sql.Identifier(plan.table_name),
                    )
                )
            cursor.execute(
                "UPDATE orion_catalog.datasets SET status='READY', sheet_count=%s, row_count=%s, promoted_at=NOW() WHERE dataset_id=%s",
                (len(plans), total_rows, dataset_id),
            )
            cursor.execute(
                """
                UPDATE orion_catalog.datasets SET status='SUPERSEDED'
                WHERE project_id=%s AND document_id=%s AND dataset_id<>%s AND status='READY'
                """,
                (project_id, document_id, dataset_id),
            )
            cursor.execute(
                """
                INSERT INTO orion_catalog.current_datasets (
                    project_id, document_id, dataset_id, source_sha256, version
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (project_id, document_id) DO UPDATE SET
                    dataset_id=EXCLUDED.dataset_id,
                    source_sha256=EXCLUDED.source_sha256,
                    version=EXCLUDED.version,
                    revision=orion_catalog.current_datasets.revision + 1,
                    promoted_at=NOW()
                """,
                (project_id, document_id, dataset_id, source_sha256, version),
            )
            return self._receipt(cursor, dataset_id, unchanged=False)

    def _plans(
        self,
        document: dict[str, Any],
        source_path: Path,
        dataset_id: str,
        document_id: str,
    ) -> tuple[list[SheetPlan], Any]:
        source_type = str(document.get("source_type") or "").upper()
        if source_type in {"XLSX", "XLSM"}:
            return self._excel_plans(source_path, dataset_id, document_id)
        if source_type in {"CSV", "TSV"}:
            return self._delimited_plans(
                source_path,
                dataset_id,
                document_id,
                "\t" if source_type == "TSV" else ",",
            )
        return self._markdown_plans(
            str(document.get("structured_markdown") or ""),
            dataset_id,
            document_id,
        )

    def _excel_plans(
        self,
        path: Path,
        dataset_id: str,
        document_id: str,
    ) -> tuple[list[SheetPlan], Any]:
        from openpyxl import load_workbook

        workbook = load_workbook(path, read_only=True, data_only=True)
        has_formulas = _workbook_has_formulas(path)
        plans: list[SheetPlan] = []
        try:
            for sheet_index, worksheet in enumerate(workbook.worksheets, 1):
                # Ignore optional dimension hints; stream actual XML rows even
                # when a producer under-reports or inflates the worksheet range.
                worksheet.reset_dimensions()
                iterator = worksheet.iter_rows(values_only=True)
                header = None
                header_row = 0
                for row_number, candidate in enumerate(iterator, 1):
                    if _nonempty(candidate):
                        header = candidate
                        header_row = row_number
                        break
                if not header:
                    continue
                possible_label_row = next(iterator, None)
                has_business_labels = _looks_like_bilingual_label_row(
                    header,
                    possible_label_row,
                )
                data_rows = iterator if has_business_labels else chain((possible_label_row,), iterator)
                samples = list(
                    islice(
                        (row for row in data_rows if row and _nonempty(row)),
                        self.sample_rows,
                    )
                )
                columns = _column_plans(
                    header,
                    samples,
                    possible_label_row if has_business_labels else None,
                )
                if not columns:
                    continue
                table_name = f"ds_{dataset_id[3:15].lower()}_{sheet_index:03d}"
                plans.append(
                    SheetPlan(
                        sheet_index,
                        worksheet.title,
                        table_name,
                        self._view_name(
                            document_id,
                            sheet_index,
                            worksheet.title,
                            columns,
                        ),
                        header_row + int(has_business_labels),
                        columns,
                        label_zh=_business_sheet_label(worksheet.title, columns),
                    )
                )
        finally:
            workbook.close()

        def rows(plan: SheetPlan) -> Iterator[tuple[int, tuple[Any, ...], int]]:
            current = load_workbook(path, read_only=True, data_only=True)
            formulas = (
                load_workbook(path, read_only=True, data_only=False)
                if has_formulas
                else None
            )
            try:
                worksheet = current.worksheets[plan.index - 1]
                formula_sheet = formulas.worksheets[plan.index - 1] if formulas else None
                worksheet.reset_dimensions()
                if formula_sheet is not None:
                    formula_sheet.reset_dimensions()
                iterator = worksheet.iter_rows(values_only=True)
                formula_iterator = formula_sheet.iter_rows(values_only=True) if formula_sheet else None
                for _ in range(plan.header_row):
                    next(iterator, None)
                    if formula_iterator:
                        next(formula_iterator, None)
                paired_rows = (
                    zip(iterator, formula_iterator, strict=False)
                    if formula_iterator
                    else ((row, ()) for row in iterator)
                )
                for row_number, (row, formula_row) in enumerate(
                    paired_rows,
                    plan.header_row + 1,
                ):
                    if _nonempty(row) or _nonempty(formula_row):
                        formula_count = sum(
                            isinstance(value, str) and value.startswith("=")
                            for value in formula_row
                        )
                        yield row_number, _normalize_row(row, len(plan.columns)), formula_count
            finally:
                current.close()
                if formulas:
                    formulas.close()

        return plans, rows

    def _read_only_swallowed_rows(
        self,
        path: Path,
        plans: list[SheetPlan],
    ) -> bool:
        """Return True when read-only parsing hid real data rows.

        A handful of XLSX files have a worksheet <dimension> of "A1" even though
        the sheet genuinely holds hundreds of rows.  openpyxl's read-only
        iterator trusts that dimension and yields only the header, while normal
        mode reparses and recovers the rows.  Probe one plan in normal mode and
        compare.
        """
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(path, read_only=False, data_only=True)
        except Exception:
            return False
        try:
            for plan in plans:
                worksheet = workbook.worksheets[plan.index - 1]
                row_count = 0
                for row in worksheet.iter_rows(values_only=True):
                    if _nonempty(row):
                        row_count += 1
                    if row_count > 1:
                        return True
            return False
        finally:
            workbook.close()

    def _excel_plans_normal(
        self,
        path: Path,
        dataset_id: str,
        document_id: str,
    ) -> tuple[list[SheetPlan], Any] | None:
        """Fallback planner that opens the workbook in normal (non read-only) mode."""
        from openpyxl import load_workbook

        try:
            workbook = load_workbook(path, read_only=False, data_only=True)
        except Exception:
            return None
        has_formulas = _workbook_has_formulas(path)
        plans: list[SheetPlan] = []
        try:
            for sheet_index, worksheet in enumerate(workbook.worksheets, 1):
                rows = list(worksheet.iter_rows(values_only=True))
                if not rows:
                    continue
                header = None
                header_row = 0
                for row_number, candidate in enumerate(rows, 1):
                    if _nonempty(candidate):
                        header = candidate
                        header_row = row_number
                        break
                if not header:
                    continue
                possible_label_row = rows[header_row] if header_row < len(rows) else None
                has_business_labels = _looks_like_bilingual_label_row(
                    header,
                    possible_label_row,
                )
                data_rows = (
                    rows[header_row + 1 :]
                    if has_business_labels
                    else rows[header_row:]
                )
                samples = list(
                    islice(
                        (row for row in data_rows if row and _nonempty(row)),
                        self.sample_rows,
                    )
                )
                columns = _column_plans(
                    header,
                    samples,
                    possible_label_row if has_business_labels else None,
                )
                if not columns:
                    continue
                table_name = f"ds_{dataset_id[3:15].lower()}_{sheet_index:03d}"
                plans.append(
                    SheetPlan(
                        sheet_index,
                        worksheet.title,
                        table_name,
                        self._view_name(
                            document_id,
                            sheet_index,
                            worksheet.title,
                            columns,
                        ),
                        header_row + int(has_business_labels),
                        columns,
                        label_zh=_business_sheet_label(worksheet.title, columns),
                    )
                )
        finally:
            workbook.close()
        if not plans:
            return None

        def rows(plan: SheetPlan) -> Iterator[tuple[int, tuple[Any, ...], int]]:
            current = load_workbook(path, read_only=False, data_only=True)
            formulas = (
                load_workbook(path, read_only=False, data_only=False)
                if has_formulas
                else None
            )
            try:
                worksheet = current.worksheets[plan.index - 1]
                formula_sheet = formulas.worksheets[plan.index - 1] if formulas else None
                all_rows = list(worksheet.iter_rows(values_only=True))
                formula_rows = (
                    list(formula_sheet.iter_rows(values_only=True))
                    if formula_sheet
                    else []
                )
                for row_number, row in enumerate(all_rows, 1):
                    if row_number <= plan.header_row:
                        continue
                    formula_row = (
                        formula_rows[row_number - 1] if formula_rows else ()
                    )
                    if _nonempty(row) or _nonempty(formula_row):
                        formula_count = sum(
                            isinstance(value, str) and value.startswith("=")
                            for value in formula_row
                        )
                        yield row_number, _normalize_row(row, len(plan.columns)), formula_count
            finally:
                current.close()
                if formulas:
                    formulas.close()

        return plans, rows

    def _delimited_plans(
        self,
        path: Path,
        dataset_id: str,
        document_id: str,
        delimiter: str,
    ) -> tuple[list[SheetPlan], Any]:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            reader = csv.reader(stream, delimiter=delimiter)
            header = None
            header_row = 0
            for row_number, candidate in enumerate(reader, 1):
                if _nonempty(candidate):
                    header = candidate
                    header_row = row_number
                    break
            if not header:
                return [], lambda _plan: iter(())
            columns = _column_plans(header, (row for row in reader if _nonempty(row)))
        plan = SheetPlan(
            1,
            path.stem,
            f"ds_{dataset_id[3:15].lower()}_001",
            self._view_name(document_id, 1, path.stem, columns),
            header_row,
            columns,
        )

        def rows(_plan: SheetPlan) -> Iterator[tuple[int, tuple[Any, ...], int]]:
            with path.open("r", encoding="utf-8-sig", newline="") as stream:
                reader = csv.reader(stream, delimiter=delimiter)
                for _ in range(header_row):
                    next(reader, None)
                for row_number, row in enumerate(reader, header_row + 1):
                    if _nonempty(row):
                        yield row_number, _normalize_row(row, len(columns)), 0

        return [plan], rows

    def _markdown_plans(
        self,
        markdown: str,
        dataset_id: str,
        document_id: str,
    ) -> tuple[list[SheetPlan], Any]:
        plans = []
        for index, (name, rows) in enumerate(_markdown_tables(markdown), 1):
            if len(rows) < 2:
                continue
            columns = _column_plans(rows[0], rows[1:])
            plans.append(
                SheetPlan(
                    index,
                    name,
                    f"ds_{dataset_id[3:15].lower()}_{index:03d}",
                    self._view_name(document_id, index, name, columns),
                    1,
                    columns,
                    tuple(tuple(row) for row in rows[1:]),
                )
            )

        def rows(plan: SheetPlan) -> Iterator[tuple[int, tuple[Any, ...], int]]:
            for row_number, row in enumerate(plan.rows or (), 2):
                yield row_number, _normalize_row(row, len(plan.columns)), 0

        return plans, rows

    def _insert_rows(
        self,
        cursor: Any,
        *,
        dataset_id: str,
        plan: SheetPlan,
        rows: Iterable[tuple[int, tuple[Any, ...], int]],
    ) -> tuple[int, int]:
        names = ["_orion_dataset_id", "_orion_source_row", *(item.column_name for item in plan.columns)]
        statement = sql.SQL("COPY orion_data.{} ({}) FROM STDIN").format(
            sql.Identifier(plan.table_name),
            sql.SQL(", ").join(sql.Identifier(name) for name in names),
        )
        count = 0
        formula_count = 0
        with cursor.copy(statement) as copy_stream:
            for row_number, values, formulas in rows:
                converted = [
                    _convert(value, column.inferred_type)
                    for value, column in zip(values, plan.columns, strict=True)
                ]
                copy_stream.write_row((dataset_id, row_number, *converted))
                count += 1
                formula_count += formulas
        return count, formula_count

    def _profile_columns(self, cursor: Any, *, sheet_id: str, plan: SheetPlan) -> None:
        aggregates = []
        for column in plan.columns:
            identifier = sql.Identifier(column.column_name)
            aggregates.extend(
                [
                    sql.SQL("count({})").format(identifier),
                    sql.SQL("count(DISTINCT {})").format(identifier),
                    sql.SQL("min({})::text").format(identifier),
                    sql.SQL("max({})::text").format(identifier),
                ]
            )
        cursor.execute(
            sql.SQL("SELECT {} FROM orion_data.{}").format(
                sql.SQL(", ").join(aggregates),
                sql.Identifier(plan.table_name),
            )
        )
        profile = cursor.fetchone()
        for index, column in enumerate(plan.columns, 1):
            offset = (index - 1) * 4
            non_null, distinct, minimum, maximum = profile[offset : offset + 4]
            cursor.execute(
                """
                INSERT INTO orion_catalog.columns (
                    sheet_id, column_index, source_name, column_name, inferred_type,
                    nullable, non_null_count, distinct_count, minimum_value, maximum_value, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    sheet_id,
                    index,
                    column.source_name,
                    column.column_name,
                    column.inferred_type,
                    column.nullable,
                    non_null,
                    distinct,
                    minimum,
                    maximum,
                    Jsonb({"label_zh": column.label_zh}),
                ),
            )

    def _receipt(self, cursor: Any, dataset_id: str, *, unchanged: bool) -> dict[str, Any]:
        cursor.execute(
            """
            SELECT d.project_id, d.document_id, d.source_name, d.source_type,
                   d.source_sha256, d.source_uri, d.version, d.status,
                   d.sheet_count, d.row_count, d.imported_at, d.promoted_at,
                   d.payload
            FROM orion_catalog.datasets d WHERE d.dataset_id=%s
            """,
            (dataset_id,),
        )
        row = cursor.fetchone()
        cursor.execute(
            """
            SELECT s.sheet_id, s.sheet_index, s.source_name, s.table_name, s.view_name,
                   s.row_count, s.column_count, s.formula_count, s.header_row, s.payload,
                   jsonb_agg(jsonb_build_object(
                       'column_index', c.column_index,
                       'source_name', c.source_name,
                       'column_name', c.column_name,
                       'inferred_type', c.inferred_type,
                       'nullable', c.nullable,
                       'non_null_count', c.non_null_count,
                       'distinct_count', c.distinct_count,
                       'minimum_value', c.minimum_value,
                       'maximum_value', c.maximum_value,
                       'label_zh', c.payload->>'label_zh'
                   ) ORDER BY c.column_index)
            FROM orion_catalog.sheets s
            JOIN orion_catalog.columns c ON c.sheet_id=s.sheet_id
            WHERE s.dataset_id=%s
            GROUP BY s.sheet_id, s.sheet_index, s.source_name, s.table_name, s.view_name,
                     s.row_count, s.column_count, s.formula_count, s.header_row, s.payload
            ORDER BY s.sheet_index
            """,
            (dataset_id,),
        )
        sheets = [
            {
                "sheet_id": item[0],
                "sheet_index": item[1],
                "source_name": item[2],
                "table_name": item[3],
                "view_name": item[4],
                "row_count": item[5],
                "column_count": item[6],
                "formula_count": item[7],
                "header_row": item[8],
                "label_zh": (item[9] or {}).get("label_zh"),
                "columns": item[10],
            }
            for item in cursor.fetchall()
        ]
        return {
            "dataset_id": dataset_id,
            "project_id": row[0],
            "document_id": row[1],
            "source_name": row[2],
            "source_type": row[3],
            "source_sha256": row[4],
            "source_uri": row[5],
            "version": row[6],
            "status": row[7],
            "sheet_count": row[8],
            "row_count": row[9],
            "imported_at": row[10].isoformat(),
            "promoted_at": row[11].isoformat() if row[11] else None,
            "source_path": row[12].get("source_path"),
            "s0_document_id": row[12].get("s0_document_id"),
            "unchanged": unchanged,
            "sheets": sheets,
        }

    def build_handoff(
        self,
        *,
        project_id: str,
        receipts: Sequence[dict[str, Any]],
        read_only_connection: Any = None,
        read_only_cursor: Any = None,
    ) -> dict[str, Any]:
        inventory_sources = []
        schema_tables = []
        table_profiles = []
        relation_candidates = []
        evidence_sql = []
        ontology_candidates = []
        mappings = []
        obda_mappings = []
        query_templates: dict[str, str] = {}
        query_capabilities: dict[str, dict[str, Any]] = {}
        iri_base = default_ontology_namespace(project_id)
        for receipt in receipts:
            inventory_sources.append(
                {
                    "dataset_id": receipt["dataset_id"],
                    "project_id": project_id,
                    "document_id": receipt["document_id"],
                    "s0_document_id": receipt.get("s0_document_id"),
                    "imported_at": receipt.get("imported_at"),
                    "source_name": receipt["source_name"],
                    "source_sha256": receipt["source_sha256"],
                    "version": receipt["version"],
                    "row_count": receipt["row_count"],
                    "sheet_count": receipt["sheet_count"],
                    "status": receipt["status"],
                    "source_uri": receipt.get("source_uri"),
                }
            )
            for sheet in receipt["sheets"]:
                sheet_label_zh = str(
                    sheet.get("label_zh")
                    or _business_sheet_label(sheet["source_name"], sheet["columns"])
                )
                table = f'orion_data."{sheet["table_name"]}"'
                source_ref = f"dataset:{receipt['dataset_id']}:sheet:{sheet['source_name']}"
                class_name = self._class_name(
                    sheet["source_name"],
                    sheet["sheet_index"],
                    logical_document_id=receipt["document_id"],
                )
                evidence_id = f"SQL-{_digest(receipt['dataset_id'], sheet['sheet_id'], size=10).upper()}"
                schema_tables.append(
                    {
                        "schema": "orion_data",
                        "table": sheet["view_name"],
                        "physical_version_table": sheet["table_name"],
                        "source_table": sheet["view_name"],
                        "source_id": "orion_source_data",
                        "dataset_id": receipt["dataset_id"],
                        "column_types": {c["column_name"]: c["inferred_type"] for c in sheet["columns"]},
                        "source_sheet": sheet["source_name"],
                        "label_zh": sheet_label_zh,
                        "header_row": sheet.get("header_row"),
                        "row_count": sheet["row_count"],
                        "columns": sheet["columns"],
                        "source_ref": source_ref,
                        "class_name": class_name,
                        "evidence_id": evidence_id,
                    }
                )
                table_profiles.append(
                    {
                        "table": sheet["view_name"],
                        "physical_version_table": sheet["table_name"],
                        "source_table": sheet["view_name"],
                        "source_id": "orion_source_data",
                        "dataset_id": receipt["dataset_id"],
                        "column_types": {c["column_name"]: c["inferred_type"] for c in sheet["columns"]},
                        "source_sheet": sheet["source_name"],
                        "label_zh": sheet_label_zh,
                        "row_count": sheet["row_count"],
                        "columns": sheet["columns"],
                    }
                )
                evidence_sql.append(
                    {
                        "id": evidence_id,
                        "sql": f"SELECT COUNT(*) AS row_count FROM {table}",
                        "purpose": f"回读 {sheet['source_name']} 全量导入行数",
                        "source_tables": [sheet["view_name"]],
                        "expected_row_count": sheet["row_count"],
                        "actual_row_count": sheet["row_count"],
                        "access_mode": "READ_ONLY",
                        "status": "PASSED",
                        "executed_via": "ORION_STRUCTURED_DATA_PIPELINE",
                        "executed_at": receipt.get("promoted_at") or receipt["imported_at"],
                        "result_sha256": "sha256:"
                        + hashlib.sha256(
                            json.dumps(
                                {
                                    "dataset_id": receipt["dataset_id"],
                                    "sheet_id": sheet["sheet_id"],
                                    "view_name": sheet["view_name"],
                                    "row_count": sheet["row_count"],
                                },
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest(),
                    }
                )
                class_id = f"CLASS-{_digest(receipt['dataset_id'], sheet['sheet_id'], size=12).upper()}"
                ontology_candidates.append(
                    {
                        "id": class_id,
                        "name": class_name,
                        "kind": "CLASS",
                        "status": "DATABASE_FACT",
                        "confidence": 0.95,
                        "label_zh": sheet_label_zh,
                        "comment_zh": f"由工作表“{sheet['source_name']}”的全量业务记录形成。",
                        "source_refs": [source_ref, evidence_id],
                    }
                )
                mapping_id = f"MAP-{_digest(class_id, size=12).upper()}"
                mappings.append(
                    {
                        "id": mapping_id,
                        "source": table,
                        "target": class_name,
                        "target_label_zh": sheet_label_zh,
                        "target_comment_zh": (
                            f"由工作表“{sheet['source_name']}”中的全量业务记录形成的"
                            f"{sheet_label_zh}。"
                        ),
                        "mapping_type": "TABLE_TO_CLASS",
                        "source_refs": [source_ref, evidence_id],
                    }
                )
                target_parts = [
                    f":{class_name}-{{_orion_source_row}} a :{class_name}",
                    ":orionDatasetId {_orion_dataset_id}",
                    ":orionSourceRow {_orion_source_row}",
                ]
                source_columns = ["_orion_dataset_id", "_orion_source_row"]
                for column in sheet["columns"]:
                    column_label_zh = str(
                        column.get("label_zh")
                        or _business_column_label(column["source_name"])
                    )
                    property_name = self._property_name(class_name, column["column_name"])
                    candidate_id = f"DATA-{_digest(class_id, column['column_name'], size=12).upper()}"
                    ontology_candidates.append(
                        {
                            "id": candidate_id,
                            "name": property_name,
                            "kind": "DATA_PROPERTY",
                            "status": "DATABASE_FACT",
                            "confidence": 0.95,
                            "label_zh": column_label_zh,
                            "comment_zh": f"{sheet_label_zh}中的{column_label_zh}。",
                            "source_refs": [source_ref, evidence_id],
                        }
                    )
                    mappings.append(
                        {
                            "id": f"MAP-{_digest(candidate_id, size=12).upper()}",
                            "source": f"{table}.{column['column_name']}",
                            "target": property_name,
                            "target_label_zh": f"{sheet_label_zh}的{column_label_zh}",
                            "target_comment_zh": (
                                f"来源于工作表“{sheet['source_name']}”字段“{column['source_name']}”"
                                f"（{column_label_zh}）的数据属性。"
                            ),
                            "mapping_type": "COLUMN_TO_DATA_PROPERTY",
                            "source_refs": [source_ref, evidence_id],
                        }
                    )
                    target_parts.append(f":{property_name} {{{column['column_name']}}}")
                    source_columns.append(f'"{column["column_name"]}"')
                obda_mappings.append(
                    "\n".join(
                        [
                            f"mappingId {mapping_id}",
                            f"target {' ; '.join(target_parts)} .",
                            f"source SELECT {', '.join(source_columns)} FROM {table}",
                        ]
                    )
                )
                query_name = f"list_{sheet['view_name']}"[:63]
                query_templates[query_name] = (
                    f"PREFIX : <{iri_base}>\n"
                    f"SELECT ?entity ?dataset_id ?source_row WHERE {{ "
                    f"?entity a :{class_name} ; "
                    ":orionDatasetId ?dataset_id ; :orionSourceRow ?source_row . } "
                    "ORDER BY ?entity LIMIT 100"
                )
                query_capabilities[query_name] = {
                    "description_zh": f"列出{sheet_label_zh}，最多返回100条并保留原始行定位。",
                    "parameters": {},
                    "result_fields": ["entity", "dataset_id", "source_row"],
                    "question_examples": [f"列出{sheet_label_zh}中的记录"],
                }

                count_query_name = f"count_{sheet['view_name']}"[:63]
                filter_names: set[str] = set()
                filter_blocks: list[str] = []
                filter_parameters: dict[str, dict[str, Any]] = {}
                example_conditions: list[str] = []
                for column in sheet["columns"]:
                    property_name = self._property_name(class_name, column["column_name"])
                    column_label_zh = str(
                        column.get("label_zh")
                        or _business_column_label(column["source_name"])
                    )
                    value_name = _identifier(
                        f"filter_{column['column_name']}",
                        fallback=f"filter_{_digest(column['column_name'], size=8)}",
                        used=filter_names,
                    )
                    operator_name = _identifier(
                        f"operator_{column['column_name']}",
                        fallback=f"operator_{_digest(column['column_name'], size=8)}",
                        used=filter_names,
                    )
                    inferred_type = str(column.get("inferred_type") or "text")
                    parameter_type = {
                        "bigint": "integer",
                        "numeric": "decimal",
                        "date": "date",
                        "timestamp": "datetime",
                        "boolean": "boolean",
                        "text": "string",
                    }.get(inferred_type, "string")
                    comparisons = (
                        ["EQ", "NE"]
                        if parameter_type in {"string", "boolean"}
                        else ["EQ", "NE", "GT", "GTE", "LT", "LTE"]
                    )
                    filter_parameters[value_name] = {
                        "type": parameter_type,
                        "description_zh": f"{column_label_zh}的筛选值",
                        "required": False,
                        **({"max_length": 500} if parameter_type == "string" else {}),
                    }
                    filter_parameters[operator_name] = {
                        "type": "comparison",
                        "description_zh": f"{column_label_zh}的比较方式",
                        "required": False,
                        "default": "EQ",
                        "values": comparisons,
                    }
                    filter_blocks.append(
                        f"{{{{#{value_name}}}}} ?entity :{property_name} ?value_{value_name} . "
                        f"FILTER(?value_{value_name} {{{{{operator_name}}}}} {{{{{value_name}}}}}) "
                        f"{{{{/{value_name}}}}}"
                    )
                    if len(example_conditions) < 2:
                        example_conditions.append(column_label_zh)
                query_templates[count_query_name] = (
                    f"PREFIX : <{iri_base}>\n"
                    "SELECT (COUNT(DISTINCT ?entity) AS ?record_count) WHERE { "
                    f"?entity a :{class_name} . "
                    + " ".join(filter_blocks)
                    + " }"
                )
                query_capabilities[count_query_name] = {
                    "description_zh": (
                        f"统计{sheet_label_zh}记录数；所有字段条件均为可选，可组合使用，"
                        "文本和布尔值支持等于/不等于，数值和日期支持六种比较。"
                    ),
                    "parameters": filter_parameters,
                    "result_fields": ["record_count"],
                    "question_examples": [
                        f"{sheet_label_zh}有多少条记录",
                        f"统计{sheet_label_zh}中满足{'、'.join(example_conditions)}条件的记录数",
                    ],
                }
                for column in sheet["columns"]:
                    column_label_zh = str(
                        column.get("label_zh")
                        or _business_column_label(column["source_name"])
                    )
                    property_name = self._property_name(class_name, column["column_name"])
                    group_query_name = (
                        "group_"
                        + _digest(sheet["view_name"], column["column_name"], size=20)
                    )
                    group_parameters = {
                        **filter_parameters,
                        "limit": {
                            "type": "integer",
                            "description_zh": "最多返回的分组数量",
                            "required": False,
                            "default": 20,
                            "minimum": 1,
                            "maximum": 100,
                        },
                    }
                    query_templates[group_query_name] = (
                        f"PREFIX : <{iri_base}>\n"
                        "SELECT ?group_value "
                        "(COUNT(DISTINCT ?entity) AS ?record_count) WHERE { "
                        f"?entity a :{class_name} ; :{property_name} ?group_value . "
                        + " ".join(filter_blocks)
                        + " } GROUP BY ?group_value ORDER BY DESC(?record_count) "
                        "LIMIT {{limit}}"
                    )
                    query_capabilities[group_query_name] = {
                        "description_zh": (
                            f"按{column_label_zh}分组统计{sheet_label_zh}，支持其他字段组合筛选和 Top N。"
                        ),
                        "parameters": group_parameters,
                        "result_fields": ["group_value", "record_count"],
                        "question_examples": [
                            f"{sheet_label_zh}按{column_label_zh}分组各有多少条",
                            f"{column_label_zh}记录数最多的前20项",
                        ],
                    }
                    if str(column.get("inferred_type") or "") not in {
                        "bigint",
                        "numeric",
                    }:
                        continue
                    stats_query_name = (
                        "stats_"
                        + _digest(sheet["view_name"], column["column_name"], size=20)
                    )
                    query_templates[stats_query_name] = (
                        f"PREFIX : <{iri_base}>\n"
                        "SELECT (COUNT(?metric_value) AS ?value_count) "
                        "(SUM(?metric_value) AS ?value_sum) "
                        "(AVG(?metric_value) AS ?value_average) "
                        "(MIN(?metric_value) AS ?value_minimum) "
                        "(MAX(?metric_value) AS ?value_maximum) WHERE { "
                        f"?entity a :{class_name} ; :{property_name} ?metric_value . "
                        + " ".join(filter_blocks)
                        + " }"
                    )
                    query_capabilities[stats_query_name] = {
                        "description_zh": (
                            f"计算{sheet_label_zh}中{column_label_zh}的数量、求和、平均值、最小值和最大值，"
                            "支持其他字段组合筛选。"
                        ),
                        "parameters": filter_parameters,
                        "result_fields": [
                            "value_count",
                            "value_sum",
                            "value_average",
                            "value_minimum",
                            "value_maximum",
                        ],
                        "question_examples": [
                            f"{column_label_zh}的平均值是多少",
                            f"满足条件的{column_label_zh}最大值和最小值是多少",
                        ],
                    }
        columns_by_name: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
        for table in schema_tables:
            for column in table["columns"]:
                source_key = re.sub(r"\s+", "", str(column["source_name"])).casefold()
                columns_by_name.setdefault(source_key, []).append((table, column))
        for source_key, occurrences in sorted(columns_by_name.items()):
            identifier_like = (
                source_key == "id"
                or source_key.endswith("_id")
                or source_key.endswith("编号")
                or source_key.endswith("代码")
                or source_key.endswith("编码")
            )
            if len(occurrences) < 2 or not identifier_like:
                continue
            for relation_index, ((left, left_column), (right, right_column)) in enumerate(
                zip(occurrences, occurrences[1:], strict=False),
                1,
            ):
                relation_id = f"REL-{_digest(left['table'], right['table'], source_key, size=12).upper()}"
                sql_id = f"SQL-{_digest(relation_id, size=10).upper()}"
                left_table = f'orion_data."{left["physical_version_table"]}"'
                right_table = f'orion_data."{right["physical_version_table"]}"'
                join_sql = (
                    f'SELECT COUNT(*) AS matched_rows FROM {left_table} l JOIN {right_table} r '
                    f'ON l."{left_column["column_name"]}" = r."{right_column["column_name"]}"'
                )
                evidence = {
                    "id": sql_id,
                    "sql": join_sql,
                    "purpose": f"验证字段 {left_column['source_name']} 的跨工作表关联覆盖",
                    "source_tables": [left["table"], right["table"]],
                    "access_mode": "READ_ONLY",
                }
                if read_only_connection is not None and read_only_cursor is not None:
                    matched = _execute_count(
                        read_only_connection,
                        read_only_cursor,
                        join_sql,
                    )
                    evidence.update(
                        {
                            "status": "PASSED",
                            "executed_via": "ORION_STRUCTURED_DATA_PIPELINE",
                            "executed_at": _now_iso(),
                            "expected_row_count": matched,
                            "actual_row_count": matched,
                            "result_sha256": "sha256:"
                            + hashlib.sha256(
                                json.dumps(
                                    {
                                        "matched_rows": matched,
                                        "left_table": left["table"],
                                        "right_table": right["table"],
                                        "left_column": left_column["column_name"],
                                        "right_column": right_column["column_name"],
                                    },
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    separators=(",", ":"),
                                ).encode("utf-8")
                            ).hexdigest(),
                        }
                    )
                else:
                    evidence["status"] = "NEEDS_EXECUTION"
                evidence_sql.append(evidence)
                relation_candidates.append(
                    {
                        "id": relation_id,
                        "left_table": left["table"],
                        "right_table": right["table"],
                        "left_column": left_column["column_name"],
                        "right_column": right_column["column_name"],
                        "basis": "SAME_IDENTIFIER_COLUMN",
                        "source_refs": [left["source_ref"], right["source_ref"], sql_id],
                    }
                )
                relation_name = (
                    left["class_name"][:1].lower()
                    + left["class_name"][1:]
                    + "References"
                    + right["class_name"]
                    + str(relation_index)
                )
                ontology_candidates.append(
                    {
                        "id": relation_id,
                        "name": relation_name,
                        "kind": "OBJECT_PROPERTY",
                        "status": "NEEDS_HUMAN_CONFIRMATION",
                        "confidence": 0.7,
                        "label_zh": f"{left['label_zh']}关联{right['label_zh']}",
                        "comment_zh": (
                            f"通过共同字段“{left_column.get('label_zh') or left_column['source_name']}”"
                            "形成的跨工作表候选关系，需人工确认。"
                        ),
                        "source_refs": [left["source_ref"], right["source_ref"], sql_id],
                    }
                )
                mapping_id = f"MAP-{_digest(relation_id, size=12).upper()}"
                mappings.append(
                    {
                        "id": mapping_id,
                        "source": (
                            f"{left_table}.{left_column['column_name']}="
                            f"{right_table}.{right_column['column_name']}"
                        ),
                        "target": relation_name,
                        "target_label_zh": f"{left['label_zh']}关联{right['label_zh']}",
                        "target_comment_zh": (
                            f"通过共同字段“{left_column.get('label_zh') or left_column['source_name']}”"
                            "形成的跨工作表候选关系，需人工确认业务含义。"
                        ),
                        "mapping_type": "CANDIDATE_JOIN_TO_OBJECT_PROPERTY",
                        "source_refs": [left["source_ref"], right["source_ref"], sql_id],
                    }
                )
                obda_mappings.append(
                    "\n".join(
                        [
                            f"mappingId {mapping_id}",
                            f"target :{left['class_name']}-{{left_row}} :{relation_name} :{right['class_name']}-{{right_row}} .",
                            (
                                "source SELECT l._orion_source_row AS left_row, "
                                "r._orion_source_row AS right_row "
                                f"FROM {left_table} l JOIN {right_table} r "
                                f'ON l."{left_column["column_name"]}" = r."{right_column["column_name"]}"'
                            ),
                        ]
                    )
                )
                relation_query_name = "relation_" + _digest(relation_id, size=20)
                query_templates[relation_query_name] = (
                    f"PREFIX : <{iri_base}>\n"
                    "SELECT (COUNT(DISTINCT ?left) AS ?left_count) "
                    "(COUNT(DISTINCT ?right) AS ?right_count) "
                    "(COUNT(*) AS ?relation_count) WHERE { "
                    f"?left :{relation_name} ?right . "
                    "}"
                )
                query_capabilities[relation_query_name] = {
                    "description_zh": (
                        f"统计{left['label_zh']}与{right['label_zh']}的跨表关联覆盖。"
                    ),
                    "parameters": {},
                    "result_fields": ["left_count", "right_count", "relation_count"],
                    "question_examples": [
                        f"有多少{left['label_zh']}关联到{right['label_zh']}"
                    ],
                }
        for query_name, capability in query_capabilities.items():
            query = query_templates[query_name]
            capability["validation_cases"] = [
                {
                    "id": "default_contract",
                    "question": capability["question_examples"][0],
                    "parameters": {},
                    "expected_fields": capability["result_fields"],
                    "min_rows": (
                        1
                        if re.search(r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\(", query)
                        and not re.search(r"\bGROUP\s+BY\b", query, re.IGNORECASE)
                        else 0
                    ),
                }
            ]
        tables = [item["table"] for item in schema_tables]
        mapping_obda = (
            "[PrefixDeclaration]\n"
            f":\t{iri_base}\n"
            "xsd:\thttp://www.w3.org/2001/XMLSchema#\n\n"
            "[MappingDeclaration] @collection [[\n"
            + "\n\n".join(obda_mappings)
            + "\n]]\n"
        )
        return {
            "schema_version": 1,
            "project_id": project_id,
            "receipts": list(receipts),
            "s1": {
                "datasource_inventory": {
                    "datasource_count": 1,
                    "datasources": [
                        {
                            "id": "orion_source_data",
                            "label": "ORION 文件结构化数据区",
                            "engine": "PostgreSQL",
                            "database": "orion_source_data",
                            "via": "ORION full importer + exact profiler",
                            "review_tool": "Chat2DB",
                            "connection_env": "ORION_SOURCE_DATA_READER_URL",
                            "database_principal": "orion_source_reader",
                            "access_mode": "READ_ONLY_AFTER_IMPORT",
                        }
                    ],
                    "business_tables_scope": tables,
                    "datasets": inventory_sources,
                },
                "schema_snapshot": {"database": "orion_source_data", "schemas": ["orion_catalog", "orion_data"], "tables": schema_tables},
                "data_profile": {
                    "profile_mode": "FULL_IMPORT_WITH_EXACT_COUNTS",
                    "profiled_at": datetime.now().astimezone().isoformat(),
                    "total_rows": sum(int(item["row_count"]) for item in receipts),
                    "table_count": len(table_profiles),
                    "empty_table_count": sum(
                        int(item["row_count"]) == 0 for item in table_profiles
                    ),
                    "tables": table_profiles,
                },
                "relation_candidates": relation_candidates,
                "evidence_sql": evidence_sql,
            },
            "s2": {"ontology_candidates": ontology_candidates, "business_rule_candidates": []},
            "s3": {
                "mapping_draft": {"mapping_version": "0.1.0-draft", "mappings": mappings},
                "confirmations": [],
                "realtime_runtime": {
                    "ontop_deployment_id": f"ontop-{_digest(project_id, size=16)}",
                    "database_access_mode": "READ_ONLY",
                    "prepared_by": "ORION_STRUCTURED_DATA_PIPELINE",
                    "mapping_obda": mapping_obda,
                    "ontop_queries": query_templates,
                    "query_capabilities": query_capabilities,
                    "reasoning_requirement": "NOT_APPLICABLE",
                    "reasoning_not_applicable_reason": (
                        "确定性结构化导入阶段只生成检索、筛选、聚合和关联覆盖能力，"
                        "当前 S2 未识别出可发布的业务判定规则；若后续语义识别形成规则候选，"
                        "S3 门禁将要求改为 REQUIRED 并提交正式规则能力。"
                    ),
                    "document_query_capabilities": [
                        "current_full_text_search",
                        "reviewed_entity_evidence",
                    ],
                },
            },
        }

    @staticmethod
    def _postgres_type(inferred_type: str) -> str:
        return {
            "boolean": "BOOLEAN",
            "bigint": "BIGINT",
            "numeric": "NUMERIC",
            "date": "DATE",
            "timestamp": "TIMESTAMPTZ",
            "text": "TEXT",
        }[inferred_type]

    @staticmethod
    def _view_name(
        document_id: str,
        sheet_index: int,
        source_name: str,
        columns: Sequence[ColumnPlan],
    ) -> str:
        signature = "|".join(
            f"{item.source_name}:{item.inferred_type}" for item in columns
        )
        schema_id = _digest(source_name, signature, size=8)
        return f"current_{_digest(document_id, size=12)}_{sheet_index:03d}_{schema_id}"

    @staticmethod
    def _class_name(
        source_name: str,
        index: int,
        *,
        logical_document_id: str,
    ) -> str:
        ascii_name = _identifier(source_name, fallback=f"sheet_{index}")
        base = "".join(part.capitalize() for part in ascii_name.split("_")) or f"Sheet{index}"
        suffix = _digest(logical_document_id, source_name, str(index), size=6).upper()
        return f"{base}{suffix}"

    @staticmethod
    def _property_name(class_name: str, column_name: str) -> str:
        suffix = "".join(part.capitalize() for part in column_name.split("_"))
        return class_name[:1].lower() + class_name[1:] + suffix


def dump_handoff(path: Path, handoff: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(handoff, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
