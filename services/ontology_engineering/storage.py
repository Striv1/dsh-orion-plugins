from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

STAGE_BY_FOLDER = {
    "00-document-evidence": "S0",
    "01-data-understanding": "S1",
    "02-semantic-recognition": "S2",
    "03-mapping-review": "S3",
    "04-ontology-design": "S4",
    "05-ontology-build": "S5",
    "06-quality-validation": "S6",
    "07-release": "S7",
}
LARGE_ARTIFACT_SUFFIXES = {
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".zip",
    ".tar",
    ".gz",
    ".7z",
}
ORION_WORKFLOW_SCHEMA_VERSION = 2
ORION_WORKFLOW_MIGRATION_NAME_PATTERN = re.compile(
    r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+)\.sql$"
)
ORION_WORKFLOW_MIGRATION_LOCK_NAME = "orion_workflow_schema_migrations"
ORION_WORKFLOW_MIGRATION_BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS orion_workflow;

CREATE TABLE IF NOT EXISTS orion_workflow.schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE,
    checksum_sha256 TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _json_sha256(value: Any) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(serialized).hexdigest()}"


def _normalize_event_for_storage(
    event: dict[str, Any],
    *,
    line_number: int,
    previous_event_hash: str | None,
) -> dict[str, Any]:
    """兼容旧事件格式，只生成数据库镜像字段，不改写原审计文件。"""

    normalized = dict(event)
    derived_fields: list[str] = []
    if not normalized.get("sequence"):
        normalized["sequence"] = line_number
        derived_fields.append("sequence")
    if not normalized.get("event_id"):
        normalized["event_id"] = f"EVT-LEGACY-{_json_sha256(event)[7:19].upper()}"
        derived_fields.append("event_id")
    if not normalized.get("previous_event_hash") and previous_event_hash:
        normalized["previous_event_hash"] = previous_event_hash
        derived_fields.append("previous_event_hash")
    if not normalized.get("event_hash"):
        normalized["event_hash"] = _json_sha256(normalized)
        derived_fields.append("event_hash")
    if derived_fields:
        normalized["storage_compatibility"] = {
            "source_format": "LEGACY_EVENT",
            "derived_fields": derived_fields,
            "source_file_unchanged": True,
        }
    return normalized


def _jsonb(value: Any) -> Jsonb | None:
    return None if value is None else Jsonb(value)


def _as_psycopg_url(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@dataclass(frozen=True)
class ArtifactStorageReference:
    backend: str
    object_key: str


@dataclass(frozen=True)
class _SchemaMigration:
    version: int
    name: str
    path: Path
    sql: str
    checksum_sha256: str


class WorkflowSchemaCompatibilityError(RuntimeError):
    """数据库迁移历史与当前应用不兼容，启动必须停止。"""


class MinioArtifactStore:
    """把原始资料和大产物按内容哈希保存到 S3 兼容对象存储。"""

    def __init__(
        self,
        *,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        minimum_size_bytes: int = 1024 * 1024,
    ) -> None:
        try:
            from minio import Minio
        except ImportError as exc:  # pragma: no cover - configuration error
            raise RuntimeError("启用 MinIO 前必须安装 minio Python 客户端。") from exc

        self.client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self.bucket = bucket
        self.minimum_size_bytes = minimum_size_bytes
        self._bucket_ready = False

    @classmethod
    def from_env(cls) -> MinioArtifactStore:
        return cls(
            endpoint=os.getenv("MINIO_ENDPOINT", "127.0.0.1:9000"),
            access_key=os.getenv("MINIO_ROOT_USER", "orion_local"),
            secret_key=os.getenv("MINIO_ROOT_PASSWORD", "orion_local_change_me"),
            bucket=os.getenv("ORION_ARTIFACT_BUCKET", "orion-workflow-artifacts"),
            secure=os.getenv("MINIO_SECURE", "false").lower() in {"1", "true", "yes"},
            minimum_size_bytes=int(
                os.getenv("ORION_ARTIFACT_MIN_BYTES", str(1024 * 1024))
            ),
        )

    def store_if_needed(
        self,
        path: Path,
        checksum: str,
    ) -> ArtifactStorageReference | None:
        if (
            path.stat().st_size < self.minimum_size_bytes
            and path.suffix.lower() not in LARGE_ARTIFACT_SUFFIXES
        ):
            return None

        return self.store_original(path, checksum)

    def store_original(
        self,
        path: Path,
        checksum: str,
    ) -> ArtifactStorageReference:
        """Always preserve an original file as a content-addressed object."""

        self._ensure_bucket()
        digest = checksum.removeprefix("sha256:")
        object_key = f"sha256/{digest[:2]}/{digest}"
        try:
            self.client.stat_object(self.bucket, object_key)
        except Exception as exc:
            code = str(getattr(exc, "code", ""))
            if code not in {"NoSuchKey", "NoSuchObject", "NoSuchBucket", "XMinioInvalidObjectName"}:
                raise
            self.client.fput_object(
                self.bucket,
                object_key,
                str(path),
                content_type=mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
                metadata={"sha256": checksum},
            )
        return ArtifactStorageReference("minio", object_key)

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)
        self._bucket_ready = True


