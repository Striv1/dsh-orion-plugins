"""Opt-in runner bridge; authorization originates only from persisted project control."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from services.ontology_engineering import preflight_operations
from services.ontology_engineering.engineering_tasks import EngineeringTasks, TaskConflict
from services.ontology_engineering.managed_executor import ManagedExecutor


def opted_in_tasks(workflow_home, project):
    directory = Path(workflow_home) / ".engineering-control" / project
    if not (directory / "tasks.json").exists():
        return None
    tasks = EngineeringTasks(directory)
    return tasks if tasks.project_status(project) is not None else None


def start_managed(tasks, *, project, stage, fingerprint, lease, project_dir):
    if tasks is None:
        return None
    control = tasks.project_status(project)
    receipt = (
        Path(project_dir)
        / ".stage-executions"
        / "managed-receipts"
        / f"{lease.payload['execution_id']}.json"
    )
    runner = ManagedExecutor(
        tasks,
        project=project,
        stage=stage,
        input_fingerprint=fingerprint,
        session_id=control["session_id"],
        authorization_ref=control["authorization_ref"],
        owner=f"orion-platform:{os.getpid()}",
        stage_lease=lease,
        receipt_reference=str(receipt),
    )
    runner.__enter__()
    return runner


def _atomic_receipt(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)


def commit_managed(runner, callback, *, preflight=None, expected_revision=None):
    if runner is None:
        return callback()
    token = (preflight or {}).get("preflight_token", "")
    operation_id = token.split(".", 1)[0]
    if (
        not re.fullmatch(r"PFL-[A-F0-9]{16}", operation_id)
        or "." not in token
        or not isinstance(expected_revision, int)
    ):
        raise ValueError("Managed commit requires the original formal preflight token and revision")
    binding = {
        key: runner.task[key] for key in ("task_id", "project", "stage", "input_fingerprint")
    }
    binding.update(
        execution_id=runner.stage_lease.payload["execution_id"],
        authorization_ref=runner.authorization_ref,
        status="COMMIT_INTENT",
        operation_id=operation_id,
        preflight_token=token,
        expected_revision=expected_revision,
    )
    _atomic_receipt(runner.receipt_reference, binding)

    def commit():
        result = callback()
        _atomic_receipt(
            runner.receipt_reference,
            dict(binding, status="FORMAL_COMMIT_RETURNED", formal_result=result),
        )
        return result

    return runner.commit(commit)


def guarded_client(runner, client, *, known_rejections=()):
    """Fence each subsequent client call; an already in-flight call can finish."""
    if runner is None:
        return client

    class ClientProxy:
        def __getattr__(self, name):
            value = getattr(client, name)
            if not callable(value) or name in {"close", "stop", "disconnect"}:
                return value
            return lambda *args, **kwargs: runner.effect(
                lambda: value(*args, **kwargs), known_rejections=known_rejections
            )

    return ClientProxy()


def recover_managed_commit(service, tasks, task_id, *, actor, reason):
    """Verify durable platform receipts and replay bookkeeping, never stage effects.

    Missing/partial evidence fails closed. CHECKPOINTED requires an exact full
    checkpoint; COMMITTED permits later stages only while this stage's artifacts
    remain identical to its recorded checkpoint. Tokens never leave this function.
    """
    task = tasks.read(task_id)
    project_dir = service.root / task["project"]
    path = Path(task.get("intended_receipt_reference", ""))
    expected = (
        project_dir
        / ".stage-executions"
        / "managed-receipts"
        / f"{task.get('stage_execution_id')}.json"
    )
    if path.resolve() != expected.resolve() or path.is_symlink() or not path.is_file():
        raise TaskConflict("Managed execution receipt is missing or outside its scope")
    sidecar = json.loads(path.read_text())
    operation_id = sidecar.get("operation_id", "")
    if not re.fullmatch(r"PFL-[A-F0-9]{16}", operation_id):
        raise TaskConflict("Formal operation identity is missing")
    # Context is established before the task lock, but performs no writes.
    with (
        service.managed_recovery(tasks, task_id, operation_id),
        tasks.recovery_guard(task_id) as recorded,
    ):
        # Re-read under the task lock; identity changes cannot race this recovery.
        sidecar = json.loads(path.read_text())
        for key in ("task_id", "project", "stage", "input_fingerprint", "authorization_ref"):
            if sidecar.get(key) != task.get(key):
                raise TaskConflict("Managed receipt does not match the task binding")
        if (
            sidecar.get("execution_id") != task.get("stage_execution_id")
            or sidecar.get("operation_id") != operation_id
        ):
            raise TaskConflict("Managed execution identity differs")
        lease_path = (
            project_dir / ".stage-executions" / "history" / f"{task['stage_execution_id']}.json"
        )
        if lease_path.is_symlink():
            raise TaskConflict("Execution history is not a regular receipt")
        lease = json.loads(lease_path.read_text())
        if any(
            lease.get(key) != value
            for key, value in {
                "execution_id": task["stage_execution_id"],
                "project_id": task["project"],
                "stage": task["stage"],
                "input_fingerprint": task["input_fingerprint"],
            }.items()
        ):
            raise TaskConflict("Stage execution history differs from the managed task")
        operation_path = project_dir / preflight_operations.DIRECTORY / f"{operation_id}.json"
        receipt_path = project_dir / ".preflight-submissions" / f"{operation_id}.json"
        if operation_path.is_symlink() or receipt_path.is_symlink():
            raise TaskConflict("Formal recovery evidence must not be symlinks")
        operation = preflight_operations.read(operation_path)
        receipt = json.loads(receipt_path.read_text())
        if not isinstance(receipt, dict) or not isinstance(receipt.get("payload"), dict):
            raise TaskConflict("Formal preflight payload is absent")
        if (
            not operation
            or operation.get("status") not in {"COMMITTED", "CHECKPOINTED"}
            or operation.get("failure")
        ):
            raise TaskConflict("Formal commit has no successful recoverable checkpoint")
        token = sidecar.get("preflight_token", "")
        if not token.startswith(operation_id + ".") or hashlib.sha256(
            token.encode()
        ).hexdigest() != receipt.get("token_sha256"):
            raise TaskConflict("Original formal token cannot be authenticated")
        for evidence in (operation, receipt):
            if (
                evidence.get("project_id") != task["project"]
                or evidence.get("stage") != task["stage"]
            ):
                raise TaskConflict("Formal evidence scope differs")
        if operation.get("operation_id") != operation_id or receipt.get(
            "project_revision"
        ) != sidecar.get("expected_revision"):
            raise TaskConflict("Formal operation or original revision differs")
        payload_hash = preflight_operations.digest(receipt.get("payload"))
        if receipt.get("payload_sha256") != payload_hash or operation.get(
            "request_hash"
        ) != preflight_operations.digest(
            {
                "project_id": task["project"],
                "stage": task["stage"],
                "revision": sidecar["expected_revision"],
                "payload": payload_hash,
            }
        ):
            raise TaskConflict("Formal preflight payload integrity differs")
        checkpoint = service._preflight_checkpoint(project_dir, task["stage"])
        saved = operation.get("checkpoint")
        if not isinstance(saved, dict) or not isinstance(saved.get("stage_artifacts"), str):
            raise TaskConflict("Formal commit checkpoint is absent")
        if operation["status"] == "CHECKPOINTED" and saved != checkpoint:
            raise TaskConflict("Formal checkpoint drifted; execution must not repeat")
        if saved["stage_artifacts"] != checkpoint["stage_artifacts"]:
            raise TaskConflict("Committed stage artifacts changed")
        if operation["status"] == "COMMITTED":
            result = operation.get("result")
            if (
                not isinstance(result, dict)
                or result.get("operation_id") != operation_id
                or result.get("project_id") != task["project"]
                or result.get("preflight_stage") != task["stage"]
                or result.get("preflight_payload_sha256") != payload_hash
            ):
                raise TaskConflict("Committed result identity is absent")
        else:
            result = service.commit_preflight_stage_submission(
                project_id=task["project"],
                stage=task["stage"],
                preflight_token=token,
                expected_revision=sidecar["expected_revision"],
            )
        if (
            sidecar.get("status") == "FORMAL_COMMIT_RETURNED"
            and sidecar.get("formal_result") != result
        ):
            raise TaskConflict("Managed sidecar and formal committed result differ")
        _atomic_receipt(path, dict(sidecar, status="FORMAL_COMMIT_RETURNED", formal_result=result))
        recorded(str(path), actor=actor, reason=reason)
        return {
            "task_id": task_id,
            "operation_id": operation_id,
            "status": "RECEIPT_RECORDED",
            "reexecuted": False,
        }
