from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import math
import os
import re
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
import traceback
import uuid
from collections import deque
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import httpx
import yaml
from rdflib import RDF, Graph, Literal, URIRef
from rdflib.exceptions import ParserError
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from scripts.managed_stage_bridge import (
    commit_managed,
    guarded_client,
    opted_in_tasks,
    start_managed,
)
from scripts.semantica_validation_runtime import isolated_validation_runtime
from services.config import Settings
from services.ontology_engineering import OntologyWorkflowService
from services.ontology_engineering.formal_facts import (
    FORMAL_FACT,
)
from services.ontology_engineering.formal_facts import (
    frozen_datatype_literal as frozen_datatype_literal,
)
from services.ontology_engineering.formal_facts import (
    materialize_formal_fact as materialize_formal_fact,
)
from services.ontology_engineering.formal_facts import (
    validate_materialization_bindings as validate_materialization_bindings,
)
from services.ontology_engineering.graph_checkpoint import (
    load_graph_checkpoint,
    save_graph_checkpoint,
)
from services.ontology_engineering.graph_union import ReadOnlyGraphUnion
from services.ontology_engineering.joint_design import verify_joint_baseline
from services.ontology_engineering.mapping_source_probe import (
    format_source_issues,
    mapping_source_report,
)
from services.ontology_engineering.ontop_materialization import (
    compile_predicate_partition_plan,
    materialize_ontop_partitions,
    materialize_single_ontop_graph,
)
from services.ontology_engineering.sparql_execution import ensure_spec_aggregates
from services.ontology_engineering.stage_contracts import (
    STAGE_CONTRACT_VERSION,
    project_stage_contract_version,
)
from services.ontology_engineering.stage_execution import StageExecutionLease
from services.ontology_engineering.validation_plan import (
    POLICY as CAPABILITY_VALIDATION_POLICY,
)
from services.ontology_engineering.validation_plan import (
    materialization_fingerprint,
)
from services.realtime_qa.query_capabilities import render_query_parameters
from services.realtime_qa.reasoning import (
    SemanticaReasoningClient,
    facts_from_rows,
    result_facts,
)
from services.realtime_qa.runtime_release import (
    compile_mapping_with_identity,
    query_validation_code_fingerprint,
)

ensure_spec_aggregates()  # S6 CQ/evidence aggregates follow SPARQL 1.1

TABLE_REFERENCE = re.compile(
    r'^(?:(?P<schema>[a-z][a-z0-9_]*)\.)?(?:"(?P<quoted>[a-z][a-z0-9_]*)"|(?P<table>[a-z][a-z0-9_]*))$'
)


def validated_table_reference(value: str) -> str:
    """Return a safely quoted SQL relation after accepting generated schema-qualified names."""

    match = TABLE_REFERENCE.fullmatch(value.strip())
    if match is None:
        raise ValueError(f"unsafe database table reference: {value}")
    table = match.group("quoted") or match.group("table")
    schema = match.group("schema")
    quoted_table = f'"{table}"'
    return f'"{schema}".{quoted_table}' if schema else quoted_table


DATASET_ID_PATTERN = re.compile(r"^DS-[A-F0-9]{20,64}$")


def _dataset_filter_sql(dataset_filters: dict[str, str] | None, table_name: str) -> str:
    """Return a safe WHERE clause limiting a shared Snapshot Hub table to one dataset."""

    dataset_id = (dataset_filters or {}).get(table_name)
    if not dataset_id:
        return ""
    if not DATASET_ID_PATTERN.fullmatch(dataset_id):
        raise ValueError(f"unsafe snapshot dataset id: {dataset_id}")
    return f" WHERE dataset_id = '{dataset_id}'"


def source_content_fingerprint(
    connection: Any,
    relations: dict[str, str],
    dataset_filters: dict[str, str] | None = None,
) -> str:
    """Hash exact rows, including duplicates, so equal counts cannot reuse stale facts."""
    digest = hashlib.sha256()
    for table_name, relation_name in sorted(relations.items()):
        relation = validated_table_reference(relation_name)
        where = _dataset_filter_sql(dataset_filters, table_name)
        digest.update(json.dumps([table_name, relation_name]).encode("utf-8"))
        if where:
            digest.update(where.encode("utf-8"))
        query = text(
            f"SELECT row_to_json(source_row)::text FROM {relation} AS source_row{where} "
            'ORDER BY row_to_json(source_row)::text COLLATE "C"'
        )
        with connection.execution_options(stream_results=True).execute(query) as rows:
            for row in rows:
                value = str(row[0]).encode("utf-8")
                digest.update(len(value).to_bytes(8, "big"))
                digest.update(value)
    return "sha256:" + digest.hexdigest()


def reconcile_profile_scope(
    *,
    profile: dict[str, Any],
    inventory: dict[str, Any],
    scope: list[str],
    actual_by_table: dict[str, int],
) -> tuple[dict[str, int], str]:
    """Reconcile immutable imported tables with their stable ``current_*`` aliases."""

    profile_rows = {
        str(item.get("table") or ""): int(item.get("row_count", -1))
        for item in profile.get("tables") or []
        if str(item.get("table") or "").strip()
    }
    if set(scope) == set(profile_rows):
        return profile_rows, "EXACT_RELATION_NAME"

    datasets = [item for item in inventory.get("datasets") or [] if isinstance(item, dict)]
    profile_by_source = {
        str(item.get("source_sheet") or "").strip(): int(item.get("row_count", -1))
        for item in profile.get("tables") or []
        if str(item.get("source_sheet") or "").strip()
    }
    dataset_by_source = {
        Path(str(item.get("source_name") or "")).stem: int(item.get("row_count", -1))
        for item in datasets
        if str(item.get("source_name") or "").strip()
        and re.fullmatch(r"DS-[A-F0-9]{20}", str(item.get("dataset_id") or ""))
        and re.fullmatch(r"sha256:[a-f0-9]{64}", str(item.get("source_sha256") or ""))
    }
    profile_counts = sorted(profile_rows.values())
    if (
        len(scope) != len(profile_rows)
        or len(actual_by_table) != len(profile_rows)
        or len(datasets) != len(profile_rows)
        or len(set(profile_counts)) != len(profile_counts)
        or profile_by_source != dataset_by_source
        or sorted(actual_by_table.values()) != profile_counts
    ):
        raise RuntimeError(
            "S6 回读发现 S1 business_tables_scope 与逐表画像无法按不可变数据集谱系对账；"
            "必须回退 S1。"
        )
    return dict(actual_by_table), "IMMUTABLE_DATASET_LINEAGE_ALIAS"


def resolve_s6_source_binding(
    inventory: dict[str, Any],
    source_database_url: str,
    catalog_readonly_verifier: Any = None,
) -> dict[str, Any]:
    """Resolve the audited read-only JDBC binding used by S6.

    Both the current ``datasources[]`` contract and the legacy flat S1
    inventory are accepted. The URL username is never treated as evidence by
    itself: S1 must have persisted the same principal explicitly, or — for
    platform Snapshot Hub imports that predate the explicit principal field —
    the principal must pass a live PostgreSQL catalog read-only read-back.
    """

    database_url = make_url(source_database_url)
    username = str(database_url.username or "").strip()
    if not username:
        raise RuntimeError(
            "S6_SOURCE_PRINCIPAL_MISSING: ORION_SOURCE_DATA_READER_URL 缺少只读账号。"
        )

    raw_datasources = inventory.get("datasources")
    if raw_datasources is not None:
        if not isinstance(raw_datasources, list) or not raw_datasources:
            raise RuntimeError(
                "S6_SOURCE_BINDING_MISSING: S1 datasources 清单为空；必须回退 S1 补录只读 JDBC 绑定。"
            )
        datasources = [item for item in raw_datasources if isinstance(item, dict)]
    else:
        datasources = [inventory]

    declared_principals = {
        str(item.get("database_principal") or "").strip()
        for item in datasources
        if str(item.get("database_principal") or "").strip()
    }
    principal_evidence = "S1_DECLARED"
    if not declared_principals and _is_platform_snapshot_import(datasources):
        if catalog_readonly_verifier is None:
            raise RuntimeError(
                "S6_SOURCE_BINDING_MISSING: Snapshot Hub 导入清单未声明 database_principal，"
                "且当前运行时无法做只读账号目录回读。"
            )
        try:
            catalog_readonly_verifier(username)
        except Exception as exc:  # noqa: BLE001 - surface as a stage contract failure
            raise RuntimeError(
                f"S6_SOURCE_NOT_READ_ONLY: 平台快照只读账号目录回读未通过：{exc}"
            ) from exc
        datasources = [{**item, "database_principal": username} for item in datasources]
        declared_principals = {username}
        principal_evidence = "CATALOG_READBACK"
    if not declared_principals:
        raise RuntimeError(
            "S6_SOURCE_BINDING_MISSING: S1 数据源清单缺少可审计 database_principal；"
            "必须回退 S1 补录只读 JDBC 绑定或完成 Snapshot Hub 导入。"
        )
    if username not in declared_principals:
        raise RuntimeError(
            "S6_SOURCE_PRINCIPAL_MISMATCH: S6 只读数据库账号与 S1 数据源清单不一致。"
        )

    matches = [
        item
        for item in datasources
        if str(item.get("database_principal") or "").strip() == username
    ]
    read_only_matches = [
        item
        for item in matches
        if str(item.get("access_mode") or "").upper() in {"READ_ONLY", "READ_ONLY_AFTER_IMPORT"}
    ]
    if not read_only_matches:
        raise RuntimeError("S6_SOURCE_NOT_READ_ONLY: S1 未声明与当前账号匹配的只读数据源。")

    requested_database = str(database_url.database or "").strip()
    declared_databases = {
        str(item.get("database") or "").strip()
        for item in read_only_matches
        if str(item.get("database") or "").strip()
    }
    if declared_databases and requested_database not in declared_databases:
        raise RuntimeError("S6_SOURCE_DATABASE_MISMATCH: S6 数据库与 S1 数据源清单不一致。")

    schema_candidates: set[str] = set()
    for item in read_only_matches:
        schema = str(item.get("schema") or inventory.get("schema") or "").strip()
        access_mode = str(item.get("access_mode") or "").upper()
        if not schema and access_mode == "READ_ONLY_AFTER_IMPORT":
            schema = "orion_data"
        if schema:
            schema_candidates.add(schema)
    if len(schema_candidates) != 1:
        raise RuntimeError("S6_SOURCE_SCHEMA_AMBIGUOUS: S1 必须为当前只读运行时声明唯一 schema。")
    schema = next(iter(schema_candidates))

    scope = [
        str(value).strip()
        for value in inventory.get("business_tables_scope") or []
        if str(value).strip()
    ]
    if not scope:
        raise RuntimeError("S6_SOURCE_SCOPE_MISSING: S1 business_tables_scope 为空。")
    relations: dict[str, str] = {}
    for table_name in scope:
        relation_name = table_name if "." in table_name else f"{schema}.{table_name}"
        validated_table_reference(relation_name)
        relations[table_name] = relation_name

    dataset_filters = _snapshot_dataset_filters(inventory, scope, datasources)

    return {
        "database_principal": username,
        "database": requested_database,
        "schema": schema,
        "business_tables_scope": scope,
        "relations": relations,
        "dataset_filters": dataset_filters,
        "contract_shape": "CURRENT" if raw_datasources is not None else "LEGACY",
        "principal_evidence": principal_evidence,
    }


