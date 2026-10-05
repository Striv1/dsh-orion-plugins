const API = "/orion-ontology-qa-api";
const SLOT = "conversation.chat.turnTail";
const SESSION = /^session-[a-f0-9-]{36}$/u;
const PROJECT = /^[a-z][a-z0-9-]{2,100}$/u;
const VERSION = /^\d+\.\d+\.\d+(?:[-+][a-zA-Z0-9.-]+)?$/u;
const HASH = /^sha256:[a-f0-9]{64}$/u;
const RECEIPT = /^EVD-[a-f0-9]{32}$/u;
const LABELS = { ontology: "本体正式版本", enterprise: "企业数据", internet: "联网信息", reasoning: "规则执行记录" };
const clone = value => value == null ? value : JSON.parse(JSON.stringify(value));
const failure = (message, code) => Object.assign(new Error(message), { code });
const releaseMatches = (value, binding) => ["project_id", "release_version", "release_fingerprint"].every(key => value?.[key] === binding[key]);

export function evidenceBoundary(detail) {
  if (detail?.status === "complete" && detail.complete === true) return "本次返回的来源证据完整；结论范围以实际记录为准。";
  if (detail?.status === "partial") return "本次只取得部分证据；缺失或未返回的信息保持未知。";
  if (detail?.status === "no_evidence") return "本次没有取得足以支持结论的证据。";
  return "证据完整性尚未确认；下列已返回记录不能单独证明结论已核验。";
}

