"""Read-only liveness classification for RUNNING workflow stages.

`stage_statuses[stage] == "RUNNING"` only says the stage is open. It does not
prove that a runner or agent is still working on it: a crashed process, a
closed chat or a host restart leaves the same value behind. This module turns
the durable evidence the platform already writes (stage execution leases, the
kernel owner lock, S0 document jobs and `updated_at`) into one of:

- `NOT_RUNNING`: the current stage is not RUNNING.
- `RUNNING_ACTIVE`: a live lease, owner lock or document job proves progress.
- `RUNNING_IDLE`: open and recently touched, but no runner is attached
  (usually waiting for the engineering agent's next tool call).
- `SUSPECTED_INTERRUPTED`: the last runner was interrupted, or nothing has
  touched the stage for longer than the idle threshold.

It never writes project state. Recovery still goes through the existing
status, rollback preview and reopen tools.
"""

from __future__ import annotations

import fcntl
import json
import stat
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
DEFAULT_IDLE_THRESHOLD = timedelta(hours=6)
ACTIVE_JOB_STATES = {"QUEUED", "STARTING", "RUNNING"}


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def _read(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def owner_lock_held(lock_path: Path) -> bool | None:
    """Probe the owner lock; None means the filesystem could not be inspected."""

    try:
        file_stat = lock_path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return None
    if not stat.S_ISREG(file_stat.st_mode):
        return None
    try:
        handle = lock_path.open("r+")
    except FileNotFoundError:
        return False
    except OSError:
        return None
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError:
            return None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False


def _document_job_active(project_dir: Path, state: dict[str, Any], now: datetime,
                         threshold: timedelta) -> bool:
    reference = _read(project_dir / ".s0-document-job.json")
    job_id = str(reference.get("job_id") or "")
    if not job_id or "/" in job_id or job_id.startswith("."):
        return False
    # Lazy import: document_jobs imports this package at module load.
    from services.ingestion.document_jobs import document_input_root

    runner = _read(document_input_root() / ".orion-s0-jobs" / job_id / "runner.json")
    if not runner:
        return False
    heartbeat = _parse(runner.get("heartbeat_at"))
    return str(runner.get("state") or "").upper() in ACTIVE_JOB_STATES and (
        heartbeat is None or now - heartbeat < threshold
    )


def classify_stage_liveness(
    project_dir: Path,
    state: dict[str, Any],
    *,
    now: datetime | None = None,
    idle_threshold: timedelta = DEFAULT_IDLE_THRESHOLD,
    probe_lock: bool = True,
) -> dict[str, Any]:
    now = now or _now()
    stage = str(state.get("current_stage") or "").upper()
    status = str((state.get("stage_statuses") or {}).get(stage) or "").upper()
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage or None,
        "state": "NOT_RUNNING",
        "evidence": None,
        "idle_seconds": None,
        "idle_threshold_seconds": int(idle_threshold.total_seconds()),
    }
    if not stage or status != "RUNNING":
        return result
    if str(state.get("project_status") or "").upper() == "ARCHIVED":
        # Archived projects are intentionally parked; never nag about them.
        return {**result, "evidence": "ARCHIVED"}

    execution_dir = project_dir / ".stage-executions"
    execution = _read(execution_dir / f"{stage}.json")
    execution_status = str(execution.get("status") or "").upper()
    expiry = _parse(execution.get("lease_expires_at"))
    touched = [
        moment
        for moment in (
            _parse(state.get("updated_at")),
            _parse(execution.get("heartbeat_at")),
        )
        if moment is not None
    ]
    last_touch = max(touched) if touched else None
    if last_touch is not None:
        result["idle_seconds"] = max(0, int((now - last_touch).total_seconds()))
        result["last_activity_at"] = last_touch.isoformat(timespec="seconds")

    owner_held = owner_lock_held(execution_dir / f"{stage}.owner.lock") if probe_lock else None
    if owner_held is True:
        return {**result, "state": "RUNNING_ACTIVE", "evidence": "OWNER_LOCK_HELD"}
    if (owner_held is False and execution_status == "RUNNING"
            and execution.get("kernel_lock_version") == 1 and execution.get("execution_id")):
        # Modern runners hold this lock for their entire execution. A crash
        # releases it immediately even though the durable lease has 30 minutes
        # remaining. A concurrent handoff can race this read, so only suspect.
        return {**result, "state": "SUSPECTED_INTERRUPTED",
                "evidence": "OWNER_LOCK_RELEASED",
                "execution_id": execution["execution_id"]}
    if execution_status == "RUNNING" and expiry is not None and expiry > now:
        return {**result, "state": "RUNNING_ACTIVE", "evidence": "LEASE_UNEXPIRED"}
    if stage == "S0" and _document_job_active(project_dir, state, now, idle_threshold):
        return {**result, "state": "RUNNING_ACTIVE", "evidence": "DOCUMENT_JOB_ACTIVE"}
    if execution_status in {"INTERRUPTED", "RUNNING"} and execution.get("execution_id"):
        # A RUNNING lease whose expiry passed without a held owner lock means the
        # runner process is gone; INTERRUPTED is the runner's own exit record.
        return {**result, "state": "SUSPECTED_INTERRUPTED",
                "evidence": f"EXECUTION_{execution_status}",
                "execution_id": execution.get("execution_id")}
    if last_touch is None or now - last_touch >= idle_threshold:
        return {**result, "state": "SUSPECTED_INTERRUPTED", "evidence": "NO_ACTIVITY"}
    return {**result, "state": "RUNNING_IDLE", "evidence": "RECENT_ACTIVITY"}
