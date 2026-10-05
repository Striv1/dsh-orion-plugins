"""Release-bound Wren models compiled from reviewed mapping and frozen sources.

This is a relational projection of the approved mapping, not a second ontology.
Generated statistics have explicit arithmetic names and are not approved KPIs.
Packages stay immutable; reproducible compiler output lives in a version cache.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import secrets
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any

import yaml

from services.ontology_engineering.mapping_runtime_compiler import _identity
from services.structured_data.execution_source import dataset_column, execution_schema

ADAPTER_VERSION = "orion-wren-project-v1"
ROOT = Path(__file__).resolve().parents[2]
CACHE_ROOT = Path(os.environ.get("ORION_WREN_CACHE_ROOT") or ROOT / ".orion-runtime/wren/projects")
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,100}$")
MAPPING = "02-工程定义/mapping.yaml"
SCHEMA = "06-工程追溯/01-data-understanding/schema-snapshot.json"
INVENTORY = "06-工程追溯/01-data-understanding/datasource-inventory.json"
XSD_TYPES = {"string": "varchar", "int": "bigint", "integer": "bigint", "long": "bigint",
             "decimal": "decimal", "double": "double", "float": "double", "boolean": "boolean",
             "date": "date", "dateTime": "timestamptz"}
NUMERIC_TYPES = {"bigint", "double", "decimal"}


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                               separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError("分析标识不是受支持的已登记名称。")
    return '"' + value + '"'


def _read_package(binding) -> tuple[dict, dict, dict]:
    root = Path(binding.package_path).resolve()
    data = (root / "manifest.json").read_bytes()
    if "sha256:" + hashlib.sha256(data).hexdigest() != binding.release_fingerprint:
        raise ValueError("发布清单指纹已变化，不能建立分析模型。")
    manifest = json.loads(data)
    files = {item["path"]: item["sha256"] for item in manifest.get("files", [])}

    def read(relative: str, optional: bool = False):
        if optional and relative not in files:
            return {}
        path = (root / relative).resolve()
        if relative not in files or not path.is_relative_to(root) or path.stat().st_size > 8 * 1024 * 1024:
            raise ValueError("分析定义未受发布清单保护或超过大小预算。")
        raw = path.read_bytes()
        if "sha256:" + hashlib.sha256(raw).hexdigest() != files[relative]:
            raise ValueError("分析定义文件校验失败。")
        result = yaml.safe_load(raw) if relative.endswith("yaml") else json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("分析定义必须为对象。")
        return result

    return read(MAPPING), read(SCHEMA), read(INVENTORY, optional=True)


def _source_type(raw: str) -> str:
    raw = str(raw).lower()
    if raw in {"int", "integer", "bigint", "smallint"}:
        return "bigint"
    if raw in {"double", "double precision", "float", "real"}:
        return "double"
    if raw.startswith(("numeric", "decimal")):
        return "decimal"
    if raw in {"date", "boolean", "bool"}:
        return "boolean" if raw == "bool" else raw
    if "timestamp" in raw:
        return "timestamptz" if "with time zone" in raw or "timestamptz" in raw else "timestamp"
    return "varchar"


def _cast(column: str, kind: str, *, postgres: bool) -> str:
    source = f"CAST({_identifier(column)} AS VARCHAR)"
    if kind == "varchar":
        return source
    sql_type = {"decimal": "NUMERIC" if postgres else "DECIMAL(38, 18)",
                "double": "DOUBLE PRECISION", "timestamptz": "TIMESTAMP WITH TIME ZONE"}.get(kind, kind.upper())
    return f"CAST(NULLIF({source}, '') AS {sql_type})"


def build_project(binding, *, cache_root: Path | None = None) -> dict:
    """Compile a verified OntologyReleaseBinding into a deterministic descriptor.

    Raw rules, SQL-derived classes and unresolved mappings are reported in
    ``omitted``; no conclusion is approximated by an invented SQL predicate.
    The caller must still check current publication and session authorization.
    """
    if getattr(binding, "database_access_mode", None) != "READ_ONLY":
        raise ValueError("分析仅支持已登记的只读发布绑定。")
    mapping, schema, inventory = _read_package(binding)
    schema = execution_schema(schema, inventory, project_id=binding.project_id)
    tables = schema.get("tables", [])
    mappings = mapping.get("mappings", [])
    if not isinstance(mappings, list) or len(mappings) > 2000:
        raise ValueError("发布映射数量或格式不受支持。")
    models, source_tables, classes, omitted = [], [], {}, []
    sources = {}

    def omit(item, reason):
        omitted.append({"mapping_id": str(item.get("id", "")), "target": str(item.get("target", "")),
                        "mapping_type": str(item.get("mapping_type", "")), "reason": reason})

    def table_for(item):
        derivation = item.get("derivation") or {}
        candidates = [t for t in tables if derivation.get("from_snapshot_table") in {t.get("name"), t.get("source_table")}
                      and (not derivation.get("source_id") or t.get("source_id") == derivation["source_id"])]
        if len(candidates) != 1:
            raise ValueError("映射来源缺失或存在同名来源歧义。")
        table = candidates[0]
        _identifier(table["name"])
        dataset_column(table)
        return table

    def source_for(table):
        key = (table["name"], table["dataset_id"])
        if key not in sources:
            if not isinstance(table.get("row_count"), int) or table["row_count"] < 0:
                raise ValueError("发布快照缺少完整行数，不能推断完整性。")
            source = {"name": f"st_{len(sources)}", "physical_table": table["name"],
                      "dataset_id": table["dataset_id"], "dataset_column": dataset_column(table),
                      "columns": [], "types": {}, "semantic_types": {}, "expected_count": table["row_count"],
                      "source_id": table.get("source_id"), "source_table": table.get("source_table"),
                      "file_import": table.get("file_import"), "source_kind": table.get("source_kind", "DATABASE_SNAPSHOT")}
            sources[key] = source
            source_tables.append(source)
        return sources[key]

    def add_projection(entry, physical, exposed, kind, properties=None):
        _identifier(physical)
        _identifier(exposed)
        if physical not in entry["table"].get("columns", []):
            raise ValueError("映射字段未登记在冻结快照中。")
        if any(c["name"] == exposed for c in entry["model"]["columns"]):
            raise ValueError("模型字段重名。")
        entry["projection"].append((physical, exposed, kind))
        entry["model"]["columns"].append({"name": exposed, "type": kind, "properties": {**(properties or {}), "sourceColumn": physical}})
        source = entry["source"]
        source["semantic_types"].setdefault(physical, [])
        if kind not in source["semantic_types"][physical]:
            source["semantic_types"][physical].append(kind)
        if physical not in source["columns"]:
            source["columns"].append(physical)
            source["types"][physical] = _source_type(entry["table"].get("column_types", {}).get(physical, "text"))

    for item in mappings:
        if item.get("mapping_type") != "TABLE_TO_CLASS":
            continue
        try:
            name = str(item.get("target", ""))
            _identifier(name)
            if name in classes:
                raise ValueError("重复类名不能生成唯一模型。")
            table = table_for(item)
            ids = _identity(item, table)
            if len(ids) != 1:
                raise ValueError("复合业务主键需要独立分析契约；当前不压平身份。")
            model = {"name": name, "primaryKey": "_key", "columns": [], "properties": {
                "description": item.get("target_comment_zh") or item.get("target_label_zh") or name,
                "label_zh": item.get("target_label_zh") or name, "mapping_id": item.get("id"),
                "identity_columns": ids}}
            entry = {"model": model, "table": table, "source": source_for(table), "projection": [], "ids": ids}
            add_projection(entry, ids[0], "_key", "varchar", {"description": "发布业务实例标识；空标识不生成实例"})
            classes[name] = entry
            models.append(model)
        except (ValueError, KeyError) as exc:
            omit(item, str(exc))

    for item in mappings:
        if item.get("mapping_type") != "COLUMN_TO_DATA_PROPERTY":
            continue
        try:
            entry = classes.get(item.get("domain"))
            if not entry or table_for(item) != entry["table"]:
                raise ValueError("数据属性缺少一致的可编译业务类。")
            datatype = str(item.get("datatype") or "xsd:string").removeprefix("xsd:").removeprefix("http://www.w3.org/2001/XMLSchema#")
            if datatype not in XSD_TYPES:
                raise ValueError("属性类型缺少已验证的分析转换。")
            add_projection(entry, (item.get("derivation") or {}).get("from_snapshot_column"),
                           item.get("target"), XSD_TYPES[datatype], {
                               "description": item.get("target_comment_zh") or item.get("target_label_zh") or item.get("target"),
                               "label_zh": item.get("target_label_zh") or item.get("target"), "mapping_id": item.get("id")})
        except (ValueError, KeyError) as exc:
            omit(item, str(exc))

    relationships = []
    for item in mappings:
        if item.get("mapping_type") != "CANDIDATE_JOIN_TO_OBJECT_PROPERTY":
            continue
        try:
            left, right = classes.get(item.get("domain")), classes.get(item.get("range"))
            if not left or not right or left is right or table_for(item) != left["table"]:
                raise ValueError("关系两侧缺少可编译、不同的业务类。")
            derivation = item.get("derivation") or {}
            if derivation.get("join_target") != f"{right['table']['source_table']}.{right['ids'][0]}":
                raise ValueError("关系连接目标与 range 的唯一业务标识不一致。")
            handle = str(item["target"])
            _identifier(handle)
            relation_name = "rel_" + hashlib.sha256(str(item["id"]).encode()).hexdigest()[:12]
            foreign = "_fk_" + handle
            add_projection(left, derivation.get("from_snapshot_column"), foreign, "varchar")
            left_name, right_name = left["model"]["name"], right["model"]["name"]
            if any(c["name"] == handle for c in left["model"]["columns"]):
                raise ValueError("关系与数据属性重名。")
            relationships.append({"name": relation_name, "models": [left_name, right_name], "joinType": "MANY_TO_ONE",
                                  "condition": f'{_identifier(left_name)}.{_identifier(foreign)} = {_identifier(right_name)}."_key"'})
            left["model"]["columns"].append({"name": handle, "type": right_name, "relationship": relation_name})
            for col in list(right["model"]["columns"]):
                if col["name"].startswith("_") or col.get("relationship") or col.get("isCalculated"):
                    continue
                exposed = handle + "__" + col["name"]
                _identifier(exposed)
                left["model"]["columns"].append({"name": exposed, "type": col["type"], "isCalculated": True,
                                                "expression": f'{_identifier(handle)}.{_identifier(col["name"])}',
                                                "properties": {"description": f"沿发布关系 {handle} 读取 {col['name']}"}})
        except (ValueError, KeyError) as exc:
            omit(item, str(exc))

    handled = {"TABLE_TO_CLASS", "COLUMN_TO_DATA_PROPERTY", "CANDIDATE_JOIN_TO_OBJECT_PROPERTY"}
    for item in mappings:
        if item.get("mapping_type") not in handled:
            omit(item, "规则、派生类或证据映射继续使用原发布能力；未翻译为分析事实。")
    if not models:
        raise ValueError("当前发布没有可安全编译的结构化分析模型。")
    views, cubes = [], []
    for model in models:
        public = [c for c in model["columns"] if not c["name"].startswith("_") and not c.get("relationship")]
        selected = ['"_key"', *[_identifier(c["name"]) for c in public]]
        views.append({"name": model["name"] + "__records", "statement": f'SELECT {", ".join(selected)} FROM {_identifier(model["name"])}',
                      "properties": {"description": "已发布业务实例明细；按业务标识去重，不包含规则推导结论"}})
        measures = [{"name": "count_rows", "expression": "COUNT(*)", "type": "bigint"}]
        dimensions, time_dimensions = [], []
        for col in public:
            name, kind = col["name"], col["type"]
            if kind in NUMERIC_TYPES:
                for op in ("sum", "avg", "min", "max"):
                    measures.append({"name": f"{op}_{name}", "expression": f'{op.upper()}({_identifier(name)})',
                                     "type": "double" if op == "avg" and kind != "decimal" else kind})
                measures.append({"name": "count_known_" + name, "expression": f'COUNT({_identifier(name)})', "type": "bigint"})
            elif kind in {"date", "timestamp", "timestamptz"}:
                time_dimensions.append({"name": name, "expression": _identifier(name), "type": kind})
            else:
                dimensions.append({"name": name, "expression": _identifier(name), "type": kind})
        cubes.append({"name": model["name"] + "__statistics", "baseObject": model["name"], "measures": measures,
                      "dimensions": dimensions, "timeDimensions": time_dimensions,
                      "properties": {"description": "明确运算的探索统计；非人工批准业务KPI。NULL不补零；count_rows含未知字段的实例，count_known排除未知。",
                                     "approval_status": "GENERATED_CHECKED_NOT_BUSINESS_APPROVED"}})
    mdl = {"dataSource": "duckdb", "layoutVersion": 3, "catalog": "orion", "schema": "analysis", "models": models,
           "relationships": relationships, "views": views, "cubes": cubes}
    _canonicalize_mdl(mdl)
    mdl_postgres = copy.deepcopy(mdl)
    mdl_postgres["dataSource"] = "postgres"
    for target_mdl, postgres in ((mdl, False), (mdl_postgres, True)):
        for model in target_mdl["models"]:
            entry = classes[model["name"]]
            columns = [f"{_cast(col, kind, postgres=postgres)} AS {_identifier(alias)}" for col, alias, kind in entry["projection"]]
            source = entry["source"]
            table_sql = ("orion_data." + _identifier(source["physical_table"]) if postgres
                         else "receipt.main." + _identifier(source["name"]))
            where = f'{_identifier(entry["ids"][0])} IS NOT NULL'
            if postgres:
                where += f" AND {_identifier(source['dataset_column'])} = '{source['dataset_id']}'"
            model["refSql"] = f'SELECT DISTINCT {", ".join(columns)} FROM {table_sql} WHERE {where}'
    identity = {"project_id": binding.project_id, "release_version": binding.release_version,
                "release_fingerprint": binding.release_fingerprint, "adapter_version": ADAPTER_VERSION}
    key = digest({**identity, "mdl": mdl, "mdl_postgres": mdl_postgres})[7:]
    knowledge = {"rules": "业务口径来自已发布本体与映射。未知值不能补零。规则推理使用原规则工具。统计并不代表业务KPI审批。",
                 "models": [{"name": m["name"], "description": m["properties"]["description"]} for m in models],
                 "query_examples": [], "omitted_semantics": omitted}
    descriptor = {**identity, "key": key, "mdl": mdl, "mdl_postgres": mdl_postgres,
                  "mdl_sha256": digest(mdl), "mdl_postgres_sha256": digest(mdl_postgres),
                  "source_tables": source_tables, "model_projections": [
                      {"model": name, "source_name": entry["source"]["name"], "identity_columns": entry["ids"],
                       "columns": [{"physical": physical, "name": alias, "type": kind} for physical, alias, kind in entry["projection"]]}
                      for name, entry in classes.items()],
                  "models": models, "relationships": relationships,
                  "views": views, "cubes": cubes, "knowledge": knowledge, "omitted": omitted,
                  "execution_modes": ["CONTROLLED_SNAPSHOT_POSTGRES", "ISOLATED_DUCKDB"],
                  "statistics_status": "GENERATED_CHECKED_NOT_BUSINESS_APPROVED"}
    cache = Path(cache_root) if cache_root is not None else CACHE_ROOT
    _ensure_cache_directory(cache, cache)
    directory = cache / key
    _ensure_cache_directory(cache, directory)
    _persist_project(directory, descriptor)
    descriptor["project_path"] = str(directory)
    descriptor["native_validation"] = _validate_native(descriptor)
    return descriptor


def build_live_project(binding, descriptor: dict, registrations: dict, *, cache_root: Path | None = None) -> dict:
    """Bind approved projections to explicitly registered dynamic business tables.

    Registrations are backend-owned records. A Chat2DB identifier alone never
    authorizes a live source, and metadata or result row counts are not frozen.
    """
    if (descriptor.get("project_id") != binding.project_id
            or descriptor.get("release_fingerprint") != binding.release_fingerprint):
        raise ValueError("动态分析描述与正式发布绑定不一致。")
    result = copy.deepcopy(descriptor)
    result.pop("project_path", None)
    result.pop("native_validation", None)
    databases = set()
    source_by_name = {}
    for source in result["source_tables"]:
        registration = registrations.get(source["source_id"])
        if hasattr(registration, "model_dump"):
            registration = registration.model_dump(mode="json")
        if not isinstance(registration, dict) or registration.get("state", registration.get("status")) != "ACTIVE":
            raise ValueError("动态来源尚未完成服务端只读登记。")
        contracts = registration.get("source_tables", registration.get("tables", []))
        if isinstance(contracts, dict):
            contracts = list(contracts.values())
        candidates = [table for table in contracts if table.get("source_table") == source["source_table"]
                      or table.get("qualified_source_table") == source["source_table"]]
        if not candidates:
            candidates = [table for table in contracts if table.get("physical_table") == source["source_table"]]
        if len(candidates) != 1:
            raise ValueError("动态来源表缺失或存在同名歧义。")
        contract = copy.deepcopy(candidates[0])
        schema_name, table_name = contract["physical_schema"], contract["physical_table"]
        _identifier(schema_name)
        _identifier(table_name)
        qualified = schema_name + "." + table_name
        bound = binding.source_bindings.get(source["source_id"], {})
        if hasattr(bound, "model_dump"):
            bound = bound.model_dump(mode="json")
        allowed = bound.get("authorized_columns", {}).get(qualified, bound.get("authorized_columns", {}).get(source["source_table"], []))
        registered_columns = registration.get("authorized_columns", {}).get(qualified, [])
        authorized_names = {qualified, source["source_table"]}
        if (bound.get("access_mode") != "READ_ONLY" or bound.get("status") != "ACTIVE"
                or not authorized_names.intersection(bound.get("authorized_tables", []))
                or qualified not in registration.get("authorized_tables", [])
                or contract.get("source_id") != source["source_id"]
                or not set(source["columns"]) <= set(allowed)
                or not set(source["columns"]) <= set(registered_columns)
                or not set(source["columns"]) <= set(contract["columns"])):
            raise ValueError("动态模型超出正式来源与服务登记的交集范围。")
        bound_database = bound.get("database") or bound.get("catalog")
        database = registration.get("database", contract.get("database", bound_database))
        if not database or not bound_database or database != bound_database:
            raise ValueError("动态来源数据库与正式发布身份不一致。")
        databases.add(database)
        source.update(physical_schema=schema_name, physical_table=table_name, database=database,
                      database_identity=registration.get("database_identity"),
                      qualified_source_table=qualified, expected_count=None, dataset_id=None, dataset_column=None,
                      types={column: contract["types"][column] for column in source["columns"]},
                      primary_key=contract["primary_key"], foreign_keys=contract["foreign_keys"],
                      nullable=contract["nullable"], schema_signature=contract["schema_signature"],
                      catalog_contract=contract, source_kind="LIVE_SOURCE_DATABASE")
        source_by_name[source["name"]] = source
    if len(databases) != 1:
        raise ValueError("当前动态分析仅允许同一数据库内的已授权关系。")
    models = {model["name"]: model for model in result["mdl_postgres"]["models"]}
    projections = {item["model"]: item for item in result["model_projections"]}
    for name, projection in projections.items():
        source = source_by_name[projection["source_name"]]
        if projection["identity_columns"] != source["primary_key"]:
            raise ValueError("动态业务身份缺少匹配的数据库主键约束，不能跳过粒度核验。")
        columns = []
        for column in projection["columns"]:
            physical, exposed, kind = column["physical"], column["name"], column["type"]
            source_kind = _source_type(source["types"][physical])
            # Native compatible expressions preserve index usability and Decimal.
            expression = (_identifier(physical) if source_kind == kind
                          else _cast(physical, kind, postgres=True))
            columns.append(expression + " AS " + _identifier(exposed))
        model = models[name]
        model["refSql"] = ('SELECT ' + ', '.join(columns) + ' FROM '
                           + _identifier(source["physical_schema"]) + '.' + _identifier(source["physical_table"]))
    for relation in result["mdl_postgres"]["relationships"]:
        left, right = relation["models"]
        left_projection, right_projection = projections[left], projections[right]
        left_source = source_by_name[left_projection["source_name"]]
        right_source = source_by_name[right_projection["source_name"]]
        match = re.fullmatch(r'"[^"]+"\."([^"]+)" = "[^"]+"\."_key"', relation["condition"])
        if not match:
            raise ValueError("动态关系缺少可核验键条件。")
        columns = [item["physical"] for item in left_projection["columns"] if item["name"] == match[1]]
        if not any(fk["columns"] == columns and fk["referenced_schema"] == right_source["physical_schema"]
                   and fk["referenced_table"] == right_source["physical_table"]
                   and fk["referenced_columns"] == right_projection["identity_columns"] for fk in left_source["foreign_keys"]):
            raise ValueError("动态关系缺少已验证外键；不能按名称猜测关联。")
    _canonicalize_mdl(result["mdl_postgres"])
    result["mdl_postgres_sha256"] = digest(result["mdl_postgres"])
    result["execution_scope"] = "LIVE_SOURCE_DATABASE"
    result["execution_modes"] = ["LIVE_SOURCE_DATABASE"]
    result["business_timezone"] = next(iter(registrations.values())).get("business_timezone", "Asia/Shanghai")
    result["key"] = digest({"release": result["release_fingerprint"], "mdl": result["mdl_postgres"],
                            "sources": result["source_tables"], "adapter": ADAPTER_VERSION})[7:]
    result["models"] = result["mdl_postgres"]["models"]
    result["relationships"] = result["mdl_postgres"]["relationships"]
    result["views"] = result["mdl_postgres"]["views"]
    result["cubes"] = result["mdl_postgres"]["cubes"]
    cache = Path(cache_root) if cache_root is not None else CACHE_ROOT
    _ensure_cache_directory(cache, cache)
    directory = cache / result["key"]
    _ensure_cache_directory(cache, directory)
    _persist_project(directory, result)
    result["project_path"] = str(directory)
    result["native_validation"] = _validate_native(result)
    return result


def _canonicalize_mdl(mdl: dict) -> None:
    """Match the official YAML compiler's ordering and property-key wire form."""
    def camel(key):
        return re.sub(r"_([a-z])", lambda match: match[1].upper(), key)

    def properties(value):
        if isinstance(value, dict):
            return {camel(k): properties(v) for k, v in value.items()}
        if isinstance(value, list):
            return [properties(v) for v in value]
        return value

    for category in ("models", "views", "cubes"):
        mdl[category].sort(key=lambda item: item["name"])
        for item in mdl[category]:
            if "properties" in item:
                item["properties"] = properties(item["properties"])
            for column in item.get("columns", []):
                if "properties" in column:
                    column["properties"] = properties(column["properties"])