class PostgresWorkflowMetadataStore:
    """将文件工作区镜像成可查询的 PostgreSQL 工程账本。"""

    def __init__(
        self,
        database_url: str,
        *,
        artifact_store: MinioArtifactStore | None = None,
        schema_path: Path | None = None,
        migration_dir: Path | None = None,
    ) -> None:
        if schema_path is not None and migration_dir is not None:
            raise ValueError("schema_path 与 migration_dir 不能同时设置。")
        self.database_url = _as_psycopg_url(database_url)
        self.artifact_store = artifact_store
        self._schema_path_override = schema_path
        self.migration_dir = migration_dir or (
            Path(__file__).resolve().parents[2]
            / "database/migrations/orion_workflow"
        )
        # 保留旧属性，避免依赖方读取 schema_path 时中断。默认指向 baseline migration。
        self.schema_path = schema_path or self.migration_dir / "001_baseline.sql"

    @classmethod
    def from_env(cls, database_url: str) -> PostgresWorkflowMetadataStore:
        backend = os.getenv("ORION_ARTIFACT_BACKEND", "filesystem").strip().lower()
        if backend not in {"filesystem", "minio"}:
            raise ValueError("ORION_ARTIFACT_BACKEND 只能是 filesystem 或 minio。")
        artifact_store = MinioArtifactStore.from_env() if backend == "minio" else None
        return cls(database_url, artifact_store=artifact_store)

    def ensure_schema(self) -> None:
        """在同一事务内校验迁移历史，并按版本顺序执行尚未应用的迁移。"""

        migrations = self._load_schema_migrations()
        with psycopg.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                (ORION_WORKFLOW_MIGRATION_LOCK_NAME,),
            )
            cursor.execute(ORION_WORKFLOW_MIGRATION_BOOTSTRAP_SQL)
            applied = self._read_applied_migrations(cursor)
            self._validate_migration_history(applied, migrations)
            applied_versions = {row[0] for row in applied}

            for migration in migrations:
                if migration.version in applied_versions:
                    continue
                cursor.execute(migration.sql)
                cursor.execute(
                    """
                    INSERT INTO orion_workflow.schema_migrations (
                        version, name, checksum_sha256
                    ) VALUES (%s, %s, %s)
                    """,
                    (
                        migration.version,
                        migration.name,
                        migration.checksum_sha256,
                    ),
                )

    def migration_history(self) -> list[dict[str, Any]]:
        """读取已经提交的迁移账本；调用前应先完成 ensure_schema。"""

        with psycopg.connect(self.database_url) as connection, connection.cursor() as cursor:
            rows = self._read_applied_migrations(cursor)
        return [
            {
                "version": int(version),
                "name": str(name),
                "checksum_sha256": str(checksum),
                "applied_at": (
                    applied_at.isoformat()
                    if hasattr(applied_at, "isoformat")
                    else str(applied_at)
                ),
            }
            for version, name, checksum, applied_at in rows
        ]

    def _load_schema_migrations(self) -> list[_SchemaMigration]:
        if self._schema_path_override is not None:
            paths = [self._schema_path_override]
        else:
            paths = sorted(self.migration_dir.glob("*.sql"))
        if not paths:
            raise RuntimeError(f"没有找到 ORION Workflow 数据库迁移：{self.migration_dir}")

        migrations: list[_SchemaMigration] = []
        for path in paths:
            match = ORION_WORKFLOW_MIGRATION_NAME_PATTERN.fullmatch(path.name)
            if match is None:
                if self._schema_path_override is None:
                    raise RuntimeError(
                        f"迁移文件名不合法：{path.name}，应使用 001_name.sql 格式。"
                    )
                version = 1
            else:
                version = int(match.group("version"))
            sql = path.read_text(encoding="utf-8")
            migrations.append(
                _SchemaMigration(
                    version=version,
                    name=path.name,
                    path=path,
                    sql=sql,
                    checksum_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
                )
            )

        versions = [migration.version for migration in migrations]
        expected_versions = list(range(1, ORION_WORKFLOW_SCHEMA_VERSION + 1))
        if versions != expected_versions:
            raise RuntimeError(
                "本地 ORION Workflow 迁移不连续或支持版本未同步："
                f"expected={expected_versions}, actual={versions}"
            )
        return migrations

    @staticmethod
    def _read_applied_migrations(cursor: Any) -> list[tuple[Any, ...]]:
        cursor.execute(
            """
            SELECT version, name, checksum_sha256, applied_at
            FROM orion_workflow.schema_migrations
            ORDER BY version
            """
        )
        return list(cursor.fetchall())

    @staticmethod
    def _validate_migration_history(
        applied: list[tuple[Any, ...]],
        migrations: list[_SchemaMigration],
    ) -> None:
        if not applied:
            return

        applied_versions = [int(row[0]) for row in applied]
        latest_version = max(applied_versions)
        if latest_version > ORION_WORKFLOW_SCHEMA_VERSION:
            raise WorkflowSchemaCompatibilityError(
                "数据库 ORION Workflow 结构版本高于当前应用支持范围，拒绝启动："
                f"database={latest_version}, application={ORION_WORKFLOW_SCHEMA_VERSION}。"
                "请升级应用，不允许自动降级数据库。"
            )
        expected_applied_versions = list(range(1, latest_version + 1))
        if applied_versions != expected_applied_versions:
            raise WorkflowSchemaCompatibilityError(
                "数据库 ORION Workflow 迁移历史不连续，拒绝启动："
                f"expected={expected_applied_versions}, actual={applied_versions}"
            )

        local_by_version = {migration.version: migration for migration in migrations}
        for version, name, checksum, _applied_at in applied:
            local = local_by_version.get(int(version))
            if local is None:
                raise WorkflowSchemaCompatibilityError(
                    f"数据库迁移版本 {version} 在当前应用中不存在，拒绝启动。"
                )
            if str(name) != local.name or str(checksum) != local.checksum_sha256:
                raise WorkflowSchemaCompatibilityError(
                    "数据库迁移历史与当前应用文件不一致，拒绝启动："
                    f"version={version}, database_name={name}, local_name={local.name}。"
                    "已应用迁移文件不得重命名或修改。"
                )

    def sync_project(self, project_dir: Path) -> dict[str, int]:
        project_dir = project_dir.resolve()
        project = _read_json(project_dir / "project.json")
        state = _read_json(project_dir / "workflow-state.json")
        project_id = str(state.get("project_id") or project_dir.name)
        counts = {
            "projects": 1,
            "stages": 0,
            "events": 0,
            "artifacts": 0,
            "object_artifacts": 0,
            "decisions": 0,
            "revisions": 0,
            "releases": 0,
            "mcp_invocations": 0,
        }

        manifest_path = project_dir / "artifact-manifest.json"
        manifest = _read_json(manifest_path) if manifest_path.exists() else {"files": []}
        manifest_by_path = {
            str(item.get("path")): item for item in manifest.get("files", [])
        }

        with (
            psycopg.connect(self.database_url) as connection,
            connection.cursor() as cursor,
        ):
            self._upsert_project(cursor, project_id, project, state)
            counts["stages"] = self._sync_stages(cursor, project_id, state)
            counts["events"] = self._sync_events(cursor, project_id, project_dir)
            artifact_counts = self._sync_artifacts(
                cursor,
                project_id,
                project_dir,
                manifest_by_path,
            )
            counts.update(artifact_counts)
            counts["decisions"] = self._sync_decisions(
                cursor,
                project_id,
                project_dir,
            )
            counts["revisions"] = self._sync_revisions(
                cursor,
                project_id,
                project_dir,
            )
            counts["releases"] = self._sync_releases(
                cursor,
                project_id,
                project_dir,
            )
            counts["mcp_invocations"] = self._sync_mcp_invocations(
                cursor,
                project_id,
                project_dir,
            )
        return counts

    def status(self, project_id: str | None = None) -> dict[str, Any]:
        filters = "WHERE project_id = %s" if project_id else ""
        params: tuple[str, ...] = (project_id,) if project_id else ()
        with (
            psycopg.connect(self.database_url) as connection,
            connection.cursor() as cursor,
        ):
            counts: dict[str, int] = {}
            for table in (
                "projects",
                "stage_states",
                "events",
                "artifacts",
                "decisions",
                "revisions",
                "releases",
                "mcp_invocations",
                "document_version_receipts",
                "document_current_versions",
            ):
                cursor.execute(
                    f"SELECT COUNT(*) FROM orion_workflow.{table} {filters}",  # noqa: S608
                    params,
                )
                counts[table] = int(cursor.fetchone()[0])
            cursor.execute(
                """
                SELECT project_id, event_id, sequence, event_type, event_hash, occurred_at
                FROM orion_workflow.events
                """
                + ("WHERE project_id = %s " if project_id else "")
                + "ORDER BY occurred_at DESC, sequence DESC LIMIT 1",
                params,
            )
            latest = cursor.fetchone()
        return {
            "storage": "postgresql",
            "schema": "orion_workflow",
            "counts": counts,
            "latest_event": (
                {
                    "project_id": latest[0],
                    "event_id": latest[1],
                    "sequence": latest[2],
                    "event_type": latest[3],
                    "event_hash": latest[4],
                    "occurred_at": latest[5].isoformat(),
                }
                if latest
                else None
            ),
        }

    @staticmethod
    def _upsert_project(cursor: Any, project_id: str, project: dict[str, Any], state: dict[str, Any]) -> None:
        cursor.execute(
            """
            INSERT INTO orion_workflow.projects (
                project_id, project_name, domain, intake_mode, project_status,
                current_stage, workflow_version, created_at, updated_at, payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (project_id) DO UPDATE SET
                project_name = EXCLUDED.project_name,
                domain = EXCLUDED.domain,
                intake_mode = EXCLUDED.intake_mode,
                project_status = EXCLUDED.project_status,
                current_stage = EXCLUDED.current_stage,
                workflow_version = EXCLUDED.workflow_version,
                updated_at = EXCLUDED.updated_at,
                payload = EXCLUDED.payload
            """,
            (
                project_id,
                str(project.get("project_name") or state.get("project_name") or project_id),
                str(project.get("domain") or ""),
                str(project.get("intake_mode") or state.get("intake_mode") or "HYBRID"),
                str(state.get("project_status") or project.get("status") or "IN_PROGRESS"),
                state.get("current_stage") or project.get("current_stage"),
                str(state.get("workflow_version") or "unknown"),
                project.get("created_at") or state.get("created_at"),
                state.get("updated_at") or project.get("updated_at"),
                Jsonb({"project": project, "workflow_state": state}),
            ),
        )

    @staticmethod
    def _sync_stages(cursor: Any, project_id: str, state: dict[str, Any]) -> int:
        fingerprints = state.get("stage_fingerprints") or {}
        count = 0
        for stage, status in (state.get("stage_statuses") or {}).items():
            fingerprint = fingerprints.get(stage)
            cursor.execute(
                """
                INSERT INTO orion_workflow.stage_states (
                    project_id, stage, status, fingerprint, updated_at, payload
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, stage) DO UPDATE SET
                    status = EXCLUDED.status,
                    fingerprint = EXCLUDED.fingerprint,
                    updated_at = EXCLUDED.updated_at,
                    payload = EXCLUDED.payload
                """,
                (
                    project_id,
                    stage,
                    status,
                    json.dumps(fingerprint, ensure_ascii=False, sort_keys=True)
                    if isinstance(fingerprint, dict)
                    else fingerprint,
                    state.get("updated_at"),
                    Jsonb({"fingerprint": fingerprint}),
                ),
            )
            count += 1
        return count

    @staticmethod
    def _sync_events(cursor: Any, project_id: str, project_dir: Path) -> int:
        path = project_dir / "events/agent-trace.jsonl"
        if not path.exists():
            return 0
        count = 0
        previous_event_hash: str | None = None
        for line_number, raw_line in enumerate(
            path.read_text(encoding="utf-8").splitlines(),
            1,
        ):
            if not raw_line.strip():
                continue
            event = _normalize_event_for_storage(
                json.loads(raw_line),
                line_number=line_number,
                previous_event_hash=previous_event_hash,
            )
            previous_event_hash = str(event["event_hash"])
            cursor.execute(
                """
                INSERT INTO orion_workflow.events (
                    project_id, sequence, event_id, event_type, actor, occurred_at,
                    previous_event_hash, event_hash, current_stage, project_status, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    project_id,
                    int(event["sequence"]),
                    str(event["event_id"]),
                    str(event["event_type"]),
                    str(event.get("actor") or "ORION_WORKFLOW"),
                    event.get("at") or event.get("occurred_at"),
                    event.get("previous_event_hash"),
                    event["event_hash"],
                    event.get("current_stage"),
                    event.get("project_status"),
                    Jsonb(event),
                ),
            )
            count += 1
        return count

    def _sync_artifacts(
        self,
        cursor: Any,
        project_id: str,
        project_dir: Path,
        manifest_by_path: dict[str, dict[str, Any]],
    ) -> dict[str, int]:
        cursor.execute(
            "UPDATE orion_workflow.artifacts SET is_present = FALSE WHERE project_id = %s",
            (project_id,),
        )
        artifact_count = 0
        object_count = 0
        for path in sorted(project_dir.rglob("*")):
            relative_path = path.relative_to(project_dir)
            if (
                not path.is_file()
                or path.is_symlink()
                or any(part.startswith(".") for part in relative_path.parts)
            ):
                continue
            relative = relative_path.as_posix()
            checksum = _sha256(path)
            metadata = manifest_by_path.get(relative, {})
            storage = None
            if self.artifact_store is not None:
                storage = self.artifact_store.store_if_needed(path, checksum)
            if storage:
                backend = storage.backend
                object_key = storage.object_key
                object_count += 1
            else:
                backend = "filesystem"
                object_key = relative
            first_folder = relative.split("/", 1)[0]
            artifact_payload = {
                "purpose": metadata.get("purpose"),
                "present": True,
            }
            for key in ("lifecycle_status", "historical_snapshot"):
                if metadata.get(key) is not None:
                    artifact_payload[key] = metadata[key]
            cursor.execute(
                """
                INSERT INTO orion_workflow.artifacts (
                    project_id, path, stage, display_name, artifact_type, content_type,
                    size_bytes, sha256, storage_backend, object_key, is_present, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                ON CONFLICT (project_id, path) DO UPDATE SET
                    stage = EXCLUDED.stage,
                    display_name = EXCLUDED.display_name,
                    artifact_type = EXCLUDED.artifact_type,
                    content_type = EXCLUDED.content_type,
                    size_bytes = EXCLUDED.size_bytes,
                    sha256 = EXCLUDED.sha256,
                    storage_backend = EXCLUDED.storage_backend,
                    object_key = EXCLUDED.object_key,
                    is_present = TRUE,
                    updated_at = NOW(),
                    payload = EXCLUDED.payload
                """,
                (
                    project_id,
                    relative,
                    STAGE_BY_FOLDER.get(first_folder),
                    str(metadata.get("display_name") or path.name),
                    str(metadata.get("artifact_type") or "工程产物"),
                    mimetypes.guess_type(path.name)[0],
                    path.stat().st_size,
                    checksum,
                    backend,
                    object_key,
                    Jsonb(artifact_payload),
                ),
            )
            artifact_count += 1
        return {"artifacts": artifact_count, "object_artifacts": object_count}

    @staticmethod
    def _sync_decisions(cursor: Any, project_id: str, project_dir: Path) -> int:
        count = 0
        for path in project_dir.rglob("decisions.jsonl"):
            for index, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if not raw_line.strip():
                    continue
                decision = json.loads(raw_line)
                decision_id = str(
                    decision.get("decision_id")
                    or decision.get("confirmation_id")
                    or f"{path.parent.name}-{index}"
                )
                cursor.execute(
                    """
                    INSERT INTO orion_workflow.decisions (
                        project_id, decision_id, stage, item_id, decision, decided_by,
                        decided_at, before_value, after_value, evidence, payload
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        project_id,
                        decision_id,
                        STAGE_BY_FOLDER.get(path.relative_to(project_dir).parts[0], "S3"),
                        decision.get("confirmation_id") or decision.get("item_id"),
                        str(decision.get("decision") or "RECORDED"),
                        str(decision.get("decided_by") or "unknown"),
                        decision.get("decided_at"),
                        _jsonb(decision.get("before")),
                        _jsonb(decision.get("after") or decision.get("mapping_updates")),
                        _jsonb(decision.get("evidence")),
                        Jsonb(decision),
                    ),
                )
                count += 1
        return count

    @staticmethod
    def _sync_revisions(cursor: Any, project_id: str, project_dir: Path) -> int:
        count = 0
        for path in sorted((project_dir / "revisions").glob("*/revision.json")):
            revision = _read_json(path)
            cursor.execute(
                """
                INSERT INTO orion_workflow.revisions (
                    project_id, revision_id, target_stage, status, reason,
                    requested_by, created_at, completed_at, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, revision_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    completed_at = EXCLUDED.completed_at,
                    payload = EXCLUDED.payload
                """,
                (
                    project_id,
                    revision["revision_id"],
                    revision["target_stage"],
                    revision["status"],
                    revision["reason"],
                    revision["requested_by"],
                    revision["created_at"],
                    revision.get("completed_at"),
                    Jsonb(revision),
                ),
            )
            count += 1
        return count

    @staticmethod
    def _sync_releases(cursor: Any, project_id: str, project_dir: Path) -> int:
        release_dir = project_dir / "07-release"
        publications: dict[str, tuple[Path, dict[str, Any]]] = {}
        if release_dir.exists():
            for path in sorted(release_dir.rglob("publication.json")):
                publication = _read_json(path)
                version = str(publication.get("release_version") or "")
                current = publications.get(version)
                preferred = "ontology-model-delivery-" in path.as_posix()
                current_preferred = bool(
                    current and "ontology-model-delivery-" in current[0].as_posix()
                )
                if version and (current is None or (preferred and not current_preferred)):
                    publications[version] = (path, publication)
        for version, (path, publication) in publications.items():
            package_dir = path.parent
            while package_dir != release_dir and not (package_dir / "manifest.json").exists():
                package_dir = package_dir.parent
            manifest_path = package_dir / "manifest.json"
            manifest_sha = _sha256(manifest_path) if manifest_path.exists() else None
            status = str(
                publication.get("status")
                or ("REVOKED" if publication.get("revoked_at") else "PUBLISHED")
            )
            revocation_path = release_dir / "release-revocation.json"
            revocation = _read_json(revocation_path) if revocation_path.exists() else None
            if revocation and str(revocation.get("release_version")) == version:
                status = "REVOKED"
                publication = {**publication, "revocation": revocation}
            cursor.execute(
                """
                INSERT INTO orion_workflow.releases (
                    project_id, release_version, status, approved_by, published_at,
                    revoked_at, package_path, manifest_sha256, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, release_version) DO UPDATE SET
                    status = EXCLUDED.status,
                    revoked_at = EXCLUDED.revoked_at,
                    package_path = EXCLUDED.package_path,
                    manifest_sha256 = EXCLUDED.manifest_sha256,
                    payload = EXCLUDED.payload
                """,
                (
                    project_id,
                    version,
                    status,
                    publication.get("approved_by"),
                    publication.get("published_at"),
                    (revocation or {}).get("revoked_at") or publication.get("revoked_at"),
                    package_dir.relative_to(project_dir).as_posix(),
                    manifest_sha,
                    Jsonb(publication),
                ),
            )
        return len(publications)

    @staticmethod
    def _sync_mcp_invocations(cursor: Any, project_id: str, project_dir: Path) -> int:
        candidates = (
            ("S0", "PaddleOCR", project_dir / "00-document-evidence/processing-trace.json"),
            ("S5", "Protégé", project_dir / "05-ontology-build/protege-build-report.json"),
            ("S6", "HermiT", project_dir / "06-quality-validation/hermit-report.json"),
            ("S6", "Semantica", project_dir / "06-quality-validation/semantica-report.json"),
        )
        count = 0
        seen_invocation_ids: set[str] = set()
        for stage, service_name, path in candidates:
            if not path.exists():
                continue
            payload = _read_json(path)
            raw_invocation_id = str(payload.get("run_id") or f"{stage}-{path.stem}")
            invocation_id = raw_invocation_id
            if invocation_id in seen_invocation_ids:
                invocation_id = f"{service_name.lower()}:{raw_invocation_id}"
            seen_invocation_ids.add(invocation_id)
            tool_calls = payload.get("tool_calls") or []
            tool_name = None
            if tool_calls and isinstance(tool_calls[0], dict):
                tool_name = tool_calls[0].get("tool") or tool_calls[0].get("name")
            cursor.execute(
                """
                INSERT INTO orion_workflow.mcp_invocations (
                    project_id, invocation_id, stage, service_name, tool_name, status,
                    started_at, completed_at, duration_ms, input_size_bytes,
                    output_reference, error_summary, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (project_id, invocation_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    completed_at = EXCLUDED.completed_at,
                    duration_ms = EXCLUDED.duration_ms,
                    output_reference = EXCLUDED.output_reference,
                    error_summary = EXCLUDED.error_summary,
                    payload = EXCLUDED.payload
                """,
                (
                    project_id,
                    invocation_id,
                    stage,
                    service_name,
                    tool_name,
                    str(payload.get("status") or payload.get("result") or "RECORDED"),
                    payload.get("started_at"),
                    payload.get("completed_at") or payload.get("recorded_at"),
                    payload.get("duration_ms"),
                    payload.get("input_size_bytes"),
                    payload.get("output_reference") or payload.get("output_path"),
                    payload.get("error") or payload.get("error_summary"),
                    Jsonb(payload),
                ),
            )
            count += 1
        return count
