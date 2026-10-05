from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from harness.orion_workflow_mcp import DEFAULT_WORKFLOW_HOME, OrionWorkflowTools
from harness.workflow_action_contract import allowed_workflow_tool_names
from services.ontology_engineering import OntologyWorkflowService, WorkflowError
from services.realtime_qa.deployment_automation import (
    DeploymentAutomationConfig,
    S7DeploymentAutomation,
)

ALLOWED_TOOLS = allowed_workflow_tool_names()
# Host progress observation is read-only; it is not a new UI action or write grant.
HOST_READ_ONLY_TOOLS = frozenset({"get_ontology_workflow_status", "get_business_quality", "get_business_preview"})
HOST_AUXILIARY_TOOLS = frozenset({"start_business_preview"})



def tools_for_action(tool: str) -> OrionWorkflowTools:
    if tool not in HOST_READ_ONLY_TOOLS | HOST_AUXILIARY_TOOLS:
        return OrionWorkflowTools()
    root = Path(os.getenv("ORION_WORKFLOW_HOME", str(DEFAULT_WORKFLOW_HOME))).expanduser().resolve()
    if not root.is_dir():
        raise WorkflowError("只读状态入口要求已有工程目录。")
    # Explicit construction avoids recovering other projects. Reads need no
    # ledger connection; preview receipt writes must retain the configured
    # ledger so the existing project mutation guard can verify persistence.
    automation = S7DeploymentAutomation(DeploymentAutomationConfig.from_env(root))
    storage = {} if tool in HOST_AUXILIARY_TOOLS else {
        "metadata_database_url": "", "metadata_required": False,
    }
    service = OntologyWorkflowService(
        root, release_deployment_automation=automation, **storage,
    )
    return OrionWorkflowTools(service)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload-json", required=True)
    args = parser.parse_args()
    try:
        payload: dict[str, Any] = json.loads(args.payload_json)
        tool = str(payload.get("tool") or "")
        arguments = payload.get("arguments")
        if tool not in (ALLOWED_TOOLS | HOST_READ_ONLY_TOOLS) or not isinstance(arguments, dict):
            raise WorkflowError("工程页面不允许执行该工作流操作。")
        summary, result = tools_for_action(tool).call(tool, arguments)
        response = {"ok": True, "summary": summary, "result": result}
    except (WorkflowError, TypeError, ValueError, json.JSONDecodeError) as error:
        response = {"ok": False, "detail": str(error)}
    sys.stdout.write(json.dumps(response, ensure_ascii=False))


if __name__ == "__main__":
    main()