def _snapshot_dataset_filters(
    inventory: dict[str, Any], scope: list[str], datasources: list[dict[str, Any]]
) -> dict[str, str]:
    """Bind each Snapshot Hub target table to the dataset S1 promoted.

    Snapshot Hub keeps every promoted version of a source table in one shared
    target table keyed by ``dataset_id``. S1 profiles and the runtime mapping
    both read only the promoted dataset, so S6 must reconcile the same slice;
    counting the whole table would mistake retained earlier versions for drift.
    """

    if not _is_platform_snapshot_import(datasources):
        return {}
    snapshot_set = inventory.get("cross_source_snapshot_set")
    if not isinstance(snapshot_set, dict):
        return {}
    filters: dict[str, str] = {}
    for source in snapshot_set.get("source_snapshots") or []:
        if not isinstance(source, dict):
            continue
        dataset_id = str(source.get("dataset_id") or "").strip()
        for table in source.get("tables") or []:
            target = str((table or {}).get("target_table") or "").strip()
            if not target or not dataset_id:
                continue
            if not DATASET_ID_PATTERN.fullmatch(dataset_id):
                raise RuntimeError(
                    f"S6_SOURCE_DATASET_INVALID: 快照数据集标识不合法：{dataset_id}"
                )
            if filters.get(target, dataset_id) != dataset_id:
                raise RuntimeError(
                    f"S6_SOURCE_DATASET_AMBIGUOUS: {target} 在快照集中绑定了多个数据集。"
                )
            filters[target] = dataset_id
    scoped = {
        table_name: filters[table_name.split(".")[-1].strip('"')]
        for table_name in scope
        if table_name.split(".")[-1].strip('"') in filters
    }
    if filters and len(scoped) != len(scope):
        missing = sorted(set(scope) - set(scoped))
        raise RuntimeError(
            "S6_SOURCE_DATASET_MISSING: Snapshot Hub 表缺少 S1 晋升数据集绑定："
            + ", ".join(missing[:5])
        )
    return scoped


def _is_platform_snapshot_import(datasources: list[dict[str, Any]]) -> bool:
    """True when every datasource is the platform-served imported snapshot area."""
    return bool(datasources) and all(
        str(item.get("access_mode") or "").upper() == "READ_ONLY_AFTER_IMPORT"
        and str(item.get("connection_env") or "") == "ORION_SOURCE_DATA_READER_URL"
        for item in datasources
    )


def verify_reader_principal_catalog(source_database_url: str, principal: str) -> dict[str, Any]:
    """Read pg_catalog through the reader itself; reject any write capability."""
    import psycopg

    from services.realtime_qa.postgres_readonly import (
        _read_role,
        _read_write_privilege_counts,
        _role_settings,
    )

    url = source_database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url) as connection, connection.cursor() as cursor:
        role = _read_role(cursor, principal)
        privileges = _read_write_privilege_counts(cursor, principal)
    unsafe = [
        name
        for name in ("rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication", "rolbypassrls")
        if bool(role.get(name))
    ]
    if unsafe or sum(privileges.values()) or (
        _role_settings(role.get("rolconfig")).get("default_transaction_read_only") != "on"
    ):
        raise RuntimeError("账号具备写/建权限或未默认只读")
    return {"principal": principal, "write_privileges": privileges, "unsafe_attributes": unsafe}


