import { createHash } from "node:crypto";
import { lstat, readFile, readdir } from "node:fs/promises";
import { basename, extname, isAbsolute, join, resolve } from "node:path";

export const RUNTIME_SCRIPTS = [
  "build_ontology_graph.py", "engineering_control.py", "import_database_snapshot.py",
  "managed_stage_bridge.py", "open_release_in_protege.py", "protege_role_router.py",
  "run_engineering_controller.py", "run_protege_build_stage.py", "run_quality_validation_stage.py",
  "semantica_runtime_config.py", "semantica_validation_runtime.py",
  "register_wren_source.py", "sync_published_ontologies_to_semantica.py",
  "register_realtime_runtime.py", "run_s7_realtime_deployment.py", "prepare_s7_deployment_input.py",
  "render_ontop_release_deployment.py", "verify_ontop_release_deployment.py", "ensure_semantica_runtime.sh",
].map(name => `scripts/${name}`);

export const RUNTIME_RESOURCES = [
  "services/ingestion/pdf_text_extract.swift",
  "services/ontology_engineering/report_assets/css/orion-report.css",
  "services/ontology_engineering/report_assets/js/orion-motion.js",
  "services/ontology_engineering/report_assets/js/echarts.min.js",
  "harness/contracts/workflow-ui-actions.json",
  "database/orion_source_data_schema.sql", "pyproject.toml", "uv.lock", "scripts/requirements-wren.txt",
];
export const RUNTIME_GENERATED = [
  "Makefile", "scripts/orion_runtime_manifest.py", "scripts/prepare_orion_runtime.py", "scripts/launch_orion_harness.py",
  "docs/runtime-install.md", "vendor/echarts-LICENSE.txt", "vendor/echarts-NOTICE.txt",
];
export const RUNTIME_REQUIRED = [
  "services/realtime_qa/api.py", "services/ontology_engineering/workflow.py", "harness/orion_workflow_mcp.py", "harness/orion_workflow_action.py",
  "harness/realtime_qa_mcp.py", "scripts/build_ontology_graph.py", "Makefile",
  "harness/contracts/workflow-ui-actions.json", "harness/ontology-templates/catalog.json",
  "database/migrations/orion_workflow/001_baseline.sql",
  "database/migrations/orion_workflow/002_realtime_document_current.sql",
  "database/orion_source_data_schema.sql", "pyproject.toml", "uv.lock", "scripts/requirements-wren.txt",
  "scripts/orion_runtime_manifest.py", "scripts/prepare_orion_runtime.py", "scripts/launch_orion_harness.py",
];
export const RUNTIME_UNIT_TESTS = [
  "core_realtime_api", "realtime_empty_registry", "realtime_runtime_registry", "realtime_qa",
  "realtime_qa_mcp", "realtime_evidence_receipts", "realtime_result_pages", "realtime_answer_typed_values",
  "realtime_hot_release_authority", "realtime_reasoning", "orion_workflow", "workflow_action_contract",
  "workflow_storage", "workflow_schema_migrations", "stage_jobs", "engineering_control_workflow",
  "document_job_review", "document_upload_scope", "document_source_identities", "document_cq",
  "document_fact_parameters", "document_runtime_activation", "source_preflight", "unstructured_router",
  "ontology_graph_builder", "graph_union", "snapshot_capture", "multi_source_snapshot",
  "structured_data_pipeline", "source_query_gateway", "source_query_plan", "source_registration",
  "wren_analysis", "wren_project", "analysis_assets", "analytics_api", "analytics_exports", "live_analytics",
  "protege_build_stage", "protege_role_router", "quality_validation_stage", "source_scope", "runtime_state_paths",
].map(name => `tests/unit/test_${name}.py`);

