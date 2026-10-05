import assert from "node:assert/strict";
import test from "node:test";
import { createNativeEvidenceReader, evidenceAnalysisPresentation, evidenceBoundary, installNativeEvidence } from "../../harness/plugins/orion-workbench/lib/native-evidence.js";

const sid = "session-00000000-0000-4000-8000-000000000001";
const other = "session-00000000-0000-4000-8000-000000000002";
const fingerprint = `sha256:${"a".repeat(64)}`;
const receiptHash = `sha256:${"b".repeat(64)}`;
const receiptId = `EVD-${"c".repeat(32)}`;
const response = value => ({ ok: true, status: 200, json: async () => value });
const copy = value => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

function fixture(t) {
  const f = { current: sid, reads: [], drafts: [], draft: "已有草稿和附件保持原样", unbound: false };
  f.binding = { session_id: sid, mode: "ontology_qa", project_id: "fixture-project", release_version: "1.0.0", release_fingerprint: fingerprint,
    database_access_mode: "READ_ONLY", qa_ready: true, agent_tools_ready: true, realtime_ready: true };
  f.call = { call_id: "real-call-1", query_id: "query-1", project_id: f.binding.project_id, release_version: "1.0.0", release_fingerprint: fingerprint,
    question: "哪些记录满足条件？", status: "partial", complete: false, receipt_reference: { receipt_id: receiptId, sha256: receiptHash,
      session_id: sid, query_id: "query-1", project_id: f.binding.project_id, release_version: "1.0.0", release_fingerprint: fingerprint } };
  f.sources = { session_id: sid, traces: [{ turn: 1, sources: ["ontology", "enterprise"], evidence_calls: [f.call] },
    { turn: 2, sources: ["internet"], evidence_calls: [] }] };
  f.detail = { session_id: sid, turn: 1, call_id: "real-call-1", evidence: { session_id: sid, project_id: f.binding.project_id, release_version: "1.0.0",
    release_fingerprint: fingerprint, query_id: "query-1", receipt_id: receiptId, receipt_sha256: receiptHash, status: "partial", complete: false,
    source_facts: [{ source_ref: "原始资料", variables: ["subject", "amount"], rows: [{ subject: "对象A", amount: null }], reported_count: 1 }], reasoning: [], warnings: ["金额缺失保持未知"] } };
  f.controllerBinding = copy(f.binding);
  f.bridge = { current: () => f.current, contains: id => [sid, other].includes(id),
    fillDraft: async (id, text) => { f.drafts.push({ id, text }); f.draft += `\n\n${text}`; return { sessionId: id, draft: f.draft }; },
    send: () => { throw new Error("An evidence view cannot send messages"); } };
  f.reader = createNativeEvidenceReader({ bridge: f.bridge, controller: () => ({ getState: () => ({ sessionId: sid, mode: "ontology", binding: f.controllerBinding }) }),
    fetch: async (path, init) => {
      const url = new URL(path, "http://fixture.invalid"); f.reads.push({ url, init });
      if (f.intercept) { const value = await f.intercept(url, init); if (value) return value; }
      if (url.pathname.endsWith("/session")) return response(f.unbound ? { bound: false } : copy(f.binding));
      return response(copy(url.searchParams.get("detail") === "1" ? f.detail : f.sources));
    } });
  t.after(() => f.reader.dispose());
  return f;
}

test("official turn identity selects only server-recorded calls and reads exact immutable receipt", async t => {
  const f = fixture(t);
  const turn = await f.reader.readTurn(sid, 1);
  assert.equal(turn.calls.length, 1);
  const result = await f.reader.readDetail(turn.calls[0].identity);
  assert.equal(result.receipt_id, receiptId);
  assert.equal(result.status, "partial");
  assert.equal(result.source_facts[0].rows[0].amount, null);
  const detail = f.reads.find(row => row.url.searchParams.get("detail") === "1");
  assert.equal(detail.url.searchParams.get("session_id"), sid);
  assert.equal(detail.url.searchParams.get("turn"), "1");
  assert.equal(detail.url.searchParams.get("call_id"), "real-call-1");
  assert.ok(f.reads.every(row => row.init.method === "GET" && row.init.credentials === "same-origin" && row.init.cache === "no-store"));
  assert.equal(f.drafts.length, 0);
});

test("ordinary unbound conversation does not request ontology evidence", async t => {
  const f = fixture(t); f.unbound = true;
  assert.equal(await f.reader.readTurn(sid, 1), null);
  assert.equal(f.reads.length, 1);
});

test("invalid or mutable binding identity cannot produce a source footer", async t => {
  const f = fixture(t); f.binding.database_access_mode = "WRITE";
  await assert.rejects(f.reader.readTurn(sid, 1), { code: "BINDING_INVALID" });
  f.binding.database_access_mode = "READ_ONLY"; f.binding.release_fingerprint = "unknown";
  await assert.rejects(f.reader.readTurn(sid, 1), { code: "BINDING_INVALID" });
  f.binding.release_fingerprint = fingerprint; f.binding.session_id = other;
  await assert.rejects(f.reader.readTurn(sid, 1), { code: "BINDING_INVALID" });
});

test("wrong release, wrong source session and duplicate calls are rejected", async t => {
  const f = fixture(t); f.call.release_version = "2.0.0";
  await assert.rejects(f.reader.readTurn(sid, 1), { code: "CALL_IDENTITY_INVALID" });
  f.call.release_version = "1.0.0"; f.sources.session_id = other;
  await assert.rejects(f.reader.readTurn(sid, 1), { code: "SOURCES_INVALID" });
  f.sources.session_id = sid; f.sources.traces[0].evidence_calls.push(copy(f.call));
  await assert.rejects(f.reader.readTurn(sid, 1), { code: "CALL_AMBIGUOUS" });
});