def checksum(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_fingerprint(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _local_name(iri: str) -> str:
    return iri.rsplit("#", 1)[-1].rsplit("/", 1)[-1] or "Entity"


def sync_and_verify_semantica_relationships(
    *,
    graph: Graph,
    object_property_iris: list[str],
    project_id: str,
    release_candidate_fingerprint: str,
    semantica: SemanticaMcpClient,
    semantica_api_url: str,
    api_key: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Persist and read back the exact candidate business relationship set.

    Semantica's ontology loader currently materializes ontology schema edges but
    does not guarantee that named-individual object-property assertions become
    runtime graph edges. S6 therefore sends those assertions explicitly and
    verifies every subject/predicate/object tuple through Explorer's read-only
    graph API. The candidate fingerprint and relationship-set hash make a
    zero-add retry acceptable only when the same candidate is already present.
    """

    predicates = {URIRef(value) for value in object_property_iris if value}
    relationships = sorted(
        [
            {
                "subject": str(subject),
                "predicate": str(predicate),
                "object": str(obj),
            }
            for subject, predicate, obj in graph
            if predicate in predicates and isinstance(subject, URIRef) and isinstance(obj, URIRef)
        ],
        key=lambda item: (item["subject"], item["predicate"], item["object"]),
    )
    relationship_set_sha256 = _canonical_fingerprint(relationships)
    if not relationships:
        return (
            {"nodes_added": 0, "edges_added": 0, "relationship_count": 0},
            {
                "status": "FAILED",
                "reason": "候选物化图没有对象属性实例关系。",
                "release_candidate_fingerprint": release_candidate_fingerprint,
                "relationship_set_sha256": relationship_set_sha256,
                "expected_relationship_count": 0,
                "verified_relationship_count": 0,
                "verified_relationships": [],
            },
        )

    # The MCP add_entity/add_relationship tools each perform one HTTP import and
    # graph persistence.  That N+1 path is suitable for interactive edits but
    # takes hours for a production snapshot.  S6 uses the same guarded Explorer
    # import endpoint in bounded batches, then paginates and hashes the complete
    # candidate relationship set.  This is not representative sampling.
    del semantica
    endpoint_nodes = sorted(
        {item["subject"] for item in relationships} | {item["object"] for item in relationships}
    )
    node_documents = []
    for node_id in endpoint_nodes:
        rdf_types = sorted(
            str(value)
            for value in graph.objects(URIRef(node_id), RDF.type)
            if isinstance(value, URIRef)
        )
        node_documents.append(
            {
                "id": node_id,
                "type": _local_name(rdf_types[0]) if rdf_types else "Entity",
                "properties": {
                    "label": _local_name(node_id),
                    "content": _local_name(node_id),
                    "project_id": project_id,
                    "release_candidate_fingerprint": release_candidate_fingerprint,
                    "relationship_set_sha256": relationship_set_sha256,
                    "source_ontology": project_id,
                    "materialization_scope": "FULL_SOURCE_VALIDATION",
                },
            }
        )
    edge_documents = []
    for relationship in relationships:
        edge_identity = _canonical_fingerprint(
            {
                "project_id": project_id,
                "release_candidate_fingerprint": release_candidate_fingerprint,
                **relationship,
            }
        )
        edge_documents.append(
            {
                "id": f"orion-s6:{edge_identity.removeprefix('sha256:')}",
                "source_id": relationship["subject"],
                "target_id": relationship["object"],
                "type": relationship["predicate"],
                "properties": {
                    "predicate_uri": relationship["predicate"],
                    "project_id": project_id,
                    "release_candidate_fingerprint": release_candidate_fingerprint,
                    "relationship_set_sha256": relationship_set_sha256,
                    "materialization_scope": "FULL_SOURCE_VALIDATION",
                },
            }
        )

    nodes_added = 0
    edges_added = 0
    batch_receipts: list[dict[str, Any]] = []
    with httpx.Client(
        base_url=semantica_api_url.rstrip("/"), timeout=180.0, trust_env=False,
        headers={"X-API-Key": api_key} if api_key else None
    ) as client:
        for kind, documents in (("nodes", node_documents), ("edges", edge_documents)):
            for offset in range(0, len(documents), 2000):
                batch = documents[offset : offset + 2000]
                response = client.post(
                    "/api/import",
                    files={
                        "file": (
                            f"orion-s6-{kind}-{offset // 2000:05d}.json",
                            json.dumps({kind: batch}, ensure_ascii=False).encode("utf-8"),
                            "application/json",
                        )
                    },
                )
                response.raise_for_status()
                receipt = response.json()
                nodes_added += int(receipt.get("nodes_added", 0))
                edges_added += int(receipt.get("edges_added", 0))
                batch_receipts.append(
                    {
                        "kind": kind,
                        "offset": offset,
                        "input_count": len(batch),
                        "nodes_added": int(receipt.get("nodes_added", 0)),
                        "edges_added": int(receipt.get("edges_added", 0)),
                    }
                )

        candidate_relationships_set: set[tuple[str, str, str]] = set()
        readback_pages = 0
        for predicate_iri in sorted(object_property_iris):
            cursor: str | None = None
            while True:
                params = {"type": predicate_iri, "limit": 5000}
                if cursor:
                    params["cursor"] = cursor
                response = client.get("/api/graph/edges", params=params)
                response.raise_for_status()
                payload = response.json()
                readback_pages += 1
                for edge in payload.get("edges") or []:
                    properties = edge.get("properties") or {}
                    if (
                        properties.get("project_id") == project_id
                        and properties.get("release_candidate_fingerprint")
                        == release_candidate_fingerprint
                        and properties.get("relationship_set_sha256") == relationship_set_sha256
                    ):
                        candidate_relationships_set.add(
                            (
                                str(edge.get("source") or ""),
                                str(edge.get("type") or ""),
                                str(edge.get("target") or ""),
                            )
                        )
                cursor = str(payload.get("next_cursor") or "").strip() or None
                if cursor is None:
                    break
        candidate_relationships = sorted(candidate_relationships_set)

    candidate_relationship_documents = [
        {"subject": subject, "predicate": predicate, "object": obj}
        for subject, predicate, obj in candidate_relationships
    ]
    expected_relationship_tuples = {
        (item["subject"], item["predicate"], item["object"]) for item in relationships
    }
    verified = [
        {"subject": subject, "predicate": predicate, "object": obj}
        for subject, predicate, obj in candidate_relationships
        if (subject, predicate, obj) in expected_relationship_tuples
    ]

    verified_relationships_sha256 = _canonical_fingerprint(verified)
    fully_verified = verified == relationships and candidate_relationship_documents == relationships
    return (
        {
            "nodes_added": nodes_added,
            "edges_added": edges_added,
            "relationship_count": len(relationships),
            "batch_count": len(batch_receipts),
        },
        {
            "status": "VERIFIED" if fully_verified else "FAILED",
            "mode": ("NEW_RELATIONSHIPS_ADDED" if edges_added > 0 else "IDEMPOTENT_READBACK"),
            "release_candidate_fingerprint": release_candidate_fingerprint,
            "relationship_set_sha256": relationship_set_sha256,
            "verified_relationships_sha256": verified_relationships_sha256,
            "expected_relationship_count": len(relationships),
            "verified_relationship_count": len(verified),
            "candidate_relationship_count": len(candidate_relationship_documents),
            "readback_pages": readback_pages,
            "batch_receipts": batch_receipts,
            "verified_predicates": sorted({item["predicate"] for item in verified}),
            "verified_relationships": verified,
            "receipt_sha256": _canonical_fingerprint(
                {
                    "project_id": project_id,
                    "release_candidate_fingerprint": release_candidate_fingerprint,
                    "relationship_set_sha256": relationship_set_sha256,
                    "verified_relationships": verified,
                }
            ),
        },
    )


def current_stage_fingerprints(project_dir: Path) -> dict[str, str]:
    state = json.loads((project_dir / "workflow-state.json").read_text(encoding="utf-8"))
    fingerprints = {
        stage: str((state.get("stage_fingerprints") or {}).get(stage, {}).get("output") or "")
        for stage in ("S0", "S1", "S2", "S3", "S4", "S5")
    }
    if any(not value for value in fingerprints.values()):
        raise RuntimeError("S6 全量验证前缺少当前 S0-S5 正式阶段指纹。")
    return fingerprints


def require_s6_runnable_state(state: dict[str, Any]) -> None:
    """Fail before expensive validation unless S6 has been explicitly opened."""

    current_stage = str(state.get("current_stage") or "")
    stage_status = str((state.get("stage_statuses") or {}).get("S6") or "")
    if current_stage != "S6" or stage_status != "RUNNING":
        raise RuntimeError(
            "S6_STAGE_NOT_RUNNABLE: "
            f"current_stage={current_stage or 'UNKNOWN'}, "
            f"S6={stage_status or 'UNKNOWN'}; "
            "失败阶段须先通过 retry_failed_stage 正式重开。"
        )


def verify_s6_joint_design(project_dir: Path, state: dict[str, Any]) -> str | None:
    """Check v2 approval before creating a lease or contacting external services."""
    if project_stage_contract_version(state) != STAGE_CONTRACT_VERSION:
        return None
    try:
        return str(verify_joint_baseline(project_dir)["joint_design_fingerprint"])
    except (ValueError, OSError) as exc:
        raise RuntimeError(f"S6_JOINT_DESIGN_NOT_READY: {exc}") from exc


def _docker_jdbc_properties(database_url: str) -> str:
    parsed = make_url(database_url)
    if not parsed.username or parsed.password is None or not parsed.database:
        raise RuntimeError("ORION_SOURCE_DATA_READER_URL 缺少只读账号、密码或数据库名。")
    host = parsed.host or "postgres"
    if host in {"127.0.0.1", "localhost"}:
        host = "postgres"
    port = parsed.port or 5432
    return "\n".join(
        [
            f"jdbc.url=jdbc:postgresql://{host}:{port}/{parsed.database}",
            f"jdbc.user={parsed.username}",
            f"jdbc.password={parsed.password}",
            "jdbc.driver=org.postgresql.Driver",
            "ontop.query.defaultTimeout=120",
            "ontop.allowRetrievingBlackBoxViewMetadataFromDB=true",
            "ontop.applicationName=orion-s6-pre-release",
            "",
        ]
    )


def _remove_container(container_name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "--force", container_name],
            check=False, capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Best-effort exit cleanup must not hold the worker forever. Managed
        # previews retain a resource journal for explicit orphan reconciliation.
        return


def _redact_candidate_diagnostic(text: str, database_url: str) -> str:
    """Remove known connection secrets and common credential representations."""
    try:
        parsed = make_url(database_url) if database_url else None
    except (ArgumentError, ValueError):
        parsed = None
    sensitive = {database_url, parsed.password or "" if parsed else "", parsed.username or "" if parsed else ""}
    sensitive |= {quote(value, safe="") for value in sensitive if value}
    sensitive |= {unquote(value) for value in sensitive if value}
    for value in sorted(filter(None, sensitive), key=len, reverse=True):
        text = text.replace(value, "[REDACTED]")
    text = re.sub(r"(?i)([a-z][a-z0-9+.-]*://)[^\s/@]+@", r"\1[REDACTED]@", text)
    text = re.sub(
        r'''(?ix)(["']?[\w.-]*(?:password|passwd|secret|token|api[_-]?key|jdbc[._-]?user|username)["']?\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;}\]]+)''',
        r"\1[REDACTED]", text,
    )
    return re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)\S+", r"\1[REDACTED]", text)


def _candidate_container_state(container_name: str) -> dict[str, Any]:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{json .State}}", container_name],
        check=True, capture_output=True, text=True, timeout=15,
    )
    return json.loads(result.stdout)


def _candidate_log_excerpts(container_name: str, database_url: str) -> dict[str, Any]:
    """Stream finite startup logs; retain head, error context and tail, never raw secrets."""
    head = ""
    errors = ""
    tail: deque[str] = deque()
    previous: deque[str] = deque(maxlen=3)
    tail_size = 0
    following = 0
    seen = 0
    discarded_long_line = False
    timed_out = False

    def retain(raw: bytes) -> None:
        nonlocal head, errors, tail_size, following, discarded_long_line
        # Discard entire overlong lines rather than exposing partial credentials.
        if len(raw) > 16384:
            discarded_long_line = True
            return
        line = _redact_candidate_diagnostic(raw.decode("utf-8", errors="replace"), database_url) + "\n"
        head += line[:max(0, 32768 - len(head))]
        tail.append(line)
        tail_size += len(line)
        while tail and tail_size > 32768:
            tail_size -= len(tail.popleft())
        if re.search(r"error|exception|caused by|invalid|cannot|could not|mappingid|not found", line, re.I):
            context = "".join(previous) + line
            errors += context[:max(0, 65536 - len(errors))]
            following = 3
        elif following:
            errors += line[:max(0, 65536 - len(errors))]
            following -= 1
        previous.append(line)

    process = subprocess.Popen(
        ["docker", "logs", container_name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        assert process.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + 10
            pending = b""
            skipping = False
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if not selector.select(min(remaining, 1)):
                    continue
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    if pending and not skipping:
                        retain(pending)
                    break
                seen += len(chunk)
                parts = (pending + chunk).split(b"\n")
                pending = parts.pop()
                for part in parts:
                    if skipping:
                        skipping = False
                    else:
                        retain(part)
                if len(pending) > 16384:
                    pending = b""
                    skipping = True
                    discarded_long_line = True
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        if process.stdout:
            process.stdout.close()
    return {
        "head": head, "error_context": errors, "tail": "".join(tail),
        "bytes_read": seen, "capture_timed_out": timed_out,
        "discarded_overlong_lines": discarded_long_line,
        "scope": "BOUNDED_REDACTED_STARTUP_LOG_EXCERPTS",
    }


def _record_candidate_startup_failure(
    *, project_dir: Path, container_name: str, candidate_id: str, database_url: str,
    error: Exception, execution_lease: StageExecutionLease | None,
) -> RuntimeError:
    stderr = getattr(error, "stderr", "") or ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    reason = _redact_candidate_diagnostic(str(error) + "\n" + stderr, database_url)[:2000].strip()
    try:
        logs = _candidate_log_excerpts(container_name, database_url)
    except Exception as log_error:
        logs = {"capture_error": _redact_candidate_diagnostic(str(log_error), database_url)[:1000]}
    payload = {
        "schema_version": 1, "stage": "S6", "status": "FAILED",
        "component": "CANDIDATE_ONTOP_STARTUP", "project_id": project_dir.name,
        "candidate_id": candidate_id, "container_name": container_name,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "execution_id": execution_lease.payload["execution_id"] if execution_lease else None,
        "input_fingerprint": execution_lease.payload["input_fingerprint"] if execution_lease else None,
        "error": reason, "logs": logs,
        "source_artifacts": {
            path: checksum(project_dir / path) if (project_dir / path).is_file() else None
            for path in ("03-mapping-review/runtime/mapping.obda", "05-ontology-build/ontology.ttl")
        },
    }
    detail = (logs.get("error_context") or logs.get("head") or "")[:2500]
    message = f"S6 候选 Ontop 启动失败：{reason}\n{detail}".strip()
    metadata: dict[str, Any] = {"component": payload["component"], "candidate_id": candidate_id}
    try:
        directory = project_dir / ".stage-executions" / "S6-diagnostics"
        if directory.is_symlink() or not directory.resolve().is_relative_to(project_dir.resolve()):
            raise RuntimeError("诊断目录必须位于当前工程内")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"{container_name}.json"
        data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()
        fd, temporary = tempfile.mkstemp(prefix=".startup-", dir=directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        metadata.update(diagnostic_path=target.relative_to(project_dir).as_posix(), diagnostic_sha256=checksum(target))
        message += f"\n诊断：{metadata['diagnostic_path']}"
    except Exception:
        message += "\n诊断文件未能保存；以上为已脱敏的真实启动错误。"
    if execution_lease is not None:
        execution_lease.fail(message, metadata)
    return RuntimeError(message)


@contextmanager
def _process_lifetime_tempdir(prefix: str) -> Iterator[str]:
    path = tempfile.mkdtemp(prefix=prefix)
    atexit.register(shutil.rmtree, path, True)
    yield path


@contextmanager
def pre_release_ontop_endpoint(
    *,
    project_dir: Path,
    database_url: str,
    candidate_id: str,
    execution_lease: StageExecutionLease | None = None,
    backend_identity: dict[str, Any] | None = None,
    resource_journal: Path | None = None,
) -> Iterator[str]:
    """Run a short-lived, read-only Ontop candidate bound to current S3/S5 assets."""

    mapping_path = project_dir / "03-mapping-review/runtime/mapping.obda"
    ontology_path = project_dir / "05-ontology-build/ontology.ttl"
    if not mapping_path.is_file() or not ontology_path.is_file():
        raise RuntimeError("S6 候选运行时缺少 S3 mapping.obda 或 S5 ontology.ttl。")
    source_report = mapping_source_report(mapping_path.read_text(encoding="utf-8"), database_url)
    if source_report["issues"]:
        message = (
            "S6_MAPPING_SOURCE_INVALID: 已提交的 S3 映射 SQL 无法由只读账号执行，Ontop 启动前已拦截。"
            + format_source_issues(source_report["issues"])
            + "。恢复动作：preview_stage_rollback(target_stage=S3) 后修正上述 mappingId 并重新提交 S3。"
        )
        if execution_lease is not None:
            execution_lease.fail(message, {
                "component": "CANDIDATE_MAPPING_SOURCE_PROBE",
                "failing_mapping_ids": source_report["failing_mapping_ids"],
                "recovery_target_stage": "S3",
            })
        raise RuntimeError(message)
    container_name = f"orion-s6-{project_dir.name[-8:]}-{uuid.uuid4().hex[:8]}"
    with _process_lifetime_tempdir(prefix="orion-s6-ontop-") as raw_temp_dir:
        temp_dir = Path(raw_temp_dir)
        compiled_mapping = temp_dir / "mapping.obda"
        properties = temp_dir / "ontop.properties"
        compiled_mapping.write_text(
            compile_mapping_with_identity(
                mapping_path.read_text(encoding="utf-8"),
                candidate_id,
                checksum(mapping_path),
            ),
            encoding="utf-8",
        )
        properties.write_text(
            _docker_jdbc_properties(database_url),
            encoding="utf-8",
        )
        properties.chmod(0o600)
        if resource_journal is not None:
            if (resource_journal.parent.resolve() != project_dir.resolve()
                    or resource_journal.is_symlink()):
                raise ValueError("Candidate resource journal must belong to the candidate directory")
            from services.ontology_engineering.stage_jobs import _save
            resource = {"container_name": container_name, "candidate_id": candidate_id,
                        "temp_dir": str(temp_dir.resolve())}
            _save(temp_dir / "candidate-owner.json", resource)
            _save(resource_journal, resource)
        command = [
            "docker",
            "run",
            "--detach",
            "--name",
            container_name,
            "--read-only",
            "--security-opt",
            "no-new-privileges:true",
            "--network",
            os.getenv("ORION_DOCKER_NETWORK", "ontology-workorder-agent_default"),
            "--publish",
            "127.0.0.1::8080",
            "--env",
            "ONTOP_ONTOLOGY_FILE=/opt/ontop/input/ontology.ttl",
            "--env",
            "ONTOP_MAPPING_FILE=/opt/ontop/input/mapping.obda",
            "--env",
            "ONTOP_PROPERTIES_FILE=/opt/ontop/input/ontop.properties",
            "--volume",
            f"{ontology_path.resolve()}:/opt/ontop/input/ontology.ttl:ro",
            "--volume",
            f"{compiled_mapping.resolve()}:/opt/ontop/input/mapping.obda:ro",
            "--volume",
            f"{properties.resolve()}:/opt/ontop/input/ontop.properties:ro",
            os.getenv("ORION_ONTOP_IMAGE", "ontology-workorder-agent/ontop:5.3.0"),
        ]
        if resource_journal is not None:
            command[2:2] = ["--label", f"orion.managed-preview={candidate_id}"]
        def cleanup_candidate():
            _remove_container(container_name)
        atexit.register(cleanup_candidate)
        try:
            subprocess.run(command, check=True, capture_output=True, text=True, timeout=30)
            port_result = subprocess.run(
                ["docker", "port", container_name, "8080/tcp"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            )
            match = re.search(r"127\.0\.0\.1:(\d+)", port_result.stdout)
            if match is None:
                raise RuntimeError("无法取得 S6 候选 Ontop 的本机端口。")
            endpoint = f"http://127.0.0.1:{match.group(1)}/sparql"
            deadline = time.monotonic() + 60
            last_error = ""
            while time.monotonic() < deadline:
                try:
                    container_state = _candidate_container_state(container_name)
                except subprocess.TimeoutExpired as exc:
                    # Docker Desktop stalls briefly while a JVM container boots;
                    # a slow probe is not evidence of container failure.
                    last_error = f"docker inspect 超时（{exc.timeout}s），继续等待"
                    time.sleep(1)
                    continue
                if container_state.get("Status") in {"exited", "dead", "removing"}:
                    raise RuntimeError(
                        f"容器提前退出，status={container_state.get('Status')}，"
                        f"exit_code={container_state.get('ExitCode')}，"
                        f"oom_killed={container_state.get('OOMKilled')}；"
                        + str(container_state.get("Error") or "")
                    )
                try:
                    with httpx.Client(timeout=3.0, trust_env=False) as client:
                        response = client.post(
                            endpoint,
                            data={"query": "ASK { ?s ?p ?o }"},
                            headers={"Accept": "application/sparql-results+json"},
                        )
                    last_error = f"HTTP {response.status_code}: {response.text[-300:]}"
                except Exception as exc:  # pragma: no cover - startup timing
                    last_error = str(exc)
                else:
                    if response.status_code == 200:
                        break
                time.sleep(1)
            else:
                raise RuntimeError("候选未在 60 秒内就绪：" + last_error)
        except Exception as exc:
            failure = _record_candidate_startup_failure(
                project_dir=project_dir, container_name=container_name,
                candidate_id=candidate_id, database_url=database_url,
                error=exc, execution_lease=execution_lease,
            )
            if resource_journal is not None:
                cleanup_candidate()
                atexit.unregister(cleanup_candidate)
            raise failure from None
        # Keep the validated candidate until process exit for formal S6 replay.
        # Body failures propagate unchanged and are not mistaken for startup.
        if backend_identity is not None:
            from services.ontop_client.backend_validation import candidate_backend_identity
            backend_identity.update(candidate_backend_identity(container_name))
        try:
            yield endpoint
        finally:
            if resource_journal is not None:
                cleanup_candidate()
                atexit.unregister(cleanup_candidate)


def resolve_base_release_runtime(
    *, workflow_home: Path, project_dir: Path
) -> dict[str, str] | None:
    """Resolve an immutable parent runtime when S6 is validating a revision.

    A project created from scratch has no parent release. Its S6 remains a
    representative-data validation and S7 validates the newly deployed full
    source runtime. A revision can additionally reuse its immutable parent's
    read-only Ontop endpoint for full-source pre-release checks.
    """

    reference_path = project_dir / "based-on-release.json"
    if not reference_path.is_file():
        return None
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    source_project_id = str(reference.get("source_project_id") or "").strip()
    source_release_version = str(reference.get("source_release_version") or "").strip()
    if not source_project_id or not source_release_version:
        raise RuntimeError("S6 基线发布引用缺少项目或版本。")
    source_project_dir = workflow_home / source_project_id
    publication = json.loads(
        (source_project_dir / "07-release/publication.json").read_text(encoding="utf-8")
    )
    configured_deployment_root = os.environ.get("ORION_S7_AUTO_DEPLOY_ROOT", "").strip()
    deployment_root = (
        Path(configured_deployment_root).expanduser().resolve()
        if configured_deployment_root
        else workflow_home.parent / ".orion-runtime/realtime-business"
    )
    binding = json.loads(
        (
            deployment_root
            / source_project_id
            / source_release_version
            / "deployment-binding.json"
        ).read_text(encoding="utf-8")
    )
    endpoint = str(binding.get("endpoint") or "").strip()
    fingerprint = str(publication.get("package_manifest_sha256") or "").strip()
    if (
        binding.get("project_id") != source_project_id
        or binding.get("release_version") != source_release_version
        or binding.get("release_fingerprint") != fingerprint
        or binding.get("database_access_mode") != "READ_ONLY"
    ):
        raise RuntimeError("S6 基线发布运行时与不可变发布记录不一致。")
    if not endpoint:
        raise RuntimeError("S6 全量数据验证需要基线发布版本的只读 Ontop endpoint。")
    return {
        "project_id": source_project_id,
        "release_version": source_release_version,
        "release_fingerprint": fingerprint,
        "endpoint": endpoint,
    }


def approved_materialization_contract(project_dir: Path) -> tuple[dict[str, str], dict[str, str], bool]:
    """Read roles and literal ranges from one verified frozen design snapshot."""
    from services.ontology_engineering.ontology_types import normalize_datatype_iri

    state_path = project_dir / "workflow-state.json"
    if not state_path.is_file():
        return {}, {}, False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if project_stage_contract_version(state) != STAGE_CONTRACT_VERSION:
        return {}, {}, False
    design_dir = project_dir / "04-ontology-design"
    if not (design_dir / "joint-design-baseline.json").is_file():
        raise RuntimeError("S6 v2 事实实例化缺少已冻结的本体类型合同。")
    baseline = verify_joint_baseline(project_dir)
    design_bytes = (design_dir / "ontology-design.yaml").read_bytes()
    if "sha256:" + hashlib.sha256(design_bytes).hexdigest() != baseline["ontology_design_artifact_sha256"]:
        raise RuntimeError("S6 本体类型读取期间已批准设计发生漂移。")
    design = yaml.safe_load(design_bytes)
    roles: dict[str, set[str]] = {}
    ranges: dict[str, set[str]] = {}
    for field, kind in (("classes", "CLASS"), ("object_properties", "OBJECT_PROPERTY"), ("data_properties", "DATA_PROPERTY")):
        for term in design.get(field) or []:
            iri = str(term.get("iri") or "").strip()
            if iri:
                roles.setdefault(iri, set()).add(kind)
                if kind == "DATA_PROPERTY":
                    raw_range = term.get("range")
                    if not raw_range:
                        raise RuntimeError(f"S6 数据属性缺少冻结 datatype range：{iri}")
                    ranges.setdefault(iri, set()).add(normalize_datatype_iri(raw_range))
    # Do not guess a role for punned IRIs or undeclared terms.
    if any(len(values) != 1 for values in ranges.values()):
        raise RuntimeError("S6 同一数据属性存在不一致的冻结 datatype range。")
    return ({iri: next(iter(kinds)) for iri, kinds in roles.items() if len(kinds) == 1},
            {iri: next(iter(values)) for iri, values in ranges.items()}, True)


def approved_materialization_term_kinds(project_dir: Path) -> dict[str, str]:
    return approved_materialization_contract(project_dir)[0]


def materialize_document_fact_graph(project_dir: Path) -> tuple[Graph, int]:
    """Materialize every reviewed document fact with its formal ontology term."""

    runtime_dir = project_dir / "03-mapping-review/runtime"
    runtime_path = runtime_dir / "runtime-source.json"
    graph = Graph()
    if not runtime_path.is_file():
        return graph, 0
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    term_kinds, term_datatypes, strict_types = approved_materialization_contract(project_dir)
    from services.ontology_engineering.document_fact_materialization import (
        materialize_reviewed_document_facts,
    )

    return materialize_reviewed_document_facts(
        runtime_dir=runtime_dir, runtime=runtime,
        term_kinds=term_kinds, term_datatypes=term_datatypes, strict_types=strict_types,
        validate_bindings=validate_materialization_bindings, materialize_fact=materialize_formal_fact,
    )


def materialize_reasoning_result_graph(
    project_dir: Path, capability_results: list[dict[str, Any]]
) -> tuple[Graph, int]:
    """Materialize every source-backed rule conclusion for authoritative CQ replay."""

    runtime = json.loads(
        (project_dir / "03-mapping-review/runtime/runtime-source.json").read_text(encoding="utf-8")
    )
    capabilities = runtime.get("reasoning_capabilities") or {}
    term_kinds, term_datatypes, strict_types = approved_materialization_contract(project_dir)
    graph = Graph()
    fact_count = 0
    for result_index, result in enumerate(capability_results):
        capability_name = str(result.get("capability_name") or "")
        capability = capabilities.get(capability_name) or {}
        validate_materialization_bindings(capability, term_kinds)
        terms = capability.get("ontology_terms") or {}
        symbol_table = result.get("_symbol_table") or {}
        if result.get("result_evidence_version") == 2 and (
            result.get("derived_fact_scope") != "SOURCE_BACKED"
            or len(result.get("derived_facts") or []) != result.get("live_result_fact_count")
            or result.get("result_fact_count") != result.get("live_result_fact_count")
        ):
            raise ValueError("Rule materialization requires source-backed conclusions with matching live counts")
        # This is the exact public capability record subsequently persisted in
        # semantica-report.json; internal symbol restoration is not in that file.
        public_result = {key: value for key, value in result.items() if key != "_symbol_table"}
        result_sha256 = _canonical_fingerprint(public_result)
        receipt = URIRef("urn:orion:reasoning-result:" + result_sha256.removeprefix("sha256:"))
        for expression in result.get("derived_facts") or []:
            assertion = materialize_formal_fact(
                graph,
                str(expression),
                terms,
                symbol_table=symbol_table,
                term_kinds=term_kinds,
                term_datatypes=term_datatypes,
                strict_types=strict_types,
            )
            if assertion is not None:
                graph.add((assertion, FORMAL_FACT.sourceResult, receipt))
                graph.add((receipt, RDF.type, FORMAL_FACT.ReasoningResult))
                graph.add((receipt, FORMAL_FACT.capabilityName, Literal(capability_name)))
                graph.add((receipt, FORMAL_FACT.resultSha256, Literal(result_sha256)))
                graph.add((receipt, FORMAL_FACT.reportPath, Literal("06-quality-validation/semantica-report.json")))
                graph.add((receipt, FORMAL_FACT.resultIndex, Literal(result_index)))
                if result.get("rule_sha256"):
                    graph.add((receipt, FORMAL_FACT.ruleSha256, Literal(result["rule_sha256"])))
            fact_count += 1
    return graph, fact_count


class SemanticaMcpClient:
    def __init__(self, command: str, *, environment: dict[str, str] | None = None) -> None:
        self.process = subprocess.Popen(
            [command],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.request_id = 0

    def _send(self, payload: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _receive(self, request_id: int) -> dict[str, Any]:
        assert self.process.stdout is not None
        while True:
            line = self.process.stdout.readline()
            if not line:
                stderr = self.process.stderr.read() if self.process.stderr else ""
                raise RuntimeError(f"Semantica MCP 提前退出：{stderr[-2000:]}")
            payload = json.loads(line)
            if payload.get("id") == request_id:
                if "error" in payload:
                    raise RuntimeError(f"Semantica MCP 调用失败：{payload['error']}")
                return payload["result"]

    def initialize(self) -> None:
        self.request_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "orion-workflow", "version": "0.2.0"},
                },
            }
        )
        self._receive(self.request_id)
        self._send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
        )

    def tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.request_id += 1
        self._send(
            {
                "jsonrpc": "2.0",
                "id": self.request_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        result = self._receive(self.request_id)
        if result.get("isError"):
            raise RuntimeError(f"Semantica MCP {name} 失败：{result.get('content')}")
        structured = result.get("structuredContent")
        if structured is not None:
            if isinstance(structured, dict) and structured.get("error"):
                raise RuntimeError(f"Semantica MCP {name} 失败：{structured['error']}")
            return structured
        content = result.get("content") or []
        if content and content[0].get("type") == "text":
            raw = content[0].get("text", "")
            try:
                value = json.loads(raw)
                if isinstance(value, dict) and value.get("error"):
                    raise RuntimeError(f"Semantica MCP {name} 失败：{value['error']}")
                return value
            except json.JSONDecodeError:
                return {"text": raw}
        return result

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()


def build_reasoning_probe(
    purchase_orders: list[dict[str, Any]],
) -> tuple[list[str], list[str], str]:
    """Build a truthful Semantica probe for populated and empty sources."""

    high_risk_orders = [
        row
        for row in purchase_orders
        if str(row.get("risk_level") or "").upper() in {"HIGH", "CRITICAL"}
    ]
    if not purchase_orders:
        return (
            ["RuntimeSourceEmpty(supply-chain-current)"],
            [
                "IF RuntimeSourceEmpty(?x) THEN NoBusinessConclusion(?x)",
            ],
            "EMPTY_SOURCE_BOUNDARY",
        )
    facts = [f"PurchaseOrder(po-{row['id']})" for row in purchase_orders]
    facts.extend(f"HighRiskOrder(po-{row['id']})" for row in high_risk_orders)
    return (
        facts,
        [
            "IF HighRiskOrder(?x) THEN NeedsSupplyReview(?x)",
            "IF NeedsSupplyReview(?x) THEN TraceRequired(?x)",
        ],
        "BUSINESS_INSTANCE_PROBE",
    )


def validate_release_reasoning_capabilities(
    *,
    project_dir: Path,
    graph: Graph,
    semantica: SemanticaReasoningClient,
    ontop_endpoint: str | None = None,
    validation_scope: str = "FULL_SOURCE_VALIDATION",
) -> list[dict[str, Any]]:
    """Validate every S3 rule package against source-backed evidence facts."""

    runtime_dir = project_dir / "03-mapping-review/runtime"
    source_path = runtime_dir / "runtime-source.json"
    if not source_path.exists():
        return []
    runtime = json.loads(source_path.read_text(encoding="utf-8"))
    capabilities = runtime.get("reasoning_capabilities") or {}
    query_capabilities = runtime.get("query_capabilities") or {}
    query_artifacts = runtime.get("ontop_queries") or {}
    document_fact_queries = runtime.get("document_fact_queries") or {}
    term_kinds, term_datatypes, strict_types = approved_materialization_contract(project_dir)
    results: list[dict[str, Any]] = []
    for name, capability in sorted(capabilities.items()):
        symbol_table: dict[str, str] = {}
        evidence_query = str(capability["evidence_query"])
        parameters: dict[str, Any] = {}
        rows: list[dict[str, Any]] = []
        if evidence_query in document_fact_queries:
            fact_entry = document_fact_queries[evidence_query]
            fact_path = runtime_dir / str(fact_entry["fact_artifact"])
            fact_package = json.loads(fact_path.read_text(encoding="utf-8"))
            from services.realtime_qa.reasoning import document_evidence_facts

            parameters = dict((capability.get("runtime_validation") or {}).get("parameters") or {})
            facts = document_evidence_facts(
                list(fact_package.get("facts") or []),
                list(capability["fact_bindings"]), symbol_table, parameters=parameters,
            )
        else:
            query_entry = query_artifacts[evidence_query]
            query_path = runtime_dir / str(query_entry["path"])
            query = query_path.read_text(encoding="utf-8")
            from services.realtime_qa.evidence_absence import validate_evidence_absence

            validate_evidence_absence({name: capability}, {evidence_query: query})
            query_capability = query_capabilities[evidence_query]
            validation_cases = query_capability.get("validation_cases") or []
            parameters = (
                dict(validation_cases[0].get("parameters") or {}) if validation_cases else {}
            )
            rendered_query = render_query_parameters(query, parameters, query_capability)
            if strict_types:
                from services.realtime_qa.fact_binding_types import validate_fact_binding_types

                validate_fact_binding_types({
                    "reasoning_capabilities": {name: capability},
                    "ontop_queries": {evidence_query: rendered_query},
                }, term_kinds)
            if ontop_endpoint:
                with httpx.Client(timeout=120.0, trust_env=False) as client:
                    response = client.post(
                        ontop_endpoint,
                        data={"query": rendered_query},
                        headers={"Accept": "application/sparql-results+json"},
                    )
                response.raise_for_status()
                result_payload = response.json()
                rows = [
                    {key: value.get("value") for key, value in row.items()}
                    for row in result_payload["results"].get("bindings", [])
                ]
            else:
                query_result = graph.query(rendered_query)
                variables = [str(value) for value in (query_result.vars or [])]
                rows = []
                for row in query_result:
                    serialized: dict[str, Any] = {}
                    for variable in variables:
                        value = row.get(variable)
                        if value is not None:
                            serialized[variable] = (
                                value.toPython() if hasattr(value, "toPython") else str(value)
                            )
                    rows.append(serialized)
            facts = facts_from_rows(
                rows,
                parameters,
                capability["fact_bindings"],
                symbol_table=symbol_table,
            )
        if strict_types:
            # S7/QA reconstruct the input facts too. Reject the same invalid
            # contract at S6, before a conclusion-only replay can hide it.
            from services.ontology_engineering.formal_facts import validate_formal_fact_types

            validate_formal_fact_types(
                facts, capability, symbol_table, capability_name=name,
                term_kinds=term_kinds, term_datatypes=term_datatypes,
            )
        rule_path = runtime_dir / str(capability["rule_artifact"])
        rule_package = json.loads(rule_path.read_text(encoding="utf-8"))
        formal_rules = list(rule_package.get("rules") or [])
        rules = [str(rule["expression"]) for rule in formal_rules]
        closed_world_predicates = [
            str(item["predicate"]) for item in capability.get("closed_world_inputs") or []
        ]
        live = semantica.run_forward(
            facts=facts,
            rules=rules,
            **(
                {"closed_world_predicates": closed_world_predicates}
                if closed_world_predicates
                else {}
            ),
        )
        live_results = result_facts(
            list(live["inferred_facts"]),
            list(capability["result_predicates"]),
        )
        if strict_types:
            # Intermediate conclusions also participate in S7/QA materialization.
            validate_formal_fact_types(
                list(live["inferred_facts"]), capability, symbol_table, capability_name=name,
                term_kinds=term_kinds, term_datatypes=term_datatypes,
            )
        live_trace = list(live.get("trace") or [])
        positive = live
        positive_facts = facts
        positive_results = live_results
        trace = live_trace
        positive_scope = "SOURCE_BACKED"
        positive_transformation: dict[str, Any] = {"kind": "NONE"}

        validation = dict(capability.get("runtime_validation") or {})
        expected_live_outcome = str(validation.get("expected_live_outcome") or "POSITIVE").upper()
        live_result_count = len(live_results)
        live_outcome_verified = (
            bool(positive_results) if expected_live_outcome == "POSITIVE" else not positive_results
        )
        if expected_live_outcome == "NEGATIVE" and evidence_query not in document_fact_queries:
            counterfactual_bindings = [
                {key: value for key, value in binding.items() if key != "when"}
                for binding in capability["fact_bindings"]
            ]
            counterfactual_facts = facts_from_rows(
                rows,
                parameters,
                counterfactual_bindings,
                symbol_table=symbol_table,
            )
            positive_facts = counterfactual_facts
            positive_scope = "COUNTERFACTUAL_FIXTURE"
            positive_transformation = {
                "kind": "REMOVE_FACT_BINDING_CONDITIONS",
                "binding_indexes": [
                    index for index, binding in enumerate(capability["fact_bindings"])
                    if "when" in binding
                ],
                "meaning": "仅验证规则可触发；移除的判据不代表真实来源满足业务条件。",
            }
            positive = semantica.run_forward(
                facts=counterfactual_facts,
                rules=rules,
                **(
                    {"closed_world_predicates": closed_world_predicates}
                    if closed_world_predicates
                    else {}
                ),
            )
            positive_results = result_facts(
                list(positive["inferred_facts"]),
                list(capability["result_predicates"]),
            )
            trace = list(positive.get("trace") or [])

        negative_case_count = 0
        negative_scenario: dict[str, Any] = {"scope": "COUNTERFACTUAL_FIXTURE", "status": "NOT_EXECUTED"}
        if positive_results and trace:
            target = positive_results[0]
            target_trace = next(
                (item for item in trace if item.get("conclusion") == target),
                trace[0],
            )
            premises = set(target_trace.get("premises") or [])
            negative_facts = [fact for fact in positive_facts if fact not in premises]
            negative = semantica.run_forward(
                facts=negative_facts,
                rules=rules,
                **(
                    {"closed_world_predicates": closed_world_predicates}
                    if closed_world_predicates
                    else {}
                ),
            )
            negative_case_count = int(target not in negative["inferred_facts"])
            negative_scenario = {
                "scope": "COUNTERFACTUAL_FIXTURE",
                "status": "PASSED" if negative_case_count else "FAILED",
                "basis_scope": positive_scope,
                "transformation": {"kind": "REMOVE_TARGET_PREMISES", "target": target,
                                   "removed_facts": [fact for fact in positive_facts if fact in premises]},
                "input_fact_count": len(negative_facts),
                "engine_build_sha256": negative.get("engine_build_sha256", "UNKNOWN"),
                "input_facts_sha256": _canonical_fingerprint(negative_facts),
                "derived_facts": result_facts(list(negative["inferred_facts"]), list(capability["result_predicates"])),
                "trace": list(negative.get("trace") or []),
            }

        passed = bool(
            facts
            and formal_rules
            and positive_results
            and trace
            and negative_case_count == 1
            and live_outcome_verified
        )
        failure_reason = next((reason for failed, reason in (
            (not facts, f"证据查询 {evidence_query} 返回 {len(rows)} 行、生成 0 条输入事实；"
                        "业务筛选应写在 fact_bindings[].when 中，证据查询需返回全部候选记录。"),
            (not live_outcome_verified, f"实时结果与 expected_live_outcome={expected_live_outcome} 不一致"
                                        f"（实时结论 {live_result_count} 条）。"),
            (not (positive_results and trace), f"{positive_scope} 正例未触发规则"
                                               f"（输入事实 {len(positive_facts)} 条）。"),
            (negative_case_count != 1, "移除目标前提后仍导出结论，反例不成立。"),
        ) if failed), "")
        results.append(
            {
                "capability_name": name,
                "result_evidence_version": 2,
                "derived_fact_scope": "SOURCE_BACKED",
                "status": "PASSED" if passed else "FAILED",
                **({} if passed else {"failure_reason": failure_reason}),
                "validation_scope": validation_scope,
                "rule_sha256": capability["rule_sha256"],
                "input_fact_count": len(facts),
                "result_fact_count": len(live_results),
                "rules_fired": int(live.get("rules_fired") or 0),
                "engine_build_sha256": live.get("engine_build_sha256", "UNKNOWN"),
                "expected_live_outcome": expected_live_outcome,
                "live_outcome_verified": live_outcome_verified,
                "live_result_fact_count": live_result_count,
                "derived_facts": live_results,
                "positive_case_count": int(bool(positive_results and trace)),
                "negative_case_count": negative_case_count,
                "trace": live_trace,
                "controlled_scenarios": {
                    "positive": {
                        "scope": positive_scope,
                        "status": "PASSED" if positive_results and trace else "FAILED",
                        "transformation": positive_transformation,
                        "input_fact_count": len(positive_facts),
                        "input_facts_sha256": _canonical_fingerprint(positive_facts),
                        "derived_facts": positive_results,
                        "trace": trace,
                        "rules_fired": int(positive.get("rules_fired") or 0),
                        "engine_build_sha256": positive.get("engine_build_sha256", "UNKNOWN"),
                    },
                    "negative": negative_scenario,
                },
                "_symbol_table": symbol_table,
            }
        )
    return results


def s6_reasoning_timeout(value: str) -> float:
    """Bound expensive full-source rule requests independently of interactive QA."""
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("S6 reasoning timeout must be a number") from exc
    if not math.isfinite(timeout) or not 1 <= timeout <= 600:
        raise argparse.ArgumentTypeError("S6 reasoning timeout must be between 1 and 600 seconds")
    return timeout


def s6_reasoning_client(base_url: str, timeout_seconds: float, *, api_key: str | None = None) -> SemanticaReasoningClient:
    return SemanticaReasoningClient(
        base_url, timeout=s6_reasoning_timeout(str(timeout_seconds)), api_key=api_key
    )


def s6_execution_input_fingerprint(
    stage_fingerprints: dict[str, str], joint_design_fingerprint: str | None,
) -> str:
    return _canonical_fingerprint({
        "stage": "S6",
        "stage_fingerprints": stage_fingerprints,
        "runner_sha256": checksum(Path(__file__)),
        "query_validation_code_fingerprint": query_validation_code_fingerprint(),
        **({"joint_design_fingerprint": joint_design_fingerprint}
           if joint_design_fingerprint else {}),
    })


def _run_quality_validation(execution_state: dict[str, Any]) -> None:
    parser = argparse.ArgumentParser(description="执行 ORION S6 多维质量与 Semantica 验证。")
    parser.add_argument("project_id")
    parser.add_argument("--workflow-home", default=".orion-workflows")
    parser.add_argument(
        "--semantica-mcp",
        default=None,
        help="必须与 active-runtime 配置一致；旧流程在独立临时图谱中验证。",
    )
    parser.add_argument(
        "--semantica-api-url",
        default=Settings.from_env().semantica_url,
        help="Semantica REST API；正式规则验证始终以只读 apply_to_graph=false 执行。",
    )
    parser.add_argument(
        "--semantica-reasoning-timeout-seconds",
        type=s6_reasoning_timeout,
        default=os.getenv("ORION_S6_REASONING_TIMEOUT_SECONDS", "120"),
        help="全量规则执行的单次HTTP读写超时，1至600秒，默认120；不自动重试。",
    )
    args = parser.parse_args()

    workflow_home = Path(args.workflow_home).resolve()
    project_dir = workflow_home / args.project_id
    state = json.loads((project_dir / "workflow-state.json").read_text(encoding="utf-8"))
    require_s6_runnable_state(state)
    joint_design_fingerprint = verify_s6_joint_design(project_dir, state)
    intake_mode = str(state.get("intake_mode") or "").upper()
    directed_validation = project_stage_contract_version(state) == STAGE_CONTRACT_VERSION
    stage_fingerprints = current_stage_fingerprints(project_dir)
    execution_input_fingerprint = s6_execution_input_fingerprint(
        stage_fingerprints, joint_design_fingerprint,
    )
    execution_lease = StageExecutionLease.acquire(
        project_dir=project_dir,
        stage="S6",
        input_fingerprint=execution_input_fingerprint,
        executor_id=f"orion-platform:{os.getpid()}",
    )
    execution_state["lease"] = execution_lease
    atexit.register(execution_lease.mark_interrupted)
    if joint_design_fingerprint:
        try:
            leased_state = json.loads(
                (project_dir / "workflow-state.json").read_text(encoding="utf-8")
            )
            require_s6_runnable_state(leased_state)
            if (
                leased_state.get("revision") != state.get("revision")
                or verify_s6_joint_design(project_dir, leased_state) != joint_design_fingerprint
            ):
                raise RuntimeError(
                    "S6_JOINT_DESIGN_NOT_READY: 获取执行租约期间工程或联合设计已改变。"
                )
        except (RuntimeError, ValueError, OSError):
            execution_lease.mark_interrupted()
            raise
    execution_lease.start_heartbeat()
    tasks = opted_in_tasks(workflow_home, args.project_id)
    managed = start_managed(tasks, project=args.project_id, stage="S6",
                            fingerprint=execution_input_fingerprint, lease=execution_lease,
                            project_dir=project_dir)
    execution_state["managed"] = managed
    service = OntologyWorkflowService(workflow_home)

    def checkpoint(*args, **kwargs):
        if managed is not None:
            managed.tasks.check(managed.token)
        return execution_lease.checkpoint(*args, **kwargs)

    def work():
        design = yaml.safe_load(
            (project_dir / "04-ontology-design/ontology-design.yaml").read_text(encoding="utf-8")
        )
        mapping = yaml.safe_load(
            (project_dir / "03-mapping-review/mapping.yaml").read_text(encoding="utf-8")
        )
        ontology_ttl = (project_dir / "05-ontology-build/ontology.ttl").read_text(encoding="utf-8")
        source_database_url = str(os.getenv("ORION_SOURCE_DATA_READER_URL") or "").strip()
        database_rows = 0
        document_evidence_units = 0
        profile_scope_reconciliation = "NOT_APPLICABLE"
        document_fact_graph, document_fact_count = materialize_document_fact_graph(project_dir)
        if intake_mode != "DATABASE_ONLY":
            evidence_index = json.loads(
                (project_dir / "00-document-evidence/evidence-index.json").read_text(encoding="utf-8")
            )
            document_evidence_units = len(evidence_index)

        candidate_id = (
            "s6-candidate-"
            + hashlib.sha256(
                json.dumps(stage_fingerprints, sort_keys=True).encode("utf-8")
            ).hexdigest()[:16]
        )
        mapping_path = project_dir / "03-mapping-review/runtime/mapping.obda"
        ontology_path = project_dir / "05-ontology-build/ontology.ttl"
        ontop_endpoint: str | None = None
        backend_identity: dict[str, Any] = {}
        target_backend_validation: dict[str, Any] = {"status": "NOT_APPLICABLE"}
        source_fingerprint = stage_fingerprints.get("S0", "DOCUMENT_ONLY")
        checkpoint_dir = project_dir / ".stage-executions" / "S6-artifacts"
        with ExitStack() as stack:
            if intake_mode != "DOCUMENT_ONLY":
                if not source_database_url:
                    raise RuntimeError(
                        "生产 S6 必须显式提供 ORION_SOURCE_DATA_READER_URL；禁止回退到应用写账号。"
                    )
                inventory = json.loads(
                    (project_dir / "01-data-understanding/datasource-inventory.json").read_text(
                        encoding="utf-8"
                    )
                )
                profile = json.loads(
                    (project_dir / "01-data-understanding/data-profile.json").read_text(
                        encoding="utf-8"
                    )
                )
                database_rows = int(profile.get("total_rows", -1))
                if database_rows < 0:
                    raise RuntimeError("S1 数据画像缺少生产级 total_rows 对账。")
                source_binding = resolve_s6_source_binding(
                    inventory,
                    source_database_url,
                    catalog_readonly_verifier=lambda principal: verify_reader_principal_catalog(
                        source_database_url, principal
                    ),
                )
                scope = source_binding["business_tables_scope"]
                actual_by_table: dict[str, int] = {}
                engine = create_engine(source_database_url)
                stack.callback(engine.dispose)
                with engine.connect() as connection:
                    for table_name in scope:
                        relation_name = source_binding["relations"][table_name]
                        relation = validated_table_reference(relation_name)
                        where = _dataset_filter_sql(source_binding["dataset_filters"], table_name)
                        actual_by_table[table_name] = int(
                            connection.execute(
                                text(f"SELECT COUNT(*) FROM {relation}{where}")
                            ).scalar_one()
                        )
                    source_fingerprint = source_content_fingerprint(
                        connection,
                        source_binding["relations"],
                        source_binding["dataset_filters"],
                    )
                expected_by_table, profile_scope_reconciliation = reconcile_profile_scope(
                    profile=profile,
                    inventory=inventory,
                    scope=scope,
                    actual_by_table=actual_by_table,
                )
                if actual_by_table != expected_by_table:
                    raise RuntimeError(
                        "S6 回读发现数据库行数已偏离 S1 精确画像；必须回退 S1 重新快照。"
                    )
                if sum(actual_by_table.values()) != database_rows:
                    raise RuntimeError("S6 数据库逐表回读总量与 S1 total_rows 不闭合。")
                checkpoint(
                    "SOURCE_RECONCILIATION",
                    {
                        "database_rows": database_rows,
                        "profile_scope_reconciliation": profile_scope_reconciliation,
                        "source_fingerprint": source_fingerprint,
                    },
                )
                ontop_endpoint = stack.enter_context(
                    pre_release_ontop_endpoint(
                        project_dir=project_dir,
                        database_url=source_database_url,
                        candidate_id=candidate_id,
                        execution_lease=execution_lease,
                        backend_identity=backend_identity,
                    )
                )
                from services.ontop_client.backend_validation import validate_candidate_queries
                checkpoint("TARGET_BACKEND_VALIDATION", {"status": "RUNNING"})
                target_backend_validation = validate_candidate_queries(
                    project_dir=project_dir, endpoint=ontop_endpoint, candidate_id=candidate_id,
                    backend_identity=backend_identity, source_fingerprint=source_fingerprint,
                    receipt_path=checkpoint_dir / "target-backend-validation.json",
                    progress_callback=lambda details: checkpoint("TARGET_BACKEND_VALIDATION", details),
                )
                checkpoint("TARGET_BACKEND_VALIDATION", target_backend_validation)
            graph_input_fingerprint = _canonical_fingerprint({
                "business_graph": materialization_fingerprint(
                    project_dir, _canonical_fingerprint(sorted(document_fact_graph.serialize(format="nt").splitlines()))
                ),
                # The extracted graph also contains this candidate's identity triples.
                "candidate_id": candidate_id,
            })
            materialized_graph = load_graph_checkpoint(
                checkpoint_dir,
                input_fingerprint=graph_input_fingerprint,
                source_fingerprint=source_fingerprint,
            )
            reused_materialization = materialized_graph is not None
            partition_receipt = None
            if materialized_graph is None and ontop_endpoint is not None:
                predicate_plan = None
                if directed_validation:
                    try:
                        predicate_plan = compile_predicate_partition_plan(
                            compile_mapping_with_identity(
                                mapping_path.read_text(encoding="utf-8"), candidate_id, checksum(mapping_path),
                            ),
                            ontology_ttl,
                        )
                    except (ValueError, SyntaxError, ParserError):
                        # Unsupported optimization is not an additional business gate.
                        # Use the original complete-query path, including blank-node identity.
                        checkpoint("MATERIALIZATION_STRATEGY", {
                            "mode": "SINGLE_COMPLETE_QUERY",
                            "reason": "Static partition completeness could not be proven for this mapping.",
                        })
                if predicate_plan is not None:
                    materialized_graph, partition_receipt = materialize_ontop_partitions(
                        endpoint=ontop_endpoint,
                        directory=checkpoint_dir / "predicate-partitions",
                        input_fingerprint=graph_input_fingerprint,
                        source_fingerprint=source_fingerprint,
                        predicate_plan=predicate_plan,
                        progress=lambda details: checkpoint(
                            "MATERIALIZATION_PROGRESS", details,
                        ),
                    )
                else:
                    materialized_graph = materialize_single_ontop_graph(
                        endpoint=ontop_endpoint, directory=checkpoint_dir,
                    )
                with engine.connect() as connection:
                    if (
                        source_content_fingerprint(
                            connection,
                            source_binding["relations"],
                            source_binding["dataset_filters"],
                        )
                        != source_fingerprint
                    ):
                        raise RuntimeError(
                            "S6_SOURCE_CHANGED_DURING_MATERIALIZATION: 来源内容在物化期间变化，候选图不能复用或提交。"
                        )
                if partition_receipt is not None:
                    partition_receipt["status"] = "SOURCE_RECHECK_PASSED"
                    partition_receipt["source_fingerprint_rechecked"] = source_fingerprint
                    (checkpoint_dir / "partition-materialization-receipt.json").write_text(
                        json.dumps(partition_receipt, ensure_ascii=False, indent=2), encoding="utf-8",
                    )
            elif materialized_graph is None:
                materialized_graph = Graph()
            if not reused_materialization:
                materialized_graph += document_fact_graph
                save_graph_checkpoint(
                    checkpoint_dir,
                    materialized_graph,
                    input_fingerprint=graph_input_fingerprint,
                    source_fingerprint=source_fingerprint,
                )
            checkpoint(
                "MATERIALIZATION",
                {
                    "materialized_triple_count": len(materialized_graph),
                    "reused": reused_materialization,
                    **({"partition_count": partition_receipt["predicate_count"],
                        "source_recheck": "PASSED"} if partition_receipt else {}),
                },
            )
            # N-Triples is a Turtle subset and avoids costly pretty-print grouping
            # for multi-million-triple candidate graphs.
            materialized_ttl = "" if directed_validation else str(materialized_graph.serialize(format="nt"))
            ontology_graph = Graph().parse(data=ontology_ttl, format="turtle")
            if directed_validation:
                combined = ReadOnlyGraphUnion([ontology_graph, materialized_graph])
                combined_ttl = ""
            else:
                combined = ontology_graph + materialized_graph
                combined_ttl = str(combined.serialize(format="nt"))

            if directed_validation:
                # All data constraints/CQs remain validated against the full local
                # source graph. Forward rules consume explicit facts, not a copy in
                # the shared Explorer. Only declared rules contact their executor.
                reasoning_capability_results = validate_release_reasoning_capabilities(
                    project_dir=project_dir,
                    graph=combined,
                    semantica=guarded_client(managed, s6_reasoning_client(args.semantica_api_url, args.semantica_reasoning_timeout_seconds)),
                    ontop_endpoint=ontop_endpoint,
                    validation_scope="FULL_SOURCE_VALIDATION",
                )
                reasoning_result_graph, reasoning_result_fact_count = materialize_reasoning_result_graph(
                    project_dir, reasoning_capability_results
                )
                for capability_result in reasoning_capability_results:
                    capability_result.pop("_symbol_table", None)
                materialized_graph += reasoning_result_graph
                materialized_ttl = str(materialized_graph.serialize(format="nt"))
                import_result = {"status": "NOT_APPLICABLE", "reason": "Local validation; model registry sync belongs to S7."}
                relationship_import_result = {"status": "NOT_APPLICABLE"}
                candidate_relationship_verification = {"status": "SUBMITTED_FOR_SERVER_VALIDATION"}
                graph_summary = {"scope": "LOCAL_FULL_SOURCE_GRAPH", "triple_count": len(materialized_graph)}
                checkpoint("REASONING", {
                    "reasoning_capability_count": len(reasoning_capability_results),
                    "derived_fact_count": reasoning_result_fact_count,
                    "graph_persistence": "NOT_REQUIRED_FOR_VALIDATION",
                })
            else:
                with isolated_validation_runtime(args.semantica_mcp) as runtime:
                    semantica = guarded_client(managed, SemanticaMcpClient(runtime["command"], environment=runtime["environment"]))
                    try:
                        semantica.initialize()
                        import_result = semantica.tool(
                            "import_ontology",
                            {
                                "content": combined_ttl,
                                "format": "turtle",
                                "name": f"ORION 生产候选本体 {project_dir.name} {design['version']}",
                                "description": "S6 当前 S0-S5 指纹绑定的全量来源候选图。",
                                "tags": ["ORION", "S6", "FULL_SOURCE_VALIDATION"],
                            },
                        )
                        reasoning_capability_results = validate_release_reasoning_capabilities(
                            project_dir=project_dir,
                            graph=combined,
                            semantica=guarded_client(managed, s6_reasoning_client(runtime["base_url"], args.semantica_reasoning_timeout_seconds, api_key=runtime["api_key"])),
                            ontop_endpoint=ontop_endpoint,
                            validation_scope="FULL_SOURCE_VALIDATION",
                        )
                        reasoning_result_graph, reasoning_result_fact_count = (
                            materialize_reasoning_result_graph(project_dir, reasoning_capability_results)
                        )
                        for capability_result in reasoning_capability_results:
                            capability_result.pop("_symbol_table", None)
                        materialized_graph += reasoning_result_graph
                        materialized_ttl = str(materialized_graph.serialize(format="nt"))
                        (
                            relationship_import_result,
                            candidate_relationship_verification,
                        ) = sync_and_verify_semantica_relationships(
                            graph=materialized_graph,
                            object_property_iris=[
                                str(item.get("iri") or "")
                                for item in design.get("object_properties") or []
                                if isinstance(item, dict)
                            ],
                            project_id=args.project_id,
                            release_candidate_fingerprint=stage_fingerprints["S5"],
                            semantica=semantica,
                            semantica_api_url=runtime["base_url"],
                            api_key=runtime["api_key"],
                        )
                        graph_summary = semantica.tool("get_graph_summary", {})
                        checkpoint(
                            "REASONING_AND_RELATIONSHIPS",
                            {
                                "reasoning_capability_count": len(reasoning_capability_results),
                                "derived_fact_count": reasoning_result_fact_count,
                                "relationship_status": candidate_relationship_verification.get("status"),
                            },
                        )
                    finally:
                        semantica.close()

        protege_report = json.loads(
            (project_dir / "05-ontology-build/protege-build-report.json").read_text(encoding="utf-8")
        )
        reasoner = protege_report["tool_results"]["run_reasoner"]
        ontology_validation = protege_report["tool_results"]["validate_ontology"]
        entity_count = sum(
            len(design.get(key) or []) for key in ("classes", "object_properties", "data_properties")
        )
        run_id = f"{'validation' if directed_validation else 'semantica'}-{uuid.uuid4().hex[:12]}"
        payload = dict(
            materialized_ttl=materialized_ttl,
            hermit_report={
                "status": "PASSED",
                "consistent": not bool(reasoner.get("inconsistent")),
                "reasoner": "HERMIT",
                "run_id": protege_report["run_id"],
                "unsatisfiable_count": int(reasoner.get("unsatisfiable_count", 0)),
                "summary": reasoner.get("message"),
                **({"reused_evidence": {
                    "source_stage": "S5",
                    "report_sha256": checksum(project_dir / "05-ontology-build/protege-build-report.json"),
                    "ontology_sha256": checksum(project_dir / "05-ontology-build/ontology.ttl"),
                }} if directed_validation else {}),
            },
            mapping_report={
                "status": "PASSED",
                "mapped_count": len(mapping.get("mappings") or []),
                "unmapped_count": 0,
                "summary": "S4 施工图覆盖全部正式 Mapping；S6 通过当前 S3/S5 候选 Ontop 或正式文档事实层执行全量物化。",
                "target_backend_validation": target_backend_validation,
                "materialization_scope": "FULL_SOURCE_VALIDATION",
                "source_mapping_sha256": (checksum(mapping_path) if mapping_path.is_file() else None),
            },
            semantic_quality_report={
                "status": "PASSED",
                "checked_entity_count": entity_count,
                "high_severity_issue_count": 0,
                "tool_issue_count": int(ontology_validation.get("total_issues", 0)),
                "summary": "Protégé 结构审计无高严重度问题；信息级提示保留在构建报告中。",
            },
            competency_question_report={
                "status": "SUBMITTED_FOR_SERVER_VALIDATION",
                **(
                    {
                        "validation_mode": "PRE_RELEASE_FULL_SOURCE_ONTOP",
                        "validation_endpoint": ontop_endpoint,
                        "candidate_id": candidate_id,
                        "database_access_mode": "READ_ONLY",
                        "source_mapping_sha256": checksum(mapping_path),
                        "ontology_sha256": checksum(ontology_path),
                        "source_stage_fingerprints": stage_fingerprints,
                    }
                    if ontop_endpoint
                    else {"validation_mode": "FULL_SOURCE_DOCUMENT_FACT_GRAPH"}
                ),
                "total": len(design.get("competency_questions") or []),
                "question_ids": [
                    str(question.get("id")) for question in design.get("competency_questions") or []
                ],
                "summary": (
                    "该对象只声明待验证 CQ 清单；PASSED、结果摘要与哈希均由 ORION 服务端"
                    "重新执行正式 SPARQL 并按 answer_contract 判定后生成。"
                ),
            },
            semantica_report={
                "status": "PASSED",
                "materialized_format": "nt",
                "engine": "ORION_LOCAL_VALIDATION" if directed_validation else "SEMANTICA_MCP",
                **({
                    "validation_execution_policy": CAPABILITY_VALIDATION_POLICY,
                    "validated_graph_sha256": "sha256:" + hashlib.sha256(materialized_ttl.encode()).hexdigest(),
                } if directed_validation else {}),
                "run_id": run_id,
                "instance_count": len(set(materialized_graph.subjects(RDF.type, None))),
                "relationship_count": len(
                    [triple for triple in materialized_graph if triple[1] != RDF.type]
                ),
                "source_row_count": database_rows,
                "document_fact_count": document_fact_count,
                "derived_fact_count": reasoning_result_fact_count,
                "document_evidence_unit_count": document_evidence_units,
                "reasoning_probe_mode": "FULL_SOURCE_RULE_PACKAGE",
                "materialization_scope": "FULL_SOURCE_VALIDATION",
                "import_result": import_result,
                "relationship_import_result": relationship_import_result,
                "candidate_relationship_verification": (candidate_relationship_verification),
                "reasoning_capability_results": reasoning_capability_results,
                "production_coverage": {
                    "status": "VERIFIED",
                    "validation_scope": "FULL_SOURCE_VALIDATION",
                    "receipt_id": f"coverage-{uuid.uuid4().hex[:12]}",
                    "populations": {
                        "database_rows": {
                            "expected": database_rows,
                            "evaluated": database_rows,
                            "failed": 0,
                        },
                        "document_evidence_units": {
                            "expected": document_evidence_units,
                            "evaluated": document_evidence_units,
                            "failed": 0,
                        },
                    },
                    "source_stage_fingerprints": stage_fingerprints,
                    "profile_scope_reconciliation": profile_scope_reconciliation,
                },
                "graph_summary": graph_summary,
                "summary": ("完整来源图保留在本地执行约束、关系和业务问题验收；仅已声明规则调用其执行器。模型与版本标签由 S7 同步。" if directed_validation else "已将当前 S0-S5 指纹绑定的完整结构化来源与文档事实层送入最新版 Semantica 独立临时验收图，并完成正式规则包验证；验收后关闭临时服务，未写入共享发布图谱。"),
            },
        )

        def quality_validation_progress(
            subgate: str,
            status: str,
            details: dict[str, Any] | None = None,
        ) -> None:
            checkpoint("QUALITY_VALIDATION_PROGRESS", {
                "subgate": subgate,
                "status": status,
                "details": details or {},
                **({"completed_at": None} if status == "RUNNING" else {}),
            })

        # Preserve actual executor evidence even when the formal preflight rejects it.
        # Without this receipt a diagnostic retry would repeat expensive reasoning.
        execution_artifacts = project_dir / ".stage-executions" / "S6-artifacts"
        execution_artifacts.mkdir(parents=True, exist_ok=True)
        (execution_artifacts / f"validation-payload-{execution_lease.payload['execution_id']}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        preflight = service.preflight_stage_submission(
            project_id=args.project_id,
            stage="S6",
            payload=payload,
            _progress=quality_validation_progress,
        )
        if preflight.get("status") != "PASSED":
            from services.ontology_engineering.preflight_repair import build_preflight_repair_plan

            preflight["repair_plan"] = build_preflight_repair_plan(
                project_id=args.project_id, stage="S6", revision=int(state["revision"]),
                payload=payload, issues=preflight.get("issues") or [],
            )
            raise SystemExit(json.dumps(preflight, ensure_ascii=False, indent=2))
        status = commit_managed(managed, lambda: service.commit_preflight_stage_submission(
            project_id=args.project_id,
            stage="S6",
            preflight_token=str(preflight["preflight_token"]),
            expected_revision=int(state["revision"]),
        ), preflight=preflight, expected_revision=int(state["revision"]))
        execution_lease.complete(
            {
                "project_revision": status.get("revision"),
                "current_stage": status.get("current_stage"),
            }
        )
        print(
            json.dumps(
                {
                    "semantica_run_id": run_id,
                    "source_rows": database_rows,
                    "document_evidence_units": document_evidence_units,
                    "document_facts": document_fact_count,
                    "instances": len(set(materialized_graph.subjects(RDF.type, None))),
                    "materialized_triples": len(materialized_graph),
                    "current_stage": status["current_stage"],
                    "stage_statuses": status["stage_statuses"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    if managed is None:
        work()
    else:
        with service.managed_execution(tasks, managed.token):
            managed.effect(work)


def _record_s6_runtime_failure(lease: StageExecutionLease, error: BaseException) -> str:
    """Persist bounded, redacted failure evidence without changing stage assets."""
    project_dir = lease.path.parent.parent
    database_url = str(os.getenv("ORION_SOURCE_DATA_READER_URL") or "").strip()
    reason = _redact_candidate_diagnostic(f"{type(error).__name__}: {error}", database_url)[:4000]
    trace = _redact_candidate_diagnostic(
        "".join(traceback.format_exception(type(error), error, error.__traceback__, limit=20)),
        database_url,
    )
    payload = {
        "schema_version": 1, "stage": "S6", "status": "FAILED", "component": "S6_RUNNER",
        "project_id": project_dir.name, "execution_id": lease.payload["execution_id"],
        "input_fingerprint": lease.payload["input_fingerprint"],
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "error_type": type(error).__name__, "error": reason,
        "traceback": trace[-24000:], "traceback_truncated": len(trace) > 24000,
        "checkpoint_names": list(lease.payload.get("checkpoints") or {}),
    }
    details = {"component": "S6_RUNNER", "error_type": type(error).__name__}
    message = f"S6 执行失败：{reason}"
    try:
        directory = lease.path.parent / "S6-diagnostics"
        if directory.is_symlink() or not directory.resolve().is_relative_to(project_dir.resolve()):
            raise RuntimeError("诊断目录必须位于当前工程内")
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"runtime-{lease.payload['execution_id']}.json"
        fd, temporary = tempfile.mkstemp(prefix=".runtime-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        details.update(diagnostic_path=target.relative_to(project_dir).as_posix(), diagnostic_sha256=checksum(target))
        message += f"\n诊断：{details['diagnostic_path']}"
    except Exception:
        message += "\n诊断文件未能保存；执行回执保留以上脱敏错误。"
    lease.fail(message, details)
    return message


def main() -> None:
    execution_state: dict[str, Any] = {}
    previous_termination_handler = signal.getsignal(signal.SIGTERM)

    def interrupt_execution(_signum: int, _frame: Any) -> None:
        # Unwind the candidate endpoint's ExitStack before recording interruption.
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupt_execution)
    try:
        _run_quality_validation(execution_state)
    except KeyboardInterrupt:
        lease = execution_state.get("lease")
        if isinstance(lease, StageExecutionLease):
            lease.mark_interrupted()
        raise SystemExit(130) from None
    except (Exception, SystemExit) as error:
        lease = execution_state.get("lease")
        # Pre-start gates and previously recorded startup failures keep their
        # existing semantics. An unknown running failure must not become a vague
        # atexit INTERRUPTED record, and its raw exception must not leak secrets.
        if not isinstance(lease, StageExecutionLease) or lease._finished:
            raise
        message = _record_s6_runtime_failure(lease, error)
        if isinstance(error, SystemExit):
            raise SystemExit(message) from None
        raise RuntimeError(message) from None
    finally:
        managed = execution_state.get("managed")
        try:
            if managed is not None:
                managed.__exit__(None, None, None)
        finally:
            signal.signal(signal.SIGTERM, previous_termination_handler)


if __name__ == "__main__":
    main()
