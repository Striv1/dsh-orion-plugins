import assert from "node:assert/strict";
import test from "node:test";
import { installNativeChat } from "../../harness/plugins/orion-workbench/lib/native-chat.js";

const projectId = "ontology-project-fixture";
const initialSession = "session-00000000-0000-4000-8000-000000000001";
const hash = `sha256:${"a".repeat(64)}`;
const asset = { sourceProjectId: projectId, name: "隔离本体", version: "0.1.0", release_fingerprint: hash };
const response = (status, payload) => ({ status, ok: status >= 200 && status < 300, json: async () => payload });
const readyBinding = (sessionId, changes = {}) => ({ mode: "ontology_qa", session_id: sessionId,
  project_id: projectId, project_name: "隔离本体", release_version: "0.1.0", release_fingerprint: hash,
  database_access_mode: "READ_ONLY", realtime_ready: true, agent_tools_ready: true, qa_ready: true, ...changes });

function fixture(t, options = {}) {
  const calls = [];
  const storageValues = new Map(options.stored ? [["orion.engineering.projectSessions.v1", JSON.stringify(options.stored)]] : []);
  const sessions = new Map([[initialSession, { draft: "原生旧草稿", preset: "engineering", attachmentIds: ["keep-attachment"] }]]);
  const bindings = new Map(options.bindings ?? []);
  let current = initialSession;
  let number = 1;
  const storage = { getItem: key => storageValues.get(key) ?? null,
    setItem: (key, value) => storageValues.set(key, value), removeItem: key => storageValues.delete(key) };
  const bridge = {
    current: () => current,
    contains: id => sessions.has(id),
    workspaces: () => [{ workspaceId: "chosen-workspace", title: "隔离工作区", sessionIds: [...sessions.keys()] }],
    currentWorkspace: () => ({ workspaceId: "chosen-workspace" }),
    showWorkbench: () => calls.push(["show-workbench"]),
    async create(workspaceId) {
      calls.push(["create", workspaceId]);
      current = `session-00000000-0000-4000-8000-${(++number).toString(16).padStart(12, "0")}`;
      sessions.set(current, { draft: "", preset: "standard", attachmentIds: [] });
      return current;
    },
    async open(id) { calls.push(["open", id]); current = id; return true; },
    async selectPreset(id, preset) {
      calls.push(["preset", id, preset]);
      sessions.get(id).preset = preset;
      return options.selectPreset ? options.selectPreset(id, preset) : preset;
    },
    fillDraft(id, text) {
      calls.push(["draft", id, text]);
      const session = sessions.get(id);
      session.draft = session.draft ? `${session.draft}\n\n${text}` : text;
      return { sessionId: id, draft: session.draft, attachmentCount: session.attachmentIds.length };
    },
    activity: id => options.activity ?? { available: true, running: false },
    send() { throw new Error("A navigation controller must never send a message"); },
  };
  const fetcher = async (path, init) => {
    calls.push(["fetch", path, init]);
    const url = new URL(path, "http://fixture.invalid");
    if (options.fetch) {
      const custom = await options.fetch(url, init, { sessions, bindings, bridge });
      if (custom) return custom;
    }
    if (url.pathname === "/orion-workflow-api/status") {
      return response(200, { state: { project_id: projectId, project_name: "正式工程名", revision: 24,
        current_stage: "S2", stage_statuses: { S0: "PASSED", S1: "PASSED" }, ...options.formal },
        project: { project_id: projectId } });
    }
    if (url.pathname === "/orion-engineering-session-api") {
      return options.discovered ? response(200, options.discovered) : response(404, { project_id: projectId });
    }
    if (url.pathname === "/orion-ontology-qa-api/bind") {
      const body = JSON.parse(init.body);
      const binding = readyBinding(body.session_id, options.binding);
      bindings.set(body.session_id, binding);
      return response(201, binding);
    }
    if (url.pathname === "/orion-ontology-qa-api/session") {
      const id = url.searchParams.get("session_id");
      return response(200, bindings.get(id) ?? { bound: false });
    }
    throw new Error(`Unexpected endpoint: ${url.pathname}`);
  };
  const window = { localStorage: storage, get document() { throw new Error("Native chat must not inspect DOM"); } };
  const controller = installNativeChat({ bridge, fetch: fetcher, window, storage,
    state: { navigate: route => calls.push(["navigate", route]) }, sleep: async () => {} });
  t.after(() => controller.dispose());
  return { controller, bridge, sessions, bindings, calls, window, storageValues,
    switchSession: id => { current = id; } };
}

test("native new session selects engineering without sending, preserves previous draft and attachments", async (t) => {
  const f = fixture(t);
  const result = await f.controller.openNewSession();
  assert.notEqual(result.sessionId, initialSession);
  assert.equal(result.mode, "general");
  assert.equal(f.sessions.get(result.sessionId).preset, "engineering");
  assert.deepEqual(f.sessions.get(initialSession), { draft: "原生旧草稿", preset: "engineering", attachmentIds: ["keep-attachment"] });
  assert.equal(f.calls.filter(([name]) => name === "draft" || name === "fetch").length, 0);
  assert.deepEqual(f.calls.find(([name]) => name === "create"), ["create", "chosen-workspace"]);
});

