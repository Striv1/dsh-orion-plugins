import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";

import { ReadOnlyProxyError, readOnlyFailureResponse, retryReadOnly } from "../../harness/plugins/branded-web-runtime/lib/read-only-proxy.js";
import { readRealtimeEvidenceReceipt } from "../../harness/plugins/branded-web-runtime/index.js";

function fixture() {
  const binding = { session_id: "session-bound", project_id: "project-one", release_version: "1.0",
    release_fingerprint: `sha256:${"b".repeat(64)}` };
  const envelope = { schema_version: "orion-evidence-receipt-v1", receipt_id: `EVD-${"a".repeat(32)}`,
    ...binding, query_id: "query-one", response: { session_id: binding.session_id, answer: {
      complete: true, answer_status: "complete", evidence_bundle: { query_id: "query-one", release: binding,
        evidence: [{ source_kind: "structured_db", payload: { rows: [{ id: "unchanged" }] } }] },
    } } };
  const bytes = Buffer.from(JSON.stringify(envelope));
  return { binding, bytes, reference: { ...binding, receipt_id: envelope.receipt_id, query_id: envelope.query_id,
    sha256: `sha256:${createHash("sha256").update(bytes).digest("hex")}` } };
}

const disconnected = () => Object.assign(new TypeError("fetch failed https://user:private@host?token=secret"),
  { cause: { code: "UND_ERR_SOCKET", message: "private connection detail" } });

test("a stale keepalive connection retries the same immutable receipt once, never a query", async () => {
  const f = fixture();
  const urls = [];
  const value = await readRealtimeEvidenceReceipt({ realtimeQaApiUrl: "http://service" }, f.binding, f.reference,
    async (url, options) => {
      assert.equal(options.method, undefined);
      assert.match(url, /\/evidence-receipts\//);
      urls.push(url);
      if (urls.length === 1) throw disconnected();
      return { ok: true, arrayBuffer: async () => f.bytes };
    });
  assert.equal(urls.length, 2);
  assert.equal(urls[0], urls[1]);
  assert.equal(value.source_facts[0].rows[0].id, "unchanged");
});

test("a body interrupted after HTTP success can retry its immutable read", async () => {
  const f = fixture(); let attempts = 0;
  const result = await readRealtimeEvidenceReceipt({ realtimeQaApiUrl: "http://service" }, f.binding, f.reference,
    async () => ({ ok: true, arrayBuffer: async () => { attempts += 1; if (attempts === 1) throw disconnected(); return f.bytes; } }));
  assert.equal(attempts, 2);
  assert.equal(result.receipt_id, f.reference.receipt_id);
});

test("hash mismatch is never retried or changed into success", async () => {
  const f = fixture(); let attempts = 0;
  await assert.rejects(readRealtimeEvidenceReceipt({ realtimeQaApiUrl: "http://service" }, f.binding, f.reference,
    async () => { attempts += 1; return { ok: true, arrayBuffer: async () => Buffer.from("tampered") }; }),
  (error) => error.code === "EVIDENCE_HASH_MISMATCH" && error.retryable === false);
  assert.equal(attempts, 1);
});

test("reference scope mismatch is rejected before contacting the evidence service", async () => {
  const f = fixture(); let attempts = 0;
  await assert.rejects(readRealtimeEvidenceReceipt({ realtimeQaApiUrl: "http://service" }, f.binding,
    { ...f.reference, session_id: "another-session" }, async () => { attempts += 1; }),
  (error) => error.code === "EVIDENCE_REFERENCE_MISMATCH" && !error.retryable);
  assert.equal(attempts, 0);
});

test("404 is not a transient service failure and is never retried", async () => {
  const f = fixture(); let attempts = 0;
  await assert.rejects(readRealtimeEvidenceReceipt({ realtimeQaApiUrl: "http://service" }, f.binding, f.reference,
    async () => { attempts += 1; return { ok: false, status: 404 }; }),
  (error) => error.code === "EVIDENCE_UPSTREAM_NOT_FOUND" && error.status === 404 && !error.retryable);
  assert.equal(attempts, 1);
});

test("a service 503 retries once and cancels the rejected response body", async () => {
  const f = fixture(); let attempts = 0; let cancelled = 0;
  const result = await readRealtimeEvidenceReceipt({ realtimeQaApiUrl: "http://service" }, f.binding, f.reference,
    async () => {
      attempts += 1;
      if (attempts === 1) return { ok: false, status: 503, body: { cancel: async () => { cancelled += 1; } } };
      return { ok: true, arrayBuffer: async () => f.bytes };
    });
  assert.equal(attempts, 2);
  assert.equal(cancelled, 1);
  assert.equal(result.receipt_id, f.reference.receipt_id);
});

test("an updating session log gets a fresh bounded retry instead of stale cached evidence", async () => {
  let attempts = 0;
  const events = await retryReadOnly(async () => {
    attempts += 1;
    if (attempts === 1) throw new Error("Harness session log changed while reading; retry the request");
    return [{ id: "new-complete-event" }];
  }, { pause: async () => {} });
  assert.equal(attempts, 2);
  assert.deepEqual(events, [{ id: "new-complete-event" }]);
});

test("continuously changing logs stop after one retry and disclose a safe retryable code", async () => {
  let attempts = 0;
  await assert.rejects(retryReadOnly(async () => {
    attempts += 1;
    throw new Error("Harness session log changed while reading; retry the request");
  }, { pause: async () => {} }), (error) => {
    assert.deepEqual(readOnlyFailureResponse(error), { status: 409, body: {
      code: "EVIDENCE_LOG_CHANGED", retryable: true,
      detail: "会话记录仍在更新，请重新读取本次证据；未使用旧记录替代。",
    } });
    return true;
  });
  assert.equal(attempts, 2);
});

test("timeouts are manually retryable but do not double a 30-second read", async () => {
  let attempts = 0;
  await assert.rejects(retryReadOnly(async () => {
    attempts += 1;
    throw Object.assign(new Error("secret timeout URL"), { name: "TimeoutError" });
  }, { pause: async () => {} }), (error) => error.code === "EVIDENCE_READ_TIMEOUT" && error.retryable);
  assert.equal(attempts, 1);
});

test("safe failure responses never expose provider URLs, tokens or arbitrary error text", () => {
  for (const error of [disconnected(), new Error("password=secret"), new SyntaxError("token=private"),
    new Error("Unsupported Harness session generation: /private/path")]) {
    const response = readOnlyFailureResponse(error);
    assert.doesNotMatch(JSON.stringify(response), /password|secret|token|private|https?:/);
    assert.equal(typeof response.body.retryable, "boolean");
  }
  assert.equal(readOnlyFailureResponse(new ReadOnlyProxyError("EVIDENCE_IDENTITY_MISMATCH")).status, 409);
});