def _ensure_cache_directory(root: Path, directory: Path) -> None:
    # Check even existing components; mkdir(exist_ok=True) follows symlinks.
    if root.is_symlink() or any(p.is_symlink() for p in root.parents):
        raise ValueError("分析缓存目录不能通过软链接写入。")
    relative = directory.relative_to(root)
    root.mkdir(parents=True, exist_ok=True)
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("分析缓存目录包含软链接。")
        current.mkdir(exist_ok=True)


def _atomic_cache_write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    _ensure_cache_directory(root, path.parent)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".writing-" + secrets.token_hex(12)
    try:
        try:
            old_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd)
        except FileNotFoundError:
            old_fd = None
        except OSError as exc:
            raise ValueError("分析缓存文件不能是软链接或特殊文件。") from exc
        if old_fd is not None:
            with os.fdopen(old_fd, "r") as old:
                if old.read() == content:
                    return
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory_fd)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory_fd)
        os.close(directory_fd)


def _validate_native(descriptor: dict) -> dict:
    python = Path(os.environ.get("ORION_WREN_PYTHON") or ROOT / ".orion-runtime/wren/0.15.0/.venv/bin/python")
    if not python.is_file():
        return {"status": "UNAVAILABLE", "reason": "固定 Wren 运行时未安装；未验证原生编译。"}
    environment = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "TMPDIR") if key in os.environ}
    environment.update(PYTHONNOUSERSITE="1", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1")
    result = subprocess.run([str(python), "-I", str(Path(__file__).with_name("wren_project_worker.py"))],
                            input=json.dumps({"operation": "validate_project", "descriptor": descriptor}, ensure_ascii=False),
                            text=True, capture_output=True, timeout=15, env=environment, check=False)
    if result.returncode or len(result.stdout) > 50000:
        raise ValueError("官方 Wren 编译校验进程未正常完成。")
    output = json.loads(result.stdout)
    if output.get("status") != "PASSED" or output.get("mdl_sha256") != descriptor["mdl_postgres_sha256"]:
        raise ValueError("持久模型未通过官方 Wren 编译对账。")
    return output


def _persist_project(directory: Path, descriptor: dict) -> None:
    """Create compiler-owned deterministic assets; never read them as authority."""
    def write(relative, value, yaml_file=True):
        content = (yaml.safe_dump(value, allow_unicode=True, sort_keys=False) if yaml_file
                   else json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        _atomic_cache_write(directory, relative, content)

    def snake(value):
        if isinstance(value, list):
            return [snake(v) for v in value]
        if isinstance(value, dict):
            return {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): snake(v) if k != "properties" else v for k, v in value.items()}
        return value

    write("wren_project.yml", {"schema_version": 5, "name": descriptor["project_id"], "version": descriptor["release_version"],
                               "catalog": "orion", "schema": "analysis", "data_source": "postgres"})
    mdl = descriptor["mdl_postgres"]
    for kind in ("models", "views", "cubes"):
        for item in mdl[kind]:
            write(f"{kind}/{item['name']}/metadata.yml", snake(item))
    write("relationships.yml", {"relationships": snake(mdl["relationships"])})
    write("knowledge/knowledge.yml", {"schema_version": 1})
    _atomic_cache_write(directory, "knowledge/rules/published-semantics.md", descriptor["knowledge"]["rules"] + "\n")
    write("target/mdl.json", mdl, yaml_file=False)
    write("binding.json", {k: descriptor[k] for k in ("key", "project_id", "release_version", "release_fingerprint", "mdl_postgres_sha256", "adapter_version")}, yaml_file=False)


def execute_project(payload: dict) -> dict:
    """In-process worker entry for environments with the pinned Wren packages."""
    from services.realtime_qa.wren_project_worker import execute
    return execute(payload)
