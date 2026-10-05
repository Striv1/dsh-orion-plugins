"""Opt-in durable coordination; workflow remains the authority for stage decisions.

This store never invokes work, approves stages, or retries external side effects.
Expired leases require explicit reconciliation before they can be claimed again.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager, nullcontext
from pathlib import Path


class TaskConflict(RuntimeError):
    """The caller's coordination authority is absent or stale."""


class EngineeringTasks:
    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "tasks.json"
        self.lock_path = self.directory / "tasks.lock"
        self._guard_local = threading.local()

    def authorize_project(
        self,
        project: str,
        *,
        allowed_stages: list[str],
        session_id: str,
        authorization_ref: str,
        actor: str,
        reason: str,
    ):
        """Internal trusted bridge only; never call from model-generated user text."""
        if not allowed_stages or not all(
            isinstance(v, str) and v.strip() for v in (session_id, authorization_ref, actor, reason)
        ):
            raise ValueError("Explicit project authorization required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session_id):
            raise ValueError("Unsafe session identity")
        for stage in allowed_stages:
            self.identity(project, stage, authorization_ref)
        with self._transaction() as state:
            controls = state.setdefault("controls", {})
            old = controls.get(project, {})
            audit = old.get("audit", []) + [
                {
                    "action": "authorize",
                    "actor": actor,
                    "reason": reason,
                    "time": time.time(),
                    "old_epoch": old.get("epoch", 0),
                    "new_epoch": old.get("epoch", 0) + 1,
                }
            ]
            controls[project] = {
                "enabled": True,
                "epoch": old.get("epoch", 0) + 1,
                "allowed_stages": sorted(set(allowed_stages)),
                "session_id": session_id,
                "authorization_ref": authorization_ref,
                "authorized_at": time.time(),
                "audit": audit,
            }
            return dict(controls[project])

    def project_status(self, project: str):
        with self._transaction(write=False) as state:
            control = state.get("controls", {}).get(project)
            return dict(control) if control else None

    def pause_project(
        self,
        project: str,
        *,
        session_id: str,
        actor: str,
        reason: str,
        event_time: float | None = None,
    ):
        if not all(isinstance(v, str) and v.strip() for v in (actor, reason)):
            raise ValueError("Actor and reason required")
        with self._transaction() as state:
            control = state.get("controls", {}).get(project)
            if not control or control["session_id"] != session_id:
                raise TaskConflict("Project session binding differs")
            if event_time is not None and event_time < control["authorized_at"]:
                raise TaskConflict("Cancel event predates current authorization")
            control["audit"].append(
                {
                    "action": "pause",
                    "actor": actor,
                    "reason": reason,
                    "time": time.time(),
                    "old_epoch": control["epoch"],
                    "new_epoch": control["epoch"] + 1,
                }
            )
            control.update(enabled=False, epoch=control["epoch"] + 1)

    def _check_native_stop(self, control):
        session = control["session_id"]
        if not isinstance(session, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", session
        ):
            raise TaskConflict("Unsafe persisted session identity")
        directory = self.directory.parent / "cancellations"
        legacy = directory / f"{session}.json"
        events = directory / session
        try:
            if events.exists() and (not events.is_dir() or events.is_symlink()):
                raise ValueError("Invalid cancellation event directory")
            paths = ([legacy] if legacy.exists() else []) + list(events.glob("*.json"))
            latest = None
            for path in paths:
                if path.is_symlink():
                    raise ValueError("Invalid cancellation event link")
                event = json.loads(path.read_text())
                timestamp = event["event_time_ms"]
                if (
                    event["session_id"] != session
                    or event["source"] != "HARNESS_NATIVE_USER_STOP"
                    or isinstance(timestamp, bool)
                    or not isinstance(timestamp, int | float)
                    or not 0 < timestamp < float("inf")
                ):
                    raise ValueError("Invalid cancellation journal")
                latest = timestamp if latest is None else max(latest, timestamp)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise TaskConflict("Cancellation journal unreadable; execution refused") from error
        if latest is not None and latest / 1000 >= control["authorized_at"]:
            raise TaskConflict("Native user stop is pending project reconciliation")

    def _project_authority(self, task, state, *, claiming=False):
        control = state.get("controls", {}).get(task["project"])
        if control is None:
            return  # Low-level coordination remains usable without managed opt-in.
        self._check_native_stop(control)
        if not control["enabled"] or task["stage"] not in control["allowed_stages"]:
            raise TaskConflict("Project is paused or stage is outside its authorization")
        if not claiming and task.get("project_epoch") != control["epoch"]:
            raise TaskConflict("Project authorization epoch changed")

    @contextmanager
    def project_guard(self, project: str, stage: str, token: dict | None = None):
        """Workflow bridge: task lock precedes workflow locks; no auto approval."""

        def validate(state):
            resolved_stage = stage() if callable(stage) else stage
            control = state.get("controls", {}).get(project)
            if control is None:
                return
            self._check_native_stop(control)
            if not control["enabled"] or resolved_stage not in control["allowed_stages"]:
                raise TaskConflict("Project is paused or stage is outside its authorization")
            running = [
                task
                for task in state["tasks"].values()
                if task["project"] == project and task["status"] == "RUNNING"
            ]
            if any(
                task["project"] == project and task["status"] == "NEEDS_RECONCILIATION"
                for task in state["tasks"].values()
            ):
                raise TaskConflict("Project has unresolved execution effects")
            if running and (token is None or len(running) != 1):
                raise TaskConflict("Managed running task requires its owner token")
            if token is not None:
                task = state["tasks"].get(token.get("task_id"))
                if task is None or task["project"] != project or task["stage"] != resolved_stage:
                    raise TaskConflict("Token scope differs")
                self._authorize(task, token, state)

        nested = getattr(self._guard_local, "state", None)
        if nested is not None:
            validate(nested)
            yield
            return
        with self._transaction(write=False) as state:
            validate(state)
            self._guard_local.state = state
            try:
                yield
            finally:
                del self._guard_local.state

    @staticmethod
    def identity(project: str, stage: str, input_fingerprint: str) -> str:
        if not all(isinstance(v, str) and v for v in (project, stage, input_fingerprint)):
            raise ValueError("Project, stage and input fingerprint must be nonempty strings")
        if stage not in {f"S{i}" for i in range(8)}:
            raise ValueError("Stage must be S0 through S7")
        encoded = json.dumps([project, stage, input_fingerprint], separators=(",", ":"))
        return hashlib.sha256(encoded.encode()).hexdigest()

    @contextmanager
    def _transaction(self, *, write=True, blocking=True):
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            state = (
                json.loads(self.path.read_text())
                if self.path.exists()
                else {"schema": 1, "tasks": {}}
            )
            if state.get("schema") != 1 or not isinstance(state.get("tasks"), dict):
                raise ValueError("Unsupported task store")
            for task_id, task in state["tasks"].items():
                if not isinstance(task, dict) or task.get("task_id") != task_id:
                    raise ValueError("Invalid persisted task identity")
                expected = self.identity(
                    task.get("project"), task.get("stage"), task.get("input_fingerprint")
                )
                if expected != task_id:
                    raise ValueError("Persisted task identity does not match its inputs")
            yield state
            if not write:
                return
            descriptor, name = tempfile.mkstemp(prefix=".tasks-", dir=self.directory)
            try:
                with os.fdopen(descriptor, "w") as output:
                    json.dump(state, output, ensure_ascii=False, sort_keys=True)
                    output.flush()
                    os.fsync(output.fileno())
                os.replace(name, self.path)
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            finally:
                Path(name).unlink(missing_ok=True)

    def ensure(self, project: str, stage: str, input_fingerprint: str) -> dict:
        task_id = self.identity(project, stage, input_fingerprint)
        with self._transaction() as state:
            task = state["tasks"].setdefault(
                task_id,
                {
                    "task_id": task_id,
                    "project": project,
                    "stage": stage,
                    "input_fingerprint": input_fingerprint,
                    "status": "READY",
                    "generation": 0,
                    "stop_epoch": 0,
                    "owner": None,
                    "lease_until": 0,
                    "audit": [],
                },
            )
            return dict(task)

    def read(self, task_id: str) -> dict:
        with self._transaction(write=False) as state:
            return dict(state["tasks"][task_id])

    def check(self, token: dict):
        """Check a callback token; external effect adapters must also enforce fencing.

        This check alone cannot atomically fence a later remote side effect.
        """
        with self._transaction(write=False) as state:
            self._authorize(state["tasks"][token["task_id"]], token, state)

    @contextmanager
    def guard(self, token: dict):
        """Linearize local formal commit against pause while holding task lock.

        Lock order is task store -> StageExecutionLease/workflow locks. Never
        call task APIs while holding a workflow lock, or reenter task APIs here.
        The supplied recorder commits its receipt with this transaction. A crash
        after an external commit but before this transaction commits still needs
        receipt reconciliation; remote effects are not atomically fenced here.
        """
        with self._transaction() as state:
            task = state["tasks"][token["task_id"]]
            self._authorize(task, token, state)

            def recorded(receipt_reference):
                if not isinstance(receipt_reference, str) or not receipt_reference:
                    raise ValueError("Receipt reference required")
                task.update(
                    status="RECEIPT_RECORDED",
                    receipt_reference=receipt_reference,
                    owner=None,
                    lease_until=0,
                )

            self._guard_local.state = state
            try:
                yield recorded
            finally:
                del self._guard_local.state

    def bind_execution(
        self,
        token: dict,
        *,
        authorization_ref: str,
        stage_execution_id: str,
        receipt_reference: str,
    ):
        """Persist intent before invoking effects; references are not approvals."""
        if not all(
            isinstance(v, str) and v
            for v in (authorization_ref, stage_execution_id, receipt_reference)
        ):
            raise ValueError("Explicit authorization, lease and receipt references required")
        with self._transaction() as state:
            task = state["tasks"][token["task_id"]]
            self._authorize(task, token, state)
            task.update(
                authorization_ref=authorization_ref,
                stage_execution_id=stage_execution_id,
                intended_receipt_reference=receipt_reference,
            )

    def uncertain(self, token: dict, *, reason: str):
        """Quarantine failure without overriding a newer stop epoch or owner."""
        with self._transaction() as state:
            task = state["tasks"][token["task_id"]]
            if self._token(task) != token or task["status"] != "RUNNING":
                return
            self._audit(
                task,
                action="uncertain",
                actor=token["owner"],
                reason=reason,
                new_status="NEEDS_RECONCILIATION",
            )
            task.update(
                status="NEEDS_RECONCILIATION",
                stop_epoch=task["stop_epoch"] + 1,
                owner=None,
                lease_until=0,
            )

    def recover_receipt(self, task_id: str, *, receipt_reference: str, actor: str, reason: str):
        """Caller must verify authoritative receipt identity before invoking this."""
        with self._transaction() as state:
            task = state["tasks"][task_id]
            expired = task["status"] == "RUNNING" and task["lease_until"] <= time.time()
            if not expired and task["status"] not in {"NEEDS_RECONCILIATION", "PAUSED"}:
                raise TaskConflict("Task is not recoverable")
            if receipt_reference != task.get("intended_receipt_reference"):
                raise TaskConflict("Receipt differs from durable execution intent")
            self._audit(
                task,
                action="recover_receipt",
                actor=actor,
                reason=reason,
                new_status="RECEIPT_RECORDED",
                evidence_reference=receipt_reference,
            )
            task.update(
                status="RECEIPT_RECORDED",
                receipt_reference=receipt_reference,
                owner=None,
                lease_until=0,
                stop_epoch=task["stop_epoch"] + 1,
            )

    @contextmanager
    def recovery_guard(self, task_id: str):
        """Trusted receipt replay only; never grants permission to execute work."""
        nested = getattr(self._guard_local, "state", None)
        with nullcontext(nested) if nested is not None else self._transaction() as state:
            task = state["tasks"][task_id]
            expired = task["status"] == "RUNNING" and task["lease_until"] <= time.time()
            if not expired and task["status"] not in {
                "NEEDS_RECONCILIATION",
                "PAUSED",
                "RECEIPT_RECORDED",
            }:
                raise TaskConflict("Task is not awaiting authoritative receipt recovery")

            def recorded(reference, *, actor, reason):
                if reference != task.get("intended_receipt_reference"):
                    raise TaskConflict("Recovery receipt differs from execution intent")
                if task["status"] == "RECEIPT_RECORDED":
                    return
                self._audit(
                    task,
                    action="recover_receipt",
                    actor=actor,
                    reason=reason,
                    new_status="RECEIPT_RECORDED",
                    evidence_reference=reference,
                )
                task.update(
                    status="RECEIPT_RECORDED",
                    receipt_reference=reference,
                    owner=None,
                    lease_until=0,
                    stop_epoch=task["stop_epoch"] + 1,
                )

            if nested is None:
                self._guard_local.state = state
            try:
                yield recorded
            finally:
                if nested is None:
                    del self._guard_local.state

    def claim(
        self,
        task_id: str,
        owner: str,
        lease_seconds: float = 60,
        *,
        session_id: str | None = None,
        authorization_ref: str | None = None,
    ) -> dict:
        if not isinstance(owner, str) or not owner:
            raise ValueError("Owner must be nonempty")
        if not 0 < lease_seconds <= 3600:
            raise ValueError("Lease must be positive and at most 3600 seconds")
        result = None
        with self._transaction() as state:
            task = state["tasks"][task_id]
            if session_id is not None or authorization_ref is not None:
                control = state.get("controls", {}).get(task["project"])
                if (
                    not control
                    or control["session_id"] != session_id
                    or control["authorization_ref"] != authorization_ref
                ):
                    raise TaskConflict("Explicit managed authorization binding differs")
            if task["status"] == "RUNNING" and task["lease_until"] <= time.time():
                task["status"] = "NEEDS_RECONCILIATION"
            self._project_authority(task, state, claiming=True)
            if any(
                other["task_id"] != task_id
                and other["project"] == task["project"]
                and other["stage"] == task["stage"]
                and other["status"] in {"RUNNING", "NEEDS_RECONCILIATION", "PAUSED"}
                for other in state["tasks"].values()
            ):
                raise TaskConflict("Another input revision has unresolved work in this stage")
            if task["status"] == "READY":
                task.update(
                    status="RUNNING",
                    owner=owner,
                    generation=task["generation"] + 1,
                    lease_until=time.time() + lease_seconds,
                )
                control = state.get("controls", {}).get(task["project"])
                if control is not None:
                    task["project_epoch"] = control["epoch"]
                result = self._token(task)
        if result is None:
            raise TaskConflict("Task is not claimable; expired work requires reconciliation")
        return result

    @staticmethod
    def _token(task: dict) -> dict:
        return {key: task[key] for key in ("task_id", "owner", "generation", "stop_epoch")}

    def _authorize(self, task: dict, token: dict, state: dict):
        self._project_authority(task, state)
        if (
            task["status"] != "RUNNING"
            or self._token(task) != token
            or task["lease_until"] <= time.time()
        ):
            raise TaskConflict("Stale or expired owner token")

    def heartbeat(self, token: dict, lease_seconds: float = 60):
        if not 0 < lease_seconds <= 3600:
            raise ValueError("Invalid lease duration")
        with self._transaction() as state:
            task = state["tasks"][token["task_id"]]
            self._authorize(task, token, state)
            task["lease_until"] = time.time() + lease_seconds

    def record(self, token: dict, receipt_reference: str):
        """Record an execution receipt, not a workflow stage approval or success."""
        if not isinstance(receipt_reference, str) or not receipt_reference:
            raise ValueError("Receipt reference required")
        with self._transaction() as state:
            task = state["tasks"][token["task_id"]]
            self._authorize(task, token, state)
            task.update(
                status="RECEIPT_RECORDED",
                receipt_reference=receipt_reference,
                owner=None,
                lease_until=0,
            )

    @staticmethod
    def _audit(task, *, action, actor, reason, new_status, evidence_reference=None):
        if not all(isinstance(value, str) and value.strip() for value in (actor, reason)):
            raise ValueError("Actor and reason must be nonempty strings")
        task.setdefault("audit", []).append(
            {
                "action": action,
                "actor": actor,
                "reason": reason,
                "time": time.time(),
                "old_status": task["status"],
                "new_status": new_status,
                "old_stop_epoch": task["stop_epoch"],
                "new_stop_epoch": task["stop_epoch"] + 1,
                "generation": task["generation"],
                "evidence_reference": evidence_reference,
            }
        )

    def pause(self, task_id: str, *, actor: str, reason: str):
        with self._transaction() as state:
            task = state["tasks"][task_id]
            self._audit(task, action="pause", actor=actor, reason=reason, new_status="PAUSED")
            task.update(
                status="PAUSED", stop_epoch=task["stop_epoch"] + 1, owner=None, lease_until=0
            )

    def reconcile(
        self, task_id: str, *, evidence_reference: str, retry_safe: bool, actor: str, reason: str
    ):
        """Explicit caller attestation after consulting authoritative workflow/effects.

        A false retry_safe retains the reconciliation boundary. No automatic retry
        or workflow approval is inferred from either this call or the evidence.
        This internal API trusts the caller's attestation; it does not fetch or
        independently validate the referenced evidence. It is not a public endpoint.
        """
        if (
            not isinstance(evidence_reference, str)
            or not evidence_reference
            or not isinstance(retry_safe, bool)
        ):
            raise ValueError("Explicit reconciliation evidence and boolean decision required")
        with self._transaction() as state:
            task = state["tasks"][task_id]
            if task["status"] not in {"PAUSED", "NEEDS_RECONCILIATION", "RECEIPT_RECORDED"}:
                raise TaskConflict("Task is not awaiting reconciliation")
            self._audit(
                task,
                action="reconcile",
                actor=actor,
                reason=reason,
                new_status="READY" if retry_safe else "NEEDS_RECONCILIATION",
                evidence_reference=evidence_reference,
            )
            task.update(
                status="READY" if retry_safe else "NEEDS_RECONCILIATION",
                reconciliation_reference=evidence_reference,
                owner=None,
                lease_until=0,
                stop_epoch=task["stop_epoch"] + 1,
            )
