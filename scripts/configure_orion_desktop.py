"""Print a complete static desktop patch for an already prepared ORION runtime.

This is a configuration plan, not an installer or launcher. The official app
loads the four overrides after its installed bundle. Cordis replaces each
entry's config object, so every required field is supplied here explicitly.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from launch_orion_harness import explicit_path, prepared_environment, separated

# These are the bundle's declared local defaults, never inherited credentials
# or machine configuration. Enabling writes does not enable S7 auto-deployment.
BUSINESS_ENVIRONMENT = {
    "ORION_S7_AUTO_DEPLOY": "false",
    "ORION_SOURCE_DATA_READER_URL": "",
    "ORION_WORKFLOW_METADATA_REQUIRED": "false",
    "ORION_WORKFLOW_DATABASE_URL": "",
    "ORION_ARTIFACT_BACKEND": "filesystem",
    "DATABASE_URL": "",
    "FUSEKI_URL": "http://127.0.0.1:3030/supply",
    "ONTOP_URL": "http://127.0.0.1:8080/sparql",
    "SEMANTICA_API_URL": "http://127.0.0.1:8001",
    "SEMANTICA_AUTO_SYNC": "0",
}


def configuration_plan(
    environment_file: Path,
    *,
    dsh_home: Path | None = None,
    allow_writes: bool = False,
    actor: str | None = None,
    enable_document_ingestion: bool = False,
    enable_workflow_mcp: bool = False,
    enable_realtime_mcp: bool = False,
) -> dict[str, Any]:
    """Validate the prepared source/paths and return four complete overrides.

    Neither the target Home nor its current configuration is read. Enabling a
    workflow writer still requires the backend's source, phase, approval and
    publication contracts; this plan supplies no exceptions to those gates.
    """
    options = (allow_writes, enable_document_ingestion, enable_workflow_mcp, enable_realtime_mcp)
    if any(type(value) is not bool for value in options):
        raise ValueError("Write/MCP options must be explicit booleans")
    if actor is not None and (
        not isinstance(actor, str) or not actor.strip() or actor != actor.strip()
        or len(actor) > 128 or any(ord(character) < 32 or ord(character) == 127 for character in actor)
    ):
        raise ValueError("Actor must be an explicit nonempty identifier without control characters")
    if allow_writes and actor is None:
        raise ValueError("Enabling writes requires an explicit --actor")

    values, runtime, profile = prepared_environment(environment_file)
    if values["ORION_RUNTIME_MODE"] != "managed":
        raise ValueError("Direct desktop configuration requires a prepared managed local runtime")
    home = explicit_path(dsh_home if dsh_home is not None else Path.home() / ".dsh")
    if "\n" in str(home) or "\r" in str(home):
        raise ValueError("Harness Home must not contain control characters")
    separated(home, runtime)
    target = home / "profiles/desktop/cordis.patch.yml"
    if target.is_relative_to(profile) or profile == target:
        raise ValueError("The desktop patch must be separate from the ORION runtime Profile")

    manager = {
        "mode": "managed",
        "runtimeRoot": str(runtime),
        "python": values["ORION_WORKFLOW_PYTHON"],
        "profileRoot": str(profile),
        "stateRoot": values["ORION_RUNTIME_STATE_ROOT"],
        "cacheRoot": values["ORION_RUNTIME_CACHE_ROOT"],
        "port": int(values["ORION_RUNTIME_PORT"]),
        # The manager validates this allowlist and derives every owned path.
        # In particular, PYTHON is not an accepted configured environment key.
        "environment": dict(BUSINESS_ENVIRONMENT),
    }
    workbench = {
        "backendRoot": str(runtime),
        "python": values["ORION_WORKFLOW_PYTHON"],
        "workflowHome": values["ORION_WORKFLOW_HOME"],
        "documentIngestionRoot": values["ORION_DOCUMENT_INGESTION_ROOT"],
        "realtimeQaApiUrl": values["ONTOLOGY_AGENT_API_URL"],
        "actor": actor,
        "readOnly": not allow_writes,
        "documentIngestionApiEnabled": enable_document_ingestion,
        "mcpControlEnabled": False,
    }
    # The prepared map covers the same complete Profile paths as the manager:
    # workflow/document/release/evidence, analysis/exports, Wren and Semantica.
    # Explicit scalar markers are retained so no MCP path or endpoint depends
    # on startup ORION_* environment variables.
    workflow_environment = {
        **values,
        **BUSINESS_ENVIRONMENT,
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "ORION_MCP_READ_ONLY": "0" if allow_writes else "1",
    }

    def mcp(server_name: str, module: str, *, read_only: bool) -> dict[str, Any]:
        return {
            "serverName": server_name,
            "transport": "stdio",
            "command": values["ORION_WORKFLOW_PYTHON"],
            "args": ["-m", module],
            "cwd": str(runtime),
            "env": {**workflow_environment, "ORION_MCP_READ_ONLY": "1" if read_only else "0"},
            "toolCallTimeoutMs": 90000,
            "failOnStartupError": False,
        }

    return {
        "target": str(target),
        "profile": "desktop",
        "patch": [
            {"id": "orion-runtime-manager", "disabled": False, "config": manager},
            {"id": "orion-workbench", "disabled": False, "config": workbench},
            {"id": "mcp-orion-workflow", "disabled": not enable_workflow_mcp,
             "config": mcp("orion_workflow", "harness.orion_workflow_mcp", read_only=not allow_writes)},
            {"id": "mcp-orion-realtime", "disabled": not enable_realtime_mcp,
             "config": mcp("orion_realtime", "harness.realtime_qa_mcp", read_only=True)},
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, required=True,
                        help="Absolute path to the verified prepared runtime-environment.json")
    parser.add_argument("--dsh-home", type=Path,
                        help="Target Harness Home (default: ~/.dsh; only its patch path is planned)")
    parser.add_argument("--allow-writes", action="store_true",
                        help="Enable workbench/workflow writes; requires --actor and retains stage gates")
    parser.add_argument("--actor", help="Explicit business actor identifier")
    parser.add_argument("--enable-document-ingestion", action="store_true",
                        help="Explicitly enable document ingestion API; writes still obey readOnly")
    parser.add_argument("--enable-workflow-mcp", action="store_true",
                        help="Explicitly enable the official workflow stdio MCP client")
    parser.add_argument("--enable-realtime-mcp", action="store_true",
                        help="Explicitly enable the official read-only realtime stdio MCP client")
    args = parser.parse_args()
    plan = configuration_plan(
        args.environment,
        dsh_home=args.dsh_home,
        allow_writes=args.allow_writes,
        actor=args.actor,
        enable_document_ingestion=args.enable_document_ingestion,
        enable_workflow_mcp=args.enable_workflow_mcp,
        enable_realtime_mcp=args.enable_realtime_mcp,
    )
    # JSON is also valid YAML for the official patch loader. Only the four
    # override rows go to stdout; diagnostics never enter the patch contents.
    print(json.dumps(plan["patch"], ensure_ascii=False, indent=2))
    print(f"Plan only: safely merge these four overrides into {plan['target']}; "
          "no Profile, state or environment was changed.", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, TypeError, KeyError):
        raise SystemExit("Configuration rejected: verify the prepared runtime, Profile paths "
                         "and explicit write/MCP options.") from None
