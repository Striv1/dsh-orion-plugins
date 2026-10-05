import { pathToFileURL as orionPathToFileURL, fileURLToPath as orionFileURLToPath } from 'node:url';
import { resolve as orionResolve } from 'node:path';
const orionOfficialRuntimeRoot = orionResolve(process.env.ORION_DSH_RUNTIME_ROOT || orionFileURLToPath(new URL('../../', import.meta.url)));
const orionOfficialRuntimeBase = orionPathToFileURL(orionOfficialRuntimeRoot + '/');
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { createSessionBridge } from '../../harness/plugins/orion-workbench/lib/session-bridge.js';
import { createWorkbenchClientPlugin } from '../../harness/plugins/orion-workbench/client.entry.js';

function environment() {
  const inputs = {}, log = [], bindings = {};
  const list = { byId: { a: { id: 'a', cwd: '/a', retainedBy: { mainView: 1 }, projectionValues: { agentPreset: 'engineering' } },
    b: { id: 'b', cwd: '/b', retainedBy: { mainView: 0 } } } };
  const workspaces = { items: [{ workspaceId: 'wa', path: '/a', sessionIds: ['a'] }, { workspaceId: 'wb', path: '/b', sessionIds: ['b'] }] };
  const select = id => { for (const row of Object.values(list.byId)) row.retainedBy.mainView = row.id === id ? 1 : 0; };
  for (const id of ['a', 'b']) {
    const snapshot = { draft: id === 'a' ? 'user draft' : '', attachmentIds: id === 'a' ? ['attachment-a'] : [], occurrences: [], phase: 'plain' };
    inputs[id] = { state: { getSnapshot: () => snapshot }, setDraft(text) { log.push(['draft', id, text]); snapshot.draft = text; }, submit() { throw new Error('must never submit'); } };
  }
  const ctx = { sessions: {
    list: { getSnapshot: () => list }, binding: id => bindings[id],
    scope: id => ({ get: service => { assert.equal(service, 'conversation'); return { input: { for: scope => { assert.ok(scope); return inputs[id]; } } }; } }),
    async create(options) {
      log.push(['create', options]);
      list.byId.new = { id: 'new', cwd: '/b', retainedBy: { mainView: 0 } };
      inputs.new = { state: { getSnapshot: () => ({ draft: '', attachmentIds: [], occurrences: [], phase: 'plain' }) }, setDraft() {} };
      return 'new';
    },
  }, workspaces: { list: { getSnapshot: () => workspaces } },
  uiWorkspace: { openSession(id) { log.push(['open', id]); select(id); } },
  remote: { agentPresets: { async select(id, presetId) { log.push(['preset', id, presetId]); return { ok: true, value: presetId }; } } },
  layout: { selectPanel(panel) { log.push(['panel', panel]); } } };
  return { ctx, list, inputs, workspaces, log, bindings, select };
}

test('current session and workspace derive from official mainView retention, and returned workspace lists cannot mutate the store', () => {
  const env = environment(), bridge = createSessionBridge(env.ctx);
  assert.equal(bridge.current(), 'a');
  assert.equal(bridge.currentWorkspace().workspaceId, 'wa');
  bridge.workspaces()[0].sessionIds.push('b');
  assert.deepEqual(env.workspaces.items[0].sessionIds, ['a']);
  env.list.current = 'deleted';
  env.select('b');
  assert.equal(bridge.current(), 'b');
  bridge.showWorkbench();
  assert.deepEqual(env.log, [['panel', null]]);
  assert.throws(() => bridge.open('absent'), /不存在/);
  assert.equal(bridge.open('a'), true);
});

test('native create targets the chosen workspace and waits for actual selection without touching other input drafts', async () => {
  const env = environment();
  env.ctx.uiWorkspace.openSession = id => env.log.push(['open', id]);
  let sleeps = 0;
  const bridge = createSessionBridge(env.ctx, { sleep: async () => { if (++sleeps === 2) env.select('new'); } });
  assert.equal(await bridge.create('wb'), 'new');
  assert.deepEqual(env.log, [['create', { workspaceId: 'wb' }], ['open', 'new']]);
  assert.equal(sleeps, 2);
  assert.equal(env.inputs.a.state.getSnapshot().draft, 'user draft');
  assert.deepEqual(env.inputs.a.state.getSnapshot().attachmentIds, ['attachment-a']);
});

