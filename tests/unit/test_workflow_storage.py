from pathlib import Path
from typing import Any

from services.ontology_engineering.storage import PostgresWorkflowMetadataStore


class RecordingCursor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def execute(self, statement: str, parameters: Any = None) -> None:
        self.calls.append((statement, parameters))


def test_artifact_sync_preserves_lifecycle_metadata(tmp_path: Path) -> None:
    project_dir = tmp_path / "lifecycle-project"
    artifact_path = project_dir / "03-mapping-review" / "mapping.yaml"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("mappings: []\n", encoding="utf-8")
    cursor = RecordingCursor()
    store = PostgresWorkflowMetadataStore("postgresql://unused")

    counts = store._sync_artifacts(
        cursor,
        "lifecycle-project",
        project_dir,
        {
            "03-mapping-review/mapping.yaml": {
                "display_name": "正式映射",
                "artifact_type": "Mapping",
                "purpose": "保留失效前版本供审计",
                "lifecycle_status": "INVALIDATED",
                "historical_snapshot": "revisions/REV-1/before/03-mapping-review/mapping.yaml",
            }
        },
    )

    payload = cursor.calls[-1][1][-1].obj
    assert counts == {"artifacts": 1, "object_artifacts": 0}
    assert payload["lifecycle_status"] == "INVALIDATED"
    assert payload["historical_snapshot"].startswith("revisions/REV-1/before/")


def test_artifact_sync_excludes_internal_hidden_directories_and_symlinks(
    tmp_path: Path,
) -> None:
    project_dir = tmp_path / "hidden-artifact-project"
    formal = project_dir / "04-ontology-design" / "ontology.ttl"
    preview = project_dir / ".operation-previews" / "preview.json"
    receipt = project_dir / ".operation-receipts" / "receipt.json"
    formal.parent.mkdir(parents=True)
    preview.parent.mkdir(parents=True)
    receipt.parent.mkdir(parents=True)
    formal.write_text("@prefix : <urn:test:> .\n", encoding="utf-8")
    preview.write_text('{"preview_token_sha256":"internal"}', encoding="utf-8")
    receipt.write_text('{"requested_by":"internal"}', encoding="utf-8")
    (project_dir / "linked.ttl").symlink_to(formal)
    cursor = RecordingCursor()
    store = PostgresWorkflowMetadataStore("postgresql://unused")

    counts = store._sync_artifacts(cursor, "hidden-artifact-project", project_dir, {})

    inserted_paths = [
        parameters[1]
        for statement, parameters in cursor.calls
        if "INSERT INTO orion_workflow.artifacts" in statement
    ]
    assert counts == {"artifacts": 1, "object_artifacts": 0}
    assert inserted_paths == ["04-ontology-design/ontology.ttl"]
