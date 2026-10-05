"""Shared contract failures; legacy workflow imports preserve these identities."""


class WorkflowError(RuntimeError):
    """工作流调用不满足当前状态或输入契约。"""

class WorkflowGateError(WorkflowError):
    """阶段自动门禁拒绝继续推进。"""

    def __init__(self, gate_id: str, message: str, *, path: str | None = None, reason_code: str | None = None) -> None:
        super().__init__(message)
        self.gate_id = gate_id
        self.path = path
        self.reason_code = reason_code


class CQBindingError(ValueError):
    def __init__(self, message: str, *, reason_code: str = "INVALID_CQ_CONTRACT", field: str | None = None) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.field = field
