import test from 'node:test';
import assert from 'node:assert/strict';
import { readGraphCanvasTheme } from '../../harness/web/assets/modules/graph-theme.js';
import { graphNodeInteraction, graphEdgeInteraction } from '../../harness/web/assets/modules/graph-interaction-style.js';

const themeFrom = values => readGraphCanvasTheme({}, () => ({ getPropertyValue: name => values[name] ?? '' }));

test('canvas labels take the active chrome text color after a theme change', () => {
  const light = themeFrom({ '--graph-text': '  #17263b  ' });
  const dark = themeFrom({ '--graph-text': '#e8f1fc' });
  assert.equal(graphNodeInteraction({ palette: light }).labelColor, '#17263b');
  assert.equal(graphNodeInteraction({ palette: dark }).labelColor, '#e8f1fc');
  assert.equal(graphNodeInteraction({ selected: true, palette: dark }).labelColor, '#e8f1fc');
  assert.equal(graphNodeInteraction({ hovered: true, palette: light }).labelColor, '#17263b');
});

test('themed selection and hover keep their distinct reading surfaces and interaction semantics', () => {
  const palette = themeFrom({ '--graph-label-selected-bg': '#d0e1f4', '--graph-label-hover-bg': '#e4eef9' });
  const options = { selected: true, hovered: true, hasSelection: true, hasHover: true, neighbor: true };
  const themed = graphNodeInteraction({ ...options, palette });
  const original = graphNodeInteraction(options);
  assert.equal(themed.labelBackground, '#d0e1f4');
  assert.equal(graphNodeInteraction({ hovered: true, palette }).labelBackground, '#e4eef9');
  assert.equal(graphNodeInteraction({ palette }).labelBackground, null);
  for (const key of ['opacity', 'ring', 'halo', 'bold']) assert.equal(themed[key], original[key]);
});

test('inheritance and relationships keep separate theme colors and the same geometry', () => {
  const palette = themeFrom({
    '--graph-edge-inheritance': '#526577', '--graph-edge-inheritance-active': '#263a4f',
    '--graph-edge-relation': '#774dac', '--graph-edge-relation-active': '#63358f', '--graph-edge-faded': '#8290a0',
  });
  const cases = [
    [{ kind: 'subclass' }, '#526577'],
    [{ kind: 'schema' }, '#774dac'],
    [{ kind: 'subclass', highlighted: true }, '#263a4f'],
    [{ kind: 'schema', highlighted: true, selected: true, zoom: 4 }, '#63358f'],
    [{ kind: 'schema', selected: true }, '#8290a0'],
  ];
  for (const [options, color] of cases) {
    const actual = graphEdgeInteraction({ ...options, palette });
    assert.equal(actual.color, color);
    assert.equal(actual.width, graphEdgeInteraction(options).width);
  }
});

test('instance preview edges remain readable for hover, selection and their combination', () => {
  const palette = themeFrom({
    '--graph-edge-instance': '#526f84', '--graph-edge-instance-active': '#264861',
    '--graph-edge-instance-hover': '#718698', '--graph-edge-instance-faded': '#a9b4be',
  });
  assert.equal(graphEdgeInteraction({ model: false, palette }).color, '#526f84');
  assert.equal(graphEdgeInteraction({ model: false, hovered: true, palette }).color, '#718698');
  assert.equal(graphEdgeInteraction({ model: false, selected: true, palette }).color, '#a9b4be');
  assert.equal(graphEdgeInteraction({ model: false, highlighted: true, selected: true, palette }).color, '#264861');
});

test('missing styles fall back to a usable canvas palette without touching the DOM', () => {
  let reads = 0;
  const fallback = readGraphCanvasTheme(null, () => { reads += 1; throw new Error('unexpected DOM read'); });
  assert.equal(reads, 0);
  assert.equal(themeFrom({}).labelColor, fallback.labelColor);
  assert.ok(fallback.gridColor);
  assert.notEqual(fallback.labelColor, fallback.labelOutline);
});
