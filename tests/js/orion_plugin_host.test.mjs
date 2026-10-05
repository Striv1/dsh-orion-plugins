import { pathToFileURL as orionPathToFileURL, fileURLToPath as orionFileURLToPath } from 'node:url';
import { resolve as orionResolve } from 'node:path';
const orionOfficialRuntimeRoot = orionResolve(process.env.ORION_DSH_RUNTIME_ROOT || orionFileURLToPath(new URL('../../', import.meta.url)));
const orionOfficialRuntimeBase = orionPathToFileURL(orionOfficialRuntimeRoot + '/');
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, writeFile, symlink, rm, realpath } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Readable } from "node:stream";
import { existsSync } from "node:fs";
import { ASSET_ROUTE_PREFIX, assetPath, runtimeConfig } from "../../harness/plugins/orion-workbench/index.js";
import { nativePluginWriteRejection } from "../../harness/plugins/branded-web-runtime/lib/native-plugin-access.js";
import { serveOntologyQaApi, serveWorkflowApi, serveDocumentIngestionApi, startDocumentJob } from "../../harness/plugins/branded-web-runtime/index.js";

const officialServer = new URL("node_modules/@deepseek-ai/dsh-host-webserver/lib/index.js", orionOfficialRuntimeBase);
test("official WebServer routes packaged brand, manifest and nested assets and releases the prefix on disposal", {
  skip: !existsSync(officialServer) && "Install the pinned 0.2.0-rc.2 SDK to run the actual routing contract",
}, async () => {
  const { WebServer } = await import(officialServer.href);
  // Exercise the vendor's register/match implementation without listening or
  // activating any Harness service. A prefix ending in '/' misses all URLs.
  const server = Object.create(WebServer.prototype);
  server.exact = new Map();
  server.prefixes = new Map();
  const route = { kind: "prefix", path: ASSET_ROUTE_PREFIX, handler() {} };
  const dispose = server.register(route);
  for (const path of ["/orion-workbench-assets/ahs-icon-black.svg", "/orion-workbench-assets/ahs-wordmark-white.svg",
    "/orion-workbench-assets/workbench-assets.json", "/orion-workbench-assets/modules/ontology-center/index.js"]) {
    assert.equal(server.match(path), route, path);
  }
  assert.equal(server.match("/orion-workbench-assets-private/credentials"), undefined);
  assert.equal(server.match("/assets/ahs-icon-black.svg"), undefined);
  dispose();
  assert.equal(server.match("/orion-workbench-assets/ahs-icon-black.svg"), undefined);
});

test("assets can be read inside the package, but encoded traversal and escaping symlinks cannot", async () => {
  const temporary = await mkdtemp(join(tmpdir(), "orion-plugin-assets-"));
  const root = join(temporary, "assets");
  try {
    await mkdir(root);
    await writeFile(join(root, "logo.svg"), "<svg/>");
    await writeFile(join(temporary, "private.json"), "private");
    await symlink(join(temporary, "private.json"), join(root, "outside.json"));
    assert.equal(await assetPath("/orion-workbench-assets/logo.svg", root), await realpath(join(root, "logo.svg")));
    for (const path of ["/orion-workbench-assets/%2e%2e/private.json", "/orion-workbench-assets/%2Fprivate.json",
      "/orion-workbench-assets/outside.json", "/assets/logo.svg", "/orion-workbench-assets/"]) {
      await assert.rejects(assetPath(path, root));
    }
  } finally { await rm(temporary, { recursive: true, force: true }); }
});

test("missing backend is explicit and defaults point at the selected Home, never the checkout data", () => {
  const config = runtimeConfig({}, { home: "/tmp/orion-private-home", root: "/tmp/plugin" });
  assert.equal(config.nativePlugin, true);
  assert.equal(config.backendConfigured, false);
  assert.equal(config.workflowApiEnabled, false);
  assert.equal(config.documentIngestionApiEnabled, false);
  assert.equal(config.workflowHome, "/tmp/orion-private-home/orion-workflows");
  assert.equal(config.workflowCwd, null);
  assert.equal(config.readOnly, true);
  assert.equal(config.workflowActor, null);
});

test("native read-only mode rejects every business write method; enabling writes requires an actor", () => {
  for (const method of ["POST", "PUT", "PATCH", "DELETE"]) {
    for (const path of ["/orion-workflow-api/projects", "/orion-document-api/jobs", "/orion-mcp-api/settings"]) {
      assert.match(nativePluginWriteRejection({ nativePlugin: true }, method, path), /只读/);
      assert.match(nativePluginWriteRejection({ nativePlugin: true, readOnly: false }, method, path), /操作人/);
      assert.equal(nativePluginWriteRejection({ nativePlugin: true, readOnly: false, workflowActor: "acceptance-user" }, method, path), null);
      assert.equal(nativePluginWriteRejection({ readOnly: true }, method, path), null);
    }
  }
  assert.equal(nativePluginWriteRejection({ nativePlugin: true }, "GET", "/orion-workflow-api/projects"), null);
  assert.equal(nativePluginWriteRejection({ nativePlugin: true }, "POST", "/orion-ontology-qa-api/bind"), null);
});

