import { pathToFileURL as orionPathToFileURL, fileURLToPath as orionFileURLToPath } from 'node:url';
import { resolve as orionResolve } from 'node:path';
const orionOfficialRuntimeRoot = orionResolve(process.env.ORION_DSH_RUNTIME_ROOT || orionFileURLToPath(new URL('../../', import.meta.url)));
const orionOfficialRuntimeBase = orionPathToFileURL(orionOfficialRuntimeRoot + '/');
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { existsSync } from 'node:fs';
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { createServer } from 'node:net';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { apply, createRuntimeManager, managedRuntimeEnvironment, managedRuntimePaths, managedWorkflowEnvironment, verifyRuntimeSource } from '../../harness/plugins/orion-workbench/lib/runtime-manager.js';
import { runtimeConfig } from '../../harness/plugins/orion-workbench/index.js';

const sdk = new URL("node_modules/@deepseek-ai/", orionOfficialRuntimeBase);
const installed = { skip: !existsSync(new URL('cordis/lib/index.js', sdk)) && 'Install the pinned official Cordis/subprocess fixture' };
const version = JSON.parse(await readFile(new URL('../../harness/plugins/orion-workbench/package.json', import.meta.url), 'utf8')).version;
const hash = body => createHash('sha256').update(body).digest('hex');
const shellQuote = value => "'" + value.replace(/'/g, "'\\''") + "'";

async function freePort() {
  const server = createServer();
  await new Promise(done => server.listen(0, '127.0.0.1', done));
  const { port } = server.address();
  await new Promise(done => server.close(done));
  return port;
}
async function fixture(t, overrides = {}) {
  const root = await realpath(await mkdtemp(join(tmpdir(), 'orion-owned-runtime-')));
  t.after(() => rm(root, { recursive: true, force: true }));
  const runtimeRoot = join(root, 'source'), profileRoot = join(root, 'profile'), python = join(profileRoot, '.venvs/core/bin/python');
  const required = ['pyproject.toml', 'services/realtime_qa/api.py', 'harness/orion_workflow_mcp.py',
    'services/ontology_engineering/workflow.py',
    'harness/orion_workflow_action.py', 'harness/realtime_qa_mcp.py', 'scripts/build_ontology_graph.py', 'Makefile',
    'harness/contracts/workflow-ui-actions.json', 'database/migrations/orion_workflow/001_baseline.sql',
    'database/migrations/orion_workflow/002_realtime_document_current.sql'];
  const files = [];
  for (const path of required.slice().sort()) {
    const body = Buffer.from('fixture:' + path + '\n');
    await mkdir(dirname(join(runtimeRoot, path)), { recursive: true });
    await writeFile(join(runtimeRoot, path), body);
    files.push({ path, bytes: body.length, sha256: hash(body) });
  }
  const fingerprint = createHash('sha256');
  const code = createHash('sha256');
  for (const row of files) {
    fingerprint.update(row.path).update('\0').update(row.sha256).update('\n');
    if (row.path.startsWith('services/') && row.path.endsWith('.py')) code.update(row.path.slice('services/'.length)).update('\0').update(await readFile(join(runtimeRoot, row.path))).update('\0');
  }
  const codeFingerprint = 'sha256:' + code.digest('hex');
  const manifest = { schemaVersion: 1, runtimeVersion: version, algorithm: 'sha256-path-content-v1',
    fingerprint: fingerprint.digest('hex'), files, required };
  await mkdir(join(runtimeRoot, 'contracts'), { recursive: true });
  await writeFile(join(runtimeRoot, 'contracts/runtime-source-manifest.json'), JSON.stringify(manifest));
  await mkdir(dirname(python), { recursive: true });
  await writeFile(join(profileRoot, '.venvs/core/pyvenv.cfg'), 'test fixture\n');
  // This deterministic protocol fixture replaces Python/Core, while Cordis
  // guards, actual native spawning, port ownership and range disposal are real.
  await writeFile(python + '.cjs', `
const fs = require('node:fs'), http = require('node:http'), path = require('node:path');
if (process.argv.includes('-c')) {
  console.log(JSON.stringify({code_fingerprint:process.env.ORION_TEST_FINGERPRINT}));
} else {
  const registry = process.env.ORION_REALTIME_RUNTIME_REGISTRY;
  fs.writeFileSync(path.join(path.dirname(registry),'observed-child.json'),JSON.stringify({pid:process.pid,
    workflowHome:process.env.ORION_WORKFLOW_HOME, cache:process.env.ORION_WREN_CACHE_ROOT,
    ambientBusiness:process.env.ORION_UNRELATED_PROJECT || null, ambientSecret:process.env.ORION_TEST_SECRET || null}));
  if (process.env.ORION_TEST_NO_HEALTH === '1') setInterval(()=>{},1000);
  else http.createServer((req,res)=>{
    res.setHeader('Content-Type','application/json');
    res.end(JSON.stringify({service:process.env.ORION_TEST_WRONG_HEALTH === '1' ? 'foreign' : 'orion-core-realtime-qa',
      code_fingerprint:process.env.ORION_TEST_FINGERPRINT, registry_path:registry,
      status:fs.existsSync(path.join(path.dirname(registry),'fixture-health-ready')) ? 'READY' : 'DEGRADED',
      readiness_reason:fs.existsSync(path.join(path.dirname(registry),'fixture-health-ready')) ? null : 'NO_PUBLISHED_RUNTIME',registered_runtime_count:0}));
  }).listen(Number(process.argv.at(-1)),'127.0.0.1');
}
`);
  await writeFile(python, `#!/bin/sh\nexec ${shellQuote(process.execPath)} ${shellQuote(python + '.cjs')} "$@"\n`);
  await chmod(python, 0o700);
  const config = { mode: 'managed', runtimeRoot, profileRoot, python, port: await freePort(),
    startupTimeoutMs: 3500, graceMs: 100, stopTimeoutMs: 3000,
    environment: { ORION_TEST_FINGERPRINT: codeFingerprint }, ...overrides };
  return { root, runtimeRoot, profileRoot, python, manifest, codeFingerprint, config };
}
async function officialContext(t) {
  const [{ Context }, { LocalSubprocessRuntime }] = await Promise.all([
    import(new URL('cordis/lib/index.js', sdk)), import(new URL('dsh-subprocess-local/lib/index.js', sdk)),
  ]);
  const root = new Context();
  const unprovide = root.provide('logger', { warn() {}, debug() {}, info() {}, error() {} });
  let provider;
  const fiber = root.plugin({ inject: ['logger'], apply(ctx) { provider = new LocalSubprocessRuntime(ctx); } });
  await fiber;
  t.after(async () => { await fiber.dispose(); unprovide(); });
  return { root, provider };
}

test('external facade works without a subprocess provider and guarded consumers can read it', installed, async t => {
  const { Context } = await import(new URL('cordis/lib/index.js', sdk));
  const root = new Context(), fiber = root.plugin({ apply });
  await fiber;
  let status;
  const consumer = root.plugin({ inject: ['orionRuntime'], apply(ctx) { status = ctx.orionRuntime.status(); } });
  await consumer;
  assert.equal(status.mode, 'external');
  assert.equal(status.state, 'EXTERNAL');
  await consumer.dispose(); await fiber.dispose();
  assert.equal(root.get('orionRuntime'), undefined);
});

test('missing subprocess capability leaves a failed facade available and cannot enable an unverified backend', installed, async t => {
  const f = await fixture(t, { startupTimeoutMs: 100 });
  const { Context } = await import(new URL('cordis/lib/index.js', sdk));
  const root = new Context(), fiber = root.plugin({ apply }, f.config);
  await fiber;
  const facade = root.get('orionRuntime');
  assert.equal(facade.status().reason, 'START_TIMEOUT');
  assert.equal(runtimeConfig({ backendRoot: f.runtimeRoot }, { runtime: facade }).backendConfigured, false);
  assert.equal(existsSync(join(f.profileRoot, 'state/runtime-registry.json')), false);
  await fiber.dispose();
});

test('source verification checks versions, required resources, actual contents and escaping links', async t => {
  const f = await fixture(t);
  assert.equal((await verifyRuntimeSource(f.runtimeRoot, version)).codeFingerprint, f.codeFingerprint);
  await assert.rejects(verifyRuntimeSource(f.runtimeRoot, 'different-version'), /源码或版本清单/);
  const path = join(f.runtimeRoot, 'Makefile');
  await writeFile(path, 'tampered');
  await assert.rejects(verifyRuntimeSource(f.runtimeRoot, version), /源码或版本清单/);
  await rm(path);
  await writeFile(join(f.root, 'outside'), 'fixture:Makefile\n');
  await symlink(join(f.root, 'outside'), path);
  await assert.rejects(verifyRuntimeSource(f.runtimeRoot, version), /源码或版本清单/);
});

test('managed paths separate source, Profile state and cache and cannot forward ambient secrets', () => {
  const config = { runtimeRoot: '/tmp/source', profileRoot: '/tmp/profile', python: '/tmp/profile/.venvs/core/bin/python', port: 19099 };
  const paths = managedRuntimePaths(config);
  const env = managedRuntimeEnvironment(paths, { ORION_SOURCE_DATABASE_URL: 'explicit-reader-config' },
    { ORION_WORKFLOW_HOME: '/another/profile', ORION_TEST_SECRET: 'test-value', DSH_HOME: '/private/home', PYTHONPATH: '/old/source', PATH: '/bin' });
  assert.equal(env.ORION_WORKFLOW_HOME, '/tmp/profile/state/workflows');
  assert.equal(env.ORION_REALTIME_RUNTIME_REGISTRY, '/tmp/profile/state/runtime-registry.json');
  assert.equal(env.ORION_WREN_PYTHON, '/tmp/profile/.venvs/wren/bin/python');
  assert.equal(env.ORION_TEST_SECRET, undefined);
  assert.equal(env.DSH_HOME, undefined);
  assert.equal(env.PYTHONPATH, undefined);
  assert.equal(env.PYTHON, '/tmp/profile/.venvs/core/bin/python');
  assert.equal(env.ORION_SOURCE_DATABASE_URL, 'explicit-reader-config');
  for (const changed of [{ stateRoot: '/tmp/other' }, { cacheRoot: '/tmp/profile/state/cache' },
    { runtimeRoot: '/tmp/profile/source' }, { python: '/usr/bin/python3' }, { port: 80 }]) {
    assert.throws(() => managedRuntimePaths({ ...config, ...changed }), /配置不完整/);
  }
});

test('real execFile retains executable lookup and locale with a complete string environment, without ambient credentials or business configuration', async () => {
  const paths = managedRuntimePaths({ runtimeRoot: '/tmp/source', profileRoot: '/tmp/profile',
    python: '/tmp/profile/.venvs/core/bin/python', port: 19991 });
  const ambient = { PATH: process.env.PATH, HOME: process.env.HOME, TMPDIR: tmpdir(), LANG: 'en_US.UTF-8',
    ORION_WORKFLOW_HOME: '/other-project', ORION_TEST_SECRET: 'test-value', DSH_HOME: '/private-home',
    DEEPSEEK_API_KEY: 'test-value', UNRELATED_CONFIG: 'private-config' };
  const env = managedWorkflowEnvironment(paths, {}, ambient);
  assert.ok(Object.values(env).every(value => typeof value === 'string'));
  const { stdout } = await promisify(execFile)(process.execPath, ['-e', `console.log(JSON.stringify({
    path:process.env.PATH,home:process.env.HOME,language:process.env.LANG,workflow:process.env.ORION_WORKFLOW_HOME,
    hasPrivate:['ORION_TEST_SECRET','DSH_HOME','DEEPSEEK_API_KEY','UNRELATED_CONFIG'].some(key=>key in process.env)
  }))`], { env });
  const observed = JSON.parse(stdout);
  assert.equal(observed.path, ambient.PATH);
  assert.equal(observed.home, ambient.HOME);
  assert.equal(observed.language, ambient.LANG);
  assert.equal(observed.workflow, '/tmp/profile/state/workflows');
  assert.equal(observed.hasPrivate, false);
});

test('managed Core and workflow children share only explicitly configured service endpoints', installed, async t => {
  const f = await fixture(t), { provider } = await officialContext(t);
  const paths = managedRuntimePaths(f.config);
  const endpoints = {
    CHAT2DB_ENDPOINT: 'http://other-profile.invalid/chat2db',
    FUSEKI_URL: 'http://other-profile.invalid/fuseki',
    ONTOP_URL: 'http://other-profile.invalid/ontop',
    REDIS_URL: 'redis://other-profile.invalid/3',
  };
  const previous = Object.fromEntries(Object.keys(endpoints).map(key => [key, process.env[key]]));
  t.after(() => {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  Object.assign(process.env, endpoints);
  for (const configured of [{}, { FUSEKI_URL: 'http://selected-profile.invalid/fuseki', CHAT2DB_ENDPOINT: 'http://selected-profile.invalid/chat2db' }]) {
    const environment = managedRuntimeEnvironment(paths, configured);
    const workflowEnvironment = managedWorkflowEnvironment(paths, configured);
    const child = provider.spawn({
      argv: [process.execPath, '-e', `console.log(JSON.stringify(Object.fromEntries(${JSON.stringify(Object.keys(endpoints))}.map(key => [key, process.env[key] || null]))))`],
      cwd: f.root, env: environment, graceMs: 100,
      stdio: { stdin: 'ignore', stdout: { maxBytes: 4096 }, stderr: { maxBytes: 4096 } },
    });
    t.after(async () => { child.terminate(); await child.waitForExit(); await child.done.catch(() => {}); });
    assert.equal((await child.done).exitCode, 0);
    assert.equal(await child.waitForExit(), true);
    const observed = JSON.parse(child.collected.stdout.readFrom(0).text);
    for (const key of Object.keys(endpoints)) {
      assert.equal(observed[key], configured[key] || null, `${key} must not come from another Profile`);
      assert.equal(workflowEnvironment[key] || null, observed[key], `${key} must agree across Core and workflow children`);
    }
  }
});

test('the actual official overlay and Loader interpolation keep managed MCP paths in the same Profile and preserve external bindings', installed, async () => {
  const [{ loadOverlayPatches }, { interpolate }] = await Promise.all([
    import(new URL('dsh-app-boot/lib/index.js', sdk)), import(new URL('cordis-plugin-loader/lib/index.js', sdk)),
  ]);
  const patches = loadOverlayPatches('orion-test', fileURLToPath(new URL('../../harness/plugins/orion-workbench/cordis.patch.yml', import.meta.url)));
  const rows = patches.flatMap(patch => patch.insert || []);
  const env = { ORION_RUNTIME_MODE: 'managed', ORION_RUNTIME_ROOT: '/tmp/independent-source',
    ORION_WORKFLOW_PYTHON: '/tmp/independent-profile/.venvs/core/bin/python', ORION_RUNTIME_PROFILE_ROOT: '/tmp/independent-profile',
    ORION_RUNTIME_STATE_ROOT: '/tmp/independent-profile/owned-state', ORION_RUNTIME_CACHE_ROOT: '/tmp/independent-profile/owned-cache',
    ORION_RUNTIME_PORT: '19299', ORION_ENABLE_REALTIME_QA_MCP: '1' };
  const evaluate = environment => interpolate({ process: { env: environment }, dshHomePath: (...parts) => join('/tmp/test-home', ...parts) }, rows);
  const managed = evaluate(env), manager = managed.find(row => row.id === 'orion-runtime-manager');
  const paths = managedRuntimePaths(manager.config), expected = managedRuntimeEnvironment(paths, manager.config.environment, {});
  const workflow = managed.find(row => row.id === 'mcp-orion-workflow');
  assert.equal(workflow.disabled, false);
  assert.equal(workflow.config.cwd, paths.runtimeRoot);
  assert.equal(workflow.config.command, paths.python);
  for (const key of ['ORION_WORKFLOW_HOME','ORION_DOCUMENT_INPUT_ROOT','ORION_DOCUMENT_INGESTION_ROOT','ORION_REALTIME_RUNTIME_REGISTRY',
    'ORION_REALTIME_EVIDENCE_ROOT','ORION_WORKSPACE_REFERENCE_ROOTS','ORION_EXPLORATION_INDEX_ROOT','ORION_S7_AUTO_DEPLOY_ROOT',
    'ORION_S7_AUTO_DEPLOY_CONFIG_ROOT','SEMANTICA_ACTIVE_RUNTIME_CONFIG','PYTHONPYCACHEPREFIX']) {
    assert.equal(workflow.config.env[key], expected[key], key);
  }
  assert.equal(managed.find(row => row.id === 'mcp-orion-realtime').config.env.ONTOLOGY_AGENT_API_URL, paths.realtimeQaApiUrl);
  const external = evaluate({ ORION_BACKEND_ROOT: '/tmp/existing-source', ORION_WORKFLOW_HOME: '/tmp/existing-workflows',
    ORION_REALTIME_RUNTIME_REGISTRY: '/tmp/existing-registry.json', ORION_S7_AUTO_DEPLOY_ROOT: '/tmp/existing-deployments' });
  assert.equal(external.find(row => row.id === 'orion-runtime-manager').config.mode, 'external');
  const prior = external.find(row => row.id === 'mcp-orion-workflow');
  assert.equal(prior.config.cwd, '/tmp/existing-source');
  assert.equal(prior.config.env.ORION_WORKFLOW_HOME, '/tmp/existing-workflows');
  assert.equal(prior.config.env.ORION_REALTIME_RUNTIME_REGISTRY, '/tmp/existing-registry.json');
  assert.equal(prior.config.env.ORION_S7_AUTO_DEPLOY_ROOT, '/tmp/existing-deployments');
});

test('official Cordis and local subprocess start an owned Core, accept an empty catalog and join it on consumer disable', installed, async t => {
  const f = await fixture(t), { root, provider } = await officialContext(t);
  process.env.ORION_UNRELATED_PROJECT = '/another-project'; process.env.ORION_TEST_SECRET = 'test-value';
  t.after(() => { delete process.env.ORION_UNRELATED_PROJECT; delete process.env.ORION_TEST_SECRET; });
  const unrelated = provider.spawn({ argv: [process.execPath, '-e', 'setInterval(()=>{},1000)'], cwd: f.root,
    stdio: { stdin: 'ignore', stdout: { maxBytes: 64 }, stderr: { maxBytes: 64 } }, graceMs: 100 });
  t.after(async () => { unrelated.terminate(); await unrelated.waitForExit(); await unrelated.done.catch(()=>{}); });
  const fiber = root.plugin({ apply }, f.config);
  t.after(() => fiber.dispose());
  await fiber;
  const facade = root.get('orionRuntime');
  assert.equal(facade.status().state, 'RUNNING');
  assert.equal(facade.status().coreReady, true);
  assert.equal(facade.status().qaReady, false);
  assert.equal(facade.status().reason, 'NO_PUBLISHED_RUNTIME');
  assert.equal(JSON.stringify(facade.status()).includes(f.profileRoot), false);
  const settings = runtimeConfig({ backendRoot: '/obsolete-source' }, { runtime: facade });
  assert.equal(settings.workflowCwd, f.runtimeRoot);
  assert.equal(settings.backendConfigured, true);
  assert.equal(settings.workflowEnvironment.ORION_WORKFLOW_HOME, join(f.profileRoot, 'state/workflows'));
  assert.deepEqual(JSON.parse(await readFile(join(f.profileRoot, 'state/runtime-registry.json'))), { schema_version: 1, default_project_id: null, runtimes: [] });
  const observed = JSON.parse(await readFile(join(f.profileRoot, 'state/observed-child.json')));
  assert.equal(observed.ambientBusiness, null); assert.equal(observed.ambientSecret, null);
  await writeFile(join(f.profileRoot, 'state/fixture-health-ready'), '1');
  assert.equal((await facade.refreshStatus()).qaReady, true, 'readiness is refreshed after a later publication rather than fixed at startup');
  await fiber.dispose();
  assert.equal(facade.status().state, 'STOPPED');
  assert.equal(root.get('orionRuntime'), undefined);
  assert.throws(() => process.kill(observed.pid, 0), error => error.code === 'ESRCH');
  assert.equal(await unrelated.waitForExit(AbortSignal.timeout(30)), false, 'disabling the consumer never disposes a different service child');
  assert.equal(await readFile(join(f.runtimeRoot, 'Makefile'), 'utf8'), 'fixture:Makefile\n');
});

test('occupied ports and invalid existing registries fail without adoption or file replacement', installed, async t => {
  const f = await fixture(t), { provider } = await officialContext(t);
  const server = createServer(); await new Promise(done => server.listen(f.config.port, '127.0.0.1', done));
  const blocked = createRuntimeManager(f.config, { runtimeVersion: version });
  await blocked.start(provider);
  assert.equal(blocked.status().reason, 'PORT_IN_USE');
  assert.equal(server.listening, true);
  await new Promise(done => server.close(done));
  await mkdir(join(f.profileRoot, 'state'), { recursive: true });
  const existing = JSON.stringify({ schema_version: 1, default_project_id: 'old-project', runtimes: [] });
  await writeFile(join(f.profileRoot, 'state/runtime-registry.json'), existing);
  const invalid = createRuntimeManager(f.config, { runtimeVersion: version });
  await invalid.start(provider);
  assert.equal(invalid.status().reason, 'REGISTRY_INVALID');
  assert.equal(await readFile(join(f.profileRoot, 'state/runtime-registry.json'), 'utf8'), existing);
  assert.equal(existsSync(join(f.profileRoot, 'state/observed-child.json')), false);
});

test('a health identity mismatch and startup timeout terminate the spawned range without exposing config', installed, async t => {
  const { provider } = await officialContext(t);
  for (const [extra, reason] of [[{ ORION_TEST_WRONG_HEALTH: '1' }, 'HEALTH_MISMATCH'], [{ ORION_TEST_NO_HEALTH: '1' }, 'START_TIMEOUT']]) {
    const f = await fixture(t);
    f.config.environment = { ...f.config.environment, ...extra };
    if (reason === 'START_TIMEOUT') f.config.startupTimeoutMs = 500;
    const manager = createRuntimeManager(f.config, { runtimeVersion: version });
    await manager.start(provider);
    assert.equal(manager.status().state, 'FAILED');
    assert.equal(manager.status().reason, reason);
    const observed = JSON.parse(await readFile(join(f.profileRoot, 'state/observed-child.json')));
    assert.throws(() => process.kill(observed.pid, 0), error => error.code === 'ESRCH');
    assert.equal(JSON.stringify(manager.status()).includes(f.profileRoot), false);
    assert.equal(existsSync(join(f.profileRoot, 'state/core-manager.lock')), false);
  }
});

test('a buffered healthy reply cannot mark a Core that exited during startup as running', installed, async t => {
  const f = await fixture(t), { provider } = await officialContext(t);
  let core;
  const ownedProvider = { spawn(spec) {
    const child = provider.spawn(spec);
    if (spec.argv.includes('services.realtime_qa.api')) core = child;
    return child;
  } };
  const manager = createRuntimeManager(f.config, { runtimeVersion: version, fetch: async (url, options) => {
    const response = await fetch(url, options);
    const health = await response.json();
    // The reply was valid when sent, but its owned process has exited before
    // startup consumes it. Await actual process-range exit, not a fake PID.
    core.terminate();
    await core.done;
    assert.equal(await core.waitForExit(), true);
    return { ok: true, json: async () => health };
  } });
  t.after(() => manager.dispose());
  await manager.start(ownedProvider);
  assert.equal(manager.status().state, 'FAILED');
  assert.equal(manager.status().reason, 'START_FAILED');
  assert.equal(manager.status().coreReady, false);
  assert.equal(manager.settings().backendValidated, undefined);
  assert.equal(existsSync(join(f.profileRoot, 'state/core-manager.lock')), false);
});
