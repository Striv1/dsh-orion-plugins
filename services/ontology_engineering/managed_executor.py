"""Explicitly opted-in runner adapter; no production registration or auto approval.

Use the same EngineeringTasks instance for adapter and workflow project_guard so
nested guards reuse the task lock. Lock order: task -> workflow/stage lease.
"""

from __future__ import annotations

import threading

from .engineering_tasks import TaskConflict


class ManagedExecutor:
    def __init__(
        self,
        tasks,
        *,
        project,
        stage,
        input_fingerprint,
        session_id,
        authorization_ref,
        owner,
        stage_lease,
        receipt_reference,
        lease_seconds=60,
    ):
        self.tasks = tasks
        self.project = project
        self.stage = stage
        self.session_id = session_id
        self.authorization_ref = authorization_ref
        self.owner = owner
        self.stage_lease = stage_lease
        self.receipt_reference = receipt_reference
        self.lease_seconds = lease_seconds
        self.task = tasks.ensure(project, stage, input_fingerprint)
        self.token = None
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = None
        self._committed = False

    def __enter__(self):
        control = self.tasks.project_status(self.project)
        if not control:
            raise TaskConflict("Managed executor requires explicit project opt-in")
        self.token = self.tasks.claim(
            self.task["task_id"],
            self.owner,
            self.lease_seconds,
            session_id=self.session_id,
            authorization_ref=self.authorization_ref,
        )
        try:
            self.stage_lease.assert_active()
            self.tasks.bind_execution(
                self.token,
                authorization_ref=self.authorization_ref,
                stage_execution_id=self.stage_lease.payload["execution_id"],
                receipt_reference=self.receipt_reference,
            )
        except BaseException:
            self.tasks.uncertain(self.token, reason="Stage lease or intent binding failed")
            raise

        def renew():
            while not self._stop.wait(max(0.01, self.lease_seconds / 3)):
                try:
                    self.tasks.heartbeat(self.token, self.lease_seconds)
                except Exception:
                    self._lost.set()
                    return

        self._thread = threading.Thread(
            target=renew, daemon=True, name="engineering-task-heartbeat"
        )
        self._thread.start()
        return self

    def effect(self, callback, *, known_rejections=()):
        """Remote effect is not atomic with pause; failures require reconciliation."""
        if self._lost.is_set():
            raise TaskConflict("Task heartbeat lost")
        self.tasks.check(self.token)
        self.stage_lease.assert_active()
        try:
            return callback()
        except BaseException as error:
            # Only trusted adapters may classify a definitive pre-effect rejection.
            # Transport errors and ambiguous/partial outcomes still quarantine.
            if known_rejections and isinstance(error, known_rejections):
                raise
            self.tasks.uncertain(
                self.token, reason="External effect failed; outcome requires reconciliation"
            )
            raise

    def commit(self, callback):
        """Callback writes formal receipt at intended reference, then records locally.

        A crash between these steps is recovered with reconcile_receipt, never by
        automatically repeating the callback. Workflow remains the approval gate.
        """
        with self.tasks.guard(self.token) as recorded:
            self.stage_lease.assert_active()
            result = callback()
            recorded(self.receipt_reference)
        self._committed = True
        return result

    def __exit__(self, exc_type, exc_value, traceback):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if not self._committed:
            self.tasks.uncertain(
                self.token, reason="Runner exited without committed receipt; reconcile effects"
            )
        return False

    @staticmethod
    def reconcile_receipt(tasks, task_id, *, verify_receipt, actor, reason):
        """Verifier must check project/stage/input/lease and authoritative success.

        This callback is supplied by a trusted workflow adapter, not model text.
        A false/unknown result retains the boundary and never reruns the effect.
        """
        task = tasks.read(task_id)
        if verify_receipt(task) is not True:
            raise TaskConflict("Authoritative receipt was not verified")
        tasks.recover_receipt(
            task_id,
            receipt_reference=task["intended_receipt_reference"],
            actor=actor,
            reason=reason,
        )
