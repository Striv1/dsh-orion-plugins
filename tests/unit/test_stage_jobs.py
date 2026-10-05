from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.ontology_engineering import stage_jobs, workflow


class FakeService:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.project_dir = root / "ontology-project-test"
        self.project_dir.mkdir(exist_ok=True)
        self.state = {
            "revision": 7, "current_stage": "S6", "stage_statuses": {"S6": "RUNNING"},
        }

    def _resolve_project(self, project_id: str) -> Path:
        assert project_id == "ontology-project-test"
        return self.project_dir

    def get_status(self, project_id: str) -> dict:
        self._resolve_project(project_id)
        return self.state

    def get_next_action(self, project_id: str) -> dict:
        self._resolve_project(project_id)
        return {"recommended_tool": "start_managed_stage_execution"}


def test_managed_stage_job_binds_revision_and_deduplicates_live_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(tmp_path)
    assert stage_jobs.read_stage_job(service, project_id="ontology-project-test", stage="S6")["status"] == "NOT_STARTED"
    assert not (service.project_dir / "stage-jobs").exists()
    calls = []

    def spawn(command: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append((command, kwargs))
        return SimpleNamespace(pid=12345)

    monkeypatch.setattr(stage_jobs.subprocess, "Popen", spawn)
    monkeypatch.setattr(stage_jobs, "_identity", lambda _pid: "fixed process identity")
    first = stage_jobs.start_stage_job(
        service, project_id="ontology-project-test", stage="S6", expected_revision=7,
    )
    assert first["status"] == "STARTED"
    assert "ONTOLOGY_PROJECT_ID=" not in " ".join(calls[0][0])
    assert stage_jobs.read_stage_job(service, project_id="ontology-project-test", stage="S6")["job_id"] == first["job_id"]
    again = stage_jobs.start_stage_job(
        service, project_id="ontology-project-test", stage="S6", expected_revision=7,
    )
    assert again["status"] == "ALREADY_RUNNING"
    assert len(calls) == 1
    with pytest.raises(ValueError, match="revision"):
        stage_jobs.start_stage_job(
            service, project_id="ontology-project-test", stage="S6", expected_revision=6,
        )


def test_managed_stage_worker_records_formal_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(tmp_path)
    monkeypatch.setattr(workflow, "OntologyWorkflowService", lambda _home: service)
    job_path, log_path, _ = stage_jobs._paths(service, "ontology-project-test", "S6")
    stage_jobs._save(job_path, {
        "job_id": "job-one", "project_id": "ontology-project-test", "stage": "S6",
        "status": "STARTED", "expected_revision": 7,
    })

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        assert command[:2] == ["make", "ontology-s6"]
        assert "ONTOLOGY_PROJECT_ID=ontology-project-test" in command
        service.state = {
            "revision": 8, "current_stage": "S7", "stage_statuses": {"S6": "PASSED"},
        }
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(stage_jobs.subprocess, "run", run)
    stage_jobs._worker(tmp_path, "ontology-project-test", "S6", "job-one")
    result = json.loads(job_path.read_text())
    assert result["status"] == "COMPLETED"
    assert result["workflow_stage"] == "S7"
    assert log_path.is_file()


def test_managed_stage_worker_classifies_precondition_failure_from_own_log_segment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(tmp_path)
    monkeypatch.setattr(workflow, "OntologyWorkflowService", lambda _home: service)
    job_path, log_path, _ = stage_jobs._paths(service, "ontology-project-test", "S6")
    log_path.write_text("old attempt\nmake[1]: *** [semantica-runtime] Error 1\n", encoding="utf-8")
    stage_jobs._save(job_path, {
        "job_id": "job-two", "project_id": "ontology-project-test", "stage": "S6",
        "status": "STARTED", "expected_revision": 7, "started_at": "2026-09-26T04:30:59+08:00",
    })

    def run(_command: list[str], *, stdout, **_kwargs: object) -> SimpleNamespace:
        stdout.write("build manifest 已过期\nmake[1]: *** [build-manifest-check] Error 1\n")
        stdout.flush()
        return SimpleNamespace(returncode=2)

    monkeypatch.setattr(stage_jobs.subprocess, "run", run)
    stage_jobs._worker(tmp_path, "ontology-project-test", "S6", "job-two")
    result = json.loads(job_path.read_text())
    assert result["status"] == "FAILED"
    assert result["error_code"] == "BUILD_MANIFEST_STALE"
    assert "没有执行任何阶段工作" in result["error_message_zh"]
    assert "semantica-runtime" not in result["failure_log_tail"]

    executions = service.project_dir / ".stage-executions"
    executions.mkdir()
    (executions / "S6.json").write_text(json.dumps({
        "project_id": "ontology-project-test", "stage": "S6", "execution_id": "EXEC-OLD",
        "status": "FAILED", "started_at": "2026-09-26T04:10:00+08:00", "last_error": "old gate",
    }), encoding="utf-8")
    read = stage_jobs.read_stage_job(service, project_id="ontology-project-test", stage="S6")
    assert read["stage_execution"]["belongs_to_current_job"] is False
    from harness.orion_workflow_mcp import OrionWorkflowTools
    summary = OrionWorkflowTools._summary("get_managed_stage_execution", read)
    assert "BUILD_MANIFEST_STALE" in summary and "old gate" not in summary


def test_managed_job_summary_does_not_render_as_unknown_project_status():
    from harness.orion_workflow_mcp import OrionWorkflowTools as OrionWorkflowMcpServer

    summary = OrionWorkflowMcpServer._summary("get_managed_stage_execution", {
        "job_id": "j1", "project_id": "p", "stage": "S6", "status": "RUNNING",
        "stage_execution": {"last_error": None},
    })
    assert "S6 受管任务 j1：RUNNING" in summary
    assert "UNKNOWN" not in summary and "阶段间待推进" not in summary
    failed = OrionWorkflowMcpServer._summary("get_managed_stage_execution", {
        "job_id": "j2", "stage": "S6", "status": "FAILED", "exit_code": 2,
        "stage_execution": {"last_error": "boom"},
    })
    assert "exit_code=2" in failed and "错误：boom" in failed

def test_managed_stage_read_waits_bounded_until_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeService(tmp_path)
    job_path, _log, _ = stage_jobs._paths(service, "ontology-project-test", "S6")
    stage_jobs._save(job_path, {
        "job_id": "job-w", "project_id": "ontology-project-test", "stage": "S6",
        "status": "RUNNING", "expected_revision": 7,
    })
    sleeps = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            stage_jobs._save(job_path, {
                "job_id": "job-w", "project_id": "ontology-project-test", "stage": "S6",
                "status": "COMPLETED", "expected_revision": 7, "exit_code": 0,
            })

    monkeypatch.setattr(stage_jobs.time, "sleep", fake_sleep)
    result = stage_jobs.read_stage_job(
        service, project_id="ontology-project-test", stage="S6", wait_seconds=60,
    )
    assert result["status"] == "COMPLETED" and len(sleeps) == 2
    assert "waited_seconds" in result
    immediate = stage_jobs.read_stage_job(service, project_id="ontology-project-test", stage="S6")
    assert "waited_seconds" not in immediate
    with pytest.raises(ValueError, match="wait_seconds"):
        stage_jobs.read_stage_job(
            service, project_id="ontology-project-test", stage="S6", wait_seconds=True,
        )
