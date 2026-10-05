from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Protocol
from urllib.parse import quote
from uuid import uuid4

from services.ontology_engineering.storage import MinioArtifactStore
from services.realtime_qa.document_index import S0IncrementalDocumentIndexer
from services.realtime_qa.models import (
    CurrentDocumentPointer,
    IncrementalIngestionResult,
    OriginalSnapshot,
    S0DocumentVersion,
)


class DocumentPromotionConflict(RuntimeError):
    pass


class OriginalSnapshotStore(Protocol):
    def snapshot(self, path: Path, file_sha256: str) -> OriginalSnapshot: ...

    def materialize(self, snapshot: OriginalSnapshot) -> Iterator[Path]: ...


class MinioOriginalSnapshotStore:
    """Adapt the existing workflow MinIO store for immutable S0 originals."""

    def __init__(self, store: MinioArtifactStore) -> None:
        self.store = store

    def snapshot(self, path: Path, file_sha256: str) -> OriginalSnapshot:
        reference = self.store.store_original(path, f"sha256:{file_sha256}")
        return OriginalSnapshot(
            backend="minio",
            bucket=self.store.bucket,
            object_key=reference.object_key,
            source_uri=f"minio://{self.store.bucket}/{reference.object_key}",
            file_sha256=file_sha256,
            size_bytes=path.stat().st_size,
        )

    @contextmanager
    def materialize(self, snapshot: OriginalSnapshot) -> Iterator[Path]:
        with TemporaryDirectory(prefix="orion-s0-original-") as directory:
            target = Path(directory) / "original"
            self.store.client.fget_object(
                snapshot.bucket,
                snapshot.object_key,
                str(target),
            )
            yield target


class DocumentCurrentStore(Protocol):
    def current(
        self,
        project_id: str,
        document_id: str,
    ) -> CurrentDocumentPointer | None: ...

    def promote(
        self,
        pointer: CurrentDocumentPointer,
        *,
        expected_current_sha256: str | None,
    ) -> None: ...

    def count_current(self, project_id: str) -> int: ...


class DocumentCurrentRegistry:
    """Atomically switch a document pointer only after Fuseki indexing succeeds."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def current(
        self,
        project_id: str,
        document_id: str,
    ) -> CurrentDocumentPointer | None:
        path = self._current_path(project_id, document_id)
        if not path.exists():
            return None
        return CurrentDocumentPointer.model_validate_json(path.read_text(encoding="utf-8"))

    def promote(
        self,
        pointer: CurrentDocumentPointer,
        *,
        expected_current_sha256: str | None = None,
    ) -> None:
        current = self.current(pointer.project_id, pointer.document_id)
        observed = current.file_sha256 if current is not None else None
        if observed != expected_current_sha256:
            raise DocumentPromotionConflict(
                "document current version changed before promote"
            )
        path = self._current_path(pointer.project_id, pointer.document_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        receipt_dir = path.parent / "versions"
        receipt_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = receipt_dir / f"{pointer.file_sha256}.json"
        self._atomic_write(receipt_path, pointer.model_dump_json(indent=2))
        self._atomic_write(path, pointer.model_dump_json(indent=2))

    def count_current(self, project_id: str) -> int:
        project_root = self.root / quote(project_id, safe="")
        if not project_root.exists():
            return 0
        return sum(1 for path in project_root.glob("*/current.json") if path.is_file())

    def _current_path(self, project_id: str, document_id: str) -> Path:
        return (
            self.root
            / quote(project_id, safe="")
            / quote(document_id, safe="")
            / "current.json"
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)


DocumentParser = Callable[[Path, OriginalSnapshot, str], S0DocumentVersion]


class S0IncrementalPipeline:
    def __init__(
        self,
        snapshot_store: OriginalSnapshotStore,
        indexer: S0IncrementalDocumentIndexer,
        registry: DocumentCurrentStore,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.snapshot_store = snapshot_store
        self.indexer = indexer
        self.registry = registry
        self.now = now or (lambda: datetime.now(UTC))

    def ingest(
        self,
        source_path: Path,
        project_id: str,
        document_id: str,
        parser: DocumentParser,
        *,
        force_reindex: bool = False,
    ) -> IncrementalIngestionResult:
        file_sha256 = self._sha256(source_path)
        snapshot = self.snapshot_store.snapshot(source_path, file_sha256)
        return self.ingest_snapshot(
            snapshot,
            project_id,
            document_id,
            parser,
            force_reindex=force_reindex,
        )

    def ingest_snapshot(
        self,
        snapshot: OriginalSnapshot,
        project_id: str,
        document_id: str,
        parser: DocumentParser,
        *,
        force_reindex: bool = False,
    ) -> IncrementalIngestionResult:
        file_sha256 = snapshot.file_sha256
        current = self.registry.current(project_id, document_id)
        if (
            not force_reindex
            and current is not None
            and current.file_sha256 == file_sha256
        ):
            return IncrementalIngestionResult(
                status="unchanged",
                snapshot=snapshot,
                current=current,
            )

        version = file_sha256[:16]
        with self.snapshot_store.materialize(snapshot) as immutable_path:
            if self._sha256(immutable_path) != file_sha256:
                raise ValueError("materialized MinIO original does not match its SHA-256")
            parsed = parser(immutable_path, snapshot, version)
        self._validate_parsed_document(
            parsed,
            project_id=project_id,
            document_id=document_id,
            version=version,
            snapshot=snapshot,
        )
        receipt = self.indexer.index(parsed)
        pointer = CurrentDocumentPointer(
            project_id=project_id,
            document_id=document_id,
            version=version,
            file_sha256=file_sha256,
            source_uri=snapshot.source_uri,
            graph_uri=receipt.graph_uri,
            indexed_at=receipt.indexed_at,
            promoted_at=self.now(),
        )
        self.registry.promote(
            pointer,
            expected_current_sha256=(
                current.file_sha256 if current is not None else None
            ),
        )
        return IncrementalIngestionResult(
            status="promoted",
            snapshot=snapshot,
            current=pointer,
        )

    @staticmethod
    def _validate_parsed_document(
        document: S0DocumentVersion,
        *,
        project_id: str,
        document_id: str,
        version: str,
        snapshot: OriginalSnapshot,
    ) -> None:
        expected = (
            project_id,
            document_id,
            version,
            snapshot.file_sha256,
            snapshot.source_uri,
        )
        actual = (
            document.project_id,
            document.document_id,
            document.version,
            document.file_sha256,
            document.source_uri,
        )
        if actual != expected:
            raise ValueError("parsed document does not match the immutable original contract")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