test("workbench uses current formal revision/stage and fills an unsent approval-preserving draft", async (t) => {
  const f = fixture(t);
  const result = await f.controller.openWorkbench(projectId, { projectName: "旧名称", stage: "S7", detail: "先解释现有记录。" });
  assert.equal(result.context.projectName, "正式工程名");
  assert.equal(result.context.revision, 24);
  assert.equal(result.context.stage, "S2");
  assert.equal(result.context.viewedStage, "S7");
  assert.equal(f.sessions.get(result.sessionId).draft, result.draft);
  assert.match(result.draft, /revision: 24/);
  assert.match(result.draft, /S7 仅是历史上下文/);
  assert.match(result.draft, /不能推定批准或自动提交/);
  assert.match(result.draft, /先解释现有记录/);
  assert.equal(f.sessions.get(initialSession).draft, "原生旧草稿");
  assert.deepEqual(JSON.parse(f.storageValues.get("orion.engineering.projectSessions.v1")), { [projectId]: result.sessionId });
  assert.equal(f.controller.rememberProjectSession(projectId, "正式工程名"), true);
  assert.equal(f.controller.rememberProjectSession("ontology-project-other", "正式工程名"), false);
});

test("stale revision, unauthorized reads and mismatched formal project reject before native creation", async (t) => {
  for (const settings of [
    { formal: { revision: 25 }, context: { expected_revision: 24 }, expected: "PROJECT_REVISION_CHANGED" },
    { formal: { project_id: "ontology-project-other" }, expected: "PROJECT_STATE_INVALID" },
    { fetch: async () => response(403, { detail: "当前用户无权读取该工程" }), expected: "API_REJECTED" },
  ]) {
    const f = fixture(t, settings);
    await assert.rejects(f.controller.openWorkbench(projectId, settings.context), { code: settings.expected });
    assert.equal(f.calls.some(([name]) => ["create", "preset", "draft"].includes(name)), false);
  }
});

test("an unverified local session map cannot associate a different project's discovered session", async (t) => {
  const f = fixture(t, { stored: { [projectId]: initialSession },
    discovered: { project_id: "ontology-project-other", session_id: initialSession } });
  await assert.rejects(f.controller.openWorkbench(projectId), { code: "PROJECT_SESSION_MISMATCH" });
  assert.equal(f.calls.some(([name]) => name === "open" || name === "draft"), false);
  assert.equal(f.controller.rememberProjectSession(projectId, initialSession), false);
});

test("an existing engineering session is restored and its native draft and attachments are preserved", async (t) => {
  const f = fixture(t, { discovered: { project_id: projectId, session_id: initialSession } });
  const result = await f.controller.openWorkbench(projectId);
  assert.equal(result.sessionId, initialSession);
  assert.equal(f.calls.some(([name]) => name === "create"), false);
  assert.match(f.sessions.get(initialSession).draft, /^原生旧草稿\n\n/);
  assert.deepEqual(f.sessions.get(initialSession).attachmentIds, ["keep-attachment"]);
});

test("an existing release-bound QA session is never repurposed as an engineering session", async (t) => {
  const original = readyBinding(initialSession);
  const f = fixture(t, { discovered: { project_id: projectId, session_id: initialSession }, bindings: [[initialSession, original]] });
  f.sessions.get(initialSession).preset = "ontology-qa";
  const result = await f.controller.openWorkbench(projectId);
  assert.notEqual(result.sessionId, initialSession);
  assert.equal(f.sessions.get(initialSession).preset, "ontology-qa");
  assert.equal(f.sessions.get(initialSession).draft, "原生旧草稿");
  assert.deepEqual(f.bindings.get(initialSession), original);
});

test("checkpoint recovery refuses running or unknown previous activity without creating a second executor", async (t) => {
  for (const activity of [{ available: true, running: true }, { available: false, running: null }]) {
    const f = fixture(t, { discovered: { project_id: projectId, session_id: initialSession }, activity });
    await assert.rejects(f.controller.openWorkbench(projectId, { freshSession: true }), { code: "PROJECT_ACTIVITY_UNKNOWN" });
    assert.equal(f.calls.some(([name]) => name === "create" || name === "draft"), false);
  }
});

test("QA opening binds and reads back one exact release through authenticated existing endpoints without drafting or sending", async (t) => {
  const f = fixture(t);
  const result = await f.controller.openOntology(asset);
  assert.equal(result.mode, "ontology");
  assert.equal(result.binding.project_id, projectId);
  assert.equal(result.binding.release_fingerprint, hash);
  assert.equal(result.asset.version, "0.1.0");
  assert.equal(f.sessions.get(result.sessionId).preset, "ontology-qa");
  assert.equal(f.calls.some(([name]) => name === "draft"), false);
  const [, path, request] = f.calls.find(([name, path]) => name === "fetch" && path.endsWith("/bind"));
  assert.equal(path, "/orion-ontology-qa-api/bind");
  assert.equal(request.credentials, "same-origin");
  assert.equal(request.headers["x-orion-ontology-qa-binding"], "1");
  assert.deepEqual(JSON.parse(request.body), { session_id: result.sessionId, project_id: projectId });
  assert.equal(f.calls.filter(([name, path]) => name === "fetch" && path.includes("/session?")).length, 1);
});