const forbiddenName = /^(?:\.env(?:\..*)?|\.credentials(?:\..*)?|\.npmrc|\.netrc|id_rsa|id_ed25519|.*\.(?:pem|key|p12|pfx))$/i;
const forbiddenPart = new Set([".git", ".venv", "node_modules", "__pycache__", "data", "state", ".orion-workflows", ".orion-runtime"]);
const personalPath = /(?:\/Users\/[^\s/"'`<>]+\/|\/home\/[^\s/"'`<>]+\/|[A-Za-z]:[\\/]Users[\\/][^\s\\/"'`<>]+[\\/])/;
const credential = /(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:ghp_|github_pat_)[A-Za-z0-9_]{24,}|\bsk-[A-Za-z0-9_-]{24,})/;
export const runtimeSha256 = body => createHash("sha256").update(body).digest("hex");
export const runtimePathOrder = (a, b) => a < b ? -1 : a > b ? 1 : 0;

export function runtimePath(path) {
  if (typeof path !== "string" || !path || isAbsolute(path) || path.includes("\\") || path.includes("\0") ||
      path.split("/").some(part => !part || part === "." || part === ".." || forbiddenPart.has(part) || forbiddenName.test(part))) {
    throw new Error("Unsafe runtime source path");
  }
  return path;
}

export function assertRuntimeContent(path, body) {
  runtimePath(path);
  if (body.length > 32 * 1024 * 1024) throw new Error(`Runtime input exceeds file limit: ${path}`);
  if ([".py", ".swift", ".json", ".yml", ".yaml", ".md", ".toml", ".lock", ".txt", ".sql", ".js", ".css", ".sh", ".xml", ".rdf", ".ttl"].includes(extname(path)) || basename(path) === "Makefile") {
    const source = body.toString("utf8");
    if (personalPath.test(source)) throw new Error(`Runtime input contains a personal absolute path: ${path}`);
    if (credential.test(source)) throw new Error(`Runtime input contains credential-like material: ${path}`);
  }
}

export function portableSemanticaLauncher(body) {
  let source = body.toString("utf8");
  const replacements = [
    ['CONFIG_ROOT="${ORION_PROJECT_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"',
      'CONFIG_ROOT="${ORION_RUNTIME_ROOT:-${ORION_PROJECT_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}}"\nCORE_PYTHON="${ORION_WORKFLOW_PYTHON:-${PYTHON:-}}"\ncase "$CORE_PYTHON" in\n  /*) test -x "$CORE_PYTHON" || { echo "Configured Core Python is unavailable." >&2; exit 2; } ;;\n  *) echo "Configure an explicit absolute Core Python path." >&2; exit 2 ;;\nesac'],
    ['ACTIVE_CONFIG="${SEMANTICA_ACTIVE_RUNTIME_CONFIG:-$CONFIG_ROOT/.orion-semantica/active-runtime.json}"',
      'ACTIVE_CONFIG="${SEMANTICA_ACTIVE_RUNTIME_CONFIG:?Configure an explicit Profile Semantica runtime config}"'],
    ['exec "$CONFIG_ROOT/.venv/bin/python"', 'exec "$CORE_PYTHON"'],
    ['SEMANTICA_CLI="${SEMANTICA_CLI:-${SEMANTICA_UPSTREAM_ROOT:-${HOME}/FDE/semantica-upstream-20260908}/.venv/bin/semantica}"',
      'SEMANTICA_CLI="${SEMANTICA_CLI:?Configure the external Semantica CLI}"'],
    ['SEMANTICA_PYTHON="${SEMANTICA_PYTHON:-$(dirname -- "$SEMANTICA_CLI")/python}"',
      'SEMANTICA_PYTHON="${SEMANTICA_PYTHON:?Configure the external Semantica Python}"'],
    ['ORION_ROOT="${ORION_PROJECT_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"', 'ORION_ROOT="$CONFIG_ROOT"'],
    ['STATE_DIR="${SEMANTICA_STATE_DIR:-$ORION_ROOT/.orion-semantica}"',
      'STATE_DIR="${SEMANTICA_STATE_DIR:?Configure the explicit Profile Semantica state directory}"'],
    ['SYNC_PYTHON="${SEMANTICA_SYNC_PYTHON:-$ORION_ROOT/.venv/bin/python}"', 'SYNC_PYTHON="${SEMANTICA_SYNC_PYTHON:-$CORE_PYTHON}"'],
  ];
  for (const [before, after] of replacements) {
    if (!source.includes(before)) throw new Error("Semantica launcher contract changed; review its portable export");
    source = source.replace(before, after);
  }
  if (/\$\{HOME\}|\/FDE\/|\.venv\/bin\/python|\.orion-semantica/.test(source)) throw new Error("Semantica launcher retained a legacy local default");
  return Buffer.from(source);
}

export function portableRuntimeTest(path, body) {
  let source = body.toString("utf8");
  const replace = (before, after) => {
    if (!source.includes(before)) throw new Error(`Runtime test contract changed; review its portable export: ${path}`);
    source = source.replace(before, after);
  };
  if (path === "tests/unit/test_orion_workflow.py") {
    const prefix = `def test_core_profile_and_frontend_sync_orion_workflow_skill() -> None:
    root = Path(__file__).parents[2]
    core_profile = (root / "harness/profile/cordis.patch.yml").read_text(encoding="utf-8")
    prepare = (root / "harness/web/prepare_frontend.mjs").read_text(encoding="utf-8")
    makefile = (root / "Makefile").read_text(encoding="utf-8")
    skill = (root / "harness/skills/orion-ontology-engineer/SKILL.md").read_text(encoding="utf-8")

    assert "serverName: orion_workflow" in core_profile
    assert "harness.orion_workflow_mcp" in core_profile
    assert "ORION_MCP_READ_ONLY" in core_profile
    assert not (root / "harness/profile/supply.cordis.patch.yml").exists()
    assert 'const skillName = "orion-ontology-engineer"' in prepare
    assert 'if (mode === "core")' in prepare
    assert 'DSH_HOME="$(DSH_PRODUCTION_HOME)" ORION_REQUIRE_UI_CREATE_CONFIRMATION="0"' in makefile
    assert "harness-candidate-credentialed" in makefile
    assert 'ORION_MCP_READ_ONLY="1"' in makefile`;
    replace(prefix, `def test_core_profile_and_frontend_sync_orion_workflow_skill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).parents[2]
    manifest = json.loads((root / "contracts/runtime-source-manifest.json").read_text(encoding="utf-8"))
    declared = {row["path"]: row for row in manifest["files"]}
    for path in ("harness/orion_workflow_mcp.py", "harness/orion_workflow_action.py",
                 "harness/contracts/workflow-ui-actions.json", "Makefile",
                 "harness/skills/orion-ontology-engineer/SKILL.md"):
        assert path in declared
        value = (root / path).read_bytes()
        assert len(value) == declared[path]["bytes"]
        assert hashlib.sha256(value).hexdigest() == declared[path]["sha256"]
    assert "harness/orion_workflow_mcp.py" in manifest["required"]
    skill = (root / "harness/skills/orion-ontology-engineer/SKILL.md").read_text(encoding="utf-8")
    metadata = yaml.safe_load(skill.split("---", 2)[1])
    assert metadata["name"] == "orion-ontology-engineer" and metadata["description"]
    service = OntologyWorkflowService(tmp_path)
    tools = OrionWorkflowTools(service)
    monkeypatch.setenv("ORION_MCP_READ_ONLY", "1")
    for tool in ("create_ontology_project", "publish_ontology_package", "record_quality_validation"):
        with pytest.raises(WorkflowGateError, match="只读模式"):
            tools.call(tool, {})
    assert service.list_projects()["count"] == 0`);
  }
  if (path === "tests/unit/test_workflow_action_contract.py") {
    replace("import json\n", "import hashlib\nimport json\n");
    replace(`    profile = (root / "harness/profile/cordis.patch.yml").read_text(encoding="utf-8")
    assert "workflowActionContract:" in profile
    assert expected in profile
    assert not (root / "harness/profile/supply.cordis.patch.yml").exists()`,
      `    manifest = json.loads((root / "contracts/runtime-source-manifest.json").read_text(encoding="utf-8"))
    declared = {row["path"]: row for row in manifest["files"]}
    assert expected in manifest["required"] and expected in declared
    assert "harness/orion_workflow_action.py" in manifest["required"]
    body = (root / expected).read_bytes()
    assert len(body) == declared[expected]["bytes"]
    assert hashlib.sha256(body).hexdigest() == declared[expected]["sha256"]
    shipped = load_workflow_action_contract(root / expected)
    assert frozenset(shipped) == ALLOWED_TOOLS
    assert shipped["publish_ontology_package"].actor_field == "approved_by"
    assert shipped["revoke_ontology_release"].actor_field == "revoked_by"`);
  }
  if (path === "tests/unit/test_wren_project.py") {
    replace("import json\n", "import json\nimport os\n");
    replace('WREN = ROOT / ".orion-runtime/wren/0.15.0/.venv/bin/python"',
      'WREN = Path(os.environ["ORION_WREN_PYTHON"]) if os.environ.get("ORION_WREN_PYTHON") else None');
    replace(`    if not WREN.is_file():
        pytest.skip("pinned Wren runtime not installed")`,
      `    if WREN is None or not WREN.is_absolute() or not WREN.is_file():
        pytest.fail("Install the separate pinned Wren environment and set ORION_WREN_PYTHON; real worker safety tests are required")`);
  }
  return Buffer.from(source);
}

export async function runtimeRegular(root, path) {
  runtimePath(path);
  let current = resolve(root);
  if ((await lstat(current)).isSymbolicLink()) throw new Error("Runtime source root must not be a symlink");
  for (const part of path.split("/")) {
    current = join(current, part);
    if ((await lstat(current)).isSymbolicLink()) throw new Error(`Runtime source symlink rejected: ${path}`);
  }
  const info = await lstat(current);
  if (!info.isFile() || info.size > 32 * 1024 * 1024) throw new Error(`Runtime input must be a bounded regular file: ${path}`);
  const body = await readFile(current);
  assertRuntimeContent(path, body);
  return body;
}

export async function runtimeSourceFiles(root) {
  root = resolve(root);
  const files = new Map();
  const copy = async path => files.set(path, await runtimeRegular(root, path));
  const scan = async (prefix, accepts) => {
    const directory = join(root, prefix), info = await lstat(directory);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error(`Runtime directory must be regular: ${prefix}`);
    for (const entry of (await readdir(directory, { withFileTypes: true })).sort((a, b) => runtimePathOrder(a.name, b.name))) {
      if (["__pycache__", ".DS_Store"].includes(entry.name)) continue;
      const path = `${prefix}/${entry.name}`;
      runtimePath(path);
      if (entry.isSymbolicLink()) throw new Error(`Runtime source symlink rejected: ${path}`);
      if (entry.isDirectory()) await scan(path, accepts);
      else if (entry.isFile() && accepts(path)) await copy(path);
    }
  };
  await scan("services", path => path.endsWith(".py"));
  for (const entry of await readdir(join(root, "harness"), { withFileTypes: true })) {
    if (entry.name.endsWith(".py")) await copy(`harness/${entry.name}`);
  }
  for (const path of [...RUNTIME_SCRIPTS, ...RUNTIME_RESOURCES]) await copy(path);
  await scan("database/migrations/orion_workflow", path => path.endsWith(".sql"));
  await scan("harness/ontology-templates", () => true);
  await scan("harness/skills/orion-ontology-engineer", path => path.endsWith(".md"));
  for (const id of ["engineering", "ontology-qa"]) for (const name of ["persona.txt", "preset.yml"]) await copy(`harness/profile/agent-presets/${id}/${name}`);
  return files;
}

export function fingerprintRuntimeFiles(files) {
  const records = [...files].map(([path, body]) => {
    assertRuntimeContent(path, body);
    return { path, bytes: body.length, sha256: runtimeSha256(body) };
  }).sort((a, b) => runtimePathOrder(a.path, b.path));
  const digest = createHash("sha256");
  for (const record of records) digest.update(record.path).update("\0").update(record.sha256).update("\n");
  return { algorithm: "sha256-path-content-v1", sha256: digest.digest("hex"), fileCount: records.length,
    scope: ["runtime source/resource allowlist including Makefile, scripts, migrations, templates, skills and report assets"], files: records };
}

export function createRuntimeManifest(files, runtimeVersion, { required = RUNTIME_REQUIRED } = {}) {
  if (!/^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$/.test(runtimeVersion)) throw new Error("Invalid runtime version");
  for (const path of required) if (!files.has(runtimePath(path))) throw new Error(`Required runtime input missing: ${path}`);
  const fingerprint = fingerprintRuntimeFiles(files);
  const group = prefix => fingerprint.files.filter(item => item.path.startsWith(prefix)).map(item => item.path);
  return { schemaVersion: 1, runtimeVersion, algorithm: fingerprint.algorithm, fingerprint: fingerprint.sha256,
    files: fingerprint.files, required: [...required], resources: { migrations: group("database/"),
      templates: group("harness/ontology-templates/"), reports: group("services/ontology_engineering/report_assets/"),
      contracts: group("harness/contracts/"), scripts: group("scripts/"), skills: group("harness/skills/") },
    license: "ORION-owned source is private; third-party template and resource notices are retained.",
    dependencies: { infrastructureBundled: false, coreAndWrenSeparateEnvironments: true, wheelEquivalenceVerified: false } };
}

export async function verifyRuntimeManifest(root, manifest) {
  if (manifest?.schemaVersion !== 1 || manifest.algorithm !== "sha256-path-content-v1" || !Array.isArray(manifest.files) || !Array.isArray(manifest.required) || !/^[a-f0-9]{64}$/.test(manifest.fingerprint ?? "")) throw new Error("Invalid runtime source manifest");
  const files = new Map();
  for (const record of manifest.files) {
    const path = runtimePath(record.path);
    if (files.has(path) || !Number.isSafeInteger(record.bytes) || record.bytes < 0 || !/^[a-f0-9]{64}$/.test(record.sha256 ?? "")) throw new Error("Invalid runtime file record");
    const body = await runtimeRegular(root, path);
    if (body.length !== record.bytes || runtimeSha256(body) !== record.sha256) throw new Error(`Runtime source drift: ${path}`);
    files.set(path, body);
  }
  for (const path of manifest.required) if (!files.has(runtimePath(path))) throw new Error(`Required runtime input missing: ${path}`);
  if (fingerprintRuntimeFiles(files).sha256 !== manifest.fingerprint) throw new Error("Runtime source fingerprint mismatch");
  return files;
}

export const SAFE_RUNTIME_MAKEFILE = `# Only explicit ORION runtime entry points. No credential discovery or old app launchers.
PYTHON ?= python3
ONTOLOGY_PROJECT_ID ?=
ONTOLOGY_WORKFLOW_HOME ?= $(ORION_WORKFLOW_HOME)
PROTEGE_MCP_SECRET ?=
PROTEGE_CONSTRUCTION_APPLICATION ?=
PROTEGE_ROUTING_FILE ?=
SEMANTICA_CLI ?=
SEMANTICA_PYTHON ?=
SEMANTICA_MCP_COMMAND ?=
SEMANTICA_ACTIVE_RUNTIME_CONFIG ?=
SEMANTICA_SYNC_PYTHON ?= $(PYTHON)
SEMANTICA_SYNC_SCRIPT ?= $(CURDIR)/scripts/sync_published_ontologies_to_semantica.py
export PYTHON SEMANTICA_CLI SEMANTICA_PYTHON SEMANTICA_MCP_COMMAND SEMANTICA_ACTIVE_RUNTIME_CONFIG SEMANTICA_SYNC_PYTHON SEMANTICA_SYNC_SCRIPT

.PHONY: build-manifest-check runtime-check protege-construction-route semantica-runtime ontology-s5 ontology-s6
build-manifest-check runtime-check:
\t@"$(PYTHON)" scripts/orion_runtime_manifest.py --check

protege-construction-route: build-manifest-check
\t@test -n "$(PROTEGE_CONSTRUCTION_APPLICATION)" -a -n "$(PROTEGE_ROUTING_FILE)" -a -n "$(PROTEGE_MCP_SECRET)" || { echo "Configure the explicit Protege application, routing file and MCP secret path." >&2; exit 2; }
\t@test -s "$(PROTEGE_MCP_SECRET)" || { echo "Configured Protege MCP secret is unavailable." >&2; exit 2; }
\t@"$(PYTHON)" scripts/protege_role_router.py ensure --role construction --application "$(PROTEGE_CONSTRUCTION_APPLICATION)" --routing-file "$(PROTEGE_ROUTING_FILE)" --secret "$(PROTEGE_MCP_SECRET)"

semantica-runtime: build-manifest-check
\t@test -n "$(SEMANTICA_CLI)" -a -n "$(SEMANTICA_PYTHON)" -a -n "$(SEMANTICA_MCP_COMMAND)" -a -n "$(SEMANTICA_ACTIVE_RUNTIME_CONFIG)" || { echo "Configure the external Semantica CLI, Python, MCP command and explicit Profile runtime config." >&2; exit 2; }
\t@test -s "$(SEMANTICA_ACTIVE_RUNTIME_CONFIG)" || { echo "Configured Semantica runtime selection is unavailable." >&2; exit 2; }
\t@scripts/ensure_semantica_runtime.sh ensure

ontology-s5: protege-construction-route
\t@test -n "$(ONTOLOGY_PROJECT_ID)" -a -n "$(ONTOLOGY_WORKFLOW_HOME)" || { echo "A project and Profile workflow home are required." >&2; exit 2; }
\t@"$(PYTHON)" scripts/run_protege_build_stage.py "$(ONTOLOGY_PROJECT_ID)" --workflow-home "$(ONTOLOGY_WORKFLOW_HOME)" --secret "$(PROTEGE_MCP_SECRET)" --routing-file "$(PROTEGE_ROUTING_FILE)" --lease-owner orion-platform

ontology-s6: build-manifest-check semantica-runtime
\t@test -n "$(ONTOLOGY_PROJECT_ID)" -a -n "$(ONTOLOGY_WORKFLOW_HOME)" || { echo "A project and Profile workflow home are required." >&2; exit 2; }
\t@"$(PYTHON)" scripts/run_quality_validation_stage.py "$(ONTOLOGY_PROJECT_ID)" --workflow-home "$(ONTOLOGY_WORKFLOW_HOME)"
`;
