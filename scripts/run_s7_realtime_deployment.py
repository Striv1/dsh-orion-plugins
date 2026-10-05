from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.realtime_qa.deployment_automation import (
    DeploymentAutomationConfig,
    S7DeploymentAutomation,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run or retry the durable post-S7 Ontop deployment job.",
    )
    parser.add_argument("--workflow-root", type=Path, required=True)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--release-version", required=True)
    arguments = parser.parse_args()
    workflow_root = arguments.workflow_root.expanduser().resolve()
    automation = S7DeploymentAutomation(
        DeploymentAutomationConfig.from_env(workflow_root)
    )
    result = automation.run_now(
        project_dir=workflow_root / arguments.project_id,
        release_version=arguments.release_version,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
