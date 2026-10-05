import { createHash, randomUUID } from 'node:crypto';
import { constants } from 'node:fs';
import { access, lstat, mkdir, open, readFile, realpath, unlink } from 'node:fs/promises';
import { createServer } from 'node:net';
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';

export const name = 'orion-runtime-manager';
// The facade must exist even when the subprocess provider is unavailable.
// Only the managed child fiber injects the official process service.
export const inject = [];
const SERVICE_ID = 'orion-core-realtime-qa';
const MANIFEST_PATH = 'contracts/runtime-source-manifest.json';
const REQUIRED = ['pyproject.toml', 'services/realtime_qa/api.py', 'harness/orion_workflow_mcp.py',
  'services/ontology_engineering/workflow.py',
  'harness/orion_workflow_action.py', 'harness/realtime_qa_mcp.py', 'scripts/build_ontology_graph.py',
  'Makefile', 'harness/contracts/workflow-ui-actions.json',
  'database/migrations/orion_workflow/001_baseline.sql', 'database/migrations/orion_workflow/002_realtime_document_current.sql'];
const MESSAGES = {
  CONFIG_INVALID: '本地运行时配置不完整，或状态目录不属于当前配置的独立目录。',
  SOURCE_INVALID: '运行时源码或版本清单校验失败，请重新准备匹配版本的独立运行时。',
  PYTHON_INVALID: 'Core Python 环境未通过校验，请先完成独立运行时安装检查。',
  REGISTRY_INVALID: '已有发布运行时注册表未通过校验；原文件已保留。',
  STATE_IN_USE: '当前运行时目录正在使用，请先关闭所属实例。',
  PORT_IN_USE: '指定端口已经被占用，插件未接管该服务。',
  START_FAILED: '本地 Core 服务未能启动；请查看独立运行时安装检查结果。',
  HEALTH_MISMATCH: '启动服务的身份或注册表不匹配；插件已停止自己启动的服务。',
  START_TIMEOUT: '本地 Core 服务启动超时，插件正在清理自己启动的服务。',
  STOP_FAILED: '本地 Core 服务尚未确认完全退出，请先完成运行时退出检查。',
};
class RuntimeError extends Error { constructor(code) { super(MESSAGES[code]); this.code = code; } }
const fail = code => { throw new RuntimeError(code); };
const digest = body => createHash('sha256').update(body).digest('hex');
const contains = (root, target) => { const part = relative(root, target); return part === '' || (!isAbsolute(part) && part !== '..' && !part.startsWith('../') && !part.startsWith('..\\')); };
const absolute = value => { if (typeof value !== 'string' || !isAbsolute(value)) fail('CONFIG_INVALID'); return resolve(value); };

export function managedRuntimePaths(config) {
  const runtimeRoot = absolute(config.runtimeRoot), python = absolute(config.python), profileRoot = absolute(config.profileRoot);
  const stateRoot = absolute(config.stateRoot || join(profileRoot, 'state'));
  const cacheRoot = absolute(config.cacheRoot || join(profileRoot, 'cache'));
  const port = Number(config.port);
  if (!Number.isInteger(port) || port < 1024 || port > 65535 || !contains(profileRoot, stateRoot)
    || !contains(profileRoot, cacheRoot) || stateRoot === profileRoot || cacheRoot === profileRoot
    || contains(stateRoot, cacheRoot) || contains(cacheRoot, stateRoot)
    || contains(runtimeRoot, profileRoot) || contains(profileRoot, runtimeRoot)
    || !contains(join(profileRoot, '.venvs/core'), python) || !/[/\\]bin[/\\]python(?:3(?:\.\d+)?)?$/.test(python)) fail('CONFIG_INVALID');
  return { runtimeRoot, python, profileRoot, stateRoot, cacheRoot, port,
    workflowHome: join(stateRoot, 'workflows'), documentIngestionRoot: join(stateRoot, 'document-inputs'),
    referenceRoot: join(stateRoot, 'references'), registryPath: join(stateRoot, 'runtime-registry.json'),
    realtimeQaApiUrl: `http://127.0.0.1:${port}` };
}

