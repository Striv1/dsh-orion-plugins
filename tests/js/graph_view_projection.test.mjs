import test from 'node:test';
import assert from 'node:assert/strict';
import { projectGraph, graphNeighborhood } from '../../harness/web/assets/modules/graph-view-projection.js';

const payload = {
  nodes: [
    ...['record', 'cell', 'equipment', 'isolated'].map(id => ({ id, kind: 'class', label: id })),
    { id: 'associatedCell', kind: 'objectProperty' },
    { id: 'serialNumber', kind: 'dataProperty' },
    ...['r1', 'r2', 'c1', 'e1', 'other'].map(id => ({ id, kind: 'individual', label: id })),
  ],
  edges: [
    { id: 'business', source: 'record', target: 'cell', kind: 'schema' },
    { id: 'hierarchy', source: 'equipment', target: 'record', kind: 'subclass' },
    { id: 'domain', source: 'associatedCell', target: 'record', kind: 'domain' },
    { id: 'range', source: 'associatedCell', target: 'equipment', kind: 'range' },
    { id: 'type1', source: 'r1', target: 'record', kind: 'instance' },
    { id: 'type2', source: 'r2', target: 'record', kind: 'instance' },
    { id: 'type3', source: 'c1', target: 'cell', kind: 'instance' },
    { id: 'fact1', source: 'r1', target: 'c1', kind: 'relationship' },
    { id: 'fact2', source: 'c1', target: 'e1', kind: 'relationship' },
    { id: 'dangling', source: 'r1', target: 'missing', kind: 'relationship' },
  ],
};
const ids = items => items.map(item => item.id);

test('default model preserves isolated classes and only direct business relationships', () => {
  const data = projectGraph(payload);
  assert.deepEqual(ids(data.nodes), ['record', 'cell', 'equipment', 'isolated']);
  assert.deepEqual(ids(data.links), ['business']);
  assert.deepEqual(ids(projectGraph(payload, { showHierarchy: true }).links), ['business', 'hierarchy']);
  // Search/navigation can consume this node list without leaking instance or property hits.
  assert.equal(data.nodes.find(node => node.id === 'r1'), undefined);
  assert.equal(data.nodes.find(node => node.id === 'associatedCell'), undefined);
});

test('professional view preserves all nodes while a relationship filter keeps its endpoints', () => {
  assert.equal(projectGraph(payload, { view: 'professional' }).nodes.length, payload.nodes.length);
  const data = projectGraph(payload, { view: 'professional', filter: 'domain' });
  assert.deepEqual(ids(data.nodes), ['record', 'associatedCell']);
  assert.deepEqual(ids(data.links), ['domain']);
  assert.deepEqual(projectGraph(payload, { view: 'professional', filter: 'absent' }), { nodes: [], links: [] });
});

test('unscoped instance view contains individuals and their valid business assertions', () => {
  const data = projectGraph(payload, { view: 'instances' });
  assert.deepEqual(ids(data.nodes), ['r1', 'r2', 'c1', 'e1', 'other']);
  assert.deepEqual(ids(data.links), ['fact1', 'fact2']);
});

test('class scope retains all typed seeds and only the requested depth of context', () => {
  const data = projectGraph(payload, { view: 'instances', instanceClassId: 'record' });
  assert.deepEqual(ids(data.nodes), ['r1', 'r2', 'c1']);
  assert.deepEqual(ids(data.links), ['fact1']);
  const expanded = projectGraph(payload, { view: 'instances', instanceClassId: 'record', hops: 2 });
  assert.deepEqual(ids(expanded.nodes), ['r1', 'r2', 'c1', 'e1']);
  assert.deepEqual(ids(expanded.links), ['fact1', 'fact2']);
  assert.deepEqual(projectGraph(payload, { view: 'instances', instanceClassId: 'isolated' }), { nodes: [], links: [] });
});

test('selected instance is the sole seed and can explore incoming relationships', () => {
  const data = projectGraph(payload, { view: 'instances', instanceClassId: 'record', instanceId: 'c1' });
  assert.deepEqual(ids(data.nodes), ['r1', 'c1', 'e1']);
  assert.deepEqual(ids(data.links), ['fact1', 'fact2']);
  assert.deepEqual(projectGraph(payload, { view: 'instances', instanceId: 'missing' }), { nodes: [], links: [] });
});

test('model neighborhoods cannot traverse hidden OWL definitions or instance membership', () => {
  const scope = graphNeighborhood(projectGraph(payload), 'record', 3);
  assert.deepEqual([...scope.distances], [['record', 0], ['cell', 1]]);
  assert.deepEqual(ids(scope.edges), ['business']);
  const withHierarchy = graphNeighborhood(projectGraph(payload, { showHierarchy: true }), 'equipment', 2);
  assert.equal(withHierarchy.distances.get('cell'), 2);
  const instances = graphNeighborhood(projectGraph(payload, { view: 'instances' }), 'r1', 3);
  assert.equal(instances.distances.has('r2'), false);
  assert.equal(instances.distances.get('e1'), 2);
});

test('empty graphs, missing selections and zero-hop exploration are well-defined', () => {
  assert.deepEqual(projectGraph(null), { nodes: [], links: [] });
  assert.equal(graphNeighborhood(null, 'record').distances.size, 0);
  assert.equal(graphNeighborhood(projectGraph(payload), 'missing').distances.size, 0);
  const scope = graphNeighborhood(projectGraph(payload), 'record', 0);
  assert.deepEqual([...scope.distances], [['record', 0]]);
  assert.deepEqual(scope.edges, []);
});

test('object endpoints and frozen inputs remain untouched across projection and traversal', () => {
  const source = Object.freeze({ id: 'a', kind: 'individual', x: 17 });
  const target = Object.freeze({ id: 'b', kind: 'individual', x: 29 });
  const edge = Object.freeze({ id: 'ab', kind: 'relationship', source, target });
  const input = Object.freeze({ nodes: Object.freeze([source, target]), edges: Object.freeze([edge]) });
  const data = projectGraph(input, { view: 'instances', instanceId: 'a' });
  assert.equal(data.links[0].source, 'a');
  assert.equal(data.links[0].target, 'b');
  data.nodes[0].x = 100;
  data.links[0].source = data.nodes[0];
  assert.equal(source.x, 17);
  assert.equal(edge.source, source);
  const scope = graphNeighborhood(data, 'b', 1);
  assert.deepEqual([...scope.distances], [['b', 0], ['a', 1]]);
  assert.equal(data.links[0].source, data.nodes[0]);
});