test('missing workspace, missing created catalog identity and failed selection reject without pretending success', async () => {
  const env = environment(), bridge = createSessionBridge(env.ctx, { switchAttempts: 1, sleep: async () => {} });
  await assert.rejects(bridge.create('removed-workspace'), /工作区/);
  assert.equal(env.log.length, 0);
  env.ctx.sessions.create = async () => 'uncatalogued';
  await assert.rejects(bridge.create(), /登记/);
  env.ctx.sessions.create = async () => 'b';
  env.ctx.uiWorkspace.openSession = () => {};
  await assert.rejects(bridge.create(), /切换/);
});

test('preset selection preserves structured rejection and requires exact native read-back', async () => {
  const env = environment(), bridge = createSessionBridge(env.ctx);
  assert.equal(await bridge.selectPreset('a', 'ontology-qa'), 'ontology-qa');
  for (const result of [undefined, { ok: false, error: { message: 'preset denied' } }, { ok: true, value: 'default' }]) {
    env.ctx.remote.agentPresets.select = async () => result;
    await assert.rejects(bridge.selectPreset('a', 'ontology-qa'), /preset denied|未通过回读/);
  }
});

test('native draft append preserves user text and ordered attachments and never submits', () => {
  const env = environment(), bridge = createSessionBridge(env.ctx);
  assert.deepEqual(bridge.fillDraft('a', 'approved workflow context'), {
    sessionId: 'a', draft: 'user draft\n\napproved workflow context', attachmentCount: 1,
  });
  assert.deepEqual(env.log, [['draft', 'a', 'user draft\n\napproved workflow context']]);
  assert.deepEqual(env.inputs.a.state.getSnapshot().attachmentIds, ['attachment-a']);
  assert.equal(env.inputs.b.state.getSnapshot().draft, '');
});

test('draft guards refuse changed session, command/submitting states, reference chips and empty input before mutation', () => {
  const env = environment(), bridge = createSessionBridge(env.ctx), state = env.inputs.a.state.getSnapshot();
  assert.throws(() => bridge.fillDraft('b', 'context'), /切换/);
  assert.throws(() => bridge.fillDraft('a', ''), /为空/);
  for (const phase of ['claimed', 'adjudicating', 'submitting']) {
    state.phase = phase;
    assert.throws(() => bridge.fillDraft('a', 'context'), /未就绪/);
  }
  state.phase = 'plain';
  state.occurrences.push({ ref: 'file' });
  assert.throws(() => bridge.fillDraft('a', 'context'), /文件引用/);
  assert.equal(env.log.length, 0);
  assert.equal(state.draft, 'user draft');
});

test('read-back catches ignored text, mutated attachment arrays and a concurrently selected session', () => {
  for (const behavior of ['ignore', 'attachments', 'session']) {
    const env = environment(), bridge = createSessionBridge(env.ctx), state = env.inputs.a.state.getSnapshot();
    env.inputs.a.setDraft = text => {
      if (behavior !== 'ignore') state.draft = text;
      if (behavior === 'attachments') state.attachmentIds.push('unexpected');
      if (behavior === 'session') env.select('b');
    };
    assert.throws(() => bridge.fillDraft('a', 'context'), /回读不一致/);
  }
});

test('activity distinguishes unknown session from a materialized executor and durable user turns', () => {
  const env = environment(), bridge = createSessionBridge(env.ctx);
  assert.deepEqual(bridge.activity('a'), { available: false, running: null, started: false, blank: false, presetId: 'engineering' });
  const snapshot = { openState: 'open', running: false, blank: true }, entries = [];
  env.bindings.a = { session: { getSnapshot: () => snapshot }, eventSource: { getSnapshot: () => ({ entries }) } };
  assert.deepEqual(bridge.activity('a'), { available: true, running: false, started: false, blank: true, presetId: 'engineering' });
  entries.push({ type: 'event', event: { type: 'user/message', data: { source: { kind: 'user' } } } });
  assert.equal(bridge.activity('a').started, true);
  assert.equal(bridge.activity('a').blank, false);
  snapshot.running = true;
  assert.equal(bridge.activity('a').running, true);
  snapshot.openState = 'opening';
  assert.equal(bridge.activity('a').running, null);
});

