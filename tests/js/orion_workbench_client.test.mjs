import { pathToFileURL as orionPathToFileURL, fileURLToPath as orionFileURLToPath } from 'node:url';
import { resolve as orionResolve } from 'node:path';
const orionOfficialRuntimeRoot = orionResolve(process.env.ORION_DSH_RUNTIME_ROOT || orionFileURLToPath(new URL('../../', import.meta.url)));
const orionOfficialRuntimeBase = orionPathToFileURL(orionOfficialRuntimeRoot + '/');
import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import { createWorkbenchClientPlugin } from '../../harness/plugins/orion-workbench/client.entry.js';

function environment(core = null, brandAssets) {
  const registrations = [], disposers = [], effects = [], events = [], panels = [], locales = new Map();
  let language = 'zh', theme = 'light';
  const React = {
    createElement(type, props, ...children) { return { type, props: props ?? {}, children }; },
    useState(initial) { let value = typeof initial === 'function' ? initial() : initial; return [value, next => { value = next; }]; },
    useEffect(effect) { effects.push(effect); }, useSyncExternalStore(_subscribe, read) { return read(); }, useRef(value) { return { current: value }; },
  };
  function collect(value) {
    if (typeof value === 'function') disposers.push(value);
    else if (value?.[Symbol.iterator]) for (const dispose of value) collect(dispose);
  }
  const window = { fetch: async () => { throw new Error('Unexpected fetch'); },
    document: { querySelector() { throw new Error('Must not inspect native DOM'); } } };
  const ctx = {
    effect(callback) { collect(callback()); },
    on(name, listener) { events.push({ name, listener }); return () => events.splice(events.findIndex(row => row.listener === listener), 1); },
    slots: {
      inject(name, callback) { if (!core || core.spec(name)) collect(callback()); },
      register(options, component) {
        const entry = { options, component }; registrations.push(entry);
        const release = core?.register(options, component);
        return () => { release?.(); const i = registrations.indexOf(entry); if (i >= 0) registrations.splice(i, 1); };
      },
    },
    locale: { register(namespace, dictionaries) { locales.set(namespace, dictionaries); return () => locales.delete(namespace); },
      bind(namespace) { return key => locales.get(namespace)?.[language]?.[key] ?? key; } },
    theme: { getTheme() { return { active: { colorScheme: theme } }; } },
    sessions: { list: { getSnapshot: () => ({ byId: { a: { id: 'a', retainedBy: { mainView: 1 } } } }), subscribe: () => () => {} }, binding: () => null },
    workspaces: { list: { getSnapshot: () => ({ items: [] }) } }, uiWorkspace: { openSession() {} }, remote: { agentPresets: { select() {} } },
    layout: { selectPanel(id) { panels.push(id); }, panelInfo: { getSnapshot: () => ({ activePanelId: panels.at(-1) ?? null }) } },
  };
  const plugin = createWorkbenchClientPlugin(name => { assert.equal(name, 'react'); return React; }, window, brandAssets);
  plugin.apply(ctx);
  return { plugin, ctx, React, window, registrations, locales, panels, events, effects,
    entry(name, id) { return registrations.find(row => row.options.name === name && (id == null || row.options.key === id || row.options.id === id)); },
    setLanguage(value) { language = value; }, setTheme(value) { theme = value; },
    dispose() { for (const dispose of disposers.toReversed()) dispose(); } };
}

test('client groups ontology sections under one main/sidebar seat with localized labels and a lifecycle-owned public session bridge without reading native DOM', () => {
  const env = environment();
  assert.ok(env.plugin.inject.includes('locale'));
  assert.ok(env.plugin.inject.includes('theme'));
  assert.ok(env.plugin.inject.includes('remote'));
  assert.equal(env.entry('main').options.key, env.entry('sidebar.panellist').options.id);
  assert.equal(env.entry('main').options.locale, 'orion-workbench');
  assert.deepEqual(env.registrations.filter(row => row.options.name === 'main').map(row => row.options.key),
    ['orion-workbench']);
  assert.deepEqual(env.registrations.filter(row => row.options.name === 'sidebar.panellist').map(row => row.options.label()),
    ['本体中心']);
  env.setLanguage('en');
  assert.deepEqual(env.registrations.filter(row => row.options.name === 'sidebar.panellist').map(row => row.options.label()),
    ['Ontology Center']);
  assert.equal(env.entry('shell.overlay').options.id, 'orion-workbench.error');
  assert.equal(env.registrations.some(row => row.options.name === 'root' || row.options.name === 'sidebar'), false);
  assert.equal(env.window.__ORION_DSH_SESSIONS__.current(), 'a');
  env.panels.push('orion-workbench');
  env.dispose();
  assert.equal(env.window.__ORION_DSH_SESSIONS__, undefined);
  assert.equal(env.locales.size, 0);
  assert.equal(env.registrations.length, 0);
  assert.equal(env.panels.at(-1), null);
});

