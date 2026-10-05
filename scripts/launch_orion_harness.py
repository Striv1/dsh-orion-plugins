"""Launch an explicit official Harness with a verified, prepared ORION environment."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
from pathlib import Path

from orion_runtime_manifest import verify_runtime

SDK_VERSION = "0.2.0-rc.2"
BASE_ENVIRONMENT = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE",
                    "TMPDIR", "SHELL", "TERM")


def explicit_path(value: Path | str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in str(path):
        raise ValueError("Launch paths must be explicit absolute paths without traversal")
    return path.absolute()


def no_symlinks(path: Path, *, executable_symlink: bool = False) -> None:
    for candidate in [path, *path.parents]:
        if candidate.is_symlink() and not (executable_symlink and candidate == path):
            raise ValueError("Launch paths must not traverse symbolic links")


def executable(path: Path) -> None:
    no_symlinks(path, executable_symlink=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("The explicitly selected executable is unavailable")


def separated(path: Path, runtime: Path) -> None:
    if path == runtime or path.is_relative_to(runtime) or runtime.is_relative_to(path):
        raise ValueError("Launch state and Home must be separate from immutable runtime source")
    no_symlinks(path)


def prepared_environment(environment_file: Path) -> tuple[dict[str, str], Path, Path]:
    environment_file = explicit_path(environment_file)
    no_symlinks(environment_file)
    if (environment_file.name != "runtime-environment.json" or not environment_file.is_file()
            or environment_file.stat().st_size > 65536):
        raise ValueError("Select the prepared runtime-environment.json file")
    values = json.loads(environment_file.read_text(encoding="utf-8"))
    if (not isinstance(values, dict) or not values or any(not isinstance(k, str)
            or not isinstance(v, str) or "\x00" in v or "\n" in v or "\r" in v
            for k, v in values.items())):
        raise ValueError("Prepared environment must contain path-only string values")
    runtime = explicit_path(values.get("ORION_RUNTIME_ROOT", ""))
    profile = explicit_path(values.get("ORION_RUNTIME_PROFILE_ROOT", ""))
    if environment_file.parent != profile:
        raise ValueError("Prepared environment does not belong to the selected Profile")
    separated(profile, runtime)
    verify_runtime(runtime)
    state, cache = profile / "state", profile / "cache"
    python = profile / ".venvs/core/bin/python"
    expected = {
        "ORION_BACKEND_ROOT": runtime, "ORION_RUNTIME_ROOT": runtime,
        "ORION_PROJECT_ROOT": runtime, "ORION_RUNTIME_PROFILE_ROOT": profile,
        "ORION_RUNTIME_STATE_ROOT": state, "ORION_RUNTIME_CACHE_ROOT": cache,
        "PYTHON": python, "ORION_WORKFLOW_PYTHON": python,
        "ORION_WREN_PYTHON": profile / ".venvs/wren/bin/python",
        "ORION_WORKFLOW_HOME": state / "workflows",
        "ORION_DOCUMENT_INGESTION_ROOT": state / "document-inputs",
        "ORION_DOCUMENT_INPUT_ROOT": state / "document-inputs",
        "ORION_WORKSPACE_REFERENCE_ROOTS": state / "references",
        "ORION_REALTIME_RUNTIME_REGISTRY": state / "runtime-registry.json",
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
        "SEMANTICA_ACTIVE_RUNTIME_CONFIG": state / "semantica/active-runtime.json",
        "SEMANTICA_GRAPH_PATH": state / "semantica/explorer/graph.pkl",
        "SEMANTICA_EXPLORER_LEGACY_GRAPH": state / "semantica/explorer/legacy-graph.pkl",
        "PYTHONPYCACHEPREFIX": cache / "pycache",
    }
    allowed = set(expected) | {"ORION_RUNTIME_MODE", "ORION_RUNTIME_PORT",
                              "ORION_ENABLE_REALTIME_QA_MCP", "ONTOLOGY_AGENT_API_URL"}
    if set(values) != allowed or values.get("ORION_RUNTIME_MODE") not in {"managed", "external"}:
        raise ValueError("Prepared environment has missing fields, unsupported fields or an invalid mode")
    for key, target in expected.items():
        if values[key] != str(target):
            raise ValueError("Prepared path is inconsistent with the Profile/runtime contract")
        if target.is_relative_to(profile) and target != python and key != "ORION_WREN_PYTHON":
            no_symlinks(target)
    config = profile / ".venvs/core/pyvenv.cfg"
    no_symlinks(config)
    if not config.is_file():
        raise ValueError("The Profile Core virtual environment has not been installed")
    executable(python)
    if not re.fullmatch(r"[0-9]{4,5}", values["ORION_RUNTIME_PORT"]):
        raise ValueError("Prepared Core port is invalid")
    core_port = int(values["ORION_RUNTIME_PORT"])
    if (not 1024 <= core_port <= 65535 or values["ORION_ENABLE_REALTIME_QA_MCP"] != "1"
            or values["ONTOLOGY_AGENT_API_URL"] != f"http://127.0.0.1:{core_port}"):
        raise ValueError("Prepared Core endpoint/MCP configuration is inconsistent")
    return values, runtime, profile


def launch_plan(environment_file: Path, dsh_home: Path, *, surface: str,
                node: Path | None = None, dsh_entry: Path | None = None,
                port: int = 3081, no_open: bool = False, app: Path | None = None,
                user_data_dir: Path | None = None, ambient: dict[str, str] | None = None
                ) -> tuple[Path, list[str], dict[str, str]]:
    values, runtime, _profile = prepared_environment(environment_file)
    dsh_home = explicit_path(dsh_home)
    separated(dsh_home, runtime)
    inherited = os.environ if ambient is None else ambient
    environment = {key: inherited[key] for key in BASE_ENVIRONMENT if key in inherited}
    environment.update(values)
    environment["DSH_HOME"] = str(dsh_home)
    if surface == "web":
        if node is None or dsh_entry is None or not 1024 <= port <= 65535:
            raise ValueError("Web launch needs explicit Node/DSH entry and a valid port")
        node, dsh_entry = explicit_path(node), explicit_path(dsh_entry)
        executable(node)
        no_symlinks(dsh_entry)
        if not dsh_entry.is_file() or dsh_entry.name != "bin.js":
            raise ValueError("Select the official CLI lib/bin.js entry")
        package_file = dsh_entry.parent.parent / "package.json"
        no_symlinks(package_file)
        package = json.loads(package_file.read_text(encoding="utf-8"))
        if package.get("name") != "@deepseek-ai/dsh" or package.get("version") != SDK_VERSION:
            raise ValueError("Web launch requires the pinned official Harness CLI")
        argv = [str(node), str(dsh_entry), "web", "--port", str(port)]
        if no_open:
            argv.append("--no-open")
        return node, argv, environment
    if surface != "desktop" or app is None or user_data_dir is None:
        raise ValueError("Desktop launch needs the signed app and an explicit user-data directory")
    app, user_data_dir = explicit_path(app), explicit_path(user_data_dir)
    no_symlinks(app)
    separated(user_data_dir, runtime)
    if not app.is_dir() or app.suffix != ".app":
        raise ValueError("Select the official signed macOS .app distribution")
    info_file = app / "Contents/Info.plist"
    no_symlinks(info_file)
    info = plistlib.loads(info_file.read_bytes())
    binary = info.get("CFBundleExecutable")
    if (info.get("CFBundleIdentifier") != "com.deepseek.dsh"
            or info.get("CFBundleShortVersionString") != SDK_VERSION
            or not isinstance(binary, str) or not binary or "/" in binary or "\\" in binary):
        raise ValueError("Desktop app identity/version does not match the pinned official distribution")
    entry = app / "Contents/MacOS" / binary
    executable(entry)
    return entry, [str(entry), "--user-data-dir=" + str(user_data_dir)], environment


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", type=Path, required=True)
    parser.add_argument("--dsh-home", type=Path, required=True)
    commands = parser.add_subparsers(dest="surface", required=True)
    web = commands.add_parser("web")
    web.add_argument("--node", type=Path, required=True)
    web.add_argument("--dsh-entry", type=Path, required=True)
    web.add_argument("--port", type=int, default=3081)
    web.add_argument("--no-open", action="store_true")
    desktop = commands.add_parser("desktop")
    desktop.add_argument("--app", type=Path, required=True)
    desktop.add_argument("--user-data-dir", type=Path, required=True)
    args = vars(parser.parse_args())
    environment_file = args.pop("environment")
    entry, argv, environment = launch_plan(environment_file, **args)
    # Replace this process directly: no child supervisor, detached process or
    # signal proxy, and no dependency install or configuration/state writes.
    os.execve(entry, argv, environment)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, json.JSONDecodeError, plistlib.InvalidFileException):
        raise SystemExit("Launch rejected: verify the prepared Profile/runtime paths and official entry installation") from None
