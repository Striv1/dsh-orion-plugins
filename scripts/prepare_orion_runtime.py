"""Prepare one explicit Profile's ORION paths; never install, start or overwrite state."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from orion_runtime_manifest import verify_runtime

ENV_FILE = "runtime-environment.json"
CONFIG_FILE = "runtime-manager.json"


def regular_directory(path: Path) -> None:
    for current in [path, *path.parents]:
        if current.is_symlink():
            raise ValueError("Profile directories must not traverse symlinks")
        if current.exists() and not current.is_dir():
            raise ValueError("Profile directory is occupied by a non-directory")


def write_new(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
    path.chmod(0o600)


def prepare(runtime_root: Path, profile_root: Path, python: Path, *, port: int = 8091,
            wren_python: Path | None = None) -> dict:
    if not 1024 <= port <= 65535:
        raise ValueError("Core port is outside the allowed range")
    if not runtime_root.is_absolute() or not profile_root.is_absolute() or not python.is_absolute():
        raise ValueError("runtime root, Profile root and Core Python must be explicit absolute paths")
    runtime_root = runtime_root.absolute()
    profile_root = profile_root.absolute()
    manifest = verify_runtime(runtime_root)
    expected_python = profile_root / ".venvs/core/bin/python"
    if python != expected_python or not (profile_root / ".venvs/core/pyvenv.cfg").is_file():
        raise ValueError("Core Python must be the installed Profile .venvs/core environment")
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("Install the Core environment first; the selected Python is unavailable")
    if (profile_root == runtime_root or profile_root.is_relative_to(runtime_root)
            or runtime_root.is_relative_to(profile_root)):
        raise ValueError("Profile state must be outside the frozen runtime source")
    regular_directory(profile_root)
    for name in [ENV_FILE, CONFIG_FILE]:
        if (profile_root / name).exists() or (profile_root / name).is_symlink():
            raise FileExistsError("Existing Profile configuration is preserved; use an unconfigured Profile")
    state, cache = profile_root / "state", profile_root / "cache"
    targets = {"ORION_WORKFLOW_HOME": state / "workflows",
               "ORION_DOCUMENT_INGESTION_ROOT": state / "document-inputs",
               "ORION_WORKSPACE_REFERENCE_ROOTS": state / "references",
               "ORION_REALTIME_EVIDENCE_ROOT": state / "evidence",
               "ORION_ANALYSIS_ASSET_ROOT": state / "analysis-assets",
               "ORION_ANALYTICS_EXPORT_ROOT": state / "analytics-exports",
               "ORION_WREN_SOURCE_REGISTRY_ROOT": state / "wren-sources",
               "ORION_WREN_CACHE_ROOT": cache / "wren-projects",
               "ORION_EXPLORATION_INDEX_ROOT": cache / "exploration-index",
               "ORION_S7_AUTO_DEPLOY_CONFIG_ROOT": state / "ontop-config",
               "ORION_S7_AUTO_DEPLOY_ROOT": state / "realtime-business",
               "SEMANTICA_STATE_DIR": state / "semantica/runtime",
               "SEMANTICA_EXPLORER_STATE_DIR": state / "semantica/explorer",
               "PYTHONPYCACHEPREFIX": cache / "pycache"}
    registry = state / "runtime-registry.json"
    for directory in [profile_root, state, cache, *targets.values()]:
        regular_directory(directory)
    if registry.is_symlink() or (registry.exists() and not registry.is_file()):
        raise ValueError("Existing registry is not a regular file")
    wren = wren_python or profile_root / ".venvs/wren/bin/python"
    if wren != profile_root / ".venvs/wren/bin/python":
        raise ValueError("Wren Python must use the separate Profile .venvs/wren environment")
    environment = {key: str(value) for key, value in targets.items()}
    environment.update(ORION_BACKEND_ROOT=str(runtime_root), ORION_RUNTIME_ROOT=str(runtime_root),
                       ORION_PROJECT_ROOT=str(runtime_root), ORION_RUNTIME_STATE_ROOT=str(state),
                       ORION_RUNTIME_CACHE_ROOT=str(cache),
                       ORION_RUNTIME_PROFILE_ROOT=str(profile_root), ORION_RUNTIME_MODE="managed",
                       ORION_WORKFLOW_PYTHON=str(python), PYTHON=str(python),
                       ORION_WREN_PYTHON=str(wren), ORION_REALTIME_RUNTIME_REGISTRY=str(registry),
                       ORION_DOCUMENT_INPUT_ROOT=str(targets["ORION_DOCUMENT_INGESTION_ROOT"]),
                       SEMANTICA_ACTIVE_RUNTIME_CONFIG=str(state / "semantica/active-runtime.json"),
                       SEMANTICA_GRAPH_PATH=str(state / "semantica/explorer/graph.pkl"),
                       SEMANTICA_EXPLORER_LEGACY_GRAPH=str(state / "semantica/explorer/legacy-graph.pkl"),
                       ONTOLOGY_AGENT_API_URL=f"http://127.0.0.1:{port}",
                       ORION_RUNTIME_PORT=str(port), ORION_ENABLE_REALTIME_QA_MCP="1")
    # These path/config files contain no copied credentials or business content.
    for directory in [profile_root, state, cache, *targets.values()]:
        directory.mkdir(parents=True, exist_ok=True)
    registry_created = not registry.exists()
    if registry_created:
        write_new(registry, {"schema_version": 1, "default_project_id": None, "runtimes": []})
    write_new(profile_root / ENV_FILE, environment)
    write_new(profile_root / CONFIG_FILE, {"mode": "managed", "runtimeRoot": str(runtime_root),
                                         "python": str(python), "profileRoot": str(profile_root), "port": port})
    return {"prepared": True, "runtimeVersion": manifest["runtimeVersion"],
            "fingerprint": manifest["fingerprint"], "environmentFile": str(profile_root / ENV_FILE),
            "managerConfigFile": str(profile_root / CONFIG_FILE), "registryCreated": registry_created,
            "existingRegistryPreserved": not registry_created, "dependencyInstall": False,
            "serviceStart": False, "credentialsRead": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--profile-root", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wren-python", type=Path)
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    print(json.dumps(prepare(args.runtime_root, args.profile_root, args.python, port=args.port,
                             wren_python=args.wren_python), ensure_ascii=False))


if __name__ == "__main__":
    main()
