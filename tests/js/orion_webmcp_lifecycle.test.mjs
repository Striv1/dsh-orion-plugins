import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../../harness/web/assets/brand.js', import.meta.url), 'utf8');
function environment({ native = true, unregister = true, navigatorFallback = false } = {}) {
  const foreign = { name: 'other-plugin-tool' }, tools = new Map([[foreign.name, foreign]]), calls = [];
  const modelContext = {
    registerTool(tool) {
      if (tools.has(tool.name)) throw new Error('Duplicate tool name');
      tools.set(tool.name, tool); calls.push(['register', tool.name]);
    },
    ...(unregister ? { unregisterTool(name) {
      if (!tools.delete(name)) throw new Error('Unknown tool');
      calls.push(['unregister', name]);
    } } : {}),
  };
  const window = { __ORION_NATIVE_PLUGIN__: native, dispatchEvent() {}, localStorage: { getItem: () => null },
    setInterval() { calls.push(['legacy-poll']); }, __ORION_HARNESS_ADAPTER__: { start() { calls.push(['legacy-start']); } } };
  const document = { documentElement: { dataset: {} }, ...(!navigatorFallback ? { modelContext } : {}) };
  const context = vm.createContext({ window, document, navigator: navigatorFallback ? { modelContext } : {},
    CustomEvent: class { constructor(type) { this.type = type; } },
    XMLHttpRequest: class { constructor() { throw new Error('A retired tool must not start an HTTP request'); } } });
  const evaluate = () => vm.runInContext(source, context);
  evaluate();
  return { window, document, calls, tools, foreign, evaluate, get lifecycle() { return window.__ORION_DOCUMENT_WEBMCP__; } };
}

test('native WebMCP tool leases unregister on disable and an old release cannot remove a newer instance', async () => {
  const env = environment(), name = 'inspect-document-ingestion';
  assert.equal(env.tools.has(name), false, 'loading business definitions is not plugin activation');
  const first = env.lifecycle.acquire(), descriptor = env.tools.get(name);
  assert.equal(env.window.__OWA_WEBMCP_STATUS__.registered, true);
  const second = env.lifecycle.acquire();
  first(); first();
  assert.equal(env.tools.get(name), descriptor);
  assert.equal(env.calls.filter(([action]) => action === 'register').length, 1);
  second();
  assert.equal(env.tools.has(name), false);
  assert.equal(env.window.__OWA_WEBMCP_STATUS__.registered, false);
  assert.equal((await descriptor.execute()).structuredContent.status, 'unavailable');
  const third = env.lifecycle.acquire();
  second();
  assert.equal(env.tools.has(name), true, 'late disposal cannot unregister a subsequent owner');
  third();
  assert.deepEqual([...env.tools.values()], [env.foreign]);
});

test('re-evaluating the asset shares the WebMCP owner instead of registering duplicate tools', () => {
  const env = environment({ navigatorFallback: true }), original = env.lifecycle;
  const release = original.acquire();
  env.evaluate();
  assert.equal(env.lifecycle, original);
  assert.equal(env.calls.filter(([action]) => action === 'register').length, 1);
  assert.equal(env.window.__OWA_WEBMCP_STATUS__.error, null);
  release();
  assert.deepEqual([...env.tools.values()], [env.foreign]);
});

test('legacy assembly keeps page-lifetime auto registration while native plugins require reversible registration', () => {
  const legacy = environment({ native: false, unregister: false });
  assert.equal(legacy.tools.has('inspect-document-ingestion'), true);
  assert.ok(legacy.calls.some(([action]) => action === 'legacy-start'));
  const native = environment({ unregister: false });
  const release = native.lifecycle.acquire();
  assert.equal(native.tools.has('inspect-document-ingestion'), false);
  assert.match(native.window.__OWA_WEBMCP_STATUS__.error, /lifecycle/);
  release();
  assert.equal(native.calls.some(([action]) => action === 'legacy-start'), false);
});
