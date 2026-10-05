import test from 'node:test';
import assert from 'node:assert/strict';
import { createWorkbenchAssetLoader, mountWorkbench, validateAssetManifest } from '../../harness/plugins/orion-workbench/lib/workbench-page.js';

const manifest = () => ({ schemaVersion: 1, mode: 'core', assetBase: '/orion-workbench-assets/',
  scripts: ['/orion-workbench-assets/state.js', '/orion-workbench-assets/center.js'],
  styles: ['/orion-workbench-assets/brand.css', '/orion-workbench-assets/workbench.css'] });

function environment({ failPath = null, response = () => ({ ok: true, json: async () => manifest() }) } = {}) {
  const log = [], existing = { native: true }, children = [existing];
  const window = { __OWA_PRODUCT_MODE__: 'previous' };
  const document = { createElement(tag) {
    return { tag, dataset: {}, remove() { const index = children.indexOf(this); if (index >= 0) children.splice(index, 1); } };
  }, head: { append(node) {
    children.push(node);
    log.push(node.href ?? node.src);
    queueMicrotask(() => { if ((node.href ?? node.src) === failPath) node.onerror?.(); else node.onload?.(); });
  } } };
  let requests = 0;
  const fetch = async (url, options) => {
    requests++;
    assert.equal(url, '/orion-workbench-assets/workbench-assets.json');
    assert.equal(options.credentials, 'same-origin');
    assert.equal(options.headers.Accept, 'application/json');
    return response();
  };
  return { window, document, fetch, log, existing, children, requests: () => requests,
    setFailure(path) { failPath = path; } };
}

test('manifest accepts the builder schemaVersion and rejects unsupported schema, type, empty scripts and unsafe paths', () => {
  const valid = manifest();
  assert.equal(validateAssetManifest(valid), valid);
  const invalid = [null, {}, { ...valid, version: 1, schemaVersion: undefined }, { ...valid, schemaVersion: 2 },
    { ...valid, mode: 'legacy' }, { ...valid, assetBase: '/assets/' }, { ...valid, scripts: [] },
    { ...valid, scripts: ['/orion-workbench-assets/style.css'] },
    { ...valid, styles: ['/orion-workbench-assets/entry.js'] },
    { ...valid, scripts: ['/orion-workbench-assets/../other.js'] },
    { ...valid, scripts: ['https://example.com/entry.js'] },
    { ...valid, scripts: ['/orion-workbench-assets/%2e%2e/other.js'] },
    { ...valid, scripts: ['/orion-workbench-assets/a\\b.js'] },
    { ...valid, scripts: [null] }, { ...valid, scripts: [valid.scripts[0], valid.scripts[0]] }];
  for (const value of invalid) assert.throws(() => validateAssetManifest(value), /资源/);
});

test('asset load is authenticated, cached and preserves stylesheet-before-ordered-script execution', async () => {
  const env = environment(), load = createWorkbenchAssetLoader(env);
  const first = load(), second = load();
  assert.equal(first, second);
  assert.deepEqual(await first, manifest());
  assert.deepEqual(env.log, [...manifest().styles, ...manifest().scripts]);
  assert.equal(env.window.__ORION_NATIVE_PLUGIN__, true);
  assert.equal(env.window.__OWA_PRODUCT_MODE__, 'core');
  assert.equal(env.requests(), 1);
  assert.equal(env.children[0], env.existing);
  for (const node of env.children.slice(1)) {
    assert.equal(node.dataset.orionWorkbenchAsset, node.href ?? node.src);
    assert.equal(node.onload, null);
    assert.equal(node.onerror, null);
    if (node.tag === 'script') assert.equal(node.async, false);
  }
  await load();
  assert.equal(env.requests(), 1);
});

test('failed resource load removes only owned nodes, restores flags and allows a fresh complete retry', async () => {
  const env = environment({ failPath: manifest().scripts[1] });
  env.window.__ORION_NATIVE_PLUGIN__ = false;
  const load = createWorkbenchAssetLoader(env);
  await assert.rejects(load(), /center\.js/);
  assert.deepEqual(env.children, [env.existing]);
  assert.equal(env.window.__ORION_NATIVE_PLUGIN__, false);
  assert.equal(env.window.__OWA_PRODUCT_MODE__, 'previous');
  env.setFailure(null);
  await load();
  assert.equal(env.requests(), 2);
  assert.deepEqual(env.log.slice(-4), [...manifest().styles, ...manifest().scripts]);
  assert.equal(env.children.length, 5);
});

