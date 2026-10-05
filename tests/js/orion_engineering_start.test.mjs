import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync } from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import {
  ENGINEERING_START_SLOT, buildEngineeringStartPrompt, parseEngineeringQuestions,
  engineeringStartAvailable, prepareEngineeringStartDraft, installEngineeringStart,
} from '../../harness/plugins/orion-workbench/lib/engineering-start.js';
import { createSessionBridge } from '../../harness/plugins/orion-workbench/lib/session-bridge.js';

const fields = { intakeMode: 'DOCUMENT_ONLY', goal: '比较供应商交付可靠性',
  questions: '哪些供应商需要复核？｜返回供应商及依据\n哪些订单受影响？｜返回关联订单',
  documents: '本次实际上传的制度和合同' };
const databaseScope = 'Chat2DB datasource_id="purchasing"；数据库="erp"；Schema="sales"；表="orders"；范围=仅指定表（只读）';
const deferred = () => { let resolve; const promise = new Promise(value => { resolve = value; }); return { promise, resolve }; };

function fixture() {
  let selected = 'new-session';
  let preset = 'standard';
  let input = { phase: 'plain', draft: '已有业务说明', draftRev: 2, attachmentIds: ['actual-attachment'], occurrences: [] };
  let activity = { available: true, running: false, started: false, blank: true };
  const calls = [];
  const sessions = { list: { getSnapshot: () => ({ current: selected, byId: {
    'new-session': { id: 'new-session', projectionValues: { agentPreset: preset } },
    'other-session': { id: 'other-session', projectionValues: { agentPreset: 'standard' } },
  } }) }, scope: id => ({ get(name) {
    assert.equal(name, 'conversation');
    return { input: { for() { return { state: { getSnapshot: () => input }, setDraft(value) {
      assert.equal(id, 'new-session');
      calls.push(['draft', value]); input = { ...input, draft: value, draftRev: input.draftRev + 1 };
    } }; } } };
  } }) };
  const native = createSessionBridge({ sessions });
  const bridge = {
    current: () => selected,
    activity: () => ({ ...activity, presetId: preset }),
    async selectPreset(id, value) { calls.push(['preset', id, value]); preset = value; return value; },
    fillDraft: native.fillDraft,
    create() { throw new Error('The template must not create a Session'); },
    send() { throw new Error('The template must not send a prompt'); },
  };
  return { bridge, calls, input: () => input, readInput: () => input,
    select: id => { selected = id; }, setPreset: value => { preset = value; },
    edit: values => { input = { ...input, ...values }; }, setActivity: values => { activity = { ...activity, ...values }; },
    prepare(values = {}) { return prepareEngineeringStartDraft({ sessionId: 'new-session', fields,
      bridge, readInput: () => input, prepareDatabaseScope: async () => { calls.push(['catalog']); return databaseScope; }, ...values }); },
  };
}

for (const intakeMode of ['DOCUMENT_ONLY', 'DATABASE_ONLY', 'HYBRID']) test(`${intakeMode} carries real business inputs and keeps joint design/publication approval`, () => {
  const text = buildEngineeringStartPrompt({ ...fields, intakeMode, databaseScope });
  assert.match(text, /比较供应商交付可靠性/);
  assert.match(text, /1\. 哪些供应商需要复核/);
  assert.match(text, /2\. 哪些订单受影响/);
  assert.match(text, /共 2 条用户提供的 CQ/);
  assert.match(text, /USER_PROVIDED/);
  assert.match(text, /S4.*等待我的明确批准/);
  assert.match(text, /S7.*明确批准具体版本发布/);
  assert.match(text, /问答绑定到该工程的已发布版本/);
  if (intakeMode !== 'DATABASE_ONLY') {
    assert.match(text, /本次实际上传的制度和合同/);
    assert.match(text, /不代表已完成来源绑定/);
    assert.match(text, /不得编造文件路径/);
  } else assert.doesNotMatch(text, /本次实际上传的制度和合同/);
  if (intakeMode !== 'DOCUMENT_ONLY') {
    assert.match(text, /Chat2DB datasource_id="purchasing"/);
    assert.match(text, /不得扩大范围或写入源库/);
    assert.match(text, /不得将跨库表范围扁平合并/);
    assert.match(text, /不.*未来新增表/);
  } else assert.doesNotMatch(text, /datasource_id="purchasing"/);
});

