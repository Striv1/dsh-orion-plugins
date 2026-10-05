import test from 'node:test';
import assert from 'node:assert/strict';
import { perspectiveGraphFrame } from '../../harness/web/assets/modules/graph-framing.js';

test('portrait and landscape perspective framing includes every node with space for its radius', () => {
  const nodes = [{x:-200,y:0,z:10},{x:200,y:0,z:-30},{x:0,y:180,z:20}];
  for (const aspect of [.5, 2]) {
    const frame = perspectiveGraphFrame(nodes,{x:0,y:0,z:1},50,aspect);
    for (const n of nodes) {
      const depth = frame.position.z - n.z;
      assert.ok((Math.abs(n.x-frame.center.x)+14)/depth < Math.tan(25*Math.PI/180)*aspect);
      assert.ok((Math.abs(n.y-frame.center.y)+14)/depth < Math.tan(25*Math.PI/180));
    }
  }
});
test('uninitialized points cannot produce invalid camera coordinates', () => {
  assert.equal(perspectiveGraphFrame([{x:NaN,y:0,z:0}],{x:0,y:0,z:1},50,1),null);
  const frame=perspectiveGraphFrame([{x:0,y:0,z:0}],{x:0,y:0,z:0},50,1);
  assert.ok(Object.values(frame.position).every(Number.isFinite));
});