test('brand components use packaged light/dark assets and public theme events only', () => {
  const env = environment();
  const mark = env.entry('sidebar.brand.mark').component, name = env.entry('sidebar.brand.name').component;
  let image = mark({ size: 32, className: 'official-owner-class' });
  assert.equal(image.props.src, '/orion-workbench-assets/ahs-icon-black.svg');
  assert.equal(image.props.width, 32);
  assert.equal(image.props.className, 'official-owner-class');
  assert.equal(name().props.src, '/orion-workbench-assets/ahs-wordmark-black.svg');
  const cleanup = env.effects[0]();
  assert.equal(env.events[0].name, 'theme/change');
  env.setTheme('dark');
  env.events[0].listener({ active: { colorScheme: 'dark' } });
  image = mark({ size: 20 });
  assert.equal(image.props.src, '/orion-workbench-assets/ahs-icon-white.svg');
  assert.equal(name().props.src, '/orion-workbench-assets/ahs-wordmark-white.svg');
  cleanup();
  assert.equal(env.events.length, 0);
  env.dispose();
});

test('hot enabling can restore the exact brand without any Host asset request', async () => {
  const brandAssets = {};
  for (const kind of ['icon', 'wordmark']) for (const color of ['black', 'white']) {
    const file = `ahs-${kind}-${color}.svg`;
    const bytes = await readFile(new URL(`../../harness/web/assets/${file}`, import.meta.url));
    brandAssets[file] = `data:image/svg+xml;base64,${bytes.toString('base64')}`;
  }
  // This fixture rejects all network access, including during re-enablement.
  for (let cycle = 0; cycle < 2; cycle++) {
    const env = environment(null, brandAssets);
    for (const [theme, color] of [['light', 'black'], ['dark', 'white']]) {
      env.setTheme(theme);
      for (const [slot, kind] of [['sidebar.brand.mark', 'icon'], ['sidebar.brand.name', 'wordmark']]) {
        const image = env.entry(slot).component({ size: 24 });
        assert.equal(image.props.src, brandAssets[`ahs-${kind}-${color}.svg`]);
        assert.match(Buffer.from(image.props.src.split(',')[1], 'base64').toString(), /<svg\b/);
      }
    }
    env.dispose();
    assert.equal(env.registrations.length, 0);
  }
});

const officialSlotPath = new URL("node_modules/@deepseek-ai/dsh-client-ui-slots/lib/index.js", orionOfficialRuntimeBase);
test('official candidate SlotCore elects AHS over the official fallback and restores upstream entries on uninstall', {
  skip: !existsSync(officialSlotPath) && 'Install the pinned 0.2.0-rc.2 Harness contract fixture to run this SDK integration test',
}, async () => {
  const { SlotCore } = await import(officialSlotPath.href);
  const core = new SlotCore(), root = () => 'official root', originalMark = () => 'official mark', originalName = () => 'official name';
  const nativeMain = () => 'native conversation', nativeSidebar = () => 'native sidebar', nativeWorkspaces = () => 'native workspaces';
  core.register({ name: 'root', children: {
    main: { kind: 'keyed', scope: 'root' }, 'sidebar.panellist': { kind: 'list', scope: 'root' },
    sidebar: { kind: 'single', scope: 'root' }, 'sidebar.workspaces': { kind: 'single', scope: 'root' },
    'sidebar.brand.mark': { kind: 'single', scope: 'root' }, 'sidebar.brand.name': { kind: 'single', scope: 'root' },
    'conversation.hero.brand.mark': { kind: 'single', scope: 'root' }, 'shell.overlay': { kind: 'list', scope: 'root' },
  } }, root);
  core.register({ name: 'sidebar.brand.mark' }, originalMark);
  core.register({ name: 'sidebar.brand.name' }, originalName);
  core.register({ name: 'main', key: 'session' }, nativeMain);
  core.register({ name: 'sidebar' }, nativeSidebar);
  core.register({ name: 'sidebar.workspaces' }, nativeWorkspaces);
  const env = environment(core);
  assert.equal(core.entriesOfSlot('sidebar.brand.mark')[0].component, env.entry('sidebar.brand.mark').component);
  assert.equal(core.entriesOfSlot('sidebar.brand.name')[0].component, env.entry('sidebar.brand.name').component);
  assert.equal(core.entriesOfSlot('root')[0].component, root);
  assert.deepEqual(core.entriesOfSlot('main').filter(row => row.options.key.startsWith('orion-workbench')).map(row => row.options.key),
    ['orion-workbench']);
  assert.deepEqual(core.entriesOfSlot('sidebar.panellist').map(row => row.options.id),
    ['orion-workbench']);
  assert.equal(core.entriesOfSlot('main').find(row => row.options.key === 'session').component, nativeMain);
  assert.equal(core.entriesOfSlot('sidebar')[0].component, nativeSidebar);
  assert.equal(core.entriesOfSlot('sidebar.workspaces')[0].component, nativeWorkspaces);
  env.dispose();
  assert.equal(core.entriesOfSlot('sidebar.brand.mark')[0].component, originalMark);
  assert.equal(core.entriesOfSlot('sidebar.brand.name')[0].component, originalName);
  assert.equal(core.entriesOfSlot('root')[0].component, root);
  assert.deepEqual(core.entriesOfSlot('main').map(row => row.options.key), ['session']);
  assert.equal(core.entriesOfSlot('sidebar.panellist').length, 0);
  assert.equal(core.entriesOfSlot('sidebar')[0].component, nativeSidebar);
  assert.equal(core.entriesOfSlot('sidebar.workspaces')[0].component, nativeWorkspaces);
});

