import assert from "node:assert/strict";
import { mkdtemp, mkdir, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { loadWorkflowDashboard } from "../../harness/plugins/branded-web-runtime/index.js";
import { createDocumentApi } from "../../harness/plugins/branded-web-runtime/lib/document-api.js";
import { loadRuntimeService, runtimeServiceSourcePaths } from "../../harness/plugins/branded-web-runtime/lib/runtime-service.js";

const save = async (path, value) => {
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, JSON.stringify(value));
};
const temporary = async (t) => {
  const root = await mkdtemp(join(tmpdir(), "orion-profile-environment-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return root;
};

test("document progress and dashboard cache follow each configured Profile", async (t) => {
  const root = await temporary(t);
  const home = join(root, "workflows");
  const project = "ontology-project-profile-test", job = "JOB-12345678-ABCDEF";
  await save(join(home, project, "workflow-state.json"), { project_id: project,
    project_status: "IN_PROGRESS", revision: 1, current_stage: "S0", stage_statuses: { S0: "RUNNING" } });
  await save(join(home, project, "project.json"), { project_id: project, intake_mode: "DOCUMENT_ONLY" });
  await save(join(home, project, ".s0-document-job.json"), { job_id: job, project_revision: 1 });
  const environments = [];
  for (const [name, completed] of [["a", 1], ["b", 2]]) {
    const input = join(root, name, "document-inputs");
    const directory = join(input, ".orion-s0-jobs", job);
    await save(join(directory, "request.json"), { project_id: project });
    await save(join(directory, "status.json"), { job_id: job, status: "RUNNING", completed_files: completed });
    environments.push({ ORION_DOCUMENT_INGESTION_ROOT: input,
      ORION_REALTIME_RUNTIME_REGISTRY: join(root, name, "runtime-registry.json") });
  }
  const a = await loadWorkflowDashboard(home, project, { environment: environments[0] });
  const b = await loadWorkflowDashboard(home, project, { environment: environments[1] });
  const again = await loadWorkflowDashboard(home, project, { environment: environments[0] });
  assert.equal(a.state.document_ingestion_job.completed_files, 1);
  assert.equal(b.state.document_ingestion_job.completed_files, 2);
  assert.equal(again.state.document_ingestion_job.completed_files, 1);
  assert.notEqual(a.dashboardRevision, b.dashboardRevision);
  assert.equal(a.state.document_ingestion_job.storage_paths.input_root, environments[0].ORION_DOCUMENT_INGESTION_ROOT);
});

test("release service reads only the selected Profile registry", async (t) => {
  const root = await temporary(t);
  const home = join(root, "workflows"), projectRoot = join(home, "ontology-project-a");
  const configured = join(root, "selected", "runtime-registry.json");
  const binding = join(root, "selected", "binding.json"), compose = join(root, "selected", "compose.yml");
  await save(configured, { runtimes: [{ project_id: "ontology-project-a", deployment_binding_path: binding }] });
  await save(binding, { project_id: "ontology-project-a", release_version: "1.0.0",
    compose_path: compose, endpoint: "http://127.0.0.1:18081/sparql" });
  await writeFile(compose, "name: profile-runtime-a\nservices: {}\n");
  const environment = { ORION_REALTIME_RUNTIME_REGISTRY: configured };
  const result = await loadRuntimeService({ workflowHome: home, projectRoot,
    publication: { release_version: "1.0.0" }, environment });
  assert.equal(result.compose_project, "profile-runtime-a");
  assert.equal(result.status, "BOUND");
  assert.deepEqual(await runtimeServiceSourcePaths(home, projectRoot, environment), [configured, binding, compose]);
  assert.equal((await loadRuntimeService({ workflowHome: home, projectRoot,
    publication: { release_version: "1.0.0" }, environment: {} })).kind, "unknown");
});

test("document route subprocess uses explicit Profile environment and pycache", async (t) => {
  const root = await temporary(t), input = join(root, "document-inputs");
  await mkdir(input);
  let observed;
  const handler = createDocumentApi({
    executeFile: async (command, args, options) => {
      observed = { command, args, options };
      return { stdout: JSON.stringify({ router: "fixture", version: "1", categories: [] }) };
    },
    loadWorkflowDashboard, probeMcpHttp: async () => ({ ok: false, detail: "not configured" }),
    runWorkflowTool: async () => { throw new Error("A read must not call a workflow mutation"); },
    safeProjectId: value => value,
  });
  const environment = { PATH: "/fixture-path", PYTHONPYCACHEPREFIX: join(root, "cache", "pycache") };
  const response = { writeHead() {}, end(body) { this.body = JSON.parse(body); } };
  await handler({ method: "GET", url: "/orion-document-api/status", headers: {} }, response, {
    documentIngestionApiEnabled: true, documentIngestionRoot: input, workflowHome: join(root, "workflows"),
    workflowCwd: root, workflowPython: "/fixture-python", workflowEnvironment: environment,
  });
  assert.equal(observed.command, "/fixture-python");
  assert.deepEqual(observed.options.env, environment);
  assert.equal(observed.options.cwd, root);
  assert(response.body);
});