const cordisPath = new URL("node_modules/@deepseek-ai/cordis/lib/index.js", orionOfficialRuntimeBase);
const cordisFixture = { skip: !existsSync(cordisPath) && 'Install the pinned Harness Cordis contract fixture for guarded-context integration' };

async function guardedEnvironment() {
  const { Context, Service } = await import(cordisPath.href);
  const root = new Context(), env = environment(), calls = [];
  const providers = Object.keys(env.ctx).filter(name => name !== 'remote').map(name => root.provide(name, env.ctx[name]));
  providers.push(root.provide('slots', {}), root.provide('locale', {}), root.provide('theme', {}));
  // Real Cordis Service trackers resolve ctx.remote.agentPresets through the
  // separately injected remote.agentPresets service, just as the SDK Gateway.
  class RemoteFacade extends Service { constructor(ctx) { super(ctx, 'remote'); } }
  class AgentPresets extends Service {
    constructor(ctx) { super(ctx, 'remote.agentPresets'); }
    async select(sessionId, presetId) {
      calls.push({ sessionId, presetId, callerFiber: this.ctx.fiber });
      return { ok: true, value: presetId };
    }
  }
  let namespace;
  const facade = root.plugin(ctx => { new RemoteFacade(ctx); });
  const presetProvider = root.plugin(ctx => { namespace = new AgentPresets(ctx); });
  await Promise.all([facade, presetProvider]);
  return { root, env, calls, namespace, presetProvider, facade, providers,
    async dispose() { await presetProvider.dispose(); await facade.dispose(); for (const release of providers.toReversed()) await release(); } };
}

test('real Cordis rejects a namespace-only consumer when it first accesses the remote facade', cordisFixture, async () => {
  const guarded = await guardedEnvironment();
  let bridge;
  const fiber = guarded.root.plugin({ inject: ['remote.agentPresets'], apply(ctx) { bridge = createSessionBridge(ctx); } });
  try {
    await fiber;
    await assert.rejects(bridge.selectPreset('a', 'engineering'), /cannot get property "remote" without inject/);
    assert.equal(guarded.calls.length, 0);
  } finally { await fiber.dispose(); await guarded.dispose(); }
});

test('the production client dependency declaration passes real Cordis guards and preserves the caller for preset selection', cordisFixture, async () => {
  const guarded = await guardedEnvironment();
  const plugin = createWorkbenchClientPlugin(() => ({}), {});
  let bridge;
  const fiber = guarded.root.plugin({ inject: plugin.inject, apply(ctx) { bridge = createSessionBridge(ctx); } });
  try {
    await fiber;
    assert.equal(await bridge.selectPreset('a', 'engineering'), 'engineering');
    assert.equal(await bridge.selectPreset('b', 'ontology-qa'), 'ontology-qa');
    assert.deepEqual(guarded.calls.map(({ sessionId, presetId }) => ({ sessionId, presetId })), [
      { sessionId: 'a', presetId: 'engineering' }, { sessionId: 'b', presetId: 'ontology-qa' },
    ]);
    assert.equal(guarded.calls.every(call => call.callerFiber === fiber.ctx.fiber), true);
    await guarded.presetProvider.dispose();
    await assert.rejects(bridge.selectPreset('a', 'engineering'), /inactive context|without inject/);
    assert.equal(guarded.calls.length, 2, 'withdrawn namespace must never invoke a stale selection');
  } finally { await fiber.dispose(); await guarded.dispose(); }
});

test('real Cordis guards also refuse a facade-only consumer access to the independently provided namespace', cordisFixture, async () => {
  const guarded = await guardedEnvironment();
  let bridge;
  const fiber = guarded.root.plugin({ inject: ['remote'], apply(ctx) { bridge = createSessionBridge(ctx); } });
  try {
    await fiber;
    await assert.rejects(bridge.selectPreset('a', 'engineering'), /cannot get property "remote.agentPresets" without inject/);
    assert.equal(guarded.calls.length, 0);
  } finally { await fiber.dispose(); await guarded.dispose(); }
});