test("response Session, turn, call, release, query and receipt hashes are all checked", async t => {
  const changes = [
    detail => { detail.session_id = other; }, detail => { detail.turn = 2; }, detail => { detail.call_id = "another-call"; },
    detail => { detail.evidence.session_id = other; }, detail => { detail.evidence.project_id = "other-project"; },
    detail => { detail.evidence.release_version = "2.0.0"; }, detail => { detail.evidence.release_fingerprint = `sha256:${"f".repeat(64)}`; },
    detail => { detail.evidence.query_id = "other-query"; }, detail => { detail.evidence.receipt_id = `EVD-${"d".repeat(32)}`; },
    detail => { detail.evidence.receipt_sha256 = `sha256:${"e".repeat(64)}`; },
  ];
  for (const change of changes) {
    const f = fixture(t); const { calls } = await f.reader.readTurn(sid, 1); change(f.detail);
    await assert.rejects(f.reader.readDetail(calls[0].identity), { code: "DETAIL_IDENTITY_INVALID" });
  }
});

test("unknown or contradictory completion flags never become a successful conclusion", () => {
  assert.match(evidenceBoundary({ status: "unknown", complete: true }), /尚未确认/);
  assert.match(evidenceBoundary({ status: "complete", complete: false }), /尚未确认/);
  assert.match(evidenceBoundary({ status: "partial", complete: true }), /部分证据/);
  assert.match(evidenceBoundary({ status: "no_evidence", complete: true }), /没有取得/);
});

test("a late read after switching Session cannot display or fill another conversation", async t => {
  const f = fixture(t), wait = deferred(), began = deferred();
  f.intercept = async url => { if (url.pathname.endsWith("/sources")) { began.resolve(); await wait.promise; return response(copy(f.sources)); } };
  const read = f.reader.readTurn(sid, 1); await began.promise; f.current = other; wait.resolve();
  await assert.rejects(read, { code: "SESSION_CHANGED" }); assert.equal(f.drafts.length, 0);
});

test("a late source response after the native release changes cannot be reused", async t => {
  const f = fixture(t), wait = deferred(), began = deferred();
  f.intercept = async url => { if (url.pathname.endsWith("/sources")) { began.resolve(); await wait.promise; return response(copy(f.sources)); } };
  const read = f.reader.readTurn(sid, 1); await began.promise;
  f.controllerBinding.release_fingerprint = `sha256:${"f".repeat(64)}`; wait.resolve();
  await assert.rejects(read, { code: "RELEASE_CHANGED" });
});

test("disable aborts owned reads and cannot resurrect cached evidence", async t => {
  const f = fixture(t), wait = deferred(), began = deferred(); let signal;
  f.intercept = async (url, init) => { if (url.pathname.endsWith("/sources")) { signal = init.signal; began.resolve(); await wait.promise; return response(copy(f.sources)); } };
  const read = f.reader.readTurn(sid, 1); await began.promise; f.reader.dispose();
  assert.equal(signal.aborted, true); wait.resolve();
  await assert.rejects(read, { code: "DISPOSED" });
  await assert.rejects(f.reader.readTurn(sid, 1), { code: "DISPOSED" });
});

test("follow-up appends an unsent native draft only after verifying the current receipt", async t => {
  const f = fixture(t); const { calls } = await f.reader.readTurn(sid, 1);
  const result = await f.reader.followUp(calls[0].identity, "还有哪些金额未知？");
  assert.ok(result.draft.startsWith("已有草稿和附件保持原样"));
  assert.match(result.draft, /金额未知/); assert.ok(result.draft.includes(receiptId));
  assert.match(result.draft, /不要修改本体或推定批准/); assert.equal(f.drafts.length, 1);
  f.current = other;
  await assert.rejects(f.reader.followUp(calls[0].identity, "其他问题"), { code: "SESSION_CHANGED" });
  assert.equal(f.drafts.length, 1);
});

test("analysis remains a read-only receipt view and never treats IDs or untyped strings as metrics", () => {
  const source = { variables: ["id", "amount", "lexical", "typed", "missing"], rows: [{ id: 12, amount: 4, lexical: "5", typed: "6", missing: null }],
    row_terms: [{ typed: { type: "literal", value: "6", datatype: "http://www.w3.org/2001/XMLSchema#decimal" } }] };
  const value = evidenceAnalysisPresentation(source, { session_id: sid, project_id: "fixture-project", release_version: "1.0.0", release_fingerprint: fingerprint });
  assert.deepEqual(value.options.metricFields, ["amount", "typed"]);
  assert.equal(value.options.readOnly, true); assert.equal(value.options.skipCatalog, true);
  assert.equal(value.result.rows[0].missing, null);
  assert.throws(() => evidenceAnalysisPresentation({ rows: [null] }, {}), { code: "ANALYSIS_NOT_TABULAR" });
});

test("native evidence occupies the additive official turnTail slot without touching page DOM", () => {
  let declaration, component, teardown;
  const ctx = { slots: { inject: (name, mount) => { assert.equal(name, "conversation.chat.turnTail"); return mount(); },
    register: (options, value) => { declaration = options; component = value; return () => {}; } }, effect: callback => { teardown = callback(); } };
  const React = { createElement: () => {}, useState: value => [value, () => {}], useEffect: () => {} };
  const result = installNativeEvidence({ ctx, React, bridge: { current: () => sid, contains: () => true }, fetch: async () => { throw new Error("No read while mounting or rendering an unclosed turn"); } });
  assert.equal(declaration.id, "orion-workbench.evidence"); assert.equal(declaration.name, "conversation.chat.turnTail");
  assert.equal(component({ sessionId: sid, turn: { turn: 1, status: "unknown" }, seq: 3 }), null);
  teardown(); result.dispose();
});