test("approval and document writes reject missing configured identity even when the caller supplies an actor", async () => {
  const root = await mkdtemp(join(tmpdir(), "orion-actor-rejection-"));
  try {
    for (const workflowActor of [undefined, "", "  ", 42, "operator\nother"]) {
      for (const [handler, path, payload] of [
        [serveWorkflowApi, "/orion-workflow-api/confirmation", { decided_by: "forged-operator" }],
        [serveWorkflowApi, "/orion-workflow-api/action", {
          tool: "record_s0_scope_decision", arguments: { project_id: "project-test", decided_by: "forged-operator" },
        }],
        [serveDocumentIngestionApi, "/orion-document-api/jobs", { actor: "forged-operator" }],
        [serveDocumentIngestionApi, "/orion-document-api/jobs/JOB-TEST-00000001/cancel", {}],
        [serveDocumentIngestionApi, "/orion-document-api/jobs/JOB-TEST-00000001/commit", { reviewed_by: "forged-operator" }],
      ]) {
        const request = Readable.from([Buffer.from(JSON.stringify(payload))]);
        Object.assign(request, { method: "POST", url: path, headers: { "x-orion-workflow-confirmation": "1" } });
        let result;
        const response = { writeHead(status) { this.status = status; }, end(body) { result = JSON.parse(body); } };
        await handler(request, response, {
          workflowApiEnabled: true, workflowHome: join(root, "workflows"), workflowActor,
          workflowActionContract: orionFileURLToPath(new URL("../../harness/contracts/workflow-ui-actions.json", import.meta.url)),
          documentIngestionApiEnabled: true, documentIngestionRoot: join(root, "documents"),
        });
        assert.equal(response.status, 403, path);
        assert.match(result.detail, /操作人/);
      }
    }
    assert.equal(existsSync(join(root, "documents")), false);
    assert.equal(existsSync(join(root, "workflows")), false);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("document queue records the configured operator and never creates a job without one", async () => {
  const root = await mkdtemp(join(tmpdir(), "orion-actor-queue-"));
  const config = { workflowHome: join(root, "workflows"), documentIngestionRoot: join(root, "documents") };
  try {
    await assert.rejects(startDocumentJob(config, { actor: "forged-operator", source_path: "input.txt" }),
      error => error.code === "ACTOR_REQUIRED");
    assert.equal(existsSync(config.documentIngestionRoot), false);
    const queued = await startDocumentJob({ ...config, workflowActor: "test-operator" },
      { actor: "forged-operator", source_path: "input.txt" });
    const jobRoot = join(config.documentIngestionRoot, ".orion-s0-jobs", queued.job_id);
    const request = JSON.parse(await readFile(join(jobRoot, "request.json"), "utf8"));
    assert.equal(request.actor, "test-operator");
    assert.equal(queued.status, "QUEUED");
    const runner = JSON.parse(await readFile(join(jobRoot, "runner.json"), "utf8"));
    assert.equal(runner.pid, null);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("read-only analytics never forwards a save or baseline mutation, while list/get/search still read", async () => {
  const session = "session-11111111-1111-4111-8111-111111111111";
  const binding = { session_id: session, project_id: "ontology-project-acceptance", realtime_ready: true,
    release_version: "1", release_fingerprint: "sha256:" + "a".repeat(64) };
  const state = { ready: Promise.resolve(), bindings: new Map([[session, binding]]) };
  const calls = [];
  const previous = globalThis.fetch;
  globalThis.fetch = async (_url, options) => {
    calls.push(JSON.parse(options.body));
    return { ok: true, status: 200, json: async () => ({ session_id: session, release_match: "verified",
      expected_release: { project_id: binding.project_id, release_version: binding.release_version,
        release_fingerprint: binding.release_fingerprint }, assets: [] }) };
  };
  const request = async (action) => {
    const req = Readable.from([Buffer.from(JSON.stringify({ session_id: session, action }))]);
    Object.assign(req, { method: "POST", url: "/orion-ontology-qa-api/analytics/assets", headers: {} });
    let result;
    const res = { writeHead(status) { this.status = status; }, end(body) { result = JSON.parse(body); } };
    await serveOntologyQaApi(req, res, { nativePlugin: true, readOnly: true, realtimeQaApiUrl: "http://isolated-core" }, state);
    return { status: res.status, result };
  };
  try {
    for (const action of ["remember", "knowledge", "report", "evaluation", "evaluate", "unknown"]) {
      const response = await request(action);
      assert.equal(response.status, 403);
      assert.match(response.result.detail, /只读/);
    }
    assert.equal(calls.length, 0);
    for (const action of ["list", "get", "search"]) assert.equal((await request(action)).status, 200);
    assert.deepEqual(calls.map(item => item.action), ["list", "get", "search"]);
    assert.ok(calls.every(item => item.expected_release.release_fingerprint === binding.release_fingerprint));
  } finally { globalThis.fetch = previous; }
});
