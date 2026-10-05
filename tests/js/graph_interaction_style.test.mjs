import test from 'node:test';
import assert from 'node:assert/strict';
import { graphNodeInteraction as style, tintToWhite } from '../../harness/web/assets/modules/graph-interaction-style.js';

test('hover reveals a colored ring without treating the node as a selection', () => {
  const hover = style({hovered:true,hasHover:true});
  assert.ok(hover.halo > 0); assert.ok(hover.ring > 0);
  assert.equal(hover.bold,false); assert.notEqual(hover.labelBackground,'#ededed');
});
test('unrelated nodes dim lightly for hover and strongly for selection', () => {
  assert.ok(style({hasHover:true}).opacity > style({hasSelection:true}).opacity);
  assert.equal(style({neighbor:true,hasSelection:true}).opacity,1);
});
test('selection takes precedence when the same node is also hovered', () => {
  assert.deepEqual(style({selected:true,hasSelection:true}),style({selected:true,hovered:true,hasSelection:true,hasHover:true}));
});
test('hovering another node leaves it readable without removing persistent selection', () => {
  assert.equal(style({hovered:true,hasSelection:true}).opacity,1);
  assert.equal(style({selected:true,hasSelection:true,hasHover:true}).labelBackground,'#ededed');
});
test('pointer exit restores the resting style without a residual halo', () => {
  const resting=style(); assert.equal(resting.halo,0); assert.equal(resting.ring,null); assert.equal(resting.opacity,1);
});
test('ring tint preserves each nodes hue and brightens toward white', () => {
  assert.equal(tintToWhite('#ff9d76',0),'rgb(255,157,118)');
  assert.equal(tintToWhite('#ff9d76',1),'rgb(255,255,255)');
  assert.notEqual(tintToWhite('#ff9d76',.7),tintToWhite('#80c7ad',.7));
});

// Regression: do not substitute approximate mixed colors for the live 1516 palette.
test('1516 schema edge palette and sizing', async () => {
  const { graphEdgeInteraction } = await import('../../harness/web/assets/modules/graph-interaction-style.js');
  assert.deepEqual(graphEdgeInteraction({kind:'subclass'}), {color:'rgba(195,195,195,0.4)',width:3});
  assert.deepEqual(graphEdgeInteraction({kind:'schema',highlighted:true,zoom:4}), {color:'rgba(214,193,255,0.95)',width:6});
  assert.equal(graphEdgeInteraction({kind:'subclass',highlighted:true}).color, 'rgba(235,235,235,0.95)');
  assert.equal(graphEdgeInteraction({selected:true}).color, 'rgba(48,48,48,0.4)');
  assert.equal(graphEdgeInteraction({model:false,highlighted:true}).width, 1.85);
});
