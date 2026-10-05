from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator

from services.realtime_qa.incremental_pipeline import S0IncrementalPipeline
from services.realtime_qa.models import (
    DocumentPageEvidence,
    IncrementalIngestionResult,
    OntologyReleaseBinding,
    OriginalSnapshot,
    S0DocumentVersion,
)

ENTITY_IRI = re.compile(r"^(?:https?://|urn:)[^\s<>{}\"']+$")
PAGE_NUMBER = re.compile(r"第\s*(\d+)\s*页")


class S0RealtimeAdapterError(ValueError):
    pass


class ReviewedEntityLinks(BaseModel):
    project_id: str = Field(min_length=1)
    release_fingerprint: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    reviewed_by: str = Field(min_length=1)
    reviewed_at: datetime
    documents: dict[str, list[str]]

    @field_validator("documents")
    @classmethod
    def validate_documents(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if not value:
            raise ValueError("document entity links are empty")
        for document_id, entity_iris in value.items():
            if not document_id.strip() or not entity_iris:
                raise ValueError("every document must have reviewed entity links")
            for entity_iri in entity_iris:
                if not ENTITY_IRI.fullmatch(entity_iri):
                    raise ValueError(f"invalid entity IRI: {entity_iri}")
        return value


@dataclass(frozen=True)
class PreparedS0Document:
    project_id: str
    document_id: str
    snapshot: OriginalSnapshot
    source_sha256: str
    title: str
    document_type: str
    processed_at: datetime
    full_text: str
    mentions_entities: tuple[str, ...]
    pages: tuple[DocumentPageEvidence, ...]

    def parse_snapshot(
        self,
        path: Path,
        snapshot: OriginalSnapshot,
        version: str,
    ) -> S0DocumentVersion:
        if _sha256(path) != self.source_sha256:
            raise S0RealtimeAdapterError(
                f"immutable original changed for document: {self.document_id}"
            )
        return S0DocumentVersion(
            project_id=self.project_id,
            document_id=self.document_id,
            version=version,
            title=self.title,
            document_type=self.document_type,
            source_uri=snapshot.source_uri,
            file_sha256=self.source_sha256,
            processed_at=self.processed_at,
            full_text=self.full_text,
            mentions_entities=list(self.mentions_entities),
            pages=list(self.pages),
        )


class S0BundleAdapter:
    """Turn a passed S0 bundle plus reviewed entity links into runtime inputs."""

    def prepare(
        self,
        *,
        project_dir: Path,
        input_root: Path,
        binding: OntologyReleaseBinding,
        entity_links_path: Path | None = None,
    ) -> list[PreparedS0Document]:
        project_dir = project_dir.resolve()
        input_root = input_root.resolve()
        stage_dir = project_dir / "00-document-evidence"
        gate = _read_json(stage_dir / "gate-results.json")
        if gate.get("stage") != "S0" or gate.get("status") != "PASSED":
            raise S0RealtimeAdapterError("S0 gate has not passed")
        links: ReviewedEntityLinks | None = None
        if entity_links_path is not None:
            links = ReviewedEntityLinks.model_validate(_read_json(entity_links_path))
            if links.project_id != binding.project_id or project_dir.name != binding.project_id:
                raise S0RealtimeAdapterError("entity links project does not match release")
            if links.release_fingerprint != binding.release_fingerprint:
                raise S0RealtimeAdapterError("entity links are not bound to this release")
        elif "current_full_text_search" not in binding.document_query_capabilities:
            raise S0RealtimeAdapterError(
                "release does not allow full-text document publication without entity links"
            )

        register = _read_json(stage_dir / "document-register.json")
        evidence_index = _read_json(stage_dir / "evidence-index.json")
        quality = _read_json(stage_dir / "ingestion-quality-report.json")
        if not isinstance(register, list) or not register:
            raise S0RealtimeAdapterError("S0 document register is empty")
        if not isinstance(evidence_index, list):
            raise S0RealtimeAdapterError("S0 evidence index is invalid")
        processed_at = datetime.fromisoformat(
            str(quality.get("reviewed_at") or gate.get("checked_at") or "")
        )

        evidence_by_document: dict[str, list[dict[str, Any]]] = {}
        for item in evidence_index:
            if not isinstance(item, dict):
                raise S0RealtimeAdapterError("S0 evidence entry is invalid")
            document_id = str(item.get("document_id") or "").strip()
            if not document_id:
                raise S0RealtimeAdapterError("S0 evidence has an empty document_id")
            evidence_by_document.setdefault(document_id, []).append(item)

        prepared: list[PreparedS0Document] = []
        seen: set[str] = set()
        for item in register:
            if not isinstance(item, dict):
                raise S0RealtimeAdapterError("S0 document entry is invalid")
            document_id = str(item.get("document_id") or "").strip()
            if not document_id or document_id in seen:
                raise S0RealtimeAdapterError("S0 document_id is empty or duplicated")
            seen.add(document_id)
            _validate_source_reference(
                input_root,
                str(item.get("source_path") or ""),
            )
            entity_iris = links.documents.get(document_id, []) if links is not None else []
            source_sha256 = str(item.get("source_sha256") or "").removeprefix(
                "sha256:"
            )
            snapshot_sha256 = str(
                item.get("original_snapshot_sha256") or ""
            ).removeprefix("sha256:")
            if (
                item.get("original_storage_backend") != "minio"
                or not str(item.get("original_bucket") or "").strip()
                or not str(item.get("original_object_key") or "").strip()
                or not str(item.get("original_source_uri") or "").startswith("minio://")
                or snapshot_sha256 != source_sha256
            ):
                raise S0RealtimeAdapterError(
                    f"verified MinIO original is missing or inconsistent for document: {document_id}"
                )
            snapshot = OriginalSnapshot(
                backend="minio",
                bucket=str(item["original_bucket"]),
                object_key=str(item["original_object_key"]),
                source_uri=str(item["original_source_uri"]),
                file_sha256=source_sha256,
                size_bytes=int(
                    item.get("original_snapshot_size_bytes")
                    or item.get("source_size_bytes")
                    or 0
                ),
            )
            markdown_path = _safe_relative(
                stage_dir,
                str(item.get("structured_markdown_path") or ""),
            )
            full_text = markdown_path.read_text(encoding="utf-8")
            markdown_sha256 = str(
                item.get("structured_markdown_sha256") or ""
            ).removeprefix("sha256:")
            if markdown_sha256 and _sha256(markdown_path) != markdown_sha256:
                raise S0RealtimeAdapterError(
                    f"structured Markdown SHA-256 mismatch for document: {document_id}"
                )
            evidence_rows = evidence_by_document.get(document_id) or []
            if not evidence_rows:
                raise S0RealtimeAdapterError(
                    f"S0 evidence is missing for document: {document_id}"
                )
            pages = tuple(
                self._page_evidence(full_text, item, evidence)
                for evidence in evidence_rows
            )
            prepared.append(
                PreparedS0Document(
                    project_id=binding.project_id,
                    document_id=document_id,
                    snapshot=snapshot,
                    source_sha256=source_sha256,
                    title=str(item.get("source_name") or document_id),
                    document_type=str(item.get("source_type") or "document").lower(),
                    processed_at=processed_at,
                    full_text=full_text,
                    mentions_entities=tuple(dict.fromkeys(entity_iris)),
                    pages=pages,
                )
            )
        unknown_links = set(links.documents) - seen if links is not None else set()
        if unknown_links:
            raise S0RealtimeAdapterError(
                "entity links reference unknown documents: "
                + ", ".join(sorted(unknown_links))
            )
        return prepared

    @staticmethod
    def _page_evidence(
        markdown: str,
        document: dict[str, Any],
        evidence: dict[str, Any],
    ) -> DocumentPageEvidence:
        locator = str(evidence.get("source_locator") or "")
        page_match = PAGE_NUMBER.search(locator)
        try:
            source_page = int(evidence.get("source_page") or 0)
        except (TypeError, ValueError):
            source_page = 0
        page_number = source_page or (int(page_match.group(1)) if page_match else 1)
        section = str(evidence.get("markdown_section") or "").strip()
        text = _markdown_evidence_text(markdown, section)
        confidence_value = evidence.get("confidence")
        if confidence_value is None:
            confidence_value = document.get("extraction_confidence")
        return DocumentPageEvidence(
            page_number=page_number,
            text=text or markdown.strip(),
            confidence=(
                float(confidence_value) if confidence_value is not None else None
            ),
            source_locator=locator or None,
        )


class S0RealtimePublisher:
    def __init__(
        self,
        pipeline: S0IncrementalPipeline,
        adapter: S0BundleAdapter | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.adapter = adapter or S0BundleAdapter()

    def publish(
        self,
        *,
        project_dir: Path,
        input_root: Path,
        binding: OntologyReleaseBinding,
        entity_links_path: Path | None = None,
        force_reindex: bool = False,
    ) -> list[IncrementalIngestionResult]:
        documents = self.adapter.prepare(
            project_dir=project_dir,
            input_root=input_root,
            binding=binding,
            entity_links_path=entity_links_path,
        )
        return [
            self.pipeline.ingest_snapshot(
                document.snapshot,
                document.project_id,
                document.document_id,
                document.parse_snapshot,
                force_reindex=force_reindex,
            )
            for document in documents
        ]


def _safe_relative(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise S0RealtimeAdapterError("S0 artifact path must be relative")
    target = (root / relative).resolve()
    if root not in target.parents:
        raise S0RealtimeAdapterError("S0 artifact path escapes its approved root")
    if not target.is_file():
        raise S0RealtimeAdapterError(f"S0 artifact is missing: {target.name}")
    return target


def _validate_source_reference(root: Path, relative: str) -> None:
    if not relative or Path(relative).is_absolute():
        raise S0RealtimeAdapterError("S0 source_path must be a relative provenance path")
    target = (root / relative).resolve()
    if root not in target.parents:
        raise S0RealtimeAdapterError("S0 source_path escapes its approved root")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise S0RealtimeAdapterError(f"S0 artifact is missing: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise S0RealtimeAdapterError(f"S0 artifact is invalid JSON: {path.name}") from exc


def _markdown_section(markdown: str, section: str) -> str:
    if section == "全文":
        return markdown.strip()
    lines = markdown.splitlines()
    start: int | None = None
    level = 0
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match and match.group(2) == section:
            start = index + 1
            level = len(match.group(1))
            break
    if start is None:
        return ""
    end = len(lines)
    for index in range(start, len(lines)):
        match = re.match(r"^(#{1,6})\s+", lines[index])
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "\n".join(lines[start:end]).strip()


def _markdown_evidence_text(markdown: str, section: str) -> str:
    """Resolve a reviewed locator without expanding one paragraph to the full file."""

    if not section or section == "全文":
        return markdown.strip()
    section_text = _markdown_section(markdown, section)
    if section_text:
        return section_text
    for line in markdown.splitlines():
        normalized = re.sub(r"^#{1,6}\s+", "", line).strip()
        if normalized == section:
            return normalized
    # Preserve the reviewed section label as a bounded locator fallback; never
    # expand one unresolved paragraph locator into the entire document body.
    return section