// Every ambient business/runtime variable is removed unless explicitly opted
// in. The official subprocess service also scrubs credentials and DSH_* facts.
export function managedRuntimeEnvironment(paths, configured = {}, ambient = process.env) {
  const environment = {};
  for (const key of Object.keys(ambient)) {
    if (/^(?:ORION_|SEMANTICA_|PROTEGE_|LLM_|DATABASE_|MINIO_|PYTHON|DSH_)/i.test(key) || /KEY|PASSWORD|SECRET|TOKEN/i.test(key)) environment[key] = undefined;
  }
  for (const [key, value] of Object.entries(configured)) {
    if (!/^(?:ORION_|SEMANTICA_|PROTEGE_|CHAT2DB_|LLM_|DATABASE_URL$|FUSEKI_URL$|ONTOP_URL$|REDIS_URL$|MINIO_)/.test(key)
      || typeof value !== 'string' || value.includes('\0')) fail('CONFIG_INVALID');
    environment[key] = value;
  }
  return Object.assign(environment, {
    PYTHON: paths.python, ORION_WORKFLOW_PYTHON: paths.python,
    PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1', PYTHONPYCACHEPREFIX: join(paths.cacheRoot, 'pycache'),
    ORION_RUNTIME_PROFILE_ROOT: paths.profileRoot, ORION_RUNTIME_STATE_ROOT: paths.stateRoot, ORION_RUNTIME_CACHE_ROOT: paths.cacheRoot,
    ORION_PROJECT_ROOT: paths.runtimeRoot, ORION_WORKFLOW_HOME: paths.workflowHome,
    ORION_DOCUMENT_INGESTION_ROOT: paths.documentIngestionRoot, ORION_DOCUMENT_INPUT_ROOT: paths.documentIngestionRoot,
    ORION_WORKSPACE_REFERENCE_ROOTS: paths.referenceRoot, ORION_REALTIME_RUNTIME_REGISTRY: paths.registryPath,
    ORION_REALTIME_EVIDENCE_ROOT: join(paths.stateRoot, 'evidence'), ORION_EXPLORATION_INDEX_ROOT: join(paths.cacheRoot, 'exploration-index'),
    ORION_ANALYSIS_ASSET_ROOT: join(paths.stateRoot, 'analysis-assets'), ORION_ANALYTICS_EXPORT_ROOT: join(paths.stateRoot, 'analytics-exports'),
    ORION_WREN_CACHE_ROOT: join(paths.cacheRoot, 'wren-projects'), ORION_WREN_SOURCE_REGISTRY_ROOT: join(paths.stateRoot, 'wren-sources'),
    ORION_WREN_PYTHON: join(paths.profileRoot, '.venvs/wren/bin/python'),
    ORION_S7_AUTO_DEPLOY_CONFIG_ROOT: join(paths.stateRoot, 'ontop-config'), ORION_S7_AUTO_DEPLOY_ROOT: join(paths.stateRoot, 'realtime-business'),
    SEMANTICA_ACTIVE_RUNTIME_CONFIG: join(paths.stateRoot, 'semantica/active-runtime.json'),
    SEMANTICA_STATE_DIR: join(paths.stateRoot, 'semantica/runtime'), SEMANTICA_EXPLORER_STATE_DIR: join(paths.stateRoot, 'semantica/explorer'),
    SEMANTICA_GRAPH_PATH: join(paths.stateRoot, 'semantica/explorer/graph.pkl'), SEMANTICA_EXPLORER_LEGACY_GRAPH: join(paths.stateRoot, 'semantica/explorer/legacy-graph.pkl'),
    ONTOLOGY_AGENT_API_URL: paths.realtimeQaApiUrl,
  });
}

