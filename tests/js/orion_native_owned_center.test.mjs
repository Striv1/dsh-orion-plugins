import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../../harness/web/assets/modules/ontology-center/index.js', import.meta.url), 'utf8');
class Element {
  constructor() { this.children = []; this.dataset = {}; this.listeners = {}; this.isConnected = true; this.parts = new Map(); }
  append(node) { node.parent = this; this.children.push(node); }
  insertAdjacentElement() { throw new Error('Must remain inside owned navigation'); }
  addEventListener(name, listener) { this.listeners[name] = listener; }
  querySelector(selector) { if (!this.parts.has(selector)) this.parts.set(selector, new Element()); return this.parts.get(selector); }
  querySelectorAll() { return []; }
  setAttribute() {}
  removeAttribute() {}
  remove() { this.isConnected = false; if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); }
}
function environment() {
  const parent = new Element(), navigation = new Element(), root = new Element(), operations = [], routes = [], cleared = [];
  parent.append(navigation); parent.append(root);
  let intervals = 0;
  const window = {
    __ORION_NATIVE_PLUGIN__: true,
    __ORION_ENGINEERING_BRIDGE__: { getRoot: () => root },
    __ORION_SHELL_STATE__: { get: () => ({ section: 'engineering', projectId: 'p' }), navigate: route => routes.push(route) },
    __ORION_ONTOLOGY_API__: { dashboard: async () => ({ projects: [{ project_id: 'p', project_name: 'formal name', current_stage: 'S3' }] }), catalog: async () => [] },
    __ORION_ONTOLOGY_ENGINEERING__: { close: () => operations.push('engineering.close'), open: route => operations.push(['engineering.open', route]) },
    __ORION_ONTOLOGY_REGISTRY__: { show: node => operations.push(['registry.show', node]), hide: node => operations.push(['registry.hide', node]) },
    __ORION_ONTOLOGY_TEMPLATES__: { show: node => operations.push(['templates.show', node]), hide: node => operations.push(['templates.hide', node]) },
    setInterval: () => ++intervals, clearInterval: id => cleared.push(id), requestAnimationFrame: callback => callback(),
  };
  const document = { createElement: () => new Element(), visibilityState: 'visible',
    querySelector() { throw new Error('Must not discover native frame'); }, querySelectorAll() { throw new Error('Must not discover native session tabs'); } };
  vm.runInNewContext(source, { window, document });
  return { window, root, parent, navigation, operations, routes, cleared, center: window.__ORION_ONTOLOGY_CENTER__ };
}
const flush = () => new Promise(resolve => setImmediate(resolve));

test('native center keeps context inside the owned navigation and only renders business views into its own main root', async () => {
  const env = environment();
  assert.equal(env.center.activate({ primary: 'ontology', section: 'manage' }, env.navigation), true);
  await flush();
  assert.deepEqual(env.parent.children, [env.navigation, env.root]);
  assert.equal(env.navigation.children.length, 1);
  assert.equal(env.navigation.children[0].parent, env.navigation);
  assert.ok(env.operations.some(([operation, node]) => operation === 'registry.show' && node === env.root));
  assert.equal(env.root.dataset.orionOntologyRoot, 'true');
  env.center.activate({ primary: 'ontology', section: 'templates' }, env.navigation);
  assert.equal(env.navigation.children.length, 1);
  assert.ok(env.operations.some(([operation, node]) => operation === 'templates.show' && node === env.root));
  env.center.dispose();
  assert.deepEqual(env.parent.children, [env.navigation, env.root]);
  assert.equal(env.navigation.children.length, 0);
  assert.deepEqual(env.cleared, [1]);
});

test('center actions resolve the current native chat controller after asset load instead of capturing an undefined or retired controller', async () => {
  const env = environment(), calls = [];
  env.center.activate({ primary: 'ontology', section: 'engineering' }, env.navigation);
  await flush();
  const context = env.navigation.children[0];
  env.window.__ORION_CHAT_MODES__ = { openNewSession: () => calls.push('new-first'), rememberProjectSession: (id, name) => calls.push([id, name]) };
  context.listeners.click({ target: { closest: selector => selector.includes('new-project') ? {} : null } });
  env.window.__ORION_CHAT_MODES__ = { openNewSession: () => calls.push('new-current'), rememberProjectSession: (id, name) => calls.push([id, name]) };
  context.listeners.click({ target: { closest: selector => selector.includes('new-project') ? {} : null } });
  context.listeners.click({ target: { closest: selector => selector === '[data-ontology-project]' ? { dataset: { ontologyProject: 'p' } } : null } });
  assert.deepEqual(calls, ['new-first', 'new-current', ['p', 'formal name']]);
  assert.equal(env.routes.at(-1).projectId, 'p');
  assert.equal(env.routes.at(-1).stage, 'S3');
  env.center.dispose();
});