test('manifest read and validation failures append no nodes and remain retryable', async () => {
  const responses = [{ ok: false, status: 401 }, { ok: true, json: async () => ({ ...manifest(), scripts: [] }) },
    { ok: true, json: async () => manifest() }];
  const env = environment({ response: () => responses.shift() }), load = createWorkbenchAssetLoader(env);
  await assert.rejects(load(), /401/);
  await assert.rejects(load(), /资源/);
  assert.deepEqual(env.children, [env.existing]);
  assert.equal(env.window.__ORION_NATIVE_PLUGIN__, undefined);
  assert.equal(env.window.__OWA_PRODUCT_MODE__, 'previous');
  await load();
  assert.equal(env.requests(), 3);
});

test('disable removes owned styles while retaining loaded scripts, and re-enable reloads only styles without rerunning IIFEs', async () => {
  const env = environment(), load = createWorkbenchAssetLoader(env);
  await load();
  const scripts = env.children.filter(node => node.tag === 'script');
  load.releaseStyles();
  load.releaseStyles();
  assert.deepEqual(env.children, [env.existing, ...scripts]);
  await load();
  assert.equal(env.requests(), 1, 'same-page complete script identity is retained');
  assert.deepEqual(env.log.slice(-2), manifest().styles);
  assert.equal(env.children.filter(node => node.tag === 'script').length, 2);
  assert.equal(env.children.filter(node => node.tag === 'link').length, 2);
  load.releaseStyles();
  env.setFailure(manifest().styles[1]);
  await assert.rejects(load(), /workbench\.css/);
  assert.deepEqual(env.children, [env.existing, ...scripts]);
  env.setFailure(null);
  await load();
  assert.equal(env.children.filter(node => node.tag === 'script').length, 2);
  assert.equal(env.children.filter(node => node.tag === 'link').length, 2);
});

test('disable cancels pending owned loads and obsolete completion cannot interfere with re-enable', async () => {
  const env = environment(), load = createWorkbenchAssetLoader(env);
  const originalAppend = env.document.head.append;
  env.document.head.append = node => { env.children.push(node); env.log.push(node.href ?? node.src); };
  const pending = load();
  await new Promise(resolve => setImmediate(resolve));
  const oldNodes = env.children.slice(1);
  load.releaseStyles();
  await assert.rejects(pending, /停用/);
  assert.deepEqual(env.children, [env.existing]);
  env.document.head.append = originalAppend;
  await load();
  assert.equal(env.children.length, 5);
  assert.equal(env.window.__ORION_NATIVE_PLUGIN__, true);
  for (const node of oldNodes) { assert.equal(node.onload, null); assert.equal(node.onerror, null); }
});

function mounted({ activate = () => true } = {}) {
  const root = { owned: 'main' }, navigation = { owned: 'navigation' }, log = [], errors = [];
  let callback;
  const parts = { root, navigation,
    state: { subscribe(listener) { callback = listener; listener({ primary: 'ontology', section: 'engineering' }); return () => log.push('unsubscribe'); } },
    center: { activate(route, nav) { assert.equal(nav, navigation); log.push(route.section); return activate(route, nav); }, dispose() { log.push('center.dispose'); } },
    engineering: { mount(node) { assert.equal(node, root); log.push('mount'); return () => log.push('release'); } },
    layout: { selectPanel(panel) { log.push(['panel', panel]); } }, onError(error) { errors.push(error); } };
  return { parts, root, navigation, log, errors, emit(route) { callback(route); } };
}

test('owned workbench switches routes, releases subscriptions and disposes idempotently', () => {
  const env = mounted(), dispose = mountWorkbench(env.parts);
  env.emit({ primary: 'ontology', section: 'templates' });
  env.emit({ primary: 'chat' });
  assert.deepEqual(env.log, ['mount', 'engineering', 'templates', ['panel', null]]);
  dispose();
  dispose();
  env.emit({ primary: 'ontology', section: 'manage' });
  assert.deepEqual(env.log.slice(-3), ['unsubscribe', 'center.dispose', 'release']);
  assert.equal(env.errors.length, 0);
});

test('initial activation failure is visible and releases the owned root and subscription', () => {
  for (const activate of [() => false, () => { throw new Error('registry unavailable'); }]) {
    const env = mounted({ activate });
    assert.throws(() => mountWorkbench(env.parts), /工作台|registry/);
    assert.equal(env.errors.length, 1);
    assert.deepEqual(env.log.slice(-3), ['unsubscribe', 'center.dispose', 'release']);
  }
});

test('later async activation rejection is reported once and never leaks an unhandled rejection or late callback', async () => {
  let nextError;
  const env = mounted({ activate: () => nextError ? Promise.reject(nextError) : true });
  const dispose = mountWorkbench(env.parts);
  nextError = new Error('network error loading templates');
  env.emit({ primary: 'ontology', section: 'templates' });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(env.errors, [nextError]);
  env.emit({ primary: 'ontology', section: 'manage' });
  dispose();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(env.errors.length, 1);
});
