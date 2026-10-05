from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_CONTRACT_PATH = Path(__file__).with_name("contracts") / "workflow-ui-actions.json"


@dataclass(frozen=True)
class WorkflowUiAction:
    name: str
    endpoint: str
    actor_field: str | None


def _require_non_empty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"工作流动作契约字段 {field} 必须是非空字符串。")
    return value.strip()


@lru_cache(maxsize=4)
def load_workflow_action_contract(
    path: str | Path = DEFAULT_CONTRACT_PATH,
) -> dict[str, WorkflowUiAction]:
    contract_path = Path(path).resolve()
    payload = json.loads(contract_path.read_text(encoding="utf-8"))
    if payload.get("schemaVersion") != 1:
        raise ValueError("工作流动作契约版本不受支持。")
    raw_actions = payload.get("actions")
    if not isinstance(raw_actions, dict) or not raw_actions:
        raise ValueError("工作流动作契约缺少 actions。")

    actions: dict[str, WorkflowUiAction] = {}
    for raw_name, raw_config in raw_actions.items():
        name = _require_non_empty_string(raw_name, "actions.<name>")
        if not isinstance(raw_config, dict):
            raise ValueError(f"工作流动作 {name} 的配置必须是对象。")
        endpoint = _require_non_empty_string(raw_config.get("endpoint"), f"{name}.endpoint")
        actor_field = raw_config.get("actorField")
        if actor_field is not None:
            actor_field = _require_non_empty_string(actor_field, f"{name}.actorField")
        actions[name] = WorkflowUiAction(
            name=name,
            endpoint=endpoint,
            actor_field=actor_field,
        )
    return actions


def allowed_workflow_tool_names(path: str | Path = DEFAULT_CONTRACT_PATH) -> frozenset[str]:
    return frozenset(load_workflow_action_contract(path))
