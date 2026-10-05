"""Static desktop configuration shares the real managed runtime's contracts."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


@pytest.fixture
def configurator(monkeypatch):
    monkeypatch.syspath_prepend(str(REPO / "scripts"))
    spec = importlib.util.spec_from_file_location(
        "orion_desktop_configure_test", REPO / "scripts/configure_orion_desktop.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepared(tmp_path, configurator):
    from prepare_orion_runtime import prepare

    root = tmp_path.resolve()
    runtime, profile = root / "runtime source with spaces", root / "runtime Profile with spaces"
    source = runtime / "services/realtime_qa/api.py"
    source.parent.mkdir(parents=True)
    body = b"raise RuntimeError('the configuration plan must not import or start the app')\n"
    source.write_bytes(body)
    checksum = hashlib.sha256(body).hexdigest()
    fingerprint = hashlib.sha256(
        ("services/realtime_qa/api.py\0" + checksum + "\n").encode()
    ).hexdigest()
    manifest = {
        "schemaVersion": 1, "runtimeVersion": "1.0.0-rc.5",
        "algorithm": "sha256-path-content-v1", "fingerprint": fingerprint,
        "files": [{"path": "services/realtime_qa/api.py", "sha256": checksum, "bytes": len(body)}],
        "required": ["services/realtime_qa/api.py"],
    }
    (runtime / "contracts").mkdir()
    (runtime / "contracts/runtime-source-manifest.json").write_text(json.dumps(manifest))
    python = profile / ".venvs/core/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 98\n")
    python.chmod(0o700)
    (profile / ".venvs/core/pyvenv.cfg").write_text("include-system-site-packages = false\n")
    prepare(runtime, profile, python, port=8099)
    return {
        "runtime": runtime, "profile": profile, "python": python,
        "environment": profile / "runtime-environment.json", "home": root / "official Home",
        "source": source,
    }


def indexed(plan):
    return {row["id"]: row for row in plan["patch"]}


def test_default_is_read_only_with_disabled_mcp_and_no_profile_writes(configurator, prepared):
    before = {file.relative_to(prepared["profile"]): file.read_bytes()
              for file in prepared["profile"].rglob("*") if file.is_file()}
    plan = configurator.configuration_plan(prepared["environment"], dsh_home=prepared["home"])
    rows = indexed(plan)
    assert list(rows) == ["orion-runtime-manager", "orion-workbench", "mcp-orion-workflow", "mcp-orion-realtime"]
    assert rows["orion-runtime-manager"]["disabled"] is False
    assert rows["orion-workbench"]["disabled"] is False
    workbench = rows["orion-workbench"]["config"]
    assert workbench["readOnly"] is True and workbench["actor"] is None
    assert workbench["documentIngestionApiEnabled"] is False
    for name in ["mcp-orion-workflow", "mcp-orion-realtime"]:
        assert rows[name]["disabled"] is True
        assert rows[name]["config"]["env"]["ORION_MCP_READ_ONLY"] == "1"
    after = {file.relative_to(prepared["profile"]): file.read_bytes()
             for file in prepared["profile"].rglob("*") if file.is_file()}
    assert after == before and not prepared["home"].exists()


def test_default_home_ignores_ambient_dsh_home_and_the_output_has_no_expressions(
    configurator, prepared, monkeypatch
):
    monkeypatch.setattr(Path, "home", lambda: prepared["home"])
    monkeypatch.setenv("DSH_HOME", "/foreign/home")
    monkeypatch.setenv("ORION_WORKFLOW_HOME", "/foreign/workflows")
    monkeypatch.setenv("ORION_TEST_SECRET", "must-stay-out")
    ambient = dict(os.environ)
    plan = configurator.configuration_plan(prepared["environment"])
    assert plan["target"] == str(prepared["home"] / ".dsh/profiles/desktop/cordis.patch.yml")
    text = json.dumps(plan)
    assert all(value not in text for value in ["process.env", "!!js", "dshHomePath(", "foreign", "must-stay-out"])
    assert dict(os.environ) == ambient


def test_empty_ambient_environment_preserves_full_config_and_all_mcp_arguments(
    configurator, prepared, monkeypatch
):
    expected = configurator.configuration_plan(
        prepared["environment"], dsh_home=prepared["home"],
        enable_workflow_mcp=True, enable_realtime_mcp=True,
    )
    monkeypatch.setattr(os, "environ", {})
    actual = configurator.configuration_plan(
        prepared["environment"], dsh_home=prepared["home"],
        enable_workflow_mcp=True, enable_realtime_mcp=True,
    )
    assert actual == expected and os.environ == {}
    rows = indexed(actual)
    for name, server, module in [
        ("mcp-orion-workflow", "orion_workflow", "harness.orion_workflow_mcp"),
        ("mcp-orion-realtime", "orion_realtime", "harness.realtime_qa_mcp"),
    ]:
        row = rows[name]
        assert row["disabled"] is False
        config = row["config"]
        assert set(config) == {"serverName", "transport", "command", "args", "cwd", "env",
                               "toolCallTimeoutMs", "failOnStartupError"}
        assert config["serverName"] == server and config["transport"] == "stdio"
        assert config["command"] == str(prepared["python"])
        assert config["args"] == ["-m", module] and config["cwd"] == str(prepared["runtime"])
        assert config["toolCallTimeoutMs"] == 90000 and config["failOnStartupError"] is False
        assert config["env"]["ONTOLOGY_AGENT_API_URL"] == "http://127.0.0.1:8099"


def test_mcp_environment_matches_the_real_manager_paths(configurator, prepared):
    assert NODE, "Install the repository's declared Node runtime before running contract checks"
    rows = indexed(configurator.configuration_plan(prepared["environment"], dsh_home=prepared["home"]))
    program = """