test('CQ tables preserve business acceptance requirements, and numbered/checklist rows remain one question each', () => {
  const questions = '| 编号 | 问题 | 验收要求 |\n| --- | --- | --- |\n| CQ1 | 哪些设备异常？ | 返回设备及依据 |\n| CQ2 | 哪些工单受影响？ | 返回工单 |';
  assert.deepEqual(parseEngineeringQuestions(questions), ['哪些设备异常？｜验收要求：返回设备及依据', '哪些工单受影响？｜验收要求：返回工单']);
  assert.deepEqual(parseEngineeringQuestions('1. 哪些供应商？｜说明依据\n- [ ] 哪些订单？\n2、哪些客户？'),
    ['哪些供应商？｜说明依据', '哪些订单？', '哪些客户？']);
  assert.deepEqual(parseEngineeringQuestions('| 问题 | 验收 |\n| --- | --- |'), []);
});

test('missing goals, CQs or real database selection cannot produce a draft or invent a source', async () => {
  for (const patch of [{ intakeMode: 'INVALID' }, { goal: ' ' }, { questions: '| 问题 |\n| --- |' }]) {
    const f = fixture();
    await assert.rejects(f.prepare({ fields: { ...fields, ...patch } }));
    assert.deepEqual(f.calls, []);
  }
  const f = fixture();
  await assert.rejects(f.prepare({ fields: { ...fields, intakeMode: 'DATABASE_ONLY' }, prepareDatabaseScope: async () => '' }), /只读使用/);
  assert.deepEqual(f.calls, []);
  assert.match(buildEngineeringStartPrompt({ ...fields, documents: '' }), /实际提交的资料为候选范围/);
});

test('the dock appears only for known blank standard/engineering sessions, including drafts before actual submission', () => {
  const base = { available: true, running: false, started: false, blank: true, presetId: 'standard' };
  assert.equal(engineeringStartAvailable(base), true);
  assert.equal(engineeringStartAvailable({ ...base, presetId: 'engineering' }), true);
  assert.equal(engineeringStartAvailable({ ...base, presetId: null }), true);
  for (const patch of [{ available: false }, { running: null }, { running: true }, { started: true }, { blank: false }, { presetId: 'ontology-qa' }]) {
    assert.equal(engineeringStartAvailable({ ...base, ...patch }), false);
  }
});

test('reviewed preparation explicitly selects engineering and appends without changing prior text/attachment identity or sending', async () => {
  const f = fixture();
  const result = await f.prepare();
  assert.equal(result.sessionId, 'new-session');
  assert.equal(result.draft, `已有业务说明\n\n${result.text}`);
  assert.equal(result.attachmentCount, 1);
  assert.deepEqual(f.input().attachmentIds, ['actual-attachment']);
  assert.deepEqual(f.calls.map(([name]) => name), ['preset', 'draft']);
  assert.equal(f.bridge.activity().presetId, 'engineering');
});

test('engineering sessions preserve their preset; pure document mode does not read database catalogs', async () => {
  const f = fixture(); f.setPreset('engineering');
  await f.prepare({ prepareDatabaseScope: () => { throw new Error('Document-only must not read a catalog'); } });
  assert.deepEqual(f.calls.map(([name]) => name), ['draft']);
});

test('an unselected blank preset may offer intake, and delayed preset projection cannot redirect or send the draft', async () => {
  const f = fixture(); f.setPreset(null);
  f.bridge.selectPreset = async (id, preset) => {
    f.calls.push(['preset', id, preset]);
    return preset; // Host confirmation is complete; the catalog projection lags.
  };
  await f.prepare();
  assert.deepEqual(f.calls.map(([name]) => name), ['preset', 'draft']);
  assert.equal(f.bridge.activity().presetId, null);
  assert.match(f.input().draft, /^已有业务说明\n\n请发起一个独立本体工程/);
});

test('existing reference chips and command/submission states are preserved with an explicit refusal before any preset or source request', async () => {
  for (const patch of [{ occurrences: [{ occurrenceId: 1, source: 'files', ref: 'references.md' }] }, { phase: 'claimed' }, { phase: 'submitting' }]) {
    const f = fixture(); f.edit(patch);
    const before = structuredClone(f.input());
    await assert.rejects(f.prepare(), /引用|原生输入框/);
    assert.deepEqual(f.input(), before);
    assert.deepEqual(f.calls, []);
  }
});

