import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { instancePreviewNotice } from '../../harness/web/assets/modules/graph-instance-preview.js';

test('preview distinguishes selected subjects, context, displayed instances and S6 whole graph', () => {
  const text = instancePreviewNotice({complete:false,selected_subject_count:200,context_individual_count:329,displayed_individual_count:529,full_graph_triple_count:4666842});
  for (const phrase of ['实例预览','选中主体 200','关联上下文 329','当前加载 529 个实例','S6 验收完整图 4,666,842 条三元组','仅展示部分实例与关系']) assert.ok(text.includes(phrase));
  assert.doesNotMatch(text, /总实例|全部实例/);
});

test('unknown counts are omitted instead of becoming zero', () => {
  const text = instancePreviewNotice({complete:false,selected_subject_count:200,context_individual_count:null,displayed_individual_count:undefined,full_graph_triple_count:null});
  assert.match(text, /实例预览.*选中主体 200/);
  assert.doesNotMatch(text, /上下文|当前加载|S6 验收| 0/);
});

test('zero is shown when the source actually reports zero', () => {
  assert.match(instancePreviewNotice({complete:false,selected_subject_count:0,context_individual_count:0,displayed_individual_count:0,full_graph_triple_count:0}), /选中主体 0.*关联上下文 0.*当前加载 0 个实例.*完整图 0 条/);
});

test('older responses and complete graphs do not display a partial-preview notice', () => {
  for(const value of [null,undefined,{}, {complete:true}]) assert.equal(instancePreviewNotice(value), '');
});

test('unavailable or absent instance evidence is not presented as a successful preview', () => {
  assert.match(instancePreviewNotice({status:'UNAVAILABLE',reason:'NO_MATERIALIZED_GRAPH'}), /没有可预览的实例图/);
  const text = instancePreviewNotice({status:'UNAVAILABLE',reason:'instance artifact checksum mismatch'});
  assert.match(text, /实例预览暂不可用.*S6 验收报告/);
  assert.doesNotMatch(text, /仅展示部分实例|当前加载|完整数据保留/);
});

test('viewer shows the notice as text and clears previous release notice on open', async () => {
  const source = await readFile(new URL('../../harness/web/assets/modules/ontology-graph-viewer.js',import.meta.url),'utf8');
  assert.match(source, /data-graph-preview-scope hidden/);
  assert.match(source, /previewNotice\.textContent = instancePreviewNotice\(payload\.instance_preview\)/);
  assert.match(source, /data-graph-instance-preview role="status"/);
  assert.match(source, /state\.view === "model" \|\| !dialog\.querySelector\("\[data-graph-instance-preview\]"\)\.textContent/);
  assert.match(source, /querySelector\("\[data-graph-preview-scope\]"\)\.hidden = true/);
});