import fs from 'node:fs';
import {managedRuntimeEnvironment,managedRuntimePaths} from './harness/plugins/orion-workbench/lib/runtime-manager.js';
const config=JSON.parse(fs.readFileSync(0,'utf8'));
console.log(JSON.stringify(managedRuntimeEnvironment(managedRuntimePaths(config),config.environment,{})));
"""
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", program], cwd=REPO,
        input=json.dumps(rows["orion-runtime-manager"]["config"]),
        env={}, capture_output=True, text=True, check=True, timeout=10,
    )
    expected = json.loads(result.stdout)
    assert len(expected) >= 36
    for name in ["mcp-orion-workflow", "mcp-orion-realtime"]:
        environment = rows[name]["config"]["env"]
        assert {key: environment[key] for key in expected} == expected
    workbench = rows["orion-workbench"]["config"]
    assert workbench["backendRoot"] == expected["ORION_PROJECT_ROOT"]
    assert workbench["python"] == expected["PYTHON"]
    assert workbench["workflowHome"] == expected["ORION_WORKFLOW_HOME"]
    assert workbench["documentIngestionRoot"] == expected["ORION_DOCUMENT_INGESTION_ROOT"]
    assert workbench["realtimeQaApiUrl"] == expected["ONTOLOGY_AGENT_API_URL"]


def test_writes_require_an_actor_and_do_not_relax_qa_or_deployment_gates(configurator, prepared):
    with pytest.raises(ValueError, match="actor"):
        configurator.configuration_plan(prepared["environment"], allow_writes=True)
    rows = indexed(configurator.configuration_plan(
        prepared["environment"], dsh_home=prepared["home"], allow_writes=True, actor="local-user",
        enable_document_ingestion=True, enable_workflow_mcp=True,
    ))
    assert rows["orion-workbench"]["config"]["readOnly"] is False
    assert rows["orion-workbench"]["config"]["actor"] == "local-user"
    assert rows["orion-workbench"]["config"]["documentIngestionApiEnabled"] is True
    assert rows["mcp-orion-workflow"]["config"]["env"]["ORION_MCP_READ_ONLY"] == "0"
    assert rows["mcp-orion-realtime"]["disabled"] is True
    assert rows["mcp-orion-realtime"]["config"]["env"]["ORION_MCP_READ_ONLY"] == "1"
    for row in [rows["mcp-orion-workflow"], rows["mcp-orion-realtime"]]:
        assert row["config"]["env"]["ORION_S7_AUTO_DEPLOY"] == "false"
    assert "backendValidated" not in rows["orion-workbench"]["config"]


@pytest.mark.parametrize("actor", ["", " ", " user", "user\n", "user\0", "x" * 129])
def test_invalid_actors_are_rejected_without_echoing_input(configurator, prepared, actor):
    with pytest.raises(ValueError, match="Actor"):
        configurator.configuration_plan(prepared["environment"], actor=actor)


@pytest.mark.parametrize("changes", [
    {"ORION_WORKFLOW_HOME": "/foreign/workflows"},
    {"ORION_RUNTIME_MODE": "external"},
    {"ORION_WORKFLOW_PYTHON": "/usr/bin/python3"},
    {"ORION_RUNTIME_PORT": "80"},
    {"ORION_RUNTIME_STATE_ROOT": "relative/state"},
    {"ORION_DOCUMENT_INGESTION_ROOT": "/foreign/documents"},
    {"ORION_TEST_SECRET": "must-stay-out"},
])
def test_wrong_prepared_paths_modes_and_extra_values_are_rejected(configurator, prepared, changes):
    file = prepared["environment"]
    values = json.loads(file.read_text())
    file.write_text(json.dumps({**values, **changes}))
    with pytest.raises(ValueError):
        configurator.configuration_plan(file, dsh_home=prepared["home"])


def test_source_drift_missing_core_and_unsafe_home_fail_before_a_patch_is_generated(
    configurator, prepared
):
    for home in [Path("relative/home"), prepared["runtime"] / "home",
                 prepared["runtime"].parent, prepared["home"] / ".." / "traversal"]:
        with pytest.raises(ValueError):
            configurator.configuration_plan(prepared["environment"], dsh_home=home)
    link = prepared["home"].with_name("linked home")
    link.symlink_to(prepared["profile"], target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        configurator.configuration_plan(prepared["environment"], dsh_home=link)
    prepared["source"].write_text("changed source\n")
    with pytest.raises(ValueError, match="content has changed"):
        configurator.configuration_plan(prepared["environment"], dsh_home=prepared["home"])
    prepared["source"].write_bytes(
        b"raise RuntimeError('the configuration plan must not import or start the app')\n"
    )
    (prepared["profile"] / ".venvs/core/pyvenv.cfg").unlink()
    with pytest.raises(ValueError, match="virtual environment"):
        configurator.configuration_plan(prepared["environment"], dsh_home=prepared["home"])


def test_cli_prints_only_a_json_compatible_patch_with_empty_ambient(configurator, prepared):
    previous = prepared["environment"].read_bytes()
    result = subprocess.run(
        [sys.executable, "-B", str(REPO / "scripts/configure_orion_desktop.py"),
         "--environment", str(prepared["environment"]), "--dsh-home", str(prepared["home"]),
         "--allow-writes", "--actor", "local-user", "--enable-document-ingestion",
         "--enable-workflow-mcp", "--enable-realtime-mcp"],
        env={}, capture_output=True, text=True, check=True, timeout=10,
    )
    patch = json.loads(result.stdout)
    assert len(patch) == 4 and all(type(row["disabled"]) is bool for row in patch)
    assert all(row["disabled"] is False for row in patch)
    assert str(prepared["home"] / "profiles/desktop/cordis.patch.yml") in result.stderr
    assert prepared["environment"].read_bytes() == previous
    assert not prepared["home"].exists()


def test_cli_rejects_unprepared_input_without_printing_paths_or_secrets(configurator, prepared):
    values = json.loads(prepared["environment"].read_text())
    values["ORION_TEST_SECRET"] = "must-never-be-echoed"
    prepared["environment"].write_text(json.dumps(values))
    result = subprocess.run(
        [sys.executable, "-B", str(REPO / "scripts/configure_orion_desktop.py"),
         "--environment", str(prepared["environment"])],
        env={}, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0 and result.stdout == ""
    assert "Configuration rejected" in result.stderr
    assert "must-never-be-echoed" not in result.stderr
