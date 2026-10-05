const PROJECT_ID = /^[a-z][a-z0-9-]{2,100}$/u;
const SESSION_ID = /^session-[a-f0-9-]{36}$/u;
const RELEASE_VERSION = /^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$/u;
const RELEASE_HASH = /^sha256:[a-f0-9]{64}$/u;
const PROJECT_SESSIONS = "orion.engineering.projectSessions.v1";
const WORKSPACE_KEY = "orion.chat.preferredWorkspaceId";
const QA_API = "/orion-ontology-qa-api";
const copy = (value) => value == null ? value : JSON.parse(JSON.stringify(value));
const stage = (value) => /^S[0-7]$/u.test(String(value ?? "")) ? value : null;
const fail = (message, code) => Object.assign(new Error(message), { code });

/**
 * Business chat navigation through the native session bridge. Opening a page,
 * selecting a preset and filling a draft never send a message or approve an
 * engineering action. The host continues to own all execution guards.
 */
export function installNativeChat({
  bridge,
  state,
  fetch: fetcher = globalThis.fetch,
  window: hostWindow = globalThis.window,
  storage = hostWindow?.localStorage,
  onChange = () => {},
  onError = () => {},
  sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  publish = true,
} = {}) {
  let disposed = false;
  let generation = 0;
  let activeRequest = null;
  let queue = Promise.resolve();
  let snapshot = { mode: "general", asset: null, binding: null, context: null, sessionId: null, error: null };
  const projectSessions = new Map();
  const previousController = hostWindow?.__ORION_CHAT_MODES__;

  const readStorage = (key) => {
    try { return storage?.getItem(key) ?? null; } catch { return null; }
  };
  const writeStorage = (key, value) => {
    try {
      if (value == null) storage?.removeItem(key);
      else storage?.setItem(key, value);
    } catch { /* Session navigation remains available without browser storage. */ }
  };
  try {
    const stored = JSON.parse(readStorage(PROJECT_SESSIONS) ?? "{}");
    for (const [projectId, sessionId] of Object.entries(stored)) {
      if (PROJECT_ID.test(projectId) && SESSION_ID.test(sessionId)) {
        projectSessions.set(projectId, { sessionId, verified: false });
      }
    }
  } catch { /* A stale local map is never business binding evidence. */ }

  const nativeBridge = () => {
    const value = typeof bridge === "function" ? bridge() : bridge ?? hostWindow?.__ORION_DSH_SESSIONS__;
    if (!value || typeof value.current !== "function" || typeof value.contains !== "function") {
      throw fail("原生会话服务尚未就绪，请稍后重试。", "SESSION_BRIDGE_UNAVAILABLE");
    }
    return value;
  };
  const update = (next) => {
    snapshot = { ...snapshot, ...next };
    onChange(copy(snapshot));
  };
  const check = (request, sessionId = null) => {
    if (disposed || generation !== request.id) throw fail("会话操作已经取消，请使用当前入口重试。", "OPERATION_CANCELLED");
    if (sessionId && nativeBridge().current() !== sessionId) {
      throw fail("会话已切换，本次操作没有继续，请在目标会话重试。", "SESSION_CHANGED");
    }
  };
  const operation = (task) => {
    const request = { id: ++generation, controller: new AbortController() };
    activeRequest?.controller.abort();
    activeRequest = request;
    const result = queue.then(async () => {
      check(request);
      try { return await task(request); }
      catch (error) {
        if (!disposed && request.id === generation) {
          update({ error: { message: error.message, code: error.code ?? "CHAT_OPERATION_FAILED" } });
          onError(error);
        }
        throw error;
      }
    });
    queue = result.catch(() => {});
    return result;
  };
  const requestJson = async (request, path, options = {}, allowMissing = false) => {
    check(request);
    if (typeof fetcher !== "function") throw fail("工作台接口不可用。", "API_UNAVAILABLE");
    const response = await fetcher(path, {
      credentials: "same-origin",
      ...options,
      headers: { Accept: "application/json", ...options.headers },
      signal: request.controller.signal,
    });
    check(request);
    const payload = await response.json().catch(() => null);
    check(request);
    if (allowMissing && response.status === 404) return null;
    if (!response.ok) {
      throw Object.assign(fail(payload?.detail ?? `工作台接口读取失败（${response.status}）`, "API_REJECTED"),
        { status: response.status, payload });
    }
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw fail("工作台接口未返回有效对象。", "API_RESPONSE_INVALID");
    }
    return payload;
  };
  const ensureCurrent = async (request, sessionId, open = false) => {
    if (!SESSION_ID.test(String(sessionId)) || !nativeBridge().contains(sessionId)) {
      throw fail("原生会话不存在，请重新打开工作台。", "SESSION_MISSING");
    }
    if (open) {
      if (typeof nativeBridge().open !== "function") throw fail("原生会话打开接口不可用。", "SESSION_BRIDGE_UNAVAILABLE");
      await nativeBridge().open(sessionId);
    }
    for (let attempt = 0; nativeBridge().current() !== sessionId; attempt += 1) {
      check(request);
      if (attempt >= 20) throw fail("原生会话尚未完成切换，请稍后重试。", "SESSION_SWITCH_PENDING");
      await sleep(50);
    }
    check(request, sessionId);
  };
  const workspaceId = () => {
    const value = nativeBridge();
    const workspaces = typeof value.workspaces === "function" ? value.workspaces() : [];
    const preferred = readStorage(WORKSPACE_KEY);
    if (workspaces.some((workspace) => workspace.workspaceId === preferred)) return preferred;
    return value.currentWorkspace?.()?.workspaceId ?? null;
  };
  const createSession = async (request) => {
    const value = nativeBridge();
    if (typeof value.create !== "function") throw fail("原生会话创建接口不可用。", "SESSION_BRIDGE_UNAVAILABLE");
    const sessionId = await value.create(workspaceId());
    check(request);
    await ensureCurrent(request, sessionId);
    return sessionId;
  };
  const selectPreset = async (request, sessionId, presetId) => {
    check(request, sessionId);
    if (typeof nativeBridge().selectPreset !== "function") throw fail("原生会话模式接口不可用。", "PRESET_UNAVAILABLE");
    const selected = await nativeBridge().selectPreset(sessionId, presetId);
    check(request, sessionId);
    if (selected !== presetId) throw fail("原生会话模式回读与请求不一致。", "PRESET_MISMATCH");
  };
  const fillDraft = async (request, sessionId, text) => {
    check(request, sessionId);
    if (typeof nativeBridge().fillDraft !== "function") throw fail("原生草稿接口不可用，内容尚未发送。", "DRAFT_UNAVAILABLE");
    const result = await nativeBridge().fillDraft(sessionId, text);
    check(request, sessionId);
    if (result?.sessionId !== sessionId || typeof result.draft !== "string") {
      throw fail("原生草稿回读未确认，内容尚未发送。", "DRAFT_READBACK_INVALID");
    }
    return result;
  };
  const showChat = () => {
    nativeBridge().showWorkbench?.();
    state?.navigate?.({ primary: "chat" });
  };
  const readQaBinding = async (request, sessionId) => {
    const payload = await requestJson(request, `${QA_API}/session?${new URLSearchParams({ session_id: sessionId })}`, {}, true);
    if (!payload || payload.bound === false) return null;
    if (payload.session_id !== sessionId || payload.mode !== "ontology_qa"
      || !PROJECT_ID.test(String(payload.project_id)) || !RELEASE_VERSION.test(String(payload.release_version))
      || !RELEASE_HASH.test(String(payload.release_fingerprint))) {
      throw fail("问答会话返回的正式版本身份无效。", "QA_BINDING_INVALID");
    }
    return payload;
  };
  const validateQaBinding = (payload, sessionId, asset) => {
    if (payload?.session_id !== sessionId || payload.mode !== "ontology_qa"
      || payload.project_id !== asset.sourceProjectId || !RELEASE_VERSION.test(String(payload.release_version))
      || !RELEASE_HASH.test(String(payload.release_fingerprint))) {
      throw fail("问答会话绑定与所选本体身份不一致。", "QA_BINDING_MISMATCH");
    }
    if ((asset.version && asset.version !== payload.release_version)
      || (asset.release_fingerprint && asset.release_fingerprint !== payload.release_fingerprint)) {
      throw fail("正式本体版本已变化，请回到本体管理刷新后重新发起问答。", "QA_RELEASE_CHANGED");
    }
    if (payload.realtime_ready !== true || payload.agent_tools_ready !== true
      || payload.qa_ready !== true || payload.database_access_mode !== "READ_ONLY") {
      throw fail("正式版本或当前会话问答工具尚未通过就绪校验。", "QA_NOT_READY");
    }
    return payload;
  };
  const remember = (projectId, sessionId, revision) => {
    projectSessions.set(projectId, { sessionId, revision, verified: true });
    writeStorage(PROJECT_SESSIONS, JSON.stringify(Object.fromEntries(
      [...projectSessions].map(([id, value]) => [id, value.sessionId]),
    )));
  };
  const discoverSession = async (request, projectId) => {
    const known = projectSessions.get(projectId);
    if (known?.verified && nativeBridge().contains(known.sessionId)) return known.sessionId;
    const result = await requestJson(request, `/orion-engineering-session-api?${new URLSearchParams({ project_id: projectId })}`, {}, true);
    if (!result) return null;
    if (result.project_id !== projectId || !SESSION_ID.test(String(result.session_id))) {
      throw fail("工程会话回读与请求工程不一致。", "PROJECT_SESSION_MISMATCH");
    }
    return nativeBridge().contains(result.session_id) ? result.session_id : null;
  };

  const controller = {
    getState: () => copy(snapshot),
    openNewSession: () => operation(async (request) => {
      const sessionId = await createSession(request);
      await selectPreset(request, sessionId, "engineering");
      update({ mode: "general", sessionId, asset: null, binding: null, context: null, error: null });
      showChat();
      return controller.getState();
    }),
    openWorkbench: (projectId, context = {}) => operation(async (request) => {
      if (!PROJECT_ID.test(String(projectId))) throw fail("工程编号无效。", "PROJECT_ID_INVALID");
      const payload = await requestJson(request, `/orion-workflow-api/status?${new URLSearchParams({ project_id: projectId })}`);
      const formal = payload.state;
      if (!formal || formal.project_id !== projectId || (payload.project?.project_id && payload.project.project_id !== projectId)
        || !Number.isInteger(formal.revision) || formal.revision < 0) {
        throw fail("工程正式状态与请求身份不一致。", "PROJECT_STATE_INVALID");
      }
      const expectedRevision = context.expected_revision ?? context.revision;
      if (expectedRevision != null && expectedRevision !== formal.revision) {
        throw fail("工程修订已变化，请回读当前工程后重试。", "PROJECT_REVISION_CHANGED");
      }
      let sessionId = await discoverSession(request, projectId);
      if (sessionId && context.freshSession === true) {
        let activity;
        for (let attempt = 0; attempt < 20; attempt += 1) {
          check(request);
          activity = nativeBridge().activity?.(sessionId);
          if (activity?.available === true && typeof activity.running === "boolean") break;
          await sleep(80);
        }
        if (activity?.available !== true || activity.running !== false) {
          throw fail("原工程会话仍在执行或状态尚未确认，请先停止原会话再恢复检查点。", "PROJECT_ACTIVITY_UNKNOWN");
        }
        sessionId = null;
      }
      if (sessionId && await readQaBinding(request, sessionId)) sessionId = null;
      if (sessionId) await ensureCurrent(request, sessionId, true);
      else sessionId = await createSession(request);
      await selectPreset(request, sessionId, "engineering");
      const currentStage = stage(formal.current_stage);
      const nextContext = {
        projectId, projectName: String(formal.project_name ?? payload.project?.project_name ?? projectId),
        revision: formal.revision, stage: currentStage, viewedStage: stage(context.viewedStage ?? context.stage),
        intent: context.intent === "diagnose" ? "diagnose" : "continue", freshSession: context.freshSession === true,
      };
      const action = nextContext.intent === "diagnose" ? "诊断当前工程问题" : "继续核对当前工程";
      const draft = `${action}：工程 ${projectId}（revision: ${formal.revision}）${currentStage ? `，当前阶段 ${currentStage}` : ""}。`
        + "请先回读真实工作流状态、get_next_workflow_action 和当前草稿，核对来源证据和已完成产物。不要重做已通过阶段；有受管任务先读取其状态，禁止重复启动。"
        + "修订和阶段以最新正式回执为准。保持当前用户授权范围；任何人工确认、审批与发布都必须沿现有门禁，不能推定批准或自动提交。"
        + (nextContext.viewedStage && nextContext.viewedStage !== currentStage
          ? `刚才查看的 ${nextContext.viewedStage} 仅是历史上下文，不是执行阶段。` : "")
        + (typeof context.detail === "string" && context.detail.trim() ? ` ${context.detail.trim()}` : "");
      const written = await fillDraft(request, sessionId, draft);
      remember(projectId, sessionId, formal.revision);
      update({ mode: "general", sessionId, context: nextContext, asset: null, binding: null, error: null });
      showChat();
      return { ...controller.getState(), draft: written.draft };
    }),
    openOntology: (asset) => operation(async (request) => {
      const projectId = asset?.sourceProjectId ?? asset?.ontologyId;
      if (!PROJECT_ID.test(String(projectId))) throw fail("正式本体工程编号无效。", "PROJECT_ID_INVALID");
      if ([asset.releaseStatus, asset.status, asset.versionStatus].some((value) => ["RELEASE_REVOKED", "REVOKED"].includes(value))) {
        throw fail("已撤回本体不能发起新的正式问答。", "QA_RELEASE_REVOKED");
      }
      const expected = {
        sourceProjectId: projectId, ontologyId: asset.ontologyId ?? projectId,
        name: String(asset.name ?? projectId), qualityStatus: asset.qualityStatus ?? null,
        version: RELEASE_VERSION.test(String(asset.version)) ? asset.version : null,
        release_fingerprint: asset.release_fingerprint ?? asset.releaseFingerprint ?? null,
      };
      if (expected.release_fingerprint && !RELEASE_HASH.test(String(expected.release_fingerprint))) {
        throw fail("正式本体指纹无效。", "QA_BINDING_INVALID");
      }
      const sessionId = await createSession(request);
      await selectPreset(request, sessionId, "ontology-qa");
      let bound;
      for (let attempt = 0; attempt < 3; attempt += 1) {
        check(request, sessionId);
        try {
          bound = await requestJson(request, `${QA_API}/bind`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "x-orion-ontology-qa-binding": "1" },
            body: JSON.stringify({ session_id: sessionId, project_id: projectId }),
          });
          break;
        } catch (error) {
          if (error.status !== 503 || error.payload?.agent_tools_ready !== false || attempt === 2) throw error;
          await sleep(attempt === 0 ? 500 : 1500);
        }
      }
      check(request, sessionId);
      validateQaBinding(bound, sessionId, expected);
      const readback = await readQaBinding(request, sessionId);
      check(request, sessionId);
      validateQaBinding(readback, sessionId, { ...expected, version: bound.release_version, release_fingerprint: bound.release_fingerprint });
      update({ mode: "ontology", sessionId, context: null, binding: readback, error: null,
        asset: { ...expected, name: readback.project_name ?? expected.name, version: readback.release_version,
          release_fingerprint: readback.release_fingerprint } });
      showChat();
      return controller.getState();
    }),
    applyTemplate: (template, options = {}) => operation(async (request) => {
      const text = typeof template === "string" ? template : template?.text ?? template?.draft;
      if (typeof text !== "string" || !text.trim()) throw fail("模板草稿为空。", "DRAFT_EMPTY");
      const sessionId = options.sessionId ?? nativeBridge().current();
      await ensureCurrent(request, sessionId);
      if (options.projectId && options.projectId !== (snapshot.context?.projectId ?? snapshot.asset?.sourceProjectId)) {
        throw fail("模板与当前工程或本体身份不一致。", "PROJECT_SESSION_MISMATCH");
      }
      if (snapshot.mode === "ontology") {
        if (snapshot.sessionId !== sessionId || !snapshot.binding) throw fail("当前会话未绑定所选正式本体。", "QA_BINDING_MISSING");
        const readback = await readQaBinding(request, sessionId);
        validateQaBinding(readback, sessionId, snapshot.asset);
      }
      return fillDraft(request, sessionId, text);
    }),
    rememberProjectSession(projectId, sessionId) {
      if (disposed || !PROJECT_ID.test(String(projectId))) return false;
      // Legacy callers pass a project name. Only a previously verified native
      // workbench context can associate that call; visible text is not evidence.
      const selected = SESSION_ID.test(String(sessionId)) ? sessionId : nativeBridge().current();
      if (snapshot.mode !== "general" || snapshot.context?.projectId !== projectId
        || snapshot.sessionId !== selected || nativeBridge().current() !== selected) return false;
      remember(projectId, selected, snapshot.context.revision);
      return true;
    },
    refresh: () => operation(async (request) => {
      const sessionId = nativeBridge().current();
      if (!SESSION_ID.test(String(sessionId))) return controller.getState();
      const binding = await readQaBinding(request, sessionId);
      check(request, sessionId);
      if (binding) {
        const asset = { sourceProjectId: binding.project_id, ontologyId: binding.project_id,
          name: binding.project_name ?? binding.project_id, version: binding.release_version,
          release_fingerprint: binding.release_fingerprint };
        validateQaBinding(binding, sessionId, asset);
        update({ mode: "ontology", sessionId, asset, binding, context: null, error: null });
      } else if (snapshot.sessionId !== sessionId || snapshot.mode === "ontology") {
        update({ mode: "general", sessionId, asset: null, binding: null, context: null, error: null });
      }
      return controller.getState();
    }),
    dispose() {
      if (disposed) return;
      disposed = true;
      generation += 1;
      activeRequest?.controller.abort();
      if (publish && hostWindow?.__ORION_CHAT_MODES__ === controller) {
        if (previousController) hostWindow.__ORION_CHAT_MODES__ = previousController;
        else delete hostWindow.__ORION_CHAT_MODES__;
      }
    },
  };
  controller.openEngineering = controller.openNewSession;
  if (publish && hostWindow) hostWindow.__ORION_CHAT_MODES__ = controller;
  return controller;
}
