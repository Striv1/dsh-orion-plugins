from __future__ import annotations

from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from services.realtime_qa.incremental_pipeline import DocumentPromotionConflict
from services.realtime_qa.models import CurrentDocumentPointer


def _psycopg_url(database_url: str) -> str:
    if not database_url.strip():
        raise ValueError("Configure DATABASE_URL explicitly before using the document registry")
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


class PostgresDocumentCurrentRegistry:
    """Shared current pointer with transactional compare-and-swap promotion."""

    def __init__(self, database_url: str) -> None:
        self.database_url = _psycopg_url(database_url)

    def current(
        self,
        project_id: str,
        document_id: str,
        *,
        connection: Any | None = None,
    ) -> CurrentDocumentPointer | None:
        if connection is not None:
            with connection.cursor() as cursor:
                return self._current_with_cursor(cursor, project_id, document_id)
        with psycopg.connect(self.database_url) as owned_connection, owned_connection.cursor() as cursor:
            return self._current_with_cursor(cursor, project_id, document_id)

    @staticmethod
    def _current_with_cursor(
        cursor: Any,
        project_id: str,
        document_id: str,
    ) -> CurrentDocumentPointer | None:
        cursor.execute(
            """
            SELECT project_id, document_id, version, file_sha256, source_uri,
                   graph_uri, indexed_at, promoted_at
            FROM orion_workflow.document_current_versions
            WHERE project_id = %s AND document_id = %s
            """,
            (project_id, document_id),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return CurrentDocumentPointer(
            project_id=row[0],
            document_id=row[1],
            version=row[2],
            file_sha256=row[3],
            source_uri=row[4],
            graph_uri=row[5],
            indexed_at=row[6],
            promoted_at=row[7],
        )

    def promote(
        self,
        pointer: CurrentDocumentPointer,
        *,
        expected_current_sha256: str | None,
        connection: Any | None = None,
    ) -> None:
        if connection is not None:
            with connection.cursor() as cursor:
                self._promote_with_cursor(
                    cursor,
                    pointer,
                    expected_current_sha256=expected_current_sha256,
                )
            return
        with psycopg.connect(self.database_url) as owned_connection, owned_connection.cursor() as cursor:
            self._promote_with_cursor(
                cursor,
                pointer,
                expected_current_sha256=expected_current_sha256,
            )

    def count_current(self, project_id: str) -> int:
        with psycopg.connect(self.database_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM orion_workflow.document_current_versions
                WHERE project_id = %s
                """,
                (project_id,),
            )
            row = cursor.fetchone()
        return int(row[0]) if row is not None else 0

    @staticmethod
    def _promote_with_cursor(
        cursor: Any,
        pointer: CurrentDocumentPointer,
        *,
        expected_current_sha256: str | None,
    ) -> None:
        payload = pointer.model_dump(mode="json")
        cursor.execute(
            """
            SELECT file_sha256
            FROM orion_workflow.document_current_versions
            WHERE project_id = %s AND document_id = %s
            FOR UPDATE
            """,
            (pointer.project_id, pointer.document_id),
        )
        row = cursor.fetchone()
        observed = str(row[0]) if row is not None else None
        if observed != expected_current_sha256:
            raise DocumentPromotionConflict(
                "document current version changed before promote"
            )
        cursor.execute(
            """
            INSERT INTO orion_workflow.document_version_receipts (
                project_id, document_id, file_sha256, version, source_uri,
                graph_uri, indexed_at, promoted_at, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (project_id, document_id, file_sha256) DO NOTHING
            """,
            (
                pointer.project_id,
                pointer.document_id,
                pointer.file_sha256,
                pointer.version,
                pointer.source_uri,
                pointer.graph_uri,
                pointer.indexed_at,
                pointer.promoted_at,
                Jsonb(payload),
            ),
        )
        cursor.execute(
            """
            INSERT INTO orion_workflow.document_current_versions (
                project_id, document_id, file_sha256, version, source_uri,
                graph_uri, indexed_at, promoted_at, revision, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
            ON CONFLICT (project_id, document_id) DO UPDATE SET
                file_sha256 = EXCLUDED.file_sha256,
                version = EXCLUDED.version,
                source_uri = EXCLUDED.source_uri,
                graph_uri = EXCLUDED.graph_uri,
                indexed_at = EXCLUDED.indexed_at,
                promoted_at = EXCLUDED.promoted_at,
                revision = orion_workflow.document_current_versions.revision + 1,
                payload = EXCLUDED.payload
            """,
            (
                pointer.project_id,
                pointer.document_id,
                pointer.file_sha256,
                pointer.version,
                pointer.source_uri,
                pointer.graph_uri,
                pointer.indexed_at,
                pointer.promoted_at,
                Jsonb(payload),
            ),
        )
