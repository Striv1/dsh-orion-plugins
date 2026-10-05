"""ORION 本体工程工作流内核。"""

from typing import TYPE_CHECKING, Any

from services.ontology_contracts.errors import WorkflowError, WorkflowGateError

if TYPE_CHECKING:
    from .workflow import OntologyWorkflowService

__all__ = ["OntologyWorkflowService", "WorkflowError", "WorkflowGateError"]


def __getattr__(name: str) -> Any:
    """Load the workflow only when a caller requests its public entry points."""
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import workflow

    value = getattr(workflow, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