test("QA response mismatches and readiness failures cannot become an accepted binding", async (t) => {
  const variants = [
    [{ project_id: "ontology-project-other" }, "QA_BINDING_MISMATCH"],
    [{ session_id: initialSession }, "QA_BINDING_MISMATCH"],
    [{ release_fingerprint: "invalid" }, "QA_BINDING_MISMATCH"],
    [{ release_version: "0.2.0" }, "QA_RELEASE_CHANGED"],
    [{ release_fingerprint: `sha256:${"b".repeat(64)}` }, "QA_RELEASE_CHANGED"],
    [{ agent_tools_ready: false }, "QA_NOT_READY"],
    [{ qa_ready: false }, "QA_NOT_READY"],
    [{ database_access_mode: "READ_WRITE" }, "QA_NOT_READY"],
  ];
  for (const [binding, code] of variants) {
    const f = fixture(t, { binding });
    await assert.rejects(f.controller.openOntology(asset), { code });
    assert.equal(f.controller.getState().binding, null);
    assert.equal(f.calls.some(([name]) => name === "draft"), false);
  }
});

test("QA read-back protects persistence identity and requires authenticated success", async (t) => {
  for (const tamper of [response(200, readyBinding(initialSession)), response(403, { detail: "绑定读回未授权" })]) {
    const f = fixture(t, { fetch: async (url) => url.pathname === "/orion-ontology-qa-api/session" ? tamper : null });
    await assert.rejects(f.controller.openOntology(asset));
    assert.equal(f.controller.getState().binding, null);
  }
});

test("native preset rejection prevents binding and filling any draft", async (t) => {
  const f = fixture(t, { selectPreset: () => "standard" });
  await assert.rejects(f.controller.openOntology(asset), { code: "PRESET_MISMATCH" });
  assert.equal(f.calls.some(([name]) => name === "fetch" || name === "draft"), false);
});

test("only explicit not-ready tool response retries the idempotent binding for the same session", async (t) => {
  let retries = 0;
  const f = fixture(t, { fetch: async url => url.pathname.endsWith("/bind") && retries++ === 0
    ? response(503, { agent_tools_ready: false, detail: "会话工具初始化中" }) : null });
  const result = await f.controller.openOntology(asset);
  const bindCalls = f.calls.filter(([name, path]) => name === "fetch" && path.endsWith("/bind"));
  assert.equal(bindCalls.length, 2);
  assert.equal(bindCalls[0][2].body, bindCalls[1][2].body);
  assert.equal(JSON.parse(bindCalls[0][2].body).session_id, result.sessionId);
  assert.equal(f.calls.filter(([name]) => name === "create").length, 1);
});

test("templates use native draft storage, preserve attachments and reject another current session or project", async (t) => {
  const f = fixture(t);
  const opened = await f.controller.openOntology(asset);
  f.sessions.get(opened.sessionId).attachmentIds.push("keep-qa-attachment");
  const written = await f.controller.applyTemplate("请说明当前业务记录。", { projectId });
  assert.equal(written.sessionId, opened.sessionId);
  assert.equal(written.attachmentCount, 1);
  assert.equal(f.sessions.get(opened.sessionId).draft, "请说明当前业务记录。");
  await assert.rejects(f.controller.applyTemplate("不能越界", { projectId: "ontology-project-other" }), { code: "PROJECT_SESSION_MISMATCH" });
  f.switchSession(initialSession);
  await assert.rejects(f.controller.applyTemplate("不能写到旧会话"), { code: "QA_BINDING_MISSING" });
  assert.equal(f.sessions.get(initialSession).draft, "原生旧草稿");
});

test("newer navigation and disposal cannot commit an obsolete in-flight QA response", async (t) => {
  let resolveBind;
  let started;
  const ready = new Promise(resolve => { started = resolve; });
  const pending = new Promise(resolve => { resolveBind = resolve; });
  const f = fixture(t, { fetch: async (url, init) => {
    if (!url.pathname.endsWith("/bind")) return null;
    started();
    const result = await pending;
    return response(201, readyBinding(JSON.parse(init.body).session_id, result));
  } });
  const obsolete = f.controller.openOntology(asset);
  const obsoleteRejected = assert.rejects(obsolete, { code: "OPERATION_CANCELLED" });
  await ready;
  const replacement = f.controller.openNewSession();
  resolveBind({});
  await obsoleteRejected;
  const result = await replacement;
  assert.equal(result.mode, "general");
  assert.equal(result.binding, null);
  assert.equal(f.controller.getState().sessionId, result.sessionId);
  f.controller.dispose();
  assert.equal(f.window.__ORION_CHAT_MODES__, undefined);
  await assert.rejects(f.controller.openNewSession(), { code: "OPERATION_CANCELLED" });
});