/** Read only the authenticated gateway; never infer a call from message text. */
export function createNativeEvidenceReader({ bridge, controller, fetch: fetcher = globalThis.fetch } = {}) {
  let disposed = false;
  const requests = new Set(), pending = new Map(), detailCache = new Map();
  const current = sessionId => {
    if (disposed) throw failure("证据入口已停用。", "DISPOSED");
    if (!SESSION.test(String(sessionId)) || typeof bridge?.current !== "function" || typeof bridge?.contains !== "function"
      || bridge.current() !== sessionId || bridge.contains(sessionId) !== true) {
      throw failure("会话已经切换，未使用其他会话的证据。", "SESSION_CHANGED");
    }
  };
  const nativeBinding = bound => {
    current(bound.session_id);
    const existing = (typeof controller === "function" ? controller() : controller)?.getState?.();
    if (existing?.sessionId === bound.session_id && existing.mode === "ontology" && existing.binding && !releaseMatches(existing.binding, bound)) {
      throw failure("正式版本绑定已经变化，请重新读取当前会话。", "RELEASE_CHANGED");
    }
  };
  const json = async (sessionId, path) => {
    current(sessionId);
    const abort = new AbortController(); requests.add(abort);
    try {
      const response = await fetcher(path, { method: "GET", credentials: "same-origin", cache: "no-store",
        headers: { Accept: "application/json" }, signal: abort.signal });
      const payload = await response.json(); current(sessionId);
      if (!response.ok) throw failure(payload?.detail ?? "来源证据暂时无法读取。", "SOURCE_UNAVAILABLE");
      return payload;
    } finally { requests.delete(abort); }
  };
  // Share concurrent reads across completed-turn seats, without persisting a
  // mutable Session binding or source roster in a browser/global cache.
  const once = (key, operation) => {
    if (pending.has(key)) return pending.get(key);
    const value = operation().finally(() => { if (pending.get(key) === value) pending.delete(key); });
    pending.set(key, value); return value;
  };
  const binding = async sessionId => {
    const value = await once(`binding:${sessionId}`, () => json(sessionId, `${API}/session?${new URLSearchParams({ session_id: sessionId })}`));
    current(sessionId);
    if (value?.bound === false) return null;
    if (value?.session_id !== sessionId || value.mode !== "ontology_qa" || !PROJECT.test(String(value.project_id))
      || !VERSION.test(String(value.release_version)) || !HASH.test(String(value.release_fingerprint))
      || value.database_access_mode !== "READ_ONLY") throw failure("正式版本绑定身份未确认，未展示证据。", "BINDING_INVALID");
    nativeBinding(value);
    return clone(value);
  };
  const identity = (bound, turn, call) => {
    if (!Number.isInteger(turn) || turn < 1 || typeof call?.call_id !== "string" || !call.call_id || call.call_id.length > 256
      || !releaseMatches(call, bound)) throw failure("证据调用与本轮正式版本不一致。", "CALL_IDENTITY_INVALID");
    const reference = call.receipt_reference;
    if (reference && (!RECEIPT.test(String(reference.receipt_id)) || !HASH.test(String(reference.sha256))
      || reference.session_id !== bound.session_id || !releaseMatches(reference, bound))) {
      throw failure("不可变证据回执身份未确认。", "RECEIPT_REFERENCE_INVALID");
    }
    return { sessionId: bound.session_id, turn, callId: call.call_id, queryId: call.query_id ?? reference?.query_id ?? null,
      projectId: bound.project_id, releaseVersion: bound.release_version, fingerprint: bound.release_fingerprint,
      receiptId: reference?.receipt_id ?? null, receiptHash: reference?.sha256 ?? null, question: call.question ?? null };
  };
  const checkIdentity = async value => {
    const bound = await binding(value.sessionId);
    if (!bound || bound.project_id !== value.projectId || bound.release_version !== value.releaseVersion || bound.release_fingerprint !== value.fingerprint) {
      throw failure("证据与当前会话正式版本不一致。", "RELEASE_CHANGED");
    }
    return bound;
  };
  const reader = {
    async readTurn(sessionId, turn) {
      if (!Number.isInteger(turn) || turn < 1) throw failure("回复轮次未确认。", "TURN_INVALID");
      const bound = await binding(sessionId); if (!bound) return null;
      const value = await once(`sources:${sessionId}`, () => json(sessionId, `${API}/sources?${new URLSearchParams({ session_id: sessionId })}`));
      nativeBinding(bound);
      if (value?.session_id !== sessionId || !Array.isArray(value.traces)) throw failure("来源响应与当前会话不一致。", "SOURCES_INVALID");
      const matching = value.traces.filter(trace => trace?.turn === turn);
      if (matching.length > 1) throw failure("回复来源记录不唯一，未合并证据。", "TURN_AMBIGUOUS");
      const trace = matching[0]; if (!trace) return { binding: bound, trace: null, calls: [] };
      if (trace.evidence && !releaseMatches(trace.evidence, bound)) throw failure("来源摘要与正式版本不一致。", "RELEASE_CHANGED");
      if (trace.evidence_calls != null && !Array.isArray(trace.evidence_calls)) throw failure("证据调用记录不可读取。", "CALLS_INVALID");
      const calls = (trace.evidence_calls ?? []).map(call => ({ ...clone(call), identity: identity(bound, turn, call) }));
      if (new Set(calls.map(call => call.call_id)).size !== calls.length) throw failure("证据调用编号不唯一。", "CALL_AMBIGUOUS");
      return { binding: bound, trace: clone(trace), calls };
    },
    async readDetail(value) {
      const bound = await checkIdentity(value);
      identity(bound, value.turn, { call_id: value.callId, project_id: value.projectId, release_version: value.releaseVersion, release_fingerprint: value.fingerprint });
      const key = JSON.stringify(value);
      const result = detailCache.has(key) ? detailCache.get(key) : await json(value.sessionId, `${API}/sources?${new URLSearchParams({ session_id: value.sessionId, detail: "1", turn: String(value.turn), call_id: value.callId })}`);
      nativeBinding(bound);
      const evidence = result?.evidence;
      if (result?.session_id !== value.sessionId || result.turn !== value.turn || result.call_id !== value.callId
        || evidence?.session_id !== value.sessionId || !releaseMatches(evidence, bound)
        || (value.queryId && evidence.query_id !== value.queryId)
        || (value.receiptId && (evidence.receipt_id !== value.receiptId || evidence.receipt_sha256 !== value.receiptHash))) {
        throw failure("回执与本轮回答或正式版本不一致，未展示该证据。", "DETAIL_IDENTITY_INVALID");
      }
      if (Array.isArray(evidence) || (evidence.receipt_id && (!RECEIPT.test(String(evidence.receipt_id)) || !HASH.test(String(evidence.receipt_sha256))))) {
        throw failure("证据回执格式未确认。", "DETAIL_FORMAT_INVALID");
      }
      for (const field of ["source_facts", "reasoning", "warnings", "degraded"]) {
        if (evidence[field] != null && !Array.isArray(evidence[field])) throw failure("证据内容格式未确认。", "DETAIL_FORMAT_INVALID");
      }
      if (evidence.source_facts?.some(source => !source || typeof source !== "object" || Array.isArray(source)
        || (source.rows != null && !Array.isArray(source.rows)))) throw failure("来源事实格式未确认。", "DETAIL_FORMAT_INVALID");
      if (!detailCache.has(key)) { if (detailCache.size >= 64) detailCache.delete(detailCache.keys().next().value); detailCache.set(key, clone(result)); }
      return clone(evidence);
    },
    async followUp(value, question) {
      const detail = await reader.readDetail(value);
      if (typeof question !== "string" || !question.trim()) throw failure("请填写要继续核对的问题。", "FOLLOW_UP_EMPTY");
      current(value.sessionId);
      const text = `基于刚才这次查询的真实来源继续核对：${question.trim()}\n请沿用当前正式本体与授权来源，保留部分证据、未知值和未核验结论的限制。`
        + (detail.query_id ? `关联原查询 ${detail.query_id}` : "") + (detail.receipt_id ? `，证据回执 ${detail.receipt_id}` : "") + "。不要修改本体或推定批准。";
      const result = await bridge.fillDraft(value.sessionId, text); current(value.sessionId);
      if (result?.sessionId !== value.sessionId || typeof result.draft !== "string" || !result.draft.includes(text)) {
        throw failure("追问草稿未通过回读，尚未发送。", "DRAFT_READBACK_INVALID");
      }
      return result;
    },
    dispose() { if (disposed) return; disposed = true; for (const abort of requests) abort.abort(); requests.clear(); detailCache.clear(); pending.clear(); },
  };
  return reader;
}

