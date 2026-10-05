"""Collect every CQ contract failure in one S6 pass.

S6 validates each competency question after a multi-minute materialization.
Raising on the first failed CQ forced one S3 rollback and one full S6 rerun per
failing question; collecting them lets a single correction address all.
Only answer-contract gates are collected; execution failures still stop at once.
"""
from __future__ import annotations

from services.ontology_contracts.errors import WorkflowGateError

COLLECTED_GATES = ("G-S6-CQ-SEMANTIC", "G-S6-CQ-ANSWER", "G-S6-CQ-BUSINESS-SEMANTIC")


class CqFailureCollector:
    def __init__(self) -> None:
        self.failures: list[tuple[str, WorkflowGateError]] = []

    def record(self, question_id: str, error: WorkflowGateError) -> None:
        if error.gate_id not in COLLECTED_GATES:
            raise error
        self.failures.append((question_id, error))

    @property
    def details(self) -> list[dict[str, str]]:
        return [{"question_id": qid, "status": "FAILED", "gate": error.gate_id,
                 "message": str(error)} for qid, error in self.failures]

    def raise_if_any(self) -> None:
        if not self.failures:
            return
        if len(self.failures) == 1:
            raise self.failures[0][1]
        gates = {error.gate_id for _, error in self.failures}
        gate = next(gate for gate in COLLECTED_GATES if gate in gates)
        details = "；".join(f"[{qid}] {error.gate_id}：{error}" for qid, error in self.failures)
        raise WorkflowGateError(
            gate,
            f"{len(self.failures)} 个能力问题未满足已审答案契约（本次已全部列出，"
            f"请一次核对来源证据并在同一次修订中处理）：{details}",
        )
