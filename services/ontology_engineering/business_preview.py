"""Bounded background preview using existing stage leases and runtime adapters.

All writes are confined to .business-previews. No formal stage artifacts,
workflow transitions, approval tokens, releases or chat continuations are made.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from services.ontology_contracts.errors import WorkflowError
from services.structured_data.pipeline import StructuredDataImportError

from .business_preview_inputs import (
    digest,
    input_fingerprint,
    prepare_input,
    read_context,
    select_plan,
)
from .business_preview_resources import recover_resources
from .stage_execution import StageExecutionLease
from .stage_jobs import ROOT, _identity, _locked, _save

ACTIVE = {"STARTING", "RUNNING"}
JOB_ID = re.compile(r"[a-f0-9]{64}-[12]\Z")
DEADLINE_SECONDS = 180


def _now():
    return datetime.now(UTC).isoformat()


def _directory(parent, name, *, create=False):
    path = parent / name
    if path.is_symlink() or path.resolve().parent != parent.resolve():
        raise WorkflowError("试运行目录不安全。")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    return path


def _read(path, default=None, *, limit=65536):
    if path.is_symlink():
        raise WorkflowError("试运行记录不能是符号链接。")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return default
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(limit + 1)
    try:
        value = json.loads(raw) if len(raw) <= limit else None
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError) as exc:
        raise WorkflowError("试运行记录损坏，不能当作成功或重复启动。") from exc


def _record(directory, job_id):
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise WorkflowError("试运行记录 ID 不合法。")
    job_dir = _directory(directory, job_id)
    return _read(job_dir / "receipt.json")


def _status(record):
    if not record or record.get("status") not in ACTIVE:
        return record
    record = dict(record)
    pid, identity = record.get("pid"), record.get("process_identity")
    elapsed = time.time() - record.get("started_epoch", 0)
    if type(pid) is int:
        try:
            current_identity = _identity(pid)
        except (OSError, subprocess.TimeoutExpired):
            current_identity = None
        if identity and current_identity == identity:
            return record
        if not current_identity or not identity:
            try:
                probe = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True,
                                       text=True, timeout=5, check=False)
                alive = probe.returncode == 0 and probe.stdout.strip() and not probe.stdout.strip().startswith("Z")
            except (OSError, subprocess.TimeoutExpired):
                return record  # Uncertain live ownership must never duplicate.
            if alive:
                return {**record, "message": "试运行进程仍存在，正在等待身份或最终回执核对；不会重复启动。"}
    if not pid and elapsed < 10:
        return record
    record.update(status="INTERRUPTED", message="试运行进程已结束且没有完整回执；可显式重试一次，不会重跑正式阶段。")
    return record


def _public(record):
    return {k: v for k, v in record.items() if k not in {"pid", "process_identity", "started_epoch"}}


def get_business_preview(tools, *, project_id):
    context = read_context(tools, project_id)
    response = {k: context[k] for k in ("project_id", "revision", "stage", "payload_file")}
    response.update(schema_version=1, status="NOT_READY", plans=[], active_preview=None,
                    message=context.get("message", "单项快照试运行；不代表正式阶段、全量数据或实时问答验收。"))
    directory = _directory(tools.service._resolve_project(project_id), ".business-previews")
    index = _read(directory / "index.json", {"latest": {}})
    # Execution liveness is independent of whether its design is still current
    # or even listed. Keep tracking the owner until it ends; STALE only fences
    # evidence, and must not reopen the UI's admission gate prematurely.
    active = _status(_record(directory, index["active"])) if index.get("active") else None
    if active and active["status"] in ACTIVE:
        response["active_preview"] = {key: active.get(key) for key in (
            "preview_id", "project_id", "revision", "plan_id", "case_id", "status", "message", "observed_at",
        )}
    if not context["plans"]:
        return response
    for plan in context["plans"]:
        cases = plan.get("validation_cases") or []
        entry = {"id": plan["id"], "title": plan.get("description_zh") or plan["id"],
                 "question_examples": plan.get("question_examples", []),
                 "case_ids": [c["id"] for c in cases if isinstance(c, dict) and isinstance(c.get("id"), str)],
                 "status": "NOT_STARTED"}
        job_id = index.get("latest", {}).get(plan["id"])
        record = _status(_record(directory, job_id)) if job_id else None
        if record:
            current = input_fingerprint(context, plan["id"], record.get("case_id"))
            if current != record.get("input_fingerprint"):
                record = {**record, "status": "STALE", "message": "草稿、来源或执行代码已变化，旧试运行不再代表当前设计。"}
            entry.update(status=record["status"], receipt=_public(record))
        response["plans"].append(entry)
    # A same-revision patch can race a GET. Never attach old evidence to a new draft.
    latest = tools._get_stage_draft(project_id=project_id, stage="S3")
    if latest.get("payload_file") != context["payload_file"] or latest.get("revision") != context["revision"]:
        return {**response, "status": "NOT_READY", "plans": [], "message": "草稿已变化，请刷新业务能力。"}
    response["status"] = "AVAILABLE"
    return response


@contextmanager
def _slot(home):
    """Kernel-owned process slots survive web reload and are released on death."""
    import fcntl

    directory = _directory(home, ".business-preview-slots", create=True)
    for number in range(2):
        fd = os.open(directory / f"{number}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            continue
        try:
            yield fd
        finally:
            os.close(fd)  # Worker inherits the same flock; no explicit LOCK_UN.
        return
    raise WorkflowError("平台已有两项业务试运行，请等现有任务结束后再启动。")


def start_business_preview(tools, *, project_id, expected_revision, payload_file, plan_id, case_id=None, retry=False):
    if type(retry) is not bool:
        raise WorkflowError("retry 必须是布尔值。")
    with tools.service._project_mutation_lock(project_id):
        context = read_context(tools, project_id)
        if (type(expected_revision) is not int or expected_revision != context["revision"]
                or context["current_stage"] != "S3" or not payload_file or payload_file != context["payload_file"]):
            raise WorkflowError("试运行只接受当前 S3 修订的最新草稿，请刷新后再启动。")
        plan, case = select_plan(context, plan_id, case_id)
        fingerprint = input_fingerprint(context, plan_id, case["id"])
        root = tools.service._resolve_project(project_id)
        directory = _directory(root, ".business-previews", create=True)
        with _locked(directory / "index.lock"):
            index = _read(directory / "index.json", {"latest": {}})
            active = _status(_record(directory, index["active"])) if index.get("active") else None
            if active and active["status"] in ACTIVE:
                if active["input_fingerprint"] == fingerprint:
                    return _public(active)
                raise WorkflowError("本工程已有试运行正在执行；结束后再运行修订后的能力，避免并发占用。")
            if active:
                recover_resources(_directory(directory, active["preview_id"]), active, _read)
            attempt = 1
            for number in (1, 2):
                previous = _status(_record(directory, f"{fingerprint}-{number}"))
                if previous:
                    if not retry or previous["status"] == "PASSED" or number == 2:
                        index["latest"][plan_id] = previous["preview_id"]
                        _save(directory / "index.json", index)
                        return _public(previous)
                    attempt = number + 1
            job_id = f"{fingerprint}-{attempt}"
            job_dir = _directory(directory, job_id, create=True)
            record = {"preview_id": job_id, "project_id": project_id, "revision": expected_revision,
                      "stage": "S3", "payload_file": payload_file, "plan_id": plan_id, "case_id": case["id"],
                      "input_fingerprint": fingerprint, "attempt": attempt, "max_attempts": 2,
                      "validation_scope": "S3_BUSINESS_PREVIEW", "formal_state_changed": False,
                      "source_mode": "SNAPSHOT_ONLY", "status": "STARTING", "observed_at": _now(),
                      "started_epoch": time.time(), "message": "正在准备本项业务能力的独立试运行。",
                      "diagnostics": [], "rows": [], "source_refs": []}
            try:
                job = prepare_input(context, plan, case)
            except (WorkflowError, StructuredDataImportError, ValueError, KeyError, TypeError) as exc:
                record.update(status="FAILED", message="本项业务能力还需要局部修订。", diagnostics=[{
                    "code": "DESIGN_NOT_EXECUTABLE", "message": str(exc)[:1500],
                    "repair_path": f"/mapping_draft/business_query_plans/{context['plans'].index(plan)}",
                    "next_step": "只修订这项业务计划及其依赖映射，保存草稿后重试；无需重跑其他阶段。",
                }])
                _save(job_dir / "receipt.json", record)
                index["latest"][plan_id] = job_id
                _save(directory / "index.json", index)
                return _public(record)
            with _slot(tools.service.root) as slot_fd:
                record["input_sha256"] = digest(job)
                _save(job_dir / "input.json", job)
                _save(job_dir / "receipt.json", record)
                index.update(active=job_id)
                index["latest"][plan_id] = job_id
                _save(directory / "index.json", index)
                try:
                    process = subprocess.Popen(
                        [sys.executable, "-m", "services.ontology_engineering.business_preview", "--worker",
                         "--workflow-home", str(tools.service.root), "--project-id", project_id,
                         "--job-id", job_id, "--slot-fd", str(slot_fd)], cwd=ROOT,
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        pass_fds=(slot_fd,), start_new_session=True,
                    )
                    record.update(pid=process.pid, process_identity=_identity(process.pid))
                except OSError:
                    record.update(status="FAILED", message="平台未能启动试运行进程；请稍后显式重试。")
                _save(job_dir / "receipt.json", record)
            return _public(record)


def _worker(home, project_id, job_id, slot_fd):
    from harness.orion_workflow_action import tools_for_action

    from .business_preview_execution import execute_preview

    os.environ["ORION_WORKFLOW_HOME"] = str(home)
    tools = tools_for_action("get_business_preview")
    root = tools.service._resolve_project(project_id)
    directory = _directory(root, ".business-previews")
    job_dir = _directory(directory, job_id)
    # The parent holds this mutex through spawn, so PID publication precedes work.
    with _locked(directory / "index.lock"):
        record = _record(directory, job_id)
        if record["status"] != "STARTING" or record.get("pid") != os.getpid():
            return
        record.update(status="RUNNING", message="正在验证来源并启动独立查询环境。", observed_at=_now())
        _save(job_dir / "receipt.json", record)
    lease = StageExecutionLease.acquire(project_dir=job_dir, stage="S3", input_fingerprint=record["input_fingerprint"],
                                         executor_id="ORION_BUSINESS_PREVIEW", lease_seconds=60)
    lease.start_heartbeat()

    def progress(message):
        lease.assert_active()
        if isinstance(message, dict):
            record["phase"] = str(message.get("phase", ""))[:80]
            message = message.get("message", "正在试运行。")
        record.update(message=str(message)[:500], observed_at=_now(), elapsed_seconds=round(time.time() - record["started_epoch"], 1))
        _save(job_dir / "receipt.json", record)

    def timeout(_signal, _frame):
        raise TimeoutError("preview deadline")

    old_alarm = signal.signal(signal.SIGALRM, timeout)
    old_term = signal.signal(signal.SIGTERM, timeout)
    signal.alarm(DEADLINE_SECONDS)
    try:
        # The worker writes its result under the same persistence guard as the
        # host. Keep connection setup inside the diagnostic boundary so a
        # ledger outage cannot leave an unexplained STARTING receipt.
        progress({"phase": "INPUT_VALIDATION", "message": "核对工程账本连接与本次试运行输入。"})
        tools = tools_for_action("start_business_preview")
        job = _read(job_dir / "input.json", limit=32 * 1024 * 1024)
        if digest(job) != record.get("input_sha256"):
            raise WorkflowError("试运行输入完整性不符。")
        context = read_context(tools, project_id)
        if (context["current_stage"] != "S3"
                or input_fingerprint(context, record["plan_id"], record["case_id"]) != record["input_fingerprint"]):
            outcome = {"status": "STALE", "message": "启动前草稿或来源已变化，请回读当前设计。"}
        else:
            candidate_dir = _directory(job_dir, "candidate", create=True)
            outcome = execute_preview(job, candidate_dir, progress)
        progress({"phase": "RESULT_RECONCILIATION", "message": "核对本次结果与当前草稿、来源和任务归属。"})
        with tools.service._project_mutation_lock(project_id):
            context = read_context(tools, project_id)
            if (context["current_stage"] != "S3"
                    or input_fingerprint(context, record["plan_id"], record["case_id"]) != record["input_fingerprint"]):
                outcome.update(status="STALE", message="试运行期间输入发生变化，结果不再代表当前设计。")
            lease.assert_active()
            record.update(outcome)
            lease.complete({"validation_scope": "S3_BUSINESS_PREVIEW", "preview_status": record["status"]})
    except TimeoutError:
        record.update(status="INCONCLUSIVE", message="试运行超过 180 秒预算，已停止；缩小单项能力范围后再试。",
                      diagnostics=[{"code": "PREVIEW_TIMEOUT", "message": "未完成实际验证，不能判定通过。"}])
        lease.fail("PREVIEW_TIMEOUT")
    except Exception as exc:
        from .business_preview_diagnostics import worker_failure

        record.update(status="FAILED", message="平台后台任务异常，已保留草稿；自动建模应停止并报告诊断。",
                      diagnostics=[worker_failure(exc, record.get("phase"))])
        lease.fail("PREVIEW_WORKER_ERROR")
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_alarm)
        signal.signal(signal.SIGTERM, old_term)
        record.update(observed_at=_now(), elapsed_seconds=round(time.time() - record["started_epoch"], 1))
        _save(job_dir / "receipt.json", record)
        # Keep the inherited slot until process exit, including the candidate
        # adapter's atexit container cleanup. The kernel releases it on a crash.


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true", required=True)
    parser.add_argument("--workflow-home", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--slot-fd", type=int, required=True)
    args = parser.parse_args()
    if not JOB_ID.fullmatch(args.job_id):
        raise ValueError("invalid preview ID")
    _worker(args.workflow_home, args.project_id, args.job_id, args.slot_fd)


if __name__ == "__main__":
    main()