test('a late catalog result cannot write into a newly selected session or after disposal/source changes', async () => {
  for (const mode of ['session', 'cancel', 'started']) {
    const f = fixture(), catalog = deferred();
    let current = true;
    const pending = f.prepare({ fields: { ...fields, intakeMode: 'HYBRID' },
      prepareDatabaseScope: () => catalog.promise, isCurrent: () => current });
    if (mode === 'session') f.select('other-session');
    else if (mode === 'cancel') current = false;
    else f.setActivity({ started: true, blank: false });
    catalog.resolve(databaseScope);
    await assert.rejects(pending, /已变化/);
    assert.deepEqual(f.calls, []);
    assert.equal(f.input().draft, '已有业务说明');
  }
});

test('native edits and attachment changes during catalog verification are not overwritten', async () => {
  for (const edit of [{ draft: '用户刚修改的内容', draftRev: 3 }, { attachmentIds: ['actual-attachment', 'new-attachment'] }]) {
    const f = fixture(), catalog = deferred();
    const pending = f.prepare({ fields: { ...fields, intakeMode: 'HYBRID' }, prepareDatabaseScope: () => catalog.promise });
    f.edit(edit); const before = structuredClone(f.input());
    catalog.resolve(databaseScope);
    await assert.rejects(pending, /草稿或附件已变化/);
    assert.deepEqual(f.input(), before);
    assert.deepEqual(f.calls, []);
  }
});

test('preset rejection and edits while preset selection awaits confirmation never fill a draft', async () => {
  const f = fixture();
  f.bridge.selectPreset = async () => { throw new Error('工程模式未通过回读'); };
  await assert.rejects(f.prepare(), /未通过回读/);
  assert.deepEqual(f.calls, []);
  const g = fixture(), selected = deferred();
  g.bridge.selectPreset = async () => { await selected.promise; g.setPreset('engineering'); };
  const pending = g.prepare();
  await Promise.resolve();
  g.edit({ draft: '更晚的原生输入', draftRev: 3 }); selected.resolve();
  await assert.rejects(pending, /草稿或附件已变化/);
  assert.deepEqual(g.calls, []);
  assert.equal(g.input().draft, '更晚的原生输入');
});

const runtimeRoot = resolve(process.env.ORION_DSH_RUNTIME_ROOT || fileURLToPath(new URL('../../', import.meta.url)));
const slotPath = pathToFileURL(`${runtimeRoot}/node_modules/@deepseek-ai/dsh-client-ui-slots/lib/index.js`);
test('official SlotCore owns only the input-dock contribution and clears it on plugin teardown', {
  skip: !existsSync(slotPath) && 'Install the pinned Harness SlotCore fixture for integration verification',
}, async () => {
  const { SlotCore } = await import(slotPath.href);
  const slots = new SlotCore(), releases = [], registrations = [];
  slots.register({ name: 'root', children: { [ENGINEERING_START_SLOT]: { kind: 'list', scope: 'session' } } }, () => null);
  const ctx = { slots: {
    inject(name, fn) { assert.equal(name, ENGINEERING_START_SLOT); releases.push(fn()); },
    register(options, component) { registrations.push({ options, component }); return slots.register(options, component); },
  }, effect(fn) { releases.push(fn()); }, sessions: {
    list: { subscribe: () => () => {}, getSnapshot: () => ({ byId: {} }) }, binding: () => undefined,
  } };
  const React = { createElement: (type, props) => ({ type, props }), useSyncExternalStore: (_subscribe, read) => read() };
  const bridge = { activity: () => ({ available: true, blank: true, started: false, running: false, presetId: 'standard' }) };
  const window = { document: new Proxy({}, { get() { throw new Error('Must not read or change native DOM'); } }) };
  installEngineeringStart({ ctx, React, bridge, window, ensureReady() { throw new Error('Collapsed registration must not load assets'); } });
  assert.equal(slots.entriesOfSlot(ENGINEERING_START_SLOT).length, 1);
  const entry = registrations[0];
  assert.equal(entry.options.id, 'orion-workbench.engineering-start');
  assert.equal(typeof entry.component({ sessionId: 'blank', session: { removed: false, subagent: null } }).type, 'function');
  assert.equal(entry.component({ sessionId: 'blank', session: { removed: true } }), null);
  assert.equal(entry.component({ sessionId: 'blank', session: { subagent: {} } }), null);
  for (const release of releases.toReversed()) release();
  assert.equal(slots.entriesOfSlot(ENGINEERING_START_SLOT).length, 0);
});
