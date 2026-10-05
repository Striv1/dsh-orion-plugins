from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


class StageExecutionLeaseInactive(RuntimeError):
    """The caller no longer holds an active stage execution lease."""


EXECUTION_LEASE_VERSION = "stage-execution-lease-v1"


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class StageExecutionLease:
    """Durable, resumable ownership record for long platform stage runners."""

    def __init__(self, path: Path, payload: dict[str, Any], *, lease_seconds: int,
                 owner_lock: Any) -> None:
        self.path = path
        self.payload = payload
        self.lease_seconds = lease_seconds
        self._finished = False
        self._ownership_lost = False
        self.cleanup_error: str | None = None
        self._owner_lock = owner_lock
        self._mutex = threading.RLock()
        self._heartbeat_stop = threading.Event()
        self._monotonic_start = time.monotonic()

    def start_heartbeat(self) -> None:
        """Renew during long subprocess, HTTP and graph-processing operations."""
        def renew() -> None:
            while not self._heartbeat_stop.wait(min(15.0, max(0.1, self.lease_seconds / 3))):
                with self._mutex:
                    if self._finished:
                        return
                    now = _now()
                    self.payload["heartbeat_at"] = _iso(now)
                    self.payload["lease_expires_at"] = _iso(
                        now + timedelta(seconds=self.lease_seconds)
                    )
                    if not self._persist_if_owner():
                        return

        threading.Thread(target=renew, name="orion-stage-heartbeat", daemon=True).start()

    def _release(self) -> None:
        self._heartbeat_stop.set()
        if not self._owner_lock.closed:
            self._owner_lock.close()

    def _reject_inactive(self) -> None:
        reason = "OWNERSHIP_LOST" if self._ownership_lost else "NOT_ACTIVE"
        raise StageExecutionLeaseInactive(f"{self.payload['stage']}_EXECUTION_{reason}")

    def assert_active(self) -> None:
        """Check ownership; callers must still fence their own commits.

        This read does not make an external side effect atomic with lease state.
        """
        with self._mutex:
            if self._finished or self._owner_lock.closed:
                self._reject_inactive()
            with self.path.with_suffix(".lock").open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                current = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
                if current.get("execution_id") != self.payload.get("execution_id"):
                    self._ownership_lost = True
                    self._finished = True
                    self._release()
                    self._reject_inactive()
                if current.get("status") != "RUNNING":
                    self._finished = True
                    self._release()
                    self._reject_inactive()

    def _cleanup_persist(self) -> None:
        # Called while handling runner errors and from atexit: preserve the
        # original error even when durable storage is unavailable.
        try:
            self._persist_if_owner()
        except (OSError, ValueError) as error:
            self.cleanup_error = f"{type(error).__name__}: {error}"
        finally:
            self._release()

    def _persist_if_owner(self) -> bool:
        lock_path = self.path.with_suffix(".lock")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = (
                json.loads(self.path.read_text(encoding="utf-8"))
                if self.path.is_file()
                else {}
            )
            if current.get("execution_id") != self.payload.get("execution_id"):
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                self._ownership_lost = True
                self._finished = True
                self._release()
                return False
            # A controller or recovery operation may terminate this exact
            # execution. Never revive it from a stale in-memory RUNNING payload.
            if current.get("status") != "RUNNING":
                self._finished = True
                self._release()
                return False
            self.payload["observed_elapsed_seconds"] = round(time.monotonic() - self._monotonic_start, 3)
            _write_json(self.path, self.payload)
            _write_json(self.path.parent / "history" / f"{self.payload['execution_id']}.json", self.payload)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            return True

    @classmethod
    def acquire(
        cls,
        *,
        project_dir: Path,
        stage: str,
        input_fingerprint: str,
        executor_id: str,
        lease_seconds: int = 1800,
    ) -> StageExecutionLease:
        normalized_stage = str(stage or "").strip().upper()
        if normalized_stage not in {f"S{index}" for index in range(1, 7)}:
            raise ValueError(f"stage execution lease does not support {normalized_stage}")
        execution_dir = project_dir / ".stage-executions"
        execution_dir.mkdir(exist_ok=True)
        path = execution_dir / f"{normalized_stage}.json"
        lock_path = execution_dir / f"{normalized_stage}.lock"
        now = _now()
        # Keep a kernel lock for the entire execution. A slow or CPU-bound step
        # must never become stealable merely because its timestamp expired.
        owner_lock = (execution_dir / f"{normalized_stage}.owner.lock").open("a+")
        try:
            fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner_lock.close()
            raise RuntimeError(f"{normalized_stage}_EXECUTION_ALREADY_RUNNING") from exc
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            previous = (
                json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
            )
            previous_expiry = str(previous.get("lease_expires_at") or "")
            previous_running = str(previous.get("status") or "") == "RUNNING"
            unexpired = bool(previous_expiry) and datetime.fromisoformat(previous_expiry) > now
            if previous_running and unexpired and not previous.get("kernel_lock_version"):
                owner_lock.close()
                raise RuntimeError(
                    f"{normalized_stage}_EXECUTION_ALREADY_RUNNING: "
                    f"execution_id={previous.get('execution_id')} "
                    f"executor={previous.get('executor_id')}"
                )
            same_input = previous.get("input_fingerprint") == input_fingerprint
            # A new attempt must not erase the previous execution's evidence.
            previous_id = str(previous.get("execution_id") or "")
            if previous_id.startswith("EXEC-") and previous_id[5:].isalnum():
                _write_json(execution_dir / "history" / f"{previous_id}.json", previous)
            payload = {
                "schema_version": 1,
                "lease_version": EXECUTION_LEASE_VERSION,
                "kernel_lock_version": 1,
                "project_id": project_dir.name,
                "stage": normalized_stage,
                "execution_id": f"EXEC-{uuid.uuid4().hex[:16].upper()}",
                "executor_id": executor_id,
                "input_fingerprint": input_fingerprint,
                "status": "RUNNING",
                "attempt": int(previous.get("attempt") or 0) + 1 if same_input else 1,
                "resumed_from_execution_id": (
                    previous.get("execution_id") if same_input else None
                ),
                "checkpoints": dict(previous.get("checkpoints") or {}) if same_input else {},
                "started_at": _iso(now),
                "heartbeat_at": _iso(now),
                "lease_expires_at": _iso(now + timedelta(seconds=lease_seconds)),
                "completed_at": None,
                "last_error": None,
                "observed_elapsed_seconds": 0,
            }
            _write_json(path, payload)
            _write_json(execution_dir / "history" / f"{payload['execution_id']}.json", payload)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return cls(path, payload, lease_seconds=lease_seconds, owner_lock=owner_lock)

    def checkpoint(self, name: str, details: dict[str, Any] | None = None) -> None:
        with self._mutex:
            self._checkpoint(name, details)

    def _checkpoint(self, name: str, details: dict[str, Any] | None = None) -> None:
        self.assert_active()
        now = _now()
        self.payload["heartbeat_at"] = _iso(now)
        self.payload["lease_expires_at"] = _iso(
            now + timedelta(seconds=self.lease_seconds)
        )
        self.payload.setdefault("checkpoints", {})[str(name)] = {
            "status": "PASSED",
            "completed_at": _iso(now),
            **(details or {}),
        }
        if not self._persist_if_owner():
            self._reject_inactive()

    def complete(self, details: dict[str, Any] | None = None) -> None:
        with self._mutex:
            self._complete(details)

    def _complete(self, details: dict[str, Any] | None = None) -> None:
        self.assert_active()
        now = _now()
        self.payload.update(
            {
                "status": "PASSED",
                "heartbeat_at": _iso(now),
                "lease_expires_at": _iso(now),
                "completed_at": _iso(now),
                "result": details or {},
            }
        )
        try:
            if not self._persist_if_owner():
                self._reject_inactive()
        finally:
            self._finished = True
            self._release()

    def mark_interrupted(self) -> None:
        with self._mutex:
            self._mark_interrupted()

    def fail(self, error: str, details: dict[str, Any] | None = None) -> None:
        """Record a known runner failure without changing formal stage state."""
        with self._mutex:
            if self._finished:
                return
            now = _now()
            self.payload.update(
                status="FAILED",
                heartbeat_at=_iso(now),
                lease_expires_at=_iso(now),
                completed_at=_iso(now),
                last_error=str(error),
                failure_details=details or {},
            )
            self._finished = True
            self._cleanup_persist()

    def _mark_interrupted(self) -> None:
        if self._finished:
            return
        now = _now()
        self.payload.update(
            {
                "status": "INTERRUPTED",
                "heartbeat_at": _iso(now),
                "lease_expires_at": _iso(now),
                "completed_at": _iso(now),
                "last_error": "执行进程在正式完成前退出；可按同一输入指纹恢复。",
            }
        )
        self._finished = True
        self._cleanup_persist()
