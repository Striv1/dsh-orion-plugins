"""Bounded asynchronous CSV jobs, with durable identity and cancellation.

Only server-owned plans/connections reach the worker. A completed CSV is immutable
and downloaded only after session/release and content checksum verification.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from services.realtime_qa.analytics_execution import ROOT, postgres_execution
from services.realtime_qa.wren_analysis import WREN_PYTHON

JOB_ID = re.compile(r"^EXP-[a-f0-9]{32}$")
FIELDS = ("session_id", "project_id", "release_version", "release_fingerprint")


class AnalyticsExportJobs:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("ORION_ANALYTICS_EXPORT_ROOT") or ROOT / ".orion-runtime/wren/exports")
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wren-export")
        self.slots = threading.BoundedSemaphore(2)
        self.lock = threading.RLock()
        self.tasks = {}

    def _directory(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        return os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def _write(self, job):
        directory = self._directory()
        temporary = uuid4().hex + ".tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, "w") as stream:
                json.dump(job, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.rename(temporary, job["job_id"] + ".json", src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            os.close(directory)

    def _read(self, scope, job_id):
        if not JOB_ID.fullmatch(str(job_id)):
            raise ValueError("导出任务标识不合法。")
        directory = self._directory()
        try:
            fd = os.open(job_id + ".json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        finally:
            os.close(directory)
        with os.fdopen(fd) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("导出任务不可用。")
            job = json.load(stream)
        if any(job.get(key) != scope.get(key) for key in FIELDS) or job.get("job_id") != job_id:
            raise ValueError("导出任务与当前会话及发布版本不一致。")
        return job

    def status(self, scope, job_id):
        with self.lock:
            job = self._read(scope, job_id)
            if job["status"] in {"QUEUED", "RUNNING"} and job_id not in self.tasks:
                # A process restart never turns an incomplete export into a file.
                job.update(status="INTERRUPTED", detail="执行进程已重启；请重新创建导出任务。")
                self._write(job)
                self._remove_csv(job_id)
            return {k: v for k, v in job.items() if k != "owner_pid"}

    def _remove_csv(self, job_id):
        directory = self._directory()
        try:
            for name in (job_id + ".csv", job_id + ".csv.partial"):
                with suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=directory)
        finally:
            os.close(directory)

    def create(self, scope, binding, descriptor, request, receipt, max_rows, verify_release):
        if not self.slots.acquire(blocking=False):
            raise ValueError("导出队列已满，请等待当前任务完成或取消后再试。")
        try:
            job_id = "EXP-" + uuid4().hex
            job = {**scope, "job_id": job_id, "status": "QUEUED", "created_at": datetime.now(UTC).isoformat(),
                   "owner_pid": os.getpid(), "max_rows": max_rows, "max_bytes": 100 * 1024 * 1024,
                   "origin_receipt": receipt, "question": request.question,
                   "execution_scope": descriptor.get("execution_scope", "RELEASE_SNAPSHOT_DATABASE"),
                   "model_key": descriptor["key"], "detail": "重新执行此查询；文件对应导出事务的数据范围，不伪装为原回执时刻。"}
            with self.lock:
                self._write(job)
                self.tasks[job_id] = {"cancel": threading.Event(), "process": None}
                self.pool.submit(self._run, job, binding, descriptor, request, verify_release)
            return self.status(scope, job_id)
        except BaseException:
            self.slots.release()
            raise

    def _run(self, job, binding, descriptor, request, verify_release):
        task = self.tasks[job["job_id"]]
        try:
            verify_release()
            if task["cancel"].is_set():
                raise InterruptedError
            if descriptor.get("execution_scope") == "LIVE_SOURCE_DATABASE":
                from services.realtime_qa.live_analytics import live_execution
                execution = live_execution(descriptor)
            else:
                execution = postgres_execution(binding, descriptor)
            payload = {"descriptor": {k: v for k, v in descriptor.items() if not k.startswith("_")},
                       "execution": execution, "operation": "export", "limit": job["max_rows"],
                       "plan": {"sql": request.sql, "parameters": request.parameters} if request.sql is not None else {"cube_query": request.cube_query},
                       "export": {"path": str(self.root / (job["job_id"] + ".csv")),
                                  "max_rows": job["max_rows"], "max_bytes": job["max_bytes"]}}
            with self.lock:
                if task["cancel"].is_set():
                    raise InterruptedError
                job.update(status="RUNNING", started_at=datetime.now(UTC).isoformat())
                self._write(job)
                process = subprocess.Popen([str(WREN_PYTHON), str(ROOT / "services/realtime_qa/wren_project_worker.py")],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=ROOT,
                    env={k: v for k, v in os.environ.items() if k in {"PATH", "SYSTEMROOT", "TMPDIR"}})
                task["process"] = process
            stdout, _ = process.communicate(json.dumps(payload, ensure_ascii=False), timeout=130)
            if task["cancel"].is_set():
                raise InterruptedError
            if len(stdout.encode()) > 4 * 1024 * 1024:
                raise ValueError("导出执行回执超过预算。")
            output = json.loads(stdout)
            if process.returncode or output.get("error"):
                # Driver failures must not leak DSNs or arbitrary DB server text.
                raise ValueError("导出未完成，请检查查询范围、行数预算或数据源状态。")
            output.update(output.get("export", {}))
            verify_release()
            path = self.root / (job["job_id"] + ".csv")
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            checksum = hashlib.sha256()
            with os.fdopen(fd, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > job["max_bytes"]:
                    raise ValueError("导出文件超出预算或格式无效。")
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    checksum.update(chunk)
            if (type(output.get("row_count")) is not int or not 0 <= output["row_count"] <= job["max_rows"]
                    or output.get("truncated") is True):
                raise ValueError("导出结果不完整；请缩小范围或提高导出行数预算。")
            with self.lock:
                if task["cancel"].is_set():
                    raise InterruptedError
                safe_keys = ("sql", "expanded_sql", "mdl_sha256", "row_count", "engine_version", "core_version",
                             "transaction_snapshot", "transaction", "query_started_at", "query_finished_at", "execution_mode", "data_freshness")
                job.update(status="COMPLETED", finished_at=datetime.now(UTC).isoformat(),
                           byte_count=metadata.st_size, row_count=output["row_count"], sha256="sha256:" + checksum.hexdigest(),
                           execution={k: output[k] for k in safe_keys if k in output})
                self._write(job)
        except BaseException as exc:
            process = task["process"]
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()
            self._remove_csv(job["job_id"])
            with self.lock:
                job.update(status="CANCELLED" if task["cancel"].is_set() or isinstance(exc, InterruptedError) else "FAILED",
                           finished_at=datetime.now(UTC).isoformat(),
                           detail="导出已取消；未保留不完整文件。" if task["cancel"].is_set() else "导出未完成或超过执行预算，未发布部分文件。")
                self._write(job)
        finally:
            with self.lock:
                self.tasks.pop(job["job_id"], None)
            self.slots.release()

    def cancel(self, scope, job_id):
        with self.lock:
            job = self._read(scope, job_id)
            task = self.tasks.get(job_id)
            if task:
                task["cancel"].set()
                process = task["process"]
                if process is not None and process.poll() is None:
                    process.terminate()
                return {**job, "status": "CANCELLING"}
            return self.status(scope, job_id)

    def download(self, scope, job_id):
        job = self.status(scope, job_id)
        if job["status"] != "COMPLETED":
            raise ValueError("导出尚未完成，不能下载部分数据。")
        fd = os.open(self.root / (job_id + ".csv"), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        stream = os.fdopen(fd, "rb")
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("导出文件不可用。")
            checksum = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                checksum.update(chunk)
            if "sha256:" + checksum.hexdigest() != job["sha256"]:
                raise ValueError("导出文件完整性校验失败。")
            stream.seek(0)
            return stream, job
        except BaseException:
            stream.close()
            raise
