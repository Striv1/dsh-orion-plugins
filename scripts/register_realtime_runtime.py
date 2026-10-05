from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.realtime_qa.registration import register_runtime


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register one verified S7 realtime runtime in the ORION registry."
    )
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument(
        "--deployment-binding",
        type=Path,
        help="Required for structured Ontop runtimes; omit for document-only runtimes.",
    )
    parser.add_argument("--set-default", action="store_true")
    return parser.parse_args()


def main() -> None:
    arguments = parse_args()
    result = register_runtime(
        registry_path=arguments.registry,
        project_dir=arguments.project_dir,
        deployment_binding_path=arguments.deployment_binding,
        set_default=arguments.set_default,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