export function managedWorkflowEnvironment(paths, configured = {}, ambient = process.env) {
  const environment = {};
  // execFile replaces its whole environment, unlike the official subprocess
  // overlay. Preserve executable lookup, home and locale without inheriting
  // unrelated runtime configuration or credential-shaped entries.
  for (const key of ['PATH', 'HOME', 'TMPDIR', 'TMP', 'TEMP', 'LANG', 'LC_ALL', 'LC_CTYPE', 'USER', 'LOGNAME', 'TZ', 'NO_PROXY', 'no_proxy']) {
    if (typeof ambient[key] === 'string') environment[key] = ambient[key];
  }
  for (const [key, value] of Object.entries(managedRuntimeEnvironment(paths, configured, ambient))) {
    if (typeof value === 'string') environment[key] = value;
  }
  return environment;
}

async function regularWithin(root, part) {
  if (typeof part !== 'string' || !part || isAbsolute(part) || part.includes('\\') || part.split('/').some(piece => !piece || piece === '.' || piece === '..')) fail('SOURCE_INVALID');
  const file = join(root, part), info = await lstat(file);
  if (!info.isFile() || info.isSymbolicLink() || !contains(root, await realpath(file))) fail('SOURCE_INVALID');
  return file;
}

export async function verifyRuntimeSource(runtimeRoot, runtimeVersion) {
  try {
    const root = await realpath(runtimeRoot), manifest = JSON.parse(await readFile(await regularWithin(root, MANIFEST_PATH), 'utf8'));
    if (manifest.schemaVersion !== 1 || manifest.algorithm !== 'sha256-path-content-v1' || manifest.runtimeVersion !== runtimeVersion
      || !Array.isArray(manifest.files) || !manifest.files.length || !Array.isArray(manifest.required)) fail('SOURCE_INVALID');
    const files = new Map(), aggregate = createHash('sha256'), core = createHash('sha256');
    for (const row of manifest.files.slice().sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0)) {
      if (!/^[a-f0-9]{64}$/.test(row.sha256) || !Number.isSafeInteger(row.bytes) || row.bytes < 0 || files.has(row.path)) fail('SOURCE_INVALID');
      const body = await readFile(await regularWithin(root, row.path));
      if (body.length !== row.bytes || digest(body) !== row.sha256) fail('SOURCE_INVALID');
      files.set(row.path, row);
      aggregate.update(row.path).update('\0').update(row.sha256).update('\n');
      if (row.path.startsWith('services/') && row.path.endsWith('.py')) core.update(row.path.slice('services/'.length)).update('\0').update(body).update('\0');
    }
    if (aggregate.digest('hex') !== String(manifest.fingerprint).replace(/^sha256:/, '')) fail('SOURCE_INVALID');
    for (const required of [...REQUIRED, ...manifest.required]) if (!files.has(required)) fail('SOURCE_INVALID');
    return { runtimeVersion: manifest.runtimeVersion, fingerprint: manifest.fingerprint, codeFingerprint: `sha256:${core.digest('hex')}` };
  } catch (error) { if (error instanceof RuntimeError) throw error; fail('SOURCE_INVALID'); }
}