test('ontology center groups sections in its own navigation, uses semantic icons, and restores the native hero on disposal', () => {
  const env = environment();
  const entry = env.entry('main'), props = { ...entry.options.inject(), t: env.ctx.locale.bind('orion-workbench') };
  const tree = entry.component(props), header = tree.children[0], tabs = tree.children[1], content = tree.children.at(-1);
  assert.equal(header.children[0].children[1].children[0], '本体中心');
  assert.deepEqual(tabs.children.map(child => child.children[1]), ['本体工程', '本体管理', '行业模板']);
  assert.equal(tabs.children[0].props['aria-current'], 'page');
  const compatibilityNav = content.children[0];
  assert.equal(compatibilityNav.props.hidden, true);
  assert.equal(compatibilityNav.props.style.display, 'none');
  const glyph = env.entry('sidebar.panellist').component({ size: 18 });
  assert.equal(glyph.type, 'svg');
  assert.equal(glyph.props.stroke, 'currentColor');
  const hero = env.entry('conversation.hero.brand.mark').component({ size: 34 });
  assert.equal(hero.props['data-orion-hero-brand'], true);
  assert.equal(hero.children[2].children[0], '探索属于你的智能宇宙');
  env.dispose();
  assert.equal(env.entry('conversation.hero.brand.mark'), undefined);
});

test('disable restores conversation from the owned panel and preserves unrelated native panel selection', () => {
  for (const panel of ['orion-workbench', 'native-settings']) {
    const env = environment();
    env.panels.push(panel);
    env.dispose();
    assert.equal(env.panels.at(-1), panel === 'native-settings' ? panel : null);
  }
});

test('async controller failures reach a dismissible owned overlay even after leaving the workbench page', async () => {
  const env = environment(), previousWindow = globalThis.window;
  const manifest = { schemaVersion: 1, mode: 'core', scripts: ['/orion-workbench-assets/core.js'], styles: [] };
  Object.assign(env.window.document, { createElement: () => ({ dataset: {}, remove() {} }), head: { append: node => queueMicrotask(() => node.onload?.()) } });
  env.window.fetch = async url => url.endsWith('workbench-assets.json')
    ? { ok: true, json: async () => manifest }
    : { ok: false, status: 401, json: async () => ({ error: 'authenticated access required' }) };
  let route = { primary: 'ontology', section: 'engineering' };
  env.window.__ORION_SHELL_STATE__ = { get: () => route, navigate: value => { route = value; }, subscribe(listener) { listener(route); return () => {}; } };
  env.window.__ORION_ONTOLOGY_CENTER__ = { activate: () => true, dispose() {} };
  env.window.__ORION_ENGINEERING_BRIDGE__ = { mount: () => () => {} };
  globalThis.window = env.window;
  let release;
  try {
    const main = env.entry('main'), face = main.options.inject();
    release = await face.initialize({}, {}, () => false);
    assert.ok(env.window.__ORION_CHAT_MODES__);
    await assert.rejects(env.window.__ORION_CHAT_MODES__.openWorkbench('project-a'), /401|登录|访问|工作台/);
    const overlay = env.entry('shell.overlay'), props = { ...overlay.options.inject(), t: env.ctx.locale.bind('orion-workbench') };
    const notice = overlay.component(props);
    assert.equal(notice.props.role, 'alert');
    assert.equal(notice.props.style.pointerEvents, 'auto');
    assert.ok(notice.children[0].children[0].includes('401') || notice.children[0].children[0].includes('访问'));
    props.showPage();
    assert.equal(env.panels.at(-1), 'orion-workbench');
    props.dismissError();
    assert.equal(overlay.component(props), null);
  } finally { release?.(); env.dispose(); if (previousWindow === undefined) delete globalThis.window; else globalThis.window = previousWindow; }
  assert.equal(env.window.__ORION_CHAT_MODES__, undefined);
});

