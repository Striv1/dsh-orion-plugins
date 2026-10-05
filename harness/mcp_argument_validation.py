"""Top-level MCP argument validation against the published tool schemas.

Handlers are Python keyword functions; without this check a missing field
surfaces as a raw TypeError ("missing 1 required keyword-only argument"),
which models and the UI misread as an unrelated failure.
"""

from __future__ import annotations

from typing import Any

from services.ontology_contracts.errors import WorkflowError

_RUNTIME_REPAIR_HINTS = {
    "publish_ontology_package": (
        "已发布工程的运行时恢复请调用 retry_release_runtime_deployment(project_id)，"
        "或按 get_next_workflow_action 返回的 existing_release_replay.args 原样重放。"
    ),
}


def validate_tool_arguments(
    tool_name: str, arguments: dict[str, Any], schemas: dict[str, dict[str, Any]]
) -> None:
    schema = schemas.get(tool_name)
    if not isinstance(schema, dict):
        return
    if not isinstance(arguments, dict):
        raise WorkflowError(f"{tool_name} 的参数必须是 JSON 对象。")
    properties = schema.get("properties") or {}
    missing = [key for key in schema.get("required") or [] if arguments.get(key) is None]
    unknown = (
        sorted(key for key in arguments if key not in properties)
        if schema.get("additionalProperties") is False
        else []
    )
    if not missing and not unknown:
        return
    parts = []
    if missing:
        parts.append("缺少必填参数：" + "、".join(missing))
    if unknown:
        parts.append("不支持的参数：" + "、".join(unknown))
    hint = _RUNTIME_REPAIR_HINTS.get(tool_name)
    raise WorkflowError(f"{tool_name} 参数不完整（" + "；".join(parts) + "）。" + (hint or ""))


def describe_handler_type_error(tool_name: str, error: TypeError) -> WorkflowError:
    """Convert only binding errors raised at the call site, never TypeErrors from inside a handler."""
    message = str(error)
    traceback = error.__traceback__
    raised_at_call_site = traceback is not None and traceback.tb_next is None
    if "argument" not in message or not raised_at_call_site:
        raise error
    return WorkflowError(f"{tool_name} 参数与工具定义不一致：{message}")