async function secureDirectory(root, directory) {
  if (!contains(root, directory)) fail('CONFIG_INVALID');
  const actualRoot = await realpath(root);
  let cursor = root;
  for (const part of relative(root, directory).split(/[/\\]/).filter(Boolean)) {
    cursor = join(cursor, part);
    try { await mkdir(cursor, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
    const info = await lstat(cursor);
    if (!info.isDirectory() || info.isSymbolicLink() || !contains(actualRoot, await realpath(cursor))) fail('CONFIG_INVALID');
  }
}
async function canonicalCandidate(directory) {
  try { return await realpath(directory); }
  catch (error) { if (error.code !== 'ENOENT') throw error; return join(await canonicalCandidate(dirname(directory)), directory.slice(dirname(directory).length + 1)); }
}
async function initializeRegistry(paths) {
  await secureDirectory(paths.profileRoot, paths.stateRoot);
  await secureDirectory(paths.profileRoot, paths.cacheRoot);
  await secureDirectory(paths.profileRoot, paths.workflowHome);
  let file;
  try {
    file = await open(paths.registryPath, 'wx', 0o600);
    await file.writeFile(JSON.stringify({ schema_version: 1, default_project_id: null, runtimes: [] }) + '\n');
  } catch (error) { if (error.code !== 'EEXIST') throw error; }
  finally { await file?.close(); }
  const info = await lstat(paths.registryPath);
  if (!info.isFile() || info.isSymbolicLink()) fail('REGISTRY_INVALID');
  let value;
  try { value = JSON.parse(await readFile(paths.registryPath, 'utf8')); } catch { fail('REGISTRY_INVALID'); }
  if (value.schema_version !== 1 || !Array.isArray(value.runtimes) || value.runtimes.length > 100) fail('REGISTRY_INVALID');
  const enabled = value.runtimes.filter(row => row?.enabled !== false).map(row => row?.project_id);
  if (enabled.some(id => typeof id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{2,159}$/.test(id))
    || new Set(enabled).size !== enabled.length || (value.default_project_id != null && !enabled.includes(value.default_project_id))) fail('REGISTRY_INVALID');
}
export async function assertPortAvailable(port) {
  await new Promise((done, reject) => {
    const server = createServer();
    server.once('error', () => reject(new RuntimeError('PORT_IN_USE')));
    server.listen({ host: '127.0.0.1', port, exclusive: true }, () => server.close(error => error ? reject(new RuntimeError('PORT_IN_USE')) : done()));
  });
}
const PREFLIGHT = `import importlib.metadata,json,re,sys,tomllib
from pathlib import Path
from services.realtime_qa.models import RealtimeRuntimeRegistryConfig
from services.realtime_qa.code_identity import service_code_fingerprint
assert (3,11) <= sys.version_info[:2] < (3,14)
assert Path(sys.prefix).resolve() == Path(sys.argv[1]).resolve()
project=tomllib.loads(Path('pyproject.toml').read_text())['project']
for dep in project['dependencies']:
 match=re.fullmatch(r'([^=\\[]+)(?:\\[[^\\]]+\\])?==([^; ]+)',dep)
 assert match and importlib.metadata.version(match[1]) == match[2]
RealtimeRuntimeRegistryConfig.model_validate_json(Path(sys.argv[2]).read_text())
print(json.dumps({'code_fingerprint':service_code_fingerprint()}))`;

export function createRuntimeManager(config = {}, { runtimeVersion, fetch: request = globalThis.fetch } = {}) {
  const mode = config.mode || 'external', handles = new Set(), abort = new AbortController();
  let settings = {}, state = { mode, state: mode === 'external' ? 'EXTERNAL' : 'STARTING', coreReady: false, qaReady: false, reason: null };
  let finish, started, stopping, lock, startupTimer, healthIdentity;
  const ready = new Promise(resolveReady => { finish = resolveReady; });
  const timeoutMs = Math.min(60000, Math.max(100, Number(config.startupTimeoutMs) || 15000));
  const graceMs = Math.min(10000, Math.max(100, Number(config.graceMs) || 1500));
  const waitMs = Math.min(20000, Math.max(graceMs + 500, Number(config.stopTimeoutMs) || 6000));
  const setFailure = error => {
    const reason = error instanceof RuntimeError ? error.code : 'START_FAILED';
    state = { ...state, state: 'FAILED', coreReady: false, qaReady: false, reason, detail: MESSAGES[reason] };
  };
  const settle = () => { clearTimeout(startupTimer); finish(); };
  async function releaseLock() {
    if (!lock) return;
    const current = lock; lock = null;
    try {
      if (JSON.parse(await readFile(current.path, 'utf8')).owner === current.owner) await unlink(current.path);
    } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
  async function terminateChildren() {
    const pending = [...handles];
    for (const child of pending) child.terminate();
    const signal = AbortSignal.timeout(waitMs);
    const results = await Promise.allSettled(pending.map(async child => {
      if (!await child.waitForExit(signal)) fail('STOP_FAILED');
      await child.done.catch(() => {});
      handles.delete(child);
    }));
    if (results.some(result => result.status === 'rejected')) fail('STOP_FAILED');
    await releaseLock();
  }
  function spawn(provider, paths, environment, argv) {
    abort.signal.throwIfAborted();
    const child = provider.spawn({ argv, cwd: paths.runtimeRoot, env: environment, graceMs, signal: abort.signal,
      stdio: { stdin: 'ignore', stdout: { maxBytes: 8192 }, stderr: { maxBytes: 8192 } } });
    handles.add(child);
    child.done.catch(() => {});
    return child;
  }
  async function startManaged(provider) {
    let paths = managedRuntimePaths(config);
    const source = await verifyRuntimeSource(paths.runtimeRoot, runtimeVersion);
    abort.signal.throwIfAborted();
    const actualSource = await realpath(paths.runtimeRoot), actualProfile = await canonicalCandidate(paths.profileRoot);
    if (contains(actualSource, actualProfile) || contains(actualProfile, actualSource)) fail('CONFIG_INVALID');
    const rootInfo = await lstat(paths.profileRoot);
    if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) fail('CONFIG_INVALID');
    try { await access(paths.python, constants.X_OK); await access(join(dirname(dirname(paths.python)), 'pyvenv.cfg')); }
    catch { fail('PYTHON_INVALID'); }
    paths = managedRuntimePaths({ ...config, runtimeRoot: actualSource, profileRoot: actualProfile,
      stateRoot: await canonicalCandidate(paths.stateRoot), cacheRoot: await canonicalCandidate(paths.cacheRoot),
      python: join(await realpath(dirname(paths.python)), basename(paths.python)) });
    await secureDirectory(paths.profileRoot, paths.stateRoot);
    const venv = dirname(dirname(paths.python));
    await assertPortAvailable(paths.port);
    const lockPath = join(paths.stateRoot, 'core-manager.lock'), owner = randomUUID();
    let descriptor;
    try { descriptor = await open(lockPath, 'wx', 0o600); }
    catch { fail('STATE_IN_USE'); }
    lock = { path: lockPath, owner };
    try { await descriptor.writeFile(JSON.stringify({ owner, pid: process.pid }) + '\n'); }
    finally { await descriptor.close(); }
    await initializeRegistry(paths);
    const environment = managedRuntimeEnvironment(paths, config.environment);
    const probe = spawn(provider, paths, environment, [paths.python, '-s', '-B', '-c', PREFLIGHT, venv, paths.registryPath]);
    const result = await probe.done;
    if (!await probe.waitForExit(abort.signal)) fail('PYTHON_INVALID');
    handles.delete(probe);
    let identity;
    try { identity = JSON.parse(probe.collected.stdout.readFrom(0).text); } catch { fail('PYTHON_INVALID'); }
    if (result.exitCode !== 0 || identity?.code_fingerprint !== source.codeFingerprint) fail('PYTHON_INVALID');
    // Repeat the free-port check immediately before spawning. Never adopt even
    // a same-code service: only the handle returned by this spawn is owned.
    await assertPortAvailable(paths.port);
    const core = spawn(provider, paths, environment, [paths.python, '-s', '-B', '-m', 'services.realtime_qa.api', '--host', '127.0.0.1', '--port', String(paths.port)]);
    let exited = false;
    core.done.then(() => { exited = true; if (state.state === 'RUNNING') setFailure(new RuntimeError('START_FAILED')); }, () => { exited = true; if (state.state === 'RUNNING') setFailure(new RuntimeError('START_FAILED')); });
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      abort.signal.throwIfAborted();
      if (exited) fail('START_FAILED');
      let health;
      try {
        const response = await request(paths.realtimeQaApiUrl + '/health', { signal: AbortSignal.any([abort.signal, AbortSignal.timeout(1000)]), redirect: 'error' });
        if (response.ok) health = await response.json();
      } catch { abort.signal.throwIfAborted(); }
      if (health) {
        if (health.service !== SERVICE_ID || health.code_fingerprint !== source.codeFingerprint
          || resolve(String(health.registry_path || '.')) !== paths.registryPath || health.reload_error || health.error
          || !['READY', 'DEGRADED'].includes(health.status)) fail('HEALTH_MISMATCH');
        settings = { backendRoot: paths.runtimeRoot, python: paths.python, workflowHome: paths.workflowHome,
          documentIngestionRoot: paths.documentIngestionRoot, realtimeQaApiUrl: paths.realtimeQaApiUrl,
          workflowEnvironment: managedWorkflowEnvironment(paths, config.environment), backendValidated: true };
        healthIdentity = { url: paths.realtimeQaApiUrl, registryPath: paths.registryPath, codeFingerprint: source.codeFingerprint };
        state = { mode, state: 'RUNNING', coreReady: true, qaReady: health.status === 'READY',
          reason: health.readiness_reason || null, runtimeVersion: source.runtimeVersion, fingerprint: source.fingerprint };
        return;
      }
      await sleep(100, undefined, { signal: abort.signal });
    }
    fail('START_TIMEOUT');
  }
  const facade = {
    ready,
    status: () => ({ ...state }),
    async refreshStatus() {
      if (state.state !== 'RUNNING' || abort.signal.aborted || !healthIdentity) return { ...state };
      let health;
      try {
        const response = await request(healthIdentity.url + '/health', { signal: AbortSignal.any([abort.signal, AbortSignal.timeout(1000)]), redirect: 'error' });
        if (response.ok) health = await response.json();
      } catch { /* A temporary transport failure does not grant restart or ownership. */ }
      if (state.state !== 'RUNNING' || abort.signal.aborted) return { ...state };
      const valid = health?.service === SERVICE_ID && health.code_fingerprint === healthIdentity.codeFingerprint
        && resolve(String(health.registry_path || '.')) === healthIdentity.registryPath && !health.reload_error && !health.error
        && ['READY', 'DEGRADED'].includes(health.status);
      state = { ...state, coreReady: Boolean(valid), qaReady: Boolean(valid && health.status === 'READY'),
        reason: valid ? health.readiness_reason || null : 'CORE_UNAVAILABLE' };
      return { ...state };
    },
    settings: () => ({ ...settings, ...(settings.workflowEnvironment ? { workflowEnvironment: { ...settings.workflowEnvironment } } : {}) }),
    start(provider) {
      if (mode !== 'managed') return ready;
      if (started || abort.signal.aborted) return started || ready;
      started = startManaged(provider).catch(async error => {
        if (!stopping) setFailure(abort.signal.aborted && state.reason === 'START_TIMEOUT' ? new RuntimeError('START_TIMEOUT') : error);
        abort.abort();
        try { await terminateChildren(); } catch (cleanupError) { setFailure(cleanupError); }
      }).finally(settle);
      return started;
    },
    async dispose() {
      if (stopping) return stopping;
      stopping = (async () => {
        clearTimeout(startupTimer); abort.abort();
        try {
          for (const child of handles) child.terminate();
          await started;
          await terminateChildren();
          state = { ...state, state: 'STOPPED', coreReady: false, qaReady: false };
        } catch (error) { setFailure(error); throw new RuntimeError('STOP_FAILED'); }
        finally { settle(); }
      })();
      return stopping;
    },
  };
  if (mode === 'external') {
    settings = { ...(config.runtimeRoot ? { backendRoot: resolve(config.runtimeRoot) } : {}), ...(config.python ? { python: config.python } : {}) };
    settle();
  } else if (mode !== 'managed') { setFailure(new RuntimeError('CONFIG_INVALID')); settle(); }
  else startupTimer = setTimeout(() => {
    setFailure(new RuntimeError('START_TIMEOUT')); abort.abort();
    if (!started) settle();
  }, timeoutMs);
  return facade;
}

export async function apply(ctx, config = {}) {
  const { version } = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  const manager = createRuntimeManager(config, { runtimeVersion: version });
  ctx.provide('orionRuntime', manager);
  ctx.effect(() => () => manager.dispose(), 'orion managed Core teardown');
  if (config.mode === 'managed') ctx.inject(['subprocess'], connected => {
    connected.effect(() => () => manager.dispose(), 'orion Core provider teardown');
    return manager.start(connected.subprocess);
  });
  await manager.ready;
}
