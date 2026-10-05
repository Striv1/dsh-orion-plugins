from __future__ import annotations

import json
from pathlib import Path

import pytest

from services.ingestion.document_jobs import read_document_job
from services.ontology_engineering import WorkflowError

JOB = "JOB-12345678"


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def job(tmp_path: Path, status: str = "READY_FOR_REVIEW") -> Path:
    directory = tmp_path / ".orion-s0-jobs" / JOB
    write_json(directory / "request.json", {"project_id": "project-one", "project_revision": 3})
    write_json(directory / "status.json", {"job_id": JOB, "status": status, "warnings": ["检查原始附件"]})
    write_json(directory / "runner.json", {"state": "IDLE"})
    return directory


def read(root: Path) -> dict:
    return read_document_job(project_id="project-one", job_id=JOB, root=root)


def test_ready_review_returns_actual_queue_outputs_and_does_not_write(tmp_path: Path) -> None:
    directory = job(tmp_path)
    markdown = directory / "structured-markdown/policy.md"
    markdown.parent.mkdir()
    markdown.write_text("# 采购政策\n实际解析内容", encoding="utf-8")
    write_json(directory / "document-register.json", [{
        "document_id": "DOC-1", "source_name": "policy.md", "source_sha256": "source-sha",
        "structured_markdown_path": "structured-markdown/policy.md",
    }])
    write_json(directory / "ingestion-quality-report.json", {"status": "READY_FOR_REVIEW", "warning_count": 1})
    (directory / "batch-review.html").write_text("<html>复核内容</html>")
    before = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    result = read(tmp_path)
    review = result["review"]
    assert review["artifacts"]["batch-review.html"] == str(directory / "batch-review.html")
    assert "evidence-index.json" not in review["artifacts"]
    assert review["documents"][0]["structured_markdown_path"] == str(markdown)
    assert review["documents"][0]["preview"] == "# 采购政策\n实际解析内容"
    assert review["documents"][0]["preview_truncated"] is False
    assert review["quality_summary"]["warning_count"] == 1
    assert review["next_action"] == "REVIEW_QUEUE_OUTPUTS_THEN_COMMIT"
    assert "尚未正式提交 S0" in review["notice"]
    assert "不替代全文" in review["notice"]
    assert result["warnings"] == ["检查原始附件"]
    assert before == {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def test_review_previews_are_bounded_and_report_omitted_documents(tmp_path: Path) -> None:
    directory = job(tmp_path)
    (directory / "large.md").write_text("文" * 1400)
    write_json(directory / "document-register.json", [
        {"document_id": f"DOC-{i}", "structured_markdown_path": "large.md"} for i in range(9)
    ])
    review = read(tmp_path)["review"]
    assert review["document_count"] == 9 and review["documents_truncated"] is True
    assert len(review["documents"]) == 5
    assert len(review["documents"][0]["preview"]) == 1200
    assert review["documents"][0]["preview_truncated"] is True


@pytest.mark.parametrize("unsafe", ["absolute", "traversal", "symlink", "parent_symlink", "missing"])
def test_review_never_reads_outside_queue_or_fabricates_a_path(tmp_path: Path, unsafe: str) -> None:
    directory = job(tmp_path)
    outside = tmp_path / "private.md"
    outside.write_text("PRIVATE_CONTENT")
    relative = "missing.md"
    if unsafe == "absolute":
        relative = str(outside)
    elif unsafe == "traversal":
        relative = "../../private.md"
    elif unsafe == "symlink":
        (directory / "linked.md").symlink_to(outside)
        relative = "linked.md"
    elif unsafe == "parent_symlink":
        (directory / "external").symlink_to(tmp_path, target_is_directory=True)
        relative = "external/private.md"
    write_json(directory / "document-register.json", [{"structured_markdown_path": relative}])
    result = read(tmp_path)["review"]
    assert result["documents"][0]["structured_markdown_path"] is None
    assert "preview" not in result["documents"][0]
    assert "PRIVATE_CONTENT" not in json.dumps(result)


def test_quality_report_symlink_is_not_exposed_or_read(tmp_path: Path) -> None:
    directory = job(tmp_path)
    outside = tmp_path / "quality.json"
    write_json(outside, {"private": "PRIVATE_CONTENT"})
    (directory / "ingestion-quality-report.json").symlink_to(outside)
    result = read(tmp_path)["review"]
    assert "ingestion-quality-report.json" not in result["artifacts"]
    assert "quality_summary" not in result


def test_corrupt_quality_report_is_explicitly_unreadable(tmp_path: Path) -> None:
    directory = job(tmp_path)
    (directory / "ingestion-quality-report.json").write_text("broken")
    review = read(tmp_path)["review"]
    assert "quality_summary" not in review
    assert "不能视为已完成复核" in review["read_errors"][0]


@pytest.mark.parametrize("status,action", [
    ("QUEUED", "WAIT_FOR_PARSING"), ("RUNNING", "WAIT_FOR_PARSING"),
    ("FAILED", "INSPECT_FAILURE"), ("COMMITTED", "READ_COMMIT_RESULT"),
])
def test_only_ready_status_suggests_review_then_commit(tmp_path: Path, status, action) -> None:
    job(tmp_path, status)
    review = read(tmp_path)["review"]
    assert review["next_action"] == action
    assert review["next_tool"] != "commit_document_ingestion_job"


def test_other_project_cannot_read_queue_previews(tmp_path: Path) -> None:
    job(tmp_path)
    with pytest.raises(WorkflowError, match="不属于"):
        read_document_job(project_id="project-other", job_id=JOB, root=tmp_path)