export function evidenceAnalysisPresentation(source, detail) {
  if (!Array.isArray(source?.rows) || source.rows.some(row => !row || typeof row !== "object" || Array.isArray(row))) {
    throw failure("该来源不是结果表，请在证据详情中核对原始记录。", "ANALYSIS_NOT_TABULAR");
  }
  const variables = Array.isArray(source.variables) && source.variables.length ? source.variables : [...new Set(source.rows.flatMap(row => Object.keys(row)))];
  const identifiers = /(?:^id$|(?:^|_)(?:id|identifier|code|key)$|(?:Id|ID)$|编号|编码|标识)/u;
  const numericType = /^http:\/\/www\.w3\.org\/2001\/XMLSchema#(?:decimal|double|float|integer|int|long|short|byte|nonNegativeInteger|positiveInteger|nonPositiveInteger|negativeInteger|unsignedLong|unsignedInt|unsignedShort|unsignedByte)$/u;
  const metricFields = variables.filter(field => !identifiers.test(field) && source.rows.some(row => row[field] != null) && source.rows.every((row, index) => {
    if (row[field] == null) return true;
    if (typeof row[field] === "number") return Number.isFinite(row[field]);
    const term = source.row_terms?.[index]?.[field];
    return term && ["literal", "typed-literal"].includes(term.type) && term.value === row[field] && !term["xml:lang"] && numericType.test(term.datatype ?? "");
  }));
  const complex = source.rows.some(row => Object.values(row).some(value => value !== null && typeof value === "object"));
  return {
    result: { ...clone(source), variables, truncated: source.result_truncated === true || source.truncated === true,
      session_id: detail.session_id, query_id: detail.query_id,
      expected_release: { project_id: detail.project_id, release_version: detail.release_version, release_fingerprint: detail.release_fingerprint },
      evidence_receipt: detail.receipt_id ? { receipt_id: detail.receipt_id, sha256: detail.receipt_sha256, query_id: detail.query_id } : null },
    options: { workspace: true, readOnly: true, receiptView: true, tableOnly: complex, metricFields, charts: true, skipCatalog: true, expandedCapabilities: false },
  };
}

