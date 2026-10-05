from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.realtime_qa.binding import ReleaseBindingLoader
from services.realtime_qa.deployment import render_ontop_release_deployment


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a package-bound, read-only Ontop deployment",
    )
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--properties", type=Path, required=True)
    parser.add_argument("--read-only-attestation", type=Path, required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument(
        "--image",
        default="ontology-workorder-agent/ontop:5.3.0",
    )
    parser.add_argument("--network-name")
    arguments = parser.parse_args()
    binding = ReleaseBindingLoader().load(arguments.project_dir.resolve())
    deployment = render_ontop_release_deployment(
        binding,
        output_dir=arguments.output_dir,
        properties_path=arguments.properties,
        read_only_attestation_path=arguments.read_only_attestation,
        endpoint=arguments.endpoint,
        image=arguments.image,
        network_name=arguments.network_name,
    )
    print(json.dumps(deployment, ensure_ascii=False))


if __name__ == "__main__":
    main()
