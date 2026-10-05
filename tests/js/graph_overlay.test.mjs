import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { readGraphCanvasTheme } from '../../harness/web/assets/modules/graph-theme.js';

function viewerFixture() {
  const nodes = new Map(), bodyClasses = new Set(), pending = [], calls = [];
  const element = () => ({
    attributes: new Map(), dataset: {}, hidden: false,
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, focus() {},
    setAttribute(name, value) { this.attributes.set(name, value); },
    getAttribute(name) { return this.attributes.get(name) ?? null; },
    querySelector(selector) { if (!nodes.has(selector)) nodes.set(selector, element()); return nodes.get(selector); },
    querySelectorAll() { return []; },
  });
  const body = { appendChild(node) { this.dialog = node; }, classList: {
    add(name) { bodyClasses.add(name); }, remove(name) { bodyClasses.delete(name); },
  } };
  const fetchGraph = (kind, id) => { calls.push([kind, id]); return new Promise(resolve => pending.push(resolve)); };
  const window = {
    addEventListener() {}, removeEventListener() {},
    matchMedia() { return { matches: false, addEventListener() {}, removeEventListener() {} }; },
    __ORION_ONTOLOGY_API__: { templateGraph: id => fetchGraph('template', id), graph: id => fetchGraph('asset', id) },
  };
  const context = vm.createContext({ window, readGraphCanvasTheme, document: {
    body, documentElement: {}, activeElement: null, createElement: element,
  }, MutationObserver: class { observe() {} disconnect() {} } });
  const source = readFileSync(new URL('../../harness/web/assets/modules/ontology-graph-viewer.js', import.meta.url), 'utf8');
  vm.runInContext(source.replace(/^import[^;]+;\s*/gm, ''), context);
  return { viewer: window.__ORION_ONTOLOGY_GRAPH_VIEWER__, nodes, body, bodyClasses, calls,
    complete() { pending.shift()({ nodes: [], edges: [] }); } };
}

test('template overlay labels its existing close action as returning to industry templates', async () => {
  const f = viewerFixture(), opening = f.viewer.open({ templateId: 'supply-chain', name: '供应链', version: '1' });
  const button = f.nodes.get('[data-graph-close]');
  assert.equal(button.textContent, '返回行业模板');
  assert.equal(button.getAttribute('aria-label'), '返回行业模板');
  assert.deepEqual(f.calls, [['template', 'supply-chain']]);
  f.viewer.close(); f.complete(); await opening;
  assert.equal(f.body.dialog.hidden, true);
  assert.equal(f.bodyClasses.has('owa-graph-open'), false);
});

test('reusing the overlay updates both visible and accessible return labels for each caller', async () => {
  const f = viewerFixture();
  for (const [asset, label] of [
    [{ sourceProjectId: 'published-1', name: '正式资产', version: '3' }, '返回本体管理'],
    [{ templateId: 'industry-1', name: '行业模板', version: '1' }, '返回行业模板'],
    [{ sourceProjectId: 'published-2', name: '正式资产', version: '4' }, '返回本体管理'],
  ]) {
    const opening = f.viewer.open(asset), button = f.nodes.get('[data-graph-close]');
    assert.equal(button.textContent, label);
    assert.equal(button.getAttribute('aria-label'), label);
    f.viewer.close(); f.complete(); await opening;
  }
  assert.deepEqual(f.calls, [['asset', 'published-1'], ['template', 'industry-1'], ['asset', 'published-2']]);
});
