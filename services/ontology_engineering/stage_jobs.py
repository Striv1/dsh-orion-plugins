"""Durable platform entry for the existing S5/S6 managed runners."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .joint_design import verify_joint_baseline

ROOT = Path(__file__).resolve().parents[2]
RUNNERS = {"S5": "ontology-s5", "S6": "ontology-s6"}
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_FAILURE_TAIL_LINES = 12
_FAILURE_TAIL_BYTES = 4000
# Runner preconditions that fail before any stage work starts. The previous
# stage execution record then says nothing about this attempt.
_PRECONDITION_FAILURES = (
    ("[build-manifest-check] Error", "BUILD_MANIFEST_STALE",
     "受管运行前置校验失败：build manifest 已过期，本次没有执行任何阶段工作。"
     "这是平台部署状态问题，不是工程内容问题；维护侧重新生成 manifest 后可直接重新启动本阶段。"),
    ("[semantica-runtime] Error", "SEMANTICA_RUNTIME_UNAVAILABLE",
     "受管运行前置检查失败：Semantica 运行时不可用，本次没有执行任何阶段工作；恢复服务后可直接重新启动本阶段。"),
)


def _now() -> str:
    return datetime.now().astimezone().isoformat()


def _save(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".stage-job-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _identity(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat=", "-o", "lstart=", "-o", "command="],
        capture_output=True, text=True, timeout=5, check=False,
    )
    parts = result.stdout.strip().split(maxsplit=1)
    if result.returncode != 0 or len(parts) != 2 or parts[0].startswith("Z"):
        return None
    return parts[1]


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    if path.is_symlink():
        raise ValueError("阶段任务锁不能是符号链接。")
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _paths(service: Any, project_id: str, stage: str, *, create: bool = True) -> tuple[Path, Path, Path]:
    if not _PROJECT_ID.fullmatch(project_id) or stage not in RUNNERS:
        raise ValueError("仅允许当前工程的 S5/S6 受管阶段任务。")
    project_dir = service._resolve_project(project_id)
    directory = project_dir / "stage-jobs"
    if directory.is_symlink() or not directory.resolve().is_relative_to(project_dir.resolve()):
        raise ValueError("阶段任务目录不安全。")
    if create:
        directory.mkdir(exist_ok=True)
    job = directory / f"{stage}.json"
    log = directory / f"{stage}.log"
    if job.is_symlink() or log.is_symlink():
        raise ValueError("阶段任务记录不能是符号链接。")
    return job, log, directory / f"{stage}.lock"


def _log_segment_tail(log_path: Path, offset: int) -> str:
    """Bounded tail of the log written by one job only (never earlier jobs)."""
    try:
        with log_path.open("rb") as handle:
            end = handle.seek(0, os.SEEK_END)
            handle.seek(max(min(offset, end), end - _FAILURE_TAIL_BYTES))
            text = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-_FAILURE_TAIL_LINES:])


def _precondition_failure(segment: str) -> tuple[str, str] | None:
    for marker, code, message in _PRECONDITION_FAILURES:
        if marker in segment:
            return code, message
    return None


_MAX_WAIT_SECONDS = 60
_WAIT_POLL_SECONDS = 2.0


def read_stage_job(
    service: Any, *, project_id: str, stage: str, wait_seconds: int = 0,
) -> dict[str, Any]:
    """Read a managed job; optionally block up to wait_seconds while it runs.

    The bounded wait lets the agent observe long S5/S6 work through the
    platform tool instead of shell sleeps that die with a host restart.
    """
    if isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int) or wait_seconds < 0:
        raise ValueError("wait_seconds 必须是 0 到 60 的整数。")
    deadline = time.monotonic() + min(wait_seconds, _MAX_WAIT_SECONDS)
    while True:
        result = _read_stage_job_once(service, project_id=project_id, stage=stage)
        if result.get("status") not in {"STARTED", "RUNNING"} or time.monotonic() >= deadline:
            if wait_seconds:
                result["waited_seconds"] = round(
                    min(wait_seconds, _MAX_WAIT_SECONDS) - max(deadline - time.monotonic(), 0), 1)
            return result
        time.sleep(min(_WAIT_POLL_SECONDS, max(deadline - time.monotonic(), 0.05)))


def _read_stage_job_once(service: Any, *, project_id: str, stage: str) -> dict[str, Any]:
    job_path, _log_path, _lock_path = _paths(service, project_id, stage, create=False)
    if not job_path.is_file():
        return {"project_id": project_id, "stage": stage, "status": "NOT_STARTED"}
    job = json.loads(job_path.read_text(encoding="utf-8"))
    if job.get("project_id") != project_id or job.get("stage") != stage:
        raise ValueError("阶段任务身份与请求不一致。")
    if job.get("status") == "STARTING":
        job["status"] = "NEEDS_RECONCILIATION"
    if job.get("status") in {"STARTED", "RUNNING"}:
        pid, identity = job.get("pid"), job.get("process_identity")
        if isinstance(pid, int) and identity and _identity(pid) != identity:
            job["status"] = "NEEDS_RECONCILIATION"
    result = {key: job.get(key) for key in (
        "job_id", "project_id", "stage", "status", "expected_revision",
        "started_at", "updated_at", "finished_at", "exit_code", "workflow_stage",
        "workflow_revision", "error_code", "error_message_zh", "failure_log_tail",
    )}
    lease_path = service._resolve_project(project_id) / ".stage-executions" / f"{stage}.json"
    if lease_path.is_file() and not lease_path.is_symlink():
        lease = json.loads(lease_path.read_text(encoding="utf-8"))
        if lease.get("project_id") == project_id and lease.get("stage") == stage:
            execution = {key: lease.get(key) for key in (
                "execution_id", "status", "started_at", "heartbeat_at", "last_error", "completed_at",
            )}
            # An execution that started before this job belongs to an earlier
            # attempt; say so instead of letting it read as this job's result.
            started, job_started = execution.get("started_at"), job.get("started_at")
            try:
                earlier = bool(started and job_started) and (
                    datetime.fromisoformat(started) < datetime.fromisoformat(job_started).replace(microsecond=0))
            except ValueError:
                earlier = False
            execution["belongs_to_current_job"] = not earlier
            if earlier:
                execution["note_zh"] = "该执行记录早于当前受管任务，属于上一次尝试，不代表当前任务结果。"
            result["stage_execution"] = execution
    return result


def start_stage_job(
    service: Any, *, project_id: str, stage: str, expected_revision: int,
) -> dict[str, Any]:
    job_path, log_path, lock_path = _paths(service, project_id, stage)
    with _locked(lock_path):
        state = service.get_status(project_id)
        action = service.get_next_action(project_id)
        if (state.get("revision") != expected_revision or state.get("current_stage") != stage
                or state.get("stage_statuses", {}).get(stage) != "RUNNING"
                or action.get("recommended_tool") != "start_managed_stage_execution"):
            raise ValueError("阶段或 revision 已变化，请回读正式工作流状态后再启动。")
        if stage == "S5":
            verify_joint_baseline(service._resolve_project(project_id))
        if job_path.is_file():
            old = json.loads(job_path.read_text(encoding="utf-8"))
            if old.get("status") in {"STARTED", "RUNNING"}:
                pid, identity = old.get("pid"), old.get("process_identity")
                if isinstance(pid, int) and identity and _identity(pid) == identity:
                    return {"project_id": project_id, "stage": stage, "status": "ALREADY_RUNNING",
                            "job_id": old.get("job_id")}
                raise ValueError("前次阶段任务未留下终态，必须先对账，不能重复施工。")
            if old.get("status") in {"STARTING", "NEEDS_RECONCILIATION"}:
                raise ValueError("前次阶段任务需要对账，不能重复施工。")
        job_id = uuid.uuid4().hex
        record = {"job_id": job_id, "project_id": project_id, "stage": stage,
                  "status": "STARTING", "expected_revision": expected_revision,
                  "started_at": _now(), "updated_at": _now()}
        _save(job_path, record)
        try:
            with log_path.open("a", encoding="utf-8") as log:
                os.chmod(log_path, 0o600)
                process = subprocess.Popen(
                    [sys.executable, "-m", "services.ontology_engineering.stage_jobs",
                     "--worker", "--workflow-home", str(service.root), "--project-id", project_id,
                     "--stage", stage, "--job-id", job_id],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError:
            record.update(status="FAILED_TO_START", updated_at=_now())
            _save(job_path, record)
            raise
        identity = None
        for _ in range(10):
            identity = _identity(process.pid)
            if identity:
                break
            time.sleep(0.1)
        if not identity:
            record.update(status="NEEDS_RECONCILIATION", pid=process.pid, updated_at=_now())
            _save(job_path, record)
            raise ValueError("阶段执行进程身份无法确认，禁止自动重试。")
        record.update(status="STARTED", pid=process.pid,
                      process_identity=identity, updated_at=_now())
        _save(job_path, record)
        return {"project_id": project_id, "stage": stage, "status": "STARTED",
                "job_id": job_id, "expected_revision": expected_revision}


def _worker(home: Path, project_id: str, stage: str, job_id: str) -> None:
    from .workflow import OntologyWorkflowService

    service = OntologyWorkflowService(home)
    job_path, log_path, lock_path = _paths(service, project_id, stage)
    for _ in range(50):
        with _locked(lock_path):
            job = json.loads(job_path.read_text(encoding="utf-8"))
            if job.get("job_id") != job_id:
                return
            if job.get("status") == "STARTED":
                job.update(status="RUNNING", updated_at=_now())
                _save(job_path, job)
                break
        time.sleep(0.1)
    else:
        return
    result, exit_code, error_code = "FAILED", None, None
    error_message, failure_tail = None, None
    try:
        status = service.get_status(project_id)
        if (status.get("revision") != job["expected_revision"]
                or status.get("current_stage") != stage):
            result = "STALE"
        else:
            command = ["make", RUNNERS[stage], f"ONTOLOGY_PROJECT_ID={project_id}",
                       f"ONTOLOGY_WORKFLOW_HOME={home}"]
            with log_path.open("a", encoding="utf-8") as log:
                log_offset = log.seek(0, os.SEEK_END)
                exit_code = subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                           stdout=log, stderr=subprocess.STDOUT,
                                           timeout=3600, check=False).returncode
            status = service.get_status(project_id)
            if exit_code == 0 and status["stage_statuses"].get(stage) == "PASSED":
                result = "COMPLETED"
            else:
                error_code = "RUNNER_FAILED"
                failure_tail = _log_segment_tail(log_path, log_offset)
                precondition = _precondition_failure(failure_tail)
                if precondition:
                    error_code, error_message = precondition
    except subprocess.TimeoutExpired:
        error_code = "RUNNER_TIMEOUT"
        status = service.get_status(project_id)
    except Exception:
        error_code = "WORKER_ERROR"
        status = service.get_status(project_id)
    with _locked(lock_path):
        current = json.loads(job_path.read_text(encoding="utf-8"))
        if current.get("job_id") == job_id:
            current.update(status=result, exit_code=exit_code, error_code=error_code,
                           error_message_zh=error_message, failure_log_tail=failure_tail,
                           finished_at=_now(), updated_at=_now(),
                           workflow_stage=status.get("current_stage"), workflow_revision=status.get("revision"))
            _save(job_path, current)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--workflow-home", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--stage", choices=RUNNERS, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args()
    if not args.worker:
        parser.error("Only platform-managed worker execution is supported")
    _worker(args.workflow_home, args.project_id, args.stage, args.job_id)


if __name__ == "__main__":
    main()
