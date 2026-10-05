#!/usr/bin/env python3
"""Idempotently index every published ORION release in Semantica."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from services.ontology_engineering import OntologyWorkflowService, WorkflowError
from services.ontology_engineering.semantica import PublishedOntologySemanticaSync


def sync_operation_key(
    *,
    project_id: str,
    semantica_url: str,
    ontology_path: Path,
) -> str:
    """Bind idempotency to both release content and importer semantics."""

    source_sha256 = hashlib.sha256(ontology_path.read_bytes()).hexdigest()
    material = "\0".join(
        (
            project_id,
            semantica_url.rstrip("/"),
            source_sha256,
            PublishedOntologySemanticaSync.IMPORT_PROFILE,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def release_ontology_path(
    workflow_home: Path,
    project_id: str,
    release_version: str,
) -> Path:
    """Prefer the immutable S7 package and retain a legacy workspace fallback."""

    packaged = (
        workflow_home
        / project_id
        / "07-release"
        / f"ontology-engineering-package-{release_version}"
        / "01-本体模型"
        / "ontology.ttl"
    )
    if packaged.is_file():
        return packaged
    return workflow_home / project_id / "05-ontology-build" / "ontology.ttl"


def select_current_releases(
    workflow_home: Path,
    projects: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Select one active Semantica release per stable ontology IRI.

    ORION keeps every immutable S7 package, while Semantica is the current
    explanatory runtime. Revisions normally retain the ontology IRI, so loading
    every historical project would let an older package overwrite the latest
    graph during restart recovery.
    """

    grouped: dict[str, list[dict[str, Any]]] = {}
    for project in projects:
        if project.get("project_status") != "PUBLISHED":
            continue
        project_id = str(project.get("project_id") or "").strip()
        publication_path = workflow_home / project_id / "07-release/publication.json"
        if not project_id or not publication_path.is_file():
            continue
        publication = json.loads(publication_path.read_text(encoding="utf-8"))
        ontology_iri = str(publication.get("ontology_iri") or "").strip()
        release_version = str(publication.get("release_version") or "").strip()
        try:
            semantic_version = Version(release_version)
        except InvalidVersion:
            # S7 currently rejects invalid semantic versions. Keeping an
            # explicit floor here makes recovery fail safe for older fixtures.
            semantic_version = Version("0")
        candidate = {
            **project,
            "project_id": project_id,
            "ontology_iri": ontology_iri,
            "release_version": release_version,
            "published_at": str(publication.get("published_at") or ""),
            "semantic_version": semantic_version,
        }
        grouped.setdefault(ontology_iri or f"project:{project_id}", []).append(candidate)

    selected: list[dict[str, Any]] = []
    superseded: list[dict[str, Any]] = []
    for candidates in grouped.values():
        candidates.sort(
            key=lambda item: (
                item["semantic_version"],
                item["published_at"],
                item["project_id"],
            ),
            reverse=True,
        )
        current = candidates[0]
        selected.append(current)
        for historical in candidates[1:]:
            superseded.append(
                {
                    "status": "SUPERSEDED",
                    "project_id": historical["project_id"],
                    "release_version": historical["release_version"],
                    "ontology_iri": historical["ontology_iri"],
                    "replaced_by_project_id": current["project_id"],
                    "replaced_by_release_version": current["release_version"],
                    "detail": "历史发布包保留，但不再写入当前 Semantica 运行图。",
                }
            )
    selected.sort(key=lambda item: (item["ontology_iri"], item["project_id"]))
    superseded.sort(key=lambda item: (item["ontology_iri"], item["project_id"]))
    return selected, superseded


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workflow-home", type=Path, default=Path(".orion-workflows"))
    parser.add_argument("--semantica-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()

    results: list[dict] = []
    service = OntologyWorkflowService(args.workflow_home.resolve())
    projects, superseded = select_current_releases(
        args.workflow_home.resolve(),
        service.list_projects()["projects"],
    )
    results.extend(superseded)
    for project in projects:
        project_id = str(project["project_id"])
        operation_key = sync_operation_key(
            project_id=project_id,
            semantica_url=args.semantica_url,
            ontology_path=release_ontology_path(
                args.workflow_home.resolve(),
                project_id,
                str(project["release_version"]),
            ),
        )
        try:
            results.append(
                service.sync_published_ontology_to_semantica(
                    project_id=project_id,
                    synced_by="semantica-sync-published",
                    semantica_url=args.semantica_url,
                    operation_id=f"semantica-sync:{operation_key}",
                )
            )
        except WorkflowError as exc:
            results.append(
                {
                    "status": "FAILED",
                    "project_id": project_id,
                    "detail": str(exc),
                    "error_type": type(exc).__name__,
                }
            )

    print(json.dumps({"count": len(results), "results": results}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