test('actual client disable and re-enable releases its styles and reuses already loaded business scripts', async () => {
  const env = environment(), children = [], loads = [];
  const manifest = { schemaVersion: 1, mode: 'core', scripts: ['/orion-workbench-assets/core.js'], styles: ['/orion-workbench-assets/brand.css'] };
  Object.assign(env.window.document, {
    createElement: tag => ({ tag, dataset: {}, remove() { const index = children.indexOf(this); if (index >= 0) children.splice(index, 1); } }),
    head: { append(node) { children.push(node); loads.push(node.src ?? node.href); queueMicrotask(() => node.onload?.()); } },
  });
  let requests = 0;
  env.window.fetch = async () => { requests++; return { ok: true, json: async () => manifest }; };
  const route = { primary: 'ontology', section: 'engineering' };
  env.window.__ORION_SHELL_STATE__ = { get: () => route, subscribe(listener) { listener(route); return () => {}; } };
  env.window.__ORION_ONTOLOGY_CENTER__ = { activate: () => true, dispose() {} };
  env.window.__ORION_ENGINEERING_BRIDGE__ = { mount: () => () => {} };
  let release = await env.entry('main').options.inject().initialize({}, {}, () => false);
  release(); env.dispose();
  assert.deepEqual(children.map(node => node.tag), ['script']);
  env.plugin.apply(env.ctx);
  release = await env.entry('main').options.inject().initialize({}, {}, () => false);
  assert.deepEqual(loads, [manifest.styles[0], manifest.scripts[0], manifest.styles[0]]);
  assert.equal(requests, 1);
  release(); env.dispose();
  assert.deepEqual(children.map(node => node.tag), ['script']);
});

test('real business navigation changes owned sections without splitting the sidebar or losing project routes', async () => {
  const env = environment(), activeViews = [], subscriptions = new Set(), storage = new Map();
  Object.assign(env.window.document, { createElement: () => ({ dataset: {}, remove() {} }), head: { append: node => queueMicrotask(() => node.onload?.()) } });
  env.window.localStorage = { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) };
  env.window.location = { hash: '#/chat', pathname: '/', search: '' };
  env.window.addEventListener = () => {};
  env.window.history = { pushState(_a, _b, value) { env.window.location.hash = new URL(value, 'http://local').hash; },
    replaceState(_a, _b, value) { env.window.location.hash = new URL(value, 'http://local').hash; } };
  const stateSource = await readFile(new URL('../../harness/web/assets/modules/ontology-center/state.js', import.meta.url), 'utf8');
  vm.runInNewContext(stateSource, { window: env.window });
  const state = env.window.__ORION_SHELL_STATE__, originalSubscribe = state.subscribe;
  state.subscribe = listener => { subscriptions.add(listener); const release = originalSubscribe(listener); return () => { subscriptions.delete(listener); release(); }; };
  env.window.__ORION_ONTOLOGY_CENTER__ = { activate(route, navigation) { activeViews.push({ route: { ...route }, navigation }); return true; }, dispose() {} };
  env.window.__ORION_ENGINEERING_BRIDGE__ = { mount: () => () => {} };
  let reads = 0;
  env.window.fetch = async () => { reads++; return { ok: true, json: async () => ({ schemaVersion: 1, mode: 'core', scripts: ['/orion-workbench-assets/core.js'], styles: [] }) }; };
  const face = env.entry('main').options.inject();
  let release;
  try {
    env.ctx.layout.selectPanel('orion-workbench');
    const navigation = {};
    release = await face.initialize({}, navigation, () => false);
    assert.equal(state.get().section, 'engineering');
    assert.deepEqual(env.panels, ['orion-workbench']);
    face.navigate('manage');
    assert.equal(activeViews.at(-1).route.section, 'manage');
    assert.equal(activeViews.at(-1).navigation, navigation);
    face.navigate('templates');
    assert.equal(activeViews.at(-1).route.section, 'templates');
    assert.equal(env.panels.at(-1), 'orion-workbench');
    state.navigate({ primary: 'ontology', section: 'engineering', projectId: 'formal-project', stage: 'S3' });
    assert.equal(activeViews.at(-1).route.projectId, 'formal-project');
    assert.equal(activeViews.at(-1).route.stage, 'S3');
    release();
    release = await face.initialize({}, {}, () => false);
    assert.equal(activeViews.at(-1).route.projectId, 'formal-project', 'remount preserves an explicit project route');
    face.returnToChat();
    assert.equal(state.get().primary, 'chat');
    assert.equal(env.panels.at(-1), null);
    assert.equal(reads, 1);
  } finally { release?.(); env.dispose(); }
  assert.equal(subscriptions.size, 0);
  const count = env.panels.length;
  state.navigate({ primary: 'ontology', section: 'manage' });
  assert.equal(env.panels.length, count, 'disposed workbench must not retake an official sidebar seat');
});
