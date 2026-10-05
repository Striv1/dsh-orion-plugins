import test from 'node:test';
import assert from 'node:assert/strict';
import { layeredGraphPositions } from '../../harness/web/assets/modules/graph-layered-layout.js';

const kinds = ['class', 'objectProperty', 'dataProperty', 'individual'];
const fixture = () => kinds.flatMap((kind, k) => Array.from({ length: [27, 12, 129, 8][k] }, (_, i) => ({ id: `${kind}-${i}`, kind })));
const radius = p => Math.hypot(p.x, p.y, p.z);

test('both layouts preserve distinct semantic shells even with 129 data properties', () => {
  const nodes = fixture();
  for (const dimensions of [2, 3]) {
    const result = layeredGraphPositions(nodes, { dimensions });
    assert.equal(result.size, nodes.length);
    let previousMax = 0;
    for (const kind of kinds) {
      const radii = nodes.filter(n => n.kind === kind).map(n => radius(result.get(n.id)));
      assert.ok(Math.min(...radii) > previousMax + 40);
      previousMax = Math.max(...radii);
    }
    assert.ok(previousMax < 350, 'the normal ontology stays compact enough for a large fit');
  }
});

test('dense planar layers use several rings with usable node spacing', () => {
  const nodes = fixture();
  const result = layeredGraphPositions(nodes, { dimensions: 2 });
  const data = nodes.filter(n => n.kind === 'dataProperty').map(n => result.get(n.id));
  assert.ok(new Set(data.map(p => Math.round(radius(p)))).size >= 3);
  for (const [i, p] of data.entries()) {
    assert.equal(p.z, 0);
    for (const q of data.slice(i + 1)) assert.ok(Math.hypot(p.x - q.x, p.y - q.y) >= 23);
  }
});

test('stable identity ordering ignores incoming array order without modifying nodes', () => {
  const nodes = fixture().map(Object.freeze);
  Object.freeze(nodes);
  for (const dimensions of [2, 3]) {
    assert.deepEqual(layeredGraphPositions(nodes, { dimensions }), layeredGraphPositions([...nodes].reverse(), { dimensions }));
  }
});

test('empty, singleton, and unknown kinds have finite positions and remain distinct', () => {
  assert.equal(layeredGraphPositions([]).size, 0);
  const nodes = [{id:'a',kind:'class'}, {id:'b',kind:'extension'}, {id:'c'}];
  for (const dimensions of [2, 3]) {
    const result = layeredGraphPositions(nodes, { dimensions });
    assert.equal(result.size, 3);
    for (const point of result.values()) assert.ok(Object.values(point).every(Number.isFinite));
    assert.ok(radius(result.get('b')) > radius(result.get('a')));
    assert.ok(radius(result.get('c')) > radius(result.get('b')));
  }
});

test('spherical shells distribute dense nodes across all three dimensions', () => {
  const positions = [...layeredGraphPositions(Array.from({length:129}, (_, i) => ({id:String(i),kind:'dataProperty'}))).values()];
  for (const axis of ['x', 'y', 'z']) {
    assert.ok(Math.min(...positions.map(p => p[axis])) < -140);
    assert.ok(Math.max(...positions.map(p => p[axis])) > 140);
    assert.ok(Math.abs(positions.reduce((sum, p) => sum + p[axis], 0) / positions.length) < 2);
  }
});