/** Additive official turn-tail seat; all imperative business rendering stays in our own ref. */
export function installNativeEvidence({ ctx, React, bridge, controller, fetch, onError = () => {}, analytics, ensureReady = async () => {} } = {}) {
  const reader = createNativeEvidenceReader({ bridge, controller, fetch });
  let disposed = false;
  const h = React.createElement;
  const run = (operation, report) => operation().catch(error => { if (!disposed) { report(error.message); onError(error); } });
  function Records({ title, values }) {
    const [page, setPage] = React.useState(0), rows = Array.isArray(values) ? values : [];
    if (!rows.length) return null;
    const count = Math.ceil(rows.length / 20), selected = Math.min(page, count - 1);
    return h("details", null, h("summary", null, `${title}（${rows.length} 项）`),
      h("ol", { start: selected * 20 + 1 }, ...rows.slice(selected * 20, selected * 20 + 20).map((row, index) => h("li", { key: index },
        h("pre", { style: { whiteSpace: "pre-wrap", overflowWrap: "anywhere" } }, JSON.stringify(row, null, 2))))),
      count > 1 ? h("div", null, h("button", { type: "button", disabled: selected === 0, onClick: () => setPage(selected - 1) }, "上一页"),
        h("span", null, ` ${selected + 1} / ${count}；分页展示，记录未省略 `), h("button", { type: "button", disabled: selected + 1 === count, onClick: () => setPage(selected + 1) }, "下一页")) : null);
  }
  function Analysis({ detail, identity }) {
    const [open, setOpen] = React.useState(false), [index, setIndex] = React.useState(0), [error, setError] = React.useState(null);
    const host = React.useRef(null);
    const tables = (detail.source_facts ?? []).filter(source => Array.isArray(source.rows));
    React.useEffect(() => {
      if (!open || !host.current || !tables[index]) return;
      let cancelled = false, rendered;
      const container = host.current;
      setError(null);
      run(async () => {
        await ensureReady(); if (cancelled || disposed) return;
        const current = await reader.readDetail(identity);
        if (cancelled || disposed) return;
        const panel = typeof analytics === "function" ? analytics() : analytics;
        if (typeof panel?.renderResult !== "function") throw failure("数据分析组件尚未就绪；来源记录仍可在证据详情中读取。", "ANALYTICS_UNAVAILABLE");
        const selected = (current.source_facts ?? []).filter(source => Array.isArray(source.rows))[index];
        const presentation = evidenceAnalysisPresentation(selected, current);
        rendered = panel.renderResult(container, presentation.result, presentation.options);
      }, message => { if (!cancelled) setError(message); });
      return () => { cancelled = true; rendered?.destroy?.(); rendered?.remove?.(); container.replaceChildren(); };
    }, [open, index, detail, identity]);
    if (!tables.length) return null;
    return h("section", null, h("button", { type: "button", onClick: () => setOpen(!open), "aria-expanded": open }, "数据分析"),
      open ? h("div", null, h("p", null, "这里只展示本次已返回的结果，不执行新查询或保存业务资产。"),
        tables.length > 1 ? h("select", { "aria-label": "选择本次结果表", value: index, onChange: event => setIndex(Number(event.target.value)) },
          ...tables.map((source, item) => h("option", { key: item, value: item }, source.query_template ?? source.source_ref ?? `结果表 ${item + 1}`))) : null,
        error ? h("p", { role: "alert" }, error) : null, h("div", { ref: host })) : null);
  }
  function Detail({ call }) {
    const [open, setOpen] = React.useState(false), [detail, setDetail] = React.useState(null), [error, setError] = React.useState(null);
    const [attempt, setAttempt] = React.useState(0), [question, setQuestion] = React.useState(""), [draft, setDraft] = React.useState(false);
    React.useEffect(() => {
      if (!open) return;
      let cancelled = false; setDetail(null); setError(null);
      reader.readDetail(call.identity).then(value => { if (!cancelled) setDetail(value); })
        .catch(error => { if (!cancelled) setError(error.message); });
      return () => { cancelled = true; };
    }, [open, call, attempt]);
    return h("article", { style: { marginBlock: 8 } },
      h("button", { type: "button", onClick: () => setOpen(!open), "aria-expanded": open }, call.question ? `查看证据 · ${call.question}` : "查看证据"),
      open ? h("div", null,
        error ? h("p", { role: "alert" }, error, h("button", { type: "button", onClick: () => setAttempt(attempt + 1) }, "重新读取证据")) : !detail ? h("p", { role: "status" }, "正在核对本次来源记录…") : h("div", null,
          h("p", { role: "status" }, evidenceBoundary(detail)),
          !detail.receipt_id ? h("p", null, "这条历史记录没有不可变证据回执，不能以此补齐未保存的事实或推导过程。") : null,
          ...((detail.source_facts ?? []).map((source, index) => h("section", { key: index }, h("strong", null, source.source_ref ?? "来源事实"),
            h("p", null, `来源记录声明 ${source.reported_count ?? "未提供"} 条；本次已返回 ${source.rows?.length ?? 0} 条。${source.result_truncated === true ? "返回记录存在截断。" : ""}`),
            source.fact_source ? h("pre", { style: { whiteSpace: "pre-wrap" } }, typeof source.fact_source === "string" ? source.fact_source : JSON.stringify(source.fact_source, null, 2)) : null,
            h(Records, { title: "事实记录", values: source.rows })))),
          h(Records, { title: "实际规则执行与结论", values: detail.reasoning }),
          h(Records, { title: "证据限制与执行提示", values: [...(detail.warnings ?? []), ...(detail.degraded ?? []), ...(detail.source_provenance?.degraded ?? [])] }),
          h(Analysis, { detail, identity: call.identity }),
          h("label", null, "继续追问", h("textarea", { value: question, onChange: event => { setQuestion(event.target.value); setDraft(false); }, placeholder: "填写仍需核对的问题" })),
          h("button", { type: "button", disabled: !question.trim(), onClick: () => run(async () => { await reader.followUp(call.identity, question); setDraft(true); }, setError) }, "填入追问草稿"),
          draft ? h("p", { role: "status" }, "追问已填入原生草稿，可检查后发送。") : null,
          h("details", null, h("summary", null, "回执身份与技术记录"), h("pre", { style: { whiteSpace: "pre-wrap" } }, JSON.stringify({ query: detail.query_id, receipt: detail.receipt_id ?? null, sha256: detail.receipt_sha256 ?? null, generated_at: detail.generated_at, status: detail.status, complete: detail.complete }, null, 2))))) : null);
  }
  function TurnEvidence({ sessionId, turn, seq }) {
    const [value, setValue] = React.useState(null), [error, setError] = React.useState(null), [attempt, setAttempt] = React.useState(0);
    React.useEffect(() => {
      if (turn?.status !== "closed" || !Number.isInteger(turn.turn) || turn.turn < 1) return;
      let cancelled = false; setValue(null); setError(null);
      reader.readTurn(sessionId, turn.turn).then(value => { if (!cancelled) setValue(value); }).catch(error => { if (!cancelled) setError(error.message); });
      return () => { cancelled = true; };
    }, [sessionId, turn?.turn, turn?.status, seq, attempt]);
    if (turn?.status !== "closed") return null;
    if (error) return h("aside", null, h("p", { role: "alert" }, error), h("button", { type: "button", onClick: () => setAttempt(attempt + 1) }, "重新读取本次来源"));
    if (!value?.trace) return null;
    const labels = [...new Set((value.trace.sources ?? []).filter(name => LABELS[name]))].map(name => LABELS[name]);
    return h("details", { "data-orion-native-evidence": "true", style: { marginBlock: 8, padding: 8, borderRadius: 8, border: "1px solid var(--dsw-alias-border-l2)" } },
      h("summary", null, `本次来源${labels.length ? ` · ${labels.join("、")}` : ""}`),
      h("p", null, "以下依据与这次回答、当前会话和正式版本逐项匹配。"),
      value.trace.evidence_unavailable === true ? h("p", null, "本次至少一项工具结果没有保存完整证据回执，不能从摘要补造来源或推导链。") : null,
      !value.calls.length ? h("p", null, "本次记录未保存可独立核查的查询证据；来源分类或工具名称不能证明结论已核验。") : null,
      ...value.calls.map(call => h(Detail, { key: call.call_id, call })),
      h("button", { type: "button", onClick: () => setAttempt(attempt + 1) }, "刷新本次来源"));
  }
  ctx.slots.inject(SLOT, () => ctx.slots.register({ name: SLOT, id: "orion-workbench.evidence", order: 20 }, TurnEvidence));
  ctx.effect(() => () => { disposed = true; reader.dispose(); });
  return { reader, dispose() { disposed = true; reader.dispose(); } };
}
