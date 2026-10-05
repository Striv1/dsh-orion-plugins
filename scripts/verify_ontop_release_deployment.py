from __future__ import annotations

import argparse
import json
from pathlib import Path

from services.realtime_qa.binding import OntopDeploymentVerifier, ReleaseBindingLoader


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify an Ontop endpoint against an approved S7 runtime package",
    )
    parser.add_argument("--project-dir", type=Path, required=True)
    parser.add_argument("--deployment-binding", type=Path, required=True)
    arguments = parser.parse_args()

    loader = ReleaseBindingLoader()
    binding = loader.load(arguments.project_dir.resolve())
    deployment = json.loads(
        arguments.deployment_binding.resolve().read_text(encoding="utf-8")
    )
    endpoint = str(deployment.get("endpoint") or "")
    client = loader.build_ontop_client(binding, endpoint)
    identity = OntopDeploymentVerifier().verify_endpoint(
        client,
        binding,
        arguments.deployment_binding.resolve(),
    )
    print(
        json.dumps(
            {
                "artifact_verified": True,
                "runtime_verified": True,
                "project_id": binding.project_id,
                "release_version": binding.release_version,
                "release_fingerprint": binding.release_fingerprint,
                "ontop_deployment_id": identity.deployment_id,
                "mapping_sha256": identity.mapping_sha256,
                "endpoint": identity.endpoint,
                "verified_at": identity.verified_at.isoformat(),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
