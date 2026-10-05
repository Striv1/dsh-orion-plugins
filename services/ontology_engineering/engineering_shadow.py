"""Read-only host takeover preview; workflow remains the stage authority.

This adapter deliberately cannot enqueue, approve, resume or execute anything.
It accepts the existing finalized workflow directive, never plans stages itself.
"""

from typing import Any

from .preflight_operations import digest


def preview_task(
    directive: dict[str, Any], *, authorized_stages: frozenset[str] = frozenset(),
    paused: bool | None = None,
) -> dict[str, Any]:
    policy = directive.get("execution_policy") or {}
    stage = directive.get("current_stage")
    identity = {
        "project_id": directive.get("project_id"),
        "stage": stage,
        "revision": directive.get("revision"),
        "action": directive.get("action"),
    }
    decision = "OBSERVE"
    reason = "No supported deterministic execution directive."
    if paused is not False:
        decision = "WAIT_FOR_USER_CONTROL"
        reason = "Paused or durable pause state has not been established."
    elif not identity["project_id"] or stage not in authorized_stages:
        decision = "OUTSIDE_AUTHORIZED_SCOPE"
        reason = "This stage is outside the explicitly supplied build scope."
    elif directive.get("stage_status") == "BLOCKED_HUMAN":
        decision = "WAIT_FOR_APPROVAL"
        reason = "Workflow requires a decision; a task preview grants no approval."
    elif directive.get("action") == "RECOVER_PREFLIGHT_OPERATION":
        decision = "RECOVER_EXISTING_OPERATION"
        reason = "Read the original operation result before any new execution."
    elif policy.get("mode") == "AUTO_CONTINUE" and stage in {"S5", "S6"}:
        decision = "WOULD_RUN_MANAGED_STAGE"
        reason = "Eligible for shadow comparison only; no executor is invoked."
    elif policy.get("mode") == "REPAIR_THEN_RETRY":
        decision = "WAIT_FOR_REPAIR"
        reason = "A failed operation is not automatically retried."
    return {
        "schema_version": 1,
        "mode": "SHADOW",
        "identity": identity,
        "directive_sha256": digest(directive),
        "decision": decision,
        "reason": reason,
        "executes_actions": False,
        "grants_authorization": False,
    }
