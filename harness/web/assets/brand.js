(() => {
  const requestJson = (url) =>
    new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("GET", url, true);
      request.setRequestHeader("Accept", "application/json");
      request.onload = () => {
        if (request.status < 200 || request.status >= 300) {
          reject(new Error(`HTTP ${request.status}`));
          return;
        }
        try {
          resolve(JSON.parse(request.responseText));
        } catch (error) {
          reject(error);
        }
      };
      request.onerror = () => reject(new Error("网络请求失败"));
      request.send();
    });
  const requestEngineeringStatus = (url, etag = null) =>
    new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open("GET", url, true);
      request.setRequestHeader("Accept", "application/json");
      if (etag) request.setRequestHeader("If-None-Match", etag);
      request.onload = () => {
        const nextEtag = request.getResponseHeader("ETag") ?? etag;
        if (request.status === 304) {
          resolve({ notModified: true, etag: nextEtag });
          return;
        }
        if (request.status < 200 || request.status >= 300) {
          reject(new Error(`HTTP ${request.status}`));
          return;
        }
        try {
          resolve({ payload: JSON.parse(request.responseText), etag: nextEtag });
        } catch (error) {
          reject(error);
        }
      };
      request.onerror = () => reject(new Error("网络请求失败"));
      request.send();
    });
  const documentIngestionApiBase = "/orion-document-api";
  // Browser tools are page-wide, while a native plugin instance can be
  // replaced independently. A lease owns only this tool; never clear another
  // plugin's model context, and never let an old disposer remove a new lease.
  const documentWebMcp = window.__ORION_DOCUMENT_WEBMCP__ ??= (() => {
    const owners = new Set();
    let registeredContext = null;
    const status = { api_available: false, registered: false, tool: "inspect-document-ingestion",
      mode: "read_only_capability_check", error: null };
    window.__OWA_WEBMCP_STATUS__ = status;
    const sync = () => { document.documentElement.dataset.owaWebmcpStatus = JSON.stringify(status); };
    const result = (payload) => ({ content: [{ type: "text", text: JSON.stringify(payload, null, 2) }], structuredContent: payload });
    const execute = async () => {
      if (!owners.size) return result({ status: "unavailable", read_only: true, files_written: 0, error: "工作台插件已停用。" });
      try { return result(await requestJson(`${documentIngestionApiBase}/status`)); }
      catch (runtimeError) {
        try { return result(await requestJson("/document-ingestion-status.json")); }
        catch (snapshotError) {
          return result({ status: "unavailable", mode: status.mode, read_only: true, files_written: 0,
            errors: [runtimeError, snapshotError].map(error => error instanceof Error ? error.message : String(error)) });
        }
      }
    };
    sync();
    return {
      acquire() {
        const owner = {};
        owners.add(owner);
        if (!status.registered) {
          const modelContext = document.modelContext ?? navigator.modelContext;
          status.api_available = typeof modelContext?.registerTool === "function";
          // Native plugins must be able to give their tool back on disable.
          // Legacy assembly retains its existing page-lifetime registration.
          if (!status.api_available || (window.__ORION_NATIVE_PLUGIN__ === true && typeof modelContext.unregisterTool !== "function")) {
            status.error = "WebMCP lifecycle API is unavailable in this browser";
          } else {
            try {
              modelContext.registerTool({ name: status.tool,
                description: "Inspect the current PDF/OCR-to-structured-Markdown ingestion readiness. Read-only: it does not upload, open, convert, or write files.",
                inputSchema: { type: "object", properties: {}, additionalProperties: false }, execute });
              registeredContext = modelContext;
              status.registered = true;
              status.error = null;
            } catch (error) { status.error = error instanceof Error ? error.message : String(error); }
          }
          sync();
        }
        return () => {
          if (!owners.delete(owner) || owners.size || !status.registered) return;
          try {
            registeredContext.unregisterTool(status.tool);
            registeredContext = null;
            status.registered = false;
            status.error = null;
          } catch (error) { status.error = error instanceof Error ? error.message : String(error); }
          sync();
        };
      },
    };
  })();
  if (window.__ORION_NATIVE_PLUGIN__ !== true) documentWebMcp.acquire();
  const engineeringWorkflowClient = window.__ORION_ENGINEERING_WORKFLOW_CLIENT__ ?? null;
  const workflowApiBase = engineeringWorkflowClient?.base ?? "/orion-workflow-api";
  let engineeringViewOpen = false;
  let pluginEngineeringRoot = null;
  let engineeringRequestInFlight = false;
  let engineeringRequestGeneration = 0;
  let engineeringLastFetchAt = 0;
  let engineeringData = null;
  let engineeringDataFingerprint = null;
  let engineeringDataEtag = null;
  let engineeringError = null;
  let engineeringSelectedProjectId = (() => {
    try {
      return window.localStorage.getItem("orion.engineering.selectedProjectId");
    } catch (_error) {
      return null;
    }
  })();
  let selectedEngineeringStage = (() => {
    try {
      const stored = window.localStorage.getItem("orion.engineering.selectedStage");
      return /^S[0-7]$/.test(stored ?? "") ? stored : null;
    } catch (_error) {
      return null;
    }
  })();
  let engineeringWorkspaceMode = "stage";
  const ENGINEERING_PROJECT_PAGE_SIZE = 6;
  let engineeringProjectPage = 1;
  let engineeringProjectSearch = "";
  let engineeringProjectSearchComposing = false;
  let engineeringProjectSearchTimer = null;
  const engineeringExpandedProjectFamilies = new Set();
  let engineeringProjectKindFilter = (() => {
    try {
      const stored = window.localStorage.getItem("orion.engineering.projectKindFilter");
      return ["BUSINESS", "ACCEPTANCE_TEST", "ALL"].includes(stored) ? stored : "BUSINESS";
    } catch (_error) {
      return "BUSINESS";
    }
  })();
  let engineeringProjectStatusFilter = (() => {
    try {
      const stored = window.localStorage.getItem("orion.engineering.projectStatusFilter");
      return ["ALL", "WAITING", "PUBLISHED"].includes(stored) ? stored : "ALL";
    } catch (_error) {
      return "ALL";
    }
  })();
  let engineeringSection = "summary";
  let engineeringHistoryExpanded = false;
  const engineeringDisclosureState = new Map();
  let engineeringStageNavCollapsed = false;
  let engineeringReviewOpen = false;
  let engineeringReviewConfirmationId = null;
  let engineeringReviewInFlight = false;
  let engineeringReviewMessage = null;
  const engineeringPendingReviewPrompt = (payload, view) => {
    const state = payload?.state ?? {};
    const projectId = payload?.project?.project_id ?? state.project_id;
    const stage = state.current_stage;
    if (!view.open || view.workspace !== "stage" || !projectId || projectId !== view.projectId
      || view.stage !== stage || state.project_status !== "BLOCKED_HUMAN"
      || state.stage_statuses?.[stage] !== "BLOCKED_HUMAN") return null;
    if (stage === "S3") {
      const card = (payload.confirmations ?? []).find((item) => (
        item.id === state.blocking?.confirmation_id && item.status === "PENDING"
      ));
      if (!card?.id || !(card.business_question ?? card.question) || !card.options?.length) return null;
      const basis = card.decision_basis_fingerprint ?? JSON.stringify({
        question: card.business_question ?? card.question, evidence: card.evidence, options: card.options,
      });
      return { key: JSON.stringify([projectId, stage, card.id, basis]), stage, confirmationId: card.id };
    }
    const review = payload.competencyQuestionReview;
    if (stage !== "S4" || state.blocking?.type !== "COMPETENCY_QUESTION_REVIEW"
      || review?.status !== "PENDING" || review.review_scope !== "JOINT_DESIGN"
      || !review.joint_design_fingerprint || !review.joint_design_summary) return null;
    return { key: JSON.stringify([projectId, stage, review.joint_design_fingerprint]), stage, confirmationId: null };
  };
  const createEngineeringReviewPromptTracker = (storage) => {
    const storageKey = "orion.engineering.seenReviewPrompts.v1";
    let seen = new Set();
    try {
      const saved = JSON.parse(storage()?.getItem(storageKey) ?? "[]");
      if (Array.isArray(saved)) seen = new Set(saved.filter((key) => typeof key === "string").slice(-200));
    } catch (_error) { /* In-memory deduplication remains available. */ }
    return {
      shouldOpen: (prompt, busy = false) => Boolean(prompt && !busy && !seen.has(prompt.key)),
      mark(prompt) {
        if (!prompt) return;
        seen.add(prompt.key);
        if (seen.size > 200) seen.delete(seen.values().next().value);
        try { storage()?.setItem(storageKey, JSON.stringify([...seen])); } catch (_error) { /* Optional session storage. */ }
      },
    };
  };
  const engineeringReviewPrompts = createEngineeringReviewPromptTracker(() => window.sessionStorage);
  const closeEngineeringReview = () => {
    engineeringReviewOpen = false;
    engineeringReviewConfirmationId = null;
    engineeringReviewMessage = null;
  };
  let engineeringActionDialog = null;
  let engineeringStageNotice = null;
  const createEngineeringContinuationStore = (storage) => {
    const receipts = new Map();
    const key = (projectId) => `orion.engineering.continuation.${projectId}`;
    return {
      get(projectId) {
        if (!receipts.has(projectId)) {
          try {
            const saved = JSON.parse(storage()?.getItem(key(projectId)) ?? "null");
            if (saved?.project_id === projectId) receipts.set(projectId, saved);
          } catch (_error) { /* Optional page-reload recovery. */ }
        }
        return receipts.get(projectId) ?? null;
      },
      set(receipt) {
        if (!receipt?.project_id) return;
        receipts.set(receipt.project_id, receipt);
        try {
          storage()?.setItem(key(receipt.project_id), JSON.stringify({ ...receipt, retrying: false }));
        } catch (_error) { /* The current page still shows the saved decision. */ }
      },
    };
  };
  const renderEngineeringContinuation = (receipt) => {
    if (!receipt) return "";
    const canRetry = receipt.retryable === true && ["RETRYABLE", "UNBOUND"].includes(receipt.status) && receipt.event_id;
    const detail = ["QUEUED", "ALREADY_QUEUED"].includes(receipt.status)
      ? "决定已保存，已通知原工程模型继续；模型忙时自动排队。"
      : receipt.detail;
    return `<div class="owa-stage-notice" role="status" aria-live="polite"><p>${escapeHtml(detail ?? "决定已保存。")}</p>${canRetry ? `<button type="button" data-engineering-action="retry-continuation" ${receipt.retrying ? "disabled" : ""}>${receipt.retrying ? "正在重试衔接…" : "重试模型衔接"}</button>` : ""}</div>`;
  };
  const engineeringContinuations = createEngineeringContinuationStore(() => window.sessionStorage);
  let engineeringDocumentDialogOpen = false;
  let engineeringDocumentDraft = null;
  let engineeringDocumentJob = null;
  let engineeringDocumentFiles = [];
  let engineeringDocumentSkippedFiles = [];
  let engineeringDocumentInFlight = false;
  let engineeringDocumentMessage = null;
  let engineeringDocumentReturnAction = null;
  let engineeringDocumentPollToken = 0;
  let engineeringDocumentPage = 1;
  let engineeringLatestDocumentJob = null;
  let engineeringDocumentSummaryInFlight = false;
  let engineeringDocumentSummaryLastFetchAt = 0;
  let engineeringDocumentSummaryFingerprint = null;
  const mcpControlApiBase = "/orion-mcp-api";
  let mcpControlOpen = false;
  let mcpControlRequestInFlight = false;
  let mcpControlLastFetchAt = 0;
  let mcpControlData = null;
  let mcpControlError = null;
  let mcpControlActionInFlight = null;
  let mcpControlMessage = null;
  let mcpControlTimer = null;
  let mcpControlSelectedService = null;
  let mcpControlSelectedSection = "tools";
  let mcpControlToolSearch = "";
  let mcpControlSettingsInFlight = false;
  try {
    engineeringStageNavCollapsed = window.localStorage.getItem("orion.engineering.stageNavCollapsed") === "true";
  } catch (_error) {
    engineeringStageNavCollapsed = false;
  }
  let nativeActiveTabClass = null;
  let lastNativeTabLabel = "对话";

  const escapeHtml = (value) =>
    String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  const displayOntologyName = (value) => String(value ?? "").replace(
    /(?:\s*(?:[·•]\s*)?(?:[（(]\s*)?(?:重建|修订)(?:\s*[）)])?)+\s*$/u,
    "",
  ).trim() || String(value ?? "").trim();

  const chinaDateTimeParts = (value) => {
    const date = new Date(String(value ?? ""));
    if (Number.isNaN(date.getTime())) return null;
    return Object.fromEntries(new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date).map((part) => [part.type, part.value]));
  };
  const chinaTimeLabel = (value, { seconds = true, fallback = "—" } = {}) => {
    const parts = chinaDateTimeParts(value);
    if (!parts) return fallback;
    return `${parts.hour}:${parts.minute}${seconds ? `:${parts.second}` : ""}`;
  };
  const chinaDateTimeLabel = (value, fallback = "暂无更新时间") => {
    const parts = chinaDateTimeParts(value);
    if (!parts) return fallback;
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
  };

  const stageDefinitions = [
    ["S0", "资料整理", "把 PDF、图片和文档整理为结构化文本与可追溯证据"],
    ["S1", "数据理解", "盘点数据源、表结构、字段分布、质量与关系证据"],
    ["S2", "业务语义", "识别业务对象、属性、关系、枚举与规则候选"],
    ["S3", "映射评审", "审核数据库结构到本体业务含义的正式映射"],
    ["S4", "本体设计", "形成业务类、关系、属性、唯一标识与约束施工图"],
    ["S5", "本体构建", "按施工图生成标准本体文件与数据约束"],
    ["S6", "质量验证", "执行逻辑、约束、映射、业务验收问题与运行验证"],
    ["S7", "评审发布", "人工评审后发布完整本体工程包"],
  ];
  const lifecycleV2Definitions = [
    ["S0", "目标与来源登记", "确认业务目标、预期问题与资料、单库、多库或混合来源的统一范围"],
    ["S1", "资料与数据理解", "解析资料并只读理解数据，保留来源、结构、质量与完整性证据"],
    ["S2", "业务语义草案", "从来源证据形成业务对象、关系、属性和规则草案"],
    ["S3", "语义与映射评审", "确认业务口径，检查候选映射、实体身份和数据类型是否可行"],
    ["S4", "联合设计定稿", "将本体模型、正式映射、规则和业务验收问题一起校准并确认"],
    ["S5", "构建与装配", "按已确认设计构建本体、约束、映射与规则资产并装配执行能力"],
    ["S6", "质量与业务验收", "用真实来源验证逻辑、数据约束、问数和规则推理，并核对完整覆盖"],
    ["S7", "交付与发布", "形成可审计工程交付，明确批准后发布并回读运行服务"],
  ];
  const engineeringContractVersion = (payload = {}) => payload.state?.stage_contract_version
    ?? payload.project?.stage_contract_version ?? payload.stage_contract_version;
  const usesLifecycleV2 = (payload) => engineeringContractVersion(payload) === "s0-s7-stage-contract-v2";
  const engineeringStageCatalog = (payload = {}) => {
    const catalog = payload.stage_contracts ?? payload.state?.stage_contracts;
    if (!catalog || catalog.contract_version !== engineeringContractVersion(payload)) return null;
    if (!Array.isArray(catalog.stages)) return null;
    return catalog;
  };
  const engineeringStageDefinitions = (payload = {}) => {
    const defaults = usesLifecycleV2(payload) ? lifecycleV2Definitions : stageDefinitions;
    const catalog = engineeringStageCatalog(payload);
    return defaults.map(([id, title, purpose]) => {
      const contract = catalog?.stages.find((item) => item.stage === id);
      return [id, contract?.title ?? contract?.name ?? title, contract?.purpose ?? purpose];
    });
  };
  const engineeringStageContract = (payload, stage) => engineeringStageCatalog(payload)
    ?.stages.find((item) => item.stage === stage) ?? null;
  const engineeringContractList = (value) => (Array.isArray(value) ? value : [value])
    .filter((item) => typeof item === "string" && item.trim());
  const currentStageExecution = (state = {}, stage = "") => {
    const execution = state.stage_execution;
    if (!execution || state.current_stage !== stage || execution.stage !== stage
        || (execution.project_id && state.project_id && execution.project_id !== state.project_id)) return null;
    const revision = execution.project_revision ?? execution.revision;
    if (revision != null && String(revision) !== String(state.revision)) return null;
    // Legacy execution receipts lack a revision: a run begun before the latest
    // formal state change cannot describe the current revision's execution.
    const started = Date.parse(execution.started_at);
    const changed = Date.parse(state.updated_at);
    if (revision == null && Number.isFinite(started) && Number.isFinite(changed) && started < changed) return null;
    return execution;
  };
  const s6MaterializationProgressSummary = (state = {}, activeStage = "") => {
    const execution = currentStageExecution(state, activeStage);
    const progress = execution?.checkpoints?.MATERIALIZATION_PROGRESS;
    if (activeStage !== "S6" || state.current_stage !== "S6"
        || !["RUNNING", "FAILED"].includes(state.stage_statuses?.S6)
        || !["RUNNING", "FAILED", "INTERRUPTED"].includes(execution?.status)
        || execution.stage !== "S6"
        || (execution.project_id && state.project_id && execution.project_id !== state.project_id)
        || !["PREDICATES_DISCOVERED", "PARTITION_COMPLETED"].includes(progress?.phase)) return "";
    const { completed_partitions: completed, total_partitions: total, triple_count: triples } = progress;
    if (![completed, total, triples].every((value) => Number.isSafeInteger(value) && value >= 0)
        || completed > total) return "";
    const phase = execution.status === "FAILED" ? "执行失败，已保留分段进度"
      : execution.status === "INTERRUPTED" ? "执行已中断，已保留分段进度"
        : progress.phase === "PREDICATES_DISCOVERED" ? "已发现来源谓词，准备分段提取"
          : "已完成来源提取分段";
    return `${phase}：${completed.toLocaleString("zh-CN")} / ${total.toLocaleString("zh-CN")} 个分段；候选图 ${triples.toLocaleString("zh-CN")} 条三元组。以上为来源提取进度，S6 尚未通过。`;
  };
  const s6QualityProgressSummary = (state = {}, activeStage = "") => {
    const execution = currentStageExecution(state, activeStage);
    const progress = execution?.checkpoints?.QUALITY_VALIDATION_PROGRESS;
    if (activeStage !== "S6" || state.current_stage !== "S6"
        || !["RUNNING", "FAILED"].includes(state.stage_statuses?.S6)
        || !["RUNNING", "FAILED", "INTERRUPTED"].includes(execution?.status)
        || execution.stage !== "S6"
        || (execution.project_id && state.project_id && execution.project_id !== state.project_id)
        || !progress || typeof progress.subgate !== "string"
        || !["RUNNING", "PASSED", "FAILED"].includes(progress.status)) return "";
    const labels = {
      HERMIT: "本体一致性回执核验", MAPPING: "映射覆盖核验", SEMANTIC: "业务语义核验",
      SEMANTICA: "来源与规则运行证据核验", CLASS_INSTANCES: "业务类实例覆盖核验", SHACL: "全图数据约束检查", CQ: "业务问题验收",
      GRAPH_RELATIONSHIPS: "关系完整性核验", PRODUCTION_COVERAGE: "生产能力覆盖核验", PREFLIGHT: "质量预检",
    };
    const subgate = progress.subgate;
    const label = labels[subgate] ?? (subgate.startsWith("CQ_") ? labels.CQ
      : subgate.startsWith("SHACL_") ? labels.SHACL : "质量预检");
    const status = { RUNNING: "进行中", PASSED: "本项已通过", FAILED: "失败" }[progress.status];
    const { passed, total, gate, current_question_id: questionId } = progress.details ?? {};
    const count = subgate === "CQ" && [passed, total].every((value) => Number.isSafeInteger(value) && value >= 0)
      && passed <= total ? `，已通过 ${passed} / ${total} 个问题` : "";
    const gateLabel = progress.status === "FAILED" && typeof gate === "string" && /^G-S6-[A-Z0-9_-]{1,80}$/.test(gate)
      ? `（门禁 ${gate}）` : "";
    const questionLabel = subgate === "CQ" && typeof questionId === "string" && /^CQ-[A-Za-z0-9_-]{1,80}$/.test(questionId)
      ? `，当前问题 ${questionId}` : "";
    const executionNote = execution.status === "INTERRUPTED" ? "执行已中断，保留最近验收记录。"
      : execution.status === "FAILED" && progress.status !== "FAILED" ? "执行已失败，以上为最近验收记录。" : "";
    return `当前验收项：${label} · ${status}${count}${questionLabel}${gateLabel}。${executionNote}S6 尚未通过。`;
  };
  const s7ValidationDuration = (seconds) => {
    if (!Number.isFinite(seconds) || seconds < 0) return "耗时未记录";
    const rounded = Math.floor(seconds);
    return rounded < 60 ? `${rounded} 秒` : `${Math.floor(rounded / 60)} 分 ${rounded % 60} 秒`;
  };
  const renderS7ValidationProgress = (runtime = {}, activeStage = "", now = Date.now()) => {
    const progress = runtime?.validation_progress;
    if (activeStage !== "S7" || progress?.phase !== "QUERY_VALIDATION"
        || !["RUNNING", "PASSED", "FAILED"].includes(progress.status)
        || !["RUNTIME_VERIFYING", "DEPLOYMENT_FAILED", "ONTOP_READY"].includes(runtime?.state)) return "";
    const { completed, total } = progress;
    if (![completed, total].every((value) => Number.isSafeInteger(value) && value >= 0)
        || total === 0 || completed > total) return "";
    const running = progress.status === "RUNNING" && runtime.state === "RUNTIME_VERIFYING";
    const started = Date.parse(progress.case_started_at);
    const elapsed = running && Number.isFinite(started)
      ? Math.max(0, (now - started) / 1000) : progress.case_elapsed_seconds;
    const status = progress.status === "FAILED" ? "当前检查未通过，已保留通过结果"
      : runtime.state === "DEPLOYMENT_FAILED" ? "查询进度已保留，服务准备需要处理"
      : completed === total ? "查询验收已完成"
        : running ? "正在检查下一项" : "继续准备下一项检查";
    const timing = running ? "本项已等待" : "最近一项耗时";
    const clock = running && Number.isFinite(started) ? ` data-s7-query-started-at="${started}"` : "";
    const error = progress.error || (runtime.state === "DEPLOYMENT_FAILED" ? runtime.degraded_reason : "");
    return `<div class="owa-s7-validation-progress" aria-label="业务查询验收进度">
      <p>业务查询验收：已通过 ${completed} / ${total} 项 · ${escapeHtml(status)}。${timing} <span${clock}>${escapeHtml(s7ValidationDuration(elapsed))}</span>。</p>
      <details><summary>查看验收详情</summary><p>查询：${escapeHtml(progress.query_name || "未记录")} · 案例：${escapeHtml(progress.case_id || "未记录")}</p><p>最近进度记录累计耗时：${escapeHtml(s7ValidationDuration(progress.elapsed_seconds))}</p>${error ? `<p>失败信息：${escapeHtml(error)}</p>` : ""}</details>
    </div>`;
  };
  const stageTimingDuration = (value) => {
    if (!Number.isFinite(value) || value < 0) return "未知（未记录）";
    const seconds = Math.floor(value);
    return seconds >= 3600 ? `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分`
      : seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
  };
  const renderStageTiming = (state = {}, stage = "", now = Date.now()) => {
    const timing = state.stage_timing;
    if (timing?.project_id !== state.project_id || timing?.schema_version !== 1) return "";
    const item = timing.stages?.find((row) => row.stage === stage);
    if (!item) return "";
    const asOf = Date.parse(timing.observed_at);
    const metric = (label, value, running = false) => {
      const advance = running && Number.isFinite(asOf) && Number.isFinite(value);
      const clock = advance ? ` data-stage-timing-base="${value}" data-stage-timing-at="${asOf}"` : "";
      const display = advance ? value + Math.max(0, (now - asOf) / 1000) : value;
      return `<span><b>${label}</b> <em${clock}>${escapeHtml(stageTimingDuration(display))}</em></span>`;
    };
    const runtime = item.runtime;
    const runtimeMarkup = runtime ? `<div class="owa-stage-contract-grid">${metric("运行交付", runtime.wall_clock_seconds, runtime.status === "OPEN")}${metric("运行恢复", runtime.recovery_wall_seconds, runtime.recovery_open)}</div><p>${escapeHtml(runtime.scope_zh)} 运行重试次数：${escapeHtml(runtime.retry_count ?? "未知")}；包生成：${escapeHtml(runtime.package_published_at ?? "未记录")}；正式就绪：${escapeHtml(runtime.ready_at ?? "尚无就绪回执")}。</p>` : "";
    const rows = (item.attempts ?? []).map((attempt, index) => `<li>尝试 ${(item.omitted_attempt_count ?? 0) + index + 1}：${escapeHtml(attempt.started_at)} → ${escapeHtml(attempt.ended_at ?? "进行中")} · ${escapeHtml(attempt.event_id ?? "事件标识未记录")}</li>`).join("");
    const counts = [["返工", item.rework_count], ["运行重试", runtime?.retry_count]]
      .filter(([, count]) => Number.isSafeInteger(count) && count > 0)
      .map(([label, count]) => `<span class="owa-stage-timing-count">${label} ${count} 次</span>`).join("");
    return `<section class="owa-stage-contract owa-stage-timing" aria-label="阶段耗时观测"><details class="owa-stage-timing-details" data-engineering-disclosure="timing"><summary class="owa-stage-timing-summary"><strong>阶段耗时</strong>${metric("总时长", item.wall_clock_seconds, item.wall_clock_status === "OPEN_INTERVAL")}${counts}<span class="owa-stage-timing-hint">查看明细</span></summary><div class="owa-stage-timing-body"><div class="owa-stage-contract-grid">${metric("阶段墙钟", item.wall_clock_seconds, item.wall_clock_status === "OPEN_INTERVAL")}${metric("受管运行", item.managed_execution_seconds)}${metric("工具调用", item.tool_wall_seconds)}${metric("模型生成", item.model_generation_seconds)}${metric("模型步骤", item.model_step_wall_seconds)}${metric("审批等待（含代办）", item.human_wait_seconds, item.human_wait_open)}${metric("重复返工", item.rework_wall_seconds, item.wall_clock_status === "OPEN_INTERVAL" && item.rework_count > 0)}</div>${runtimeMarkup}<p>${escapeHtml(timing.scope_zh)}</p><p>返工次数：${escapeHtml(item.rework_count ?? "未知")}；受管执行次数：${escapeHtml(item.execution_attempt_count ?? "未知")}。原生记录覆盖：${item.native_timing_status === "OBSERVED_PARTIAL" ? "仅已归属事件" : "未知，缺少归属证据"}。</p><p>${escapeHtml(timing.native_scope_zh)}</p><p>${escapeHtml(item.managed_execution_scope)}</p><p>${escapeHtml(item.human_wait_scope)}</p><details class="owa-stage-timing-history" data-engineering-disclosure="timing-history"><summary>查看尝试历史</summary>${item.omitted_attempt_count ? `<p>较早 ${escapeHtml(item.omitted_attempt_count)} 次尝试保留在正式事件历史中。</p>` : ""}<ol>${rows || "<li>尚无可定位的正式尝试区间。</li>"}</ol></details></div></details></section>`;
  };
  const renderS6ValidationEvidence = (summary = {}, projectId = "", artifacts = []) => {
    const checks = Array.isArray(summary.validation_plan?.checks)
      ? summary.validation_plan.checks.filter((item) => item && typeof item.id === "string") : [];
    const execution = summary.validation_execution;
    const receipt = execution?.mode === "REUSED_PREFLIGHT" && execution.execution_reused === true
      ? "服务端验证已执行；正式提交复用本次预检的已验证回执。"
      : execution?.mode === "EXECUTED_AT_COMMIT" && execution.execution_reused === false
        ? "服务端验证在本次正式提交时执行。" : "";
    const receiptMarkup = receipt ? `<aside class="owa-s6-validation-receipt"><p>${escapeHtml(receipt)}</p>${execution.validated_at ? `<small>验证时间：${escapeHtml(chinaTimeLabel(execution.validated_at))}</small>` : ""}${execution.preflight_id ? `<small>预检回执：${escapeHtml(execution.preflight_id)}</small>` : ""}</aside>` : "";
    const statusLabels = { PASSED: "通过", PASSED_WITH_NOTES: "带说明通过", NOT_APPLICABLE: "不适用", FAILED: "失败", PENDING: "待验证", RUNNING: "验证中", BLOCKED: "等待处理" };
    const modeLabels = { EXECUTED_IN_RUN: "本次执行", REUSED_PRIOR_STAGE: "复用已验证证据", NOT_APPLICABLE: "不适用" };
    const priority = (check) => ["FAILED", "BLOCKED"].includes(check.status) ? 0 : ["PASSED", "NOT_APPLICABLE"].includes(check.status) ? 2 : 1;
    const checkRows = [...checks].sort((a, b) => priority(a) - priority(b)).map((check) => {
      const status = statusLabels[check.status] ?? "结果未记录";
      const mode = modeLabels[check.execution_mode] ?? "执行方式未记录";
      const priorStage = check.execution_mode === "REUSED_PRIOR_STAGE" && typeof check.source_stage === "string" ? ` · 来源 ${check.source_stage}` : "";
      const hermitScope = check.execution_mode === "REUSED_PRIOR_STAGE" && check.source_stage === "S5" && ["HERMIT", "G-S6-HERMIT"].includes(check.id)
        ? "复用 S5 本体一致性回执，不代表 S6 对全量实例重新运行 HermiT。" : "";
      const refs = engineeringContractList(check.evidence_refs).map((ref) => {
        const file = artifacts.find((item) => item.path === ref.split("#", 1)[0] && item.lifecycle_status !== "INVALIDATED");
        return file ? `<a href="${escapeHtml(artifactUrl(projectId, file.path))}" target="_blank" rel="noopener">${escapeHtml(ref)}</a>` : `<span>${escapeHtml(ref)}</span>`;
      }).join("");
      return `<div class="owa-quality-row is-evidence-check" data-check-id="${escapeHtml(check.id)}" data-check-status="${escapeHtml(check.status ?? "")}"><span class="owa-quality-check" aria-hidden="true"></span><strong>${escapeHtml(check.name_zh ?? check.id)}</strong><div class="owa-quality-evidence-detail"><p>${escapeHtml(mode + priorStage)}</p>${check.reason_zh ? `<p>${escapeHtml(check.reason_zh)}</p>` : ""}${hermitScope ? `<p>${escapeHtml(hermitScope)}</p>` : ""}${refs ? `<details><summary>查看依据</summary><div>${refs}</div></details>` : ""}</div><em>${escapeHtml(status)}</em></div>`;
    });
    const rows = checkRows.slice(0, 4).join("") + (checkRows.length > 4 ? `<details class="owa-gate-more"><summary>查看其余 ${checkRows.length - 4} 项检查</summary>${checkRows.slice(4).join("")}</details>` : "");
    const deploymentTasks = Array.isArray(summary.validation_plan?.deployment_tasks)
      ? summary.validation_plan.deployment_tasks.filter((item) => item && item.stage === "S7") : [];
    const deployment = deploymentTasks.length ? `<section class="owa-s6-deployment-tasks"><h4>S7 部署交付事项</h4><p>以下保留 S6 验收时的部署计划；当前执行结果请查看 S7 阶段。</p>${deploymentTasks.map((item) => `<article><strong>${escapeHtml(item.name_zh ?? item.id ?? "部署事项")}</strong><span>${escapeHtml({ PENDING: "验收时：待 S7 执行", NOT_APPLICABLE: "验收时：不适用" }[item.status] ?? "验收时状态未记录")}</span>${item.reason_zh ? `<p>${escapeHtml(item.reason_zh)}</p>` : ""}</article>`).join("")}</section>` : "";
    return { rows, receipt: receiptMarkup, deployment };
  };
  const renderEngineeringSourceScope = (payload, stage) => {
    if (!usesLifecycleV2(payload) || !["S0", "S1"].includes(stage)) return "";
    const scope = payload.source_scope ?? payload.state?.source_scope ?? payload.project?.source_scope;
    if (!Array.isArray(scope?.sources) || !scope.sources.length) return "";
    const sourceKinds = { DOCUMENT: "文档资料", STRUCTURED_FILE: "结构化文件", DATABASE: "数据库" };
    const rows = scope.sources.map((item) => `<li><strong>${escapeHtml(item.source_name ?? item.datasource_label ?? item.database ?? item.source_path ?? item.source_id ?? "已登记来源")}</strong><span>${escapeHtml(sourceKinds[item.kind] ?? "来源")}</span>${item.authorized_tables?.length ? `<p>纳入范围：${escapeHtml(item.authorized_tables.join("、"))}</p>` : ""}</li>`).join("");
    return `<section class="owa-source-scope-summary"><h4>本工程的来源范围</h4><ul>${rows}</ul><p>资料通过受控批处理解析；数据库通过只读快照与画像理解。多库分别保留来源身份，关联含义在 S3 评审后进入 S4 联合设计。</p></section>`;
  };
  const renderEngineeringStageContract = (payload, stage) => {
    const contract = engineeringStageContract(payload, stage);
    if (!contract) return "";
    const list = (items) => `<ul>${engineeringContractList(items).map((item) => `<li>${escapeHtml(item)}</li>`).join("") || "<li>以本阶段正式报告为准</li>"}</ul>`;
    return `<section class="owa-stage-contract" aria-label="阶段职责与交付"><div class="owa-stage-contract-highlights"><div class="owa-stage-contract-overview"><span class="owa-stage-contract-label">阶段目标</span><p>${escapeHtml(contract.purpose)}</p></div><div class="owa-stage-contract-acceptance"><span class="owa-stage-contract-label">通过条件</span>${list(contract.acceptance ?? contract.gates)}</div></div><details class="owa-stage-disclosure" data-engineering-disclosure="contract"><summary><strong>职责与交付详情</strong><span>来源 · 输入 · 产物 · 分工</span></summary><div class="owa-stage-disclosure-body">${renderEngineeringSourceScope(payload, stage)}<div class="owa-stage-contract-grid"><section><h4>上游输入</h4>${list(contract.inputs)}</section><section><h4>主要产物</h4>${list(contract.outputs)}</section><section><h4>定稿边界</h4>${list(contract.freeze_boundary)}</section></div><div class="owa-stage-collaboration"><section><h4>模型负责</h4>${list(contract.model_role)}</section><section><h4>AHS 平台负责</h4>${list(contract.platform_role)}</section></div></div></details></section>`;
  };
  const renderJointDesignSummary = (review = {}) => {
    if (review.review_scope !== "JOINT_DESIGN") return "";
    const summary = review.joint_design_summary ?? {};
    const groups = [
      ["classes", "业务对象"], ["object_properties", "对象关系"], ["data_properties", "数据属性"],
      ["logical_axioms", "逻辑公理"], ["constraints", "数据约束"], ["mappings", "正式映射"], ["business_rules", "规则候选与来源"],
    ];
    const collection = (value) => [value, value?.rules, value?.business_rule_candidates].find(Array.isArray) ?? [];
    const groupsMarkup = groups.map(([key, title]) => {
      const rows = collection(summary[key]);
      return `<details><summary>${title}<span>${rows.length} 项</span></summary><ul>${rows.map((item) => {
        const label = typeof item === "string" ? item : item?.label_zh ?? item?.target_label_zh ?? item?.title_zh ?? item?.title ?? item?.name ?? item?.target ?? item?.id ?? "未命名项";
        const detail = item && typeof item === "object" ? item.comment_zh ?? item.target_comment_zh ?? item.description ?? item.definition ?? item.formal_expression ?? item.message ?? "" : "";
        return `<li><strong>${escapeHtml(label)}</strong>${detail ? `<p>${escapeHtml(detail)}</p>` : ""}</li>`;
      }).join("") || "<li>本次设计未列出此类条目；适用性以完整报告为准。</li>"}</ul></details>`;
    }).join("");
    const axiomPolicy = summary.logical_axiom_applicability;
    const axiomPolicyMarkup = axiomPolicy && typeof axiomPolicy === "object"
      ? `<section class="owa-review-question"><h4>逻辑公理适用性：${axiomPolicy.status === "NOT_APPLICABLE" ? "本次事实查询无需新增公理" : "需验证已定义公理"}</h4><p>${escapeHtml(axiomPolicy.reason ?? "请回读正式适用性证据。")}</p><p>公理 ${escapeHtml(String(axiomPolicy.axiom_count ?? 0))} 条；已审 CQ ${escapeHtml((axiomPolicy.verified_question_ids ?? []).join("、") || "见正式验收清单")}。此判定纳入本次联合设计批准，S5 将重新核对来源绑定。</p></section>`
      : "";
    const sourceRows = (summary.source_scope?.sources ?? []).map((item) => `<li>${escapeHtml(item.source_name ?? item.datasource_label ?? item.database ?? item.source_path ?? item.source_id ?? "已登记来源")}${item.authorized_tables?.length ? `<p>纳入范围：${escapeHtml(item.authorized_tables.join("、"))}</p>` : ""}</li>`).join("");
    const runtime = summary.runtime_design ?? {};
    const runtimeRules = collection(runtime.reasoning_capabilities);
    const queries = collection(runtime.queries);
    const runtimeRuleMarkup = runtimeRules.map((capability) => `<article><h4>${escapeHtml(capability.description_zh ?? capability.capability_name)}</h4><p>执行引擎：${escapeHtml(capability.engine ?? "尚未声明")} · 事实查询：${escapeHtml(capability.evidence_query ?? "尚未声明")}</p><ul>${collection(capability.rules).map((rule) => `<li><strong>${escapeHtml(rule.description_zh ?? rule.rule_id ?? "规则")}</strong><pre>${escapeHtml(rule.expression ?? rule.formal_expression ?? "")}</pre></li>`).join("")}</ul><details><summary>查看事实绑定、完整性与验证约定</summary><pre>${escapeHtml(JSON.stringify({ fact_bindings: capability.fact_bindings, ontology_terms: capability.ontology_terms, runtime_validation: capability.runtime_validation, closed_world_inputs: capability.closed_world_inputs, required_set_source: capability.required_set_source, source_rule_ids: capability.source_rule_ids }, null, 2))}</pre></details></article>`).join("");
    const queriesMarkup = queries.map((query) => `<article><h4>${escapeHtml(query.query_capability?.description_zh ?? query.query_capability?.description ?? query.query_name)}</h4><pre>${escapeHtml(query.sparql ?? "")}</pre>${query.query_capability ? `<details><summary>查看查询参数与验收约定</summary><pre>${escapeHtml(JSON.stringify(query.query_capability, null, 2))}</pre></details>` : ""}</article>`).join("");
    const runtimeMarkup = `<details><summary>正式运行规则<span>${runtimeRules.length} 项能力</span></summary>${runtimeRuleMarkup || `<p>${escapeHtml(runtime.reasoning_not_applicable_reason ?? "当前设计尚未列出正式运行规则能力。")}</p>`}<p>这里展示评审时的执行设计；实际运行结果以 S6 证据为准。</p></details><details><summary>正式查询<span>${queries.length} 项</span></summary>${queriesMarkup || "<p>当前设计未列出结构化查询。</p>"}</details>${runtime.mapping?.content ? `<details><summary>可执行数据映射</summary><pre>${escapeHtml(runtime.mapping.content)}</pre></details>` : ""}${runtime.document_query_capabilities && Object.keys(runtime.document_query_capabilities).length ? `<details><summary>文档查询能力</summary><pre>${escapeHtml(JSON.stringify(runtime.document_query_capabilities, null, 2))}</pre></details>` : ""}`;
    return `<section class="owa-joint-design-review"><h3>本次整体确认范围</h3><p>${escapeHtml(summary.approval_meaning ?? "本体模型、正式映射、业务规则和验收问题共同定稿。确认后，平台按同一版本构建与验收。")}</p>${summary.business_goal ? `<p>业务目标：${escapeHtml(summary.business_goal)}</p>` : ""}${summary.version ? `<p>设计版本：${escapeHtml(summary.version)}</p>` : ""}${axiomPolicyMarkup}<div class="owa-joint-design-summary">${groupsMarkup}${runtimeMarkup}<details><summary>来源范围</summary><ul>${sourceRows || "<li>请查阅完整设计报告中的来源说明。</li>"}</ul></details></div><details><summary>查看本次设计凭证</summary>${summary.ontology_iri ? `<p>本体标识：${escapeHtml(summary.ontology_iri)}</p>` : ""}<code>${escapeHtml(review.joint_design_fingerprint ?? "尚未提供")}</code></details></section>`;
  };

  const renderApprovedS4Review = (review) => {
    const approved = review?.status === "APPROVED";
    const summaryAvailable = approved && review.review_scope === "JOINT_DESIGN"
      && review.joint_design_fingerprint && review.joint_design_summary;
    const questions = approved && Array.isArray(review.approved_questions) ? review.approved_questions : [];
    return `<div class="owa-review-backdrop" data-engineering-action="close-review" aria-hidden="true"></div><aside class="owa-review-drawer" role="dialog" aria-modal="true" aria-label="S4 已批准评审记录（只读）">
      <header><div><span>S4 人工评审记录 · 只读</span><h2>查看当时批准的设计</h2><p>此处回读批准记录，不修改设计，也不会再次提交批准。</p></div><button type="button" data-engineering-action="close-review">关闭</button></header>
      <div class="owa-review-body">${approved ? `<section class="owa-review-question"><h3>批准记录</h3><p>批准人：${escapeHtml(review.decided_by ?? "记录缺失")}</p><p>批准时间：${escapeHtml(review.decided_at ?? "记录缺失")}</p><p>批准理由：${escapeHtml(review.rationale ?? "记录缺失")}</p></section>` : '<p role="status">当前缺少已批准评审快照，无法还原当时确认内容。请查看执行记录中的历史审批凭证。</p>'}
      ${summaryAvailable ? renderJointDesignSummary(review) : approved ? '<p>缺少当时的完整联合设计快照；仅展示已有批准记录，不以当前草稿替代。</p>' : ""}
      ${approved ? `<section><h3>已批准的业务验收问题</h3>${questions.map((item, index) => `<article class="owa-cq-item"><strong>业务问题 ${index + 1}</strong><p>${escapeHtml(item.question)}</p><p>验收要求：${escapeHtml(item.expected)}</p>${item.answer_contract?.answer_scope_zh ? `<p>回答范围：${escapeHtml(item.answer_contract.answer_scope_zh)}</p>` : ""}<details><summary>当时的查询与验收约定</summary><pre>${escapeHtml(JSON.stringify(item, null, 2))}</pre></details></article>`).join("") || '<p>已批准问题快照缺失，未使用草稿问题替代。</p>'}</section>` : ""}
      ${summaryAvailable ? `<details><summary>完整批准设计快照</summary><pre>${escapeHtml(JSON.stringify(review.joint_design_summary, null, 2))}</pre></details>` : ""}</div></aside>`;
  };
  const intakeModeDefinitions = [
    {
      id: "DOCUMENT_ONLY",
      name: "资料整理",
      summary: "适合用 PDF、Word、Excel 等文件资料直接建设本体，不连接业务数据库。",
      route: "文件资料和混合工程从 S0 开始；本路径在 S1 留痕跳过，再从 S2 至 S7 完成本体建模与发布。",
    },
    {
      id: "DATABASE_ONLY",
      name: "数据建模",
      summary: "适合已有数据库或数仓、当前没有待处理业务资料的项目。",
      route: "纯数据库工程先在 S0 留下范围决定和审计记录，然后从 S1 进入本体工程。",
    },
    {
      id: "HYBRID",
      name: "混合工程",
      summary: "适合同时使用业务资料和数据库，让两类证据共同支撑本体。",
      route: "先做 S0 资料整理，再做 S1 数据理解，从 S2 汇合并继续到发布。",
    },
  ];
  const engineeringRequestId = (scope) => `${scope}:${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
  const activityStageExplanations = {
    S0: "记录资料登记、文档识别、证据写入和门禁结论；纯数据库工程则记录为什么不处理资料。",
    S1: "数据库或混合工程记录数据源盘点、只读摸排和质量门禁；资料建模工程记录为什么本阶段不适用并继续 S2。",
    S2: "记录业务对象、关系、属性和规则候选怎样从文件证据或数据库证据中形成。",
    S3: "记录映射评审问题、人工选择、决定理由和正式映射冻结结果。",
    S4: "记录本体施工图与业务验收问题的生成、自动评审和必要调整。",
    S5: "记录正式本体文件、数据约束和构建工具的真实执行结果。",
    S6: "记录逻辑、约束、映射、业务问题和真实实例的验证过程。",
    S7: "记录发布、暂缓、恢复、撤回和版本修订等负责人决定。",
  };
  const stageGuidance = {
    S0: {
      purpose: "先把原始资料整理成机器可读、人员可复核、结论可回到原文页码的标准证据。",
      tasks: ["登记原文件和内容校验码", "通过文档识别工具生成结构化文本", "保存页码证据索引、质量报告和真实工具轨迹"],
    },
    S1: {
      purpose: "先确认企业有哪些可信、可建模的数据，避免后续本体建立在错误数据源或内部系统表上。",
      tasks: ["盘点真实数据源和可用业务表", "分析主键、外键、数据量与固定取值分布", "区分客户业务数据与工作台内部数据"],
    },
    S2: {
      purpose: "把数据库的技术结构翻译成业务对象、关系、属性和规则候选，并明确哪些是事实、哪些只是人工智能辅助理解。",
      tasks: ["识别业务对象和属性候选", "把外键翻译为业务关系候选", "隔离辅助推测与尚未确认的业务规则"],
    },
    S3: {
      purpose: "把语义候选收敛成唯一、可追溯的正式映射，作为后续设计不能随意改变的输入。",
      tasks: ["检查数据库来源与业务含义是否匹配", "记录自动决定、人工决定和证据", "生成正式映射文件并通过质量门禁"],
    },
    S4: {
      purpose: "在真正构建本体文件前形成施工图，明确业务类、属性、唯一标识、约束和需要回答的业务问题。",
      tasks: ["把正式映射落实到业务类和属性设计", "定义适用对象、取值范围、唯一标识与约束", "编制可执行的业务验收问题"],
    },
    S5: {
      purpose: "让专业本体编辑器按施工图确定性生成正式本体资产，而不是让模型临场自由发挥。",
      tasks: ["生成标准本体文件与数据约束", "使用逻辑推理工具做分类检查", "保存本体编辑器的工具调用与导出证据"],
    },
    S6: {
      purpose: "证明本体不仅结构正确，而且装入真实业务实例后能够通过约束、回答问题并推出有效知识。",
      tasks: ["执行逻辑推理与数据约束检查", "复核映射、语义质量与验收问题", "通过图谱运行服务验证真实实例和推理结果"],
    },
    S7: {
      purpose: "在资产正式发布前设置最后一道人工控制点，确认版本、质量证据和工程包完整性。",
      tasks: ["汇总 S0～S6 的阶段成果和质量证据", "等待负责人明确批准发布", "生成带内容校验清单的正式工程包"],
    },
  };
  const workflowInteractionPolicy = Object.freeze({
    REQUIREMENTS: Object.freeze({
      execution: "AUTO_WHEN_READY",
      humanGate: "MISSING_INPUT_ONLY",
      summary: "整理建设目标与输入来源；必要输入齐全后自动创建工程。",
    }),
    S0: Object.freeze({
      execution: "BACKGROUND_UNTIL_GATE",
      humanGate: "WARNING_OR_FAILURE_ONLY",
      summary: "资料解析与无警告质量提交自动完成；只有警告或失败才暂停。",
    }),
    S1: Object.freeze({
      execution: "AUTO",
      humanGate: "SCOPE_CONFLICT_ONLY",
      summary: "自动完成只读数据理解；只有不可安全推断的范围冲突才询问。",
    }),
    S2: Object.freeze({
      execution: "AUTO",
      humanGate: "NONE",
      summary: "自动形成业务对象、属性、关系和规则候选，不设置常规确认。",
    }),
    S3: Object.freeze({
      execution: "AUTO",
      humanGate: "UNSAFE_AMBIGUITY_ONLY",
      summary: "自动生成并冻结证据充分的映射；只有无法安全决定时才询问。",
    }),
    S4: Object.freeze({
      execution: "AUTO",
      humanGate: "MISSING_BUSINESS_GOAL_ONLY",
      summary: "自动形成施工图和可执行业务问题；缺少必要业务目标时才询问。",
    }),
    S5: Object.freeze({
      execution: "AUTO",
      humanGate: "NONE",
      summary: "自动构建本体与约束文件，不设置常规确认。",
    }),
    S6: Object.freeze({
      execution: "AUTO",
      humanGate: "FAILURE_REMEDIATION_ONLY",
      summary: "自动验证；失败时说明原因、影响和修复入口。",
    }),
    S7: Object.freeze({
      execution: "HUMAN_GATE",
      humanGate: "RELEASE_APPROVAL",
      summary: "负责人明确选择发布、暂缓或返回修订。",
    }),
  });
  const stagePrefixes = {
    S0: "00-document-evidence/",
    S1: "01-data-understanding/",
    S2: "02-semantic-recognition/",
    S3: "03-mapping-review/",
    S4: "04-ontology-design/",
    S5: "05-ontology-build/",
    S6: "06-quality-validation/",
    S7: "07-release/",
  };
  const eventLabels = {
    PROJECT_CREATED: "创建本体工程项目",
    STAGE_EXECUTION_ATTEMPTED: "记录阶段执行尝试",
    DOCUMENT_EVIDENCE_RECORDED: "资料与证据整理完成",
    DOCUMENT_BATCH_COMPLETED: "非结构化资料批处理完成",
    S0_SCOPE_DECISION_RECORDED: "记录 S0 数据库直连范围判定",
    S1_SCOPE_DECISION_RECORDED: "记录 S1 资料建模范围判定",
    S1_SCOPE_RECONCILED: "按真实数据库修正工程范围",
    STAGE_PASSED: "阶段门禁通过",
    STAGE_FAILED: "阶段校验失败",
    STAGE_RETRY_STARTED: "修正输入并重试",
    HUMAN_GATE_BLOCKED: "等待第 1 道人工门禁确认",
    HUMAN_DECISION_RECORDED: "记录建模决定",
    COMPETENCY_QUESTION_REVIEW_REQUIRED: "等待确认业务问题",
    COMPETENCY_QUESTION_REVIEW_APPROVED: "业务问题评审通过",
    FIRST_VERSION_COMPLETED: "S1～S3 第一版完成",
    ONTOLOGY_DESIGN_COMPLETED: "本体设计门禁通过",
    ONTOLOGY_BUILD_COMPLETED: "本体编辑器（Protégé）构建完成",
    QUALITY_VALIDATION_COMPLETED: "多维质量验证通过",
    ONTOLOGY_PACKAGE_PUBLISHED: "本体工程包已生成",
    REALTIME_DEPLOYMENT_QUEUED: "运行时部署已排队",
    REALTIME_DEPLOYMENT_VERIFYING: "正在验证运行时",
    REALTIME_DEPLOYMENT_READY: "运行时验证通过",
    REALTIME_DEPLOYMENT_FAILED: "运行时验证失败",
    ONTOLOGY_PUBLICATION_DEFERRED: "负责人选择暂不发布",
    ONTOLOGY_PUBLICATION_RESUMED: "恢复发布审批",
    ONTOLOGY_RELEASE_REVOKED: "已发布版本撤回",
    REVISION_PROJECT_CREATED_FROM_RELEASE: "基于发布版本新建修订",
    PROJECT_ARCHIVED: "归档本体工程",
    PROJECT_RESTORED: "恢复本体工程",
    STAGE_REOPENED_FOR_CORRECTION: "留痕重开阶段",
    REVISION_COMPLETED: "阶段调整重新验证完成",
  };
  const eventExecutors = {
    PROJECT_CREATED: "Harness",
    STAGE_EXECUTION_ATTEMPTED: "Harness / MCP",
    DOCUMENT_EVIDENCE_RECORDED: "PaddleOCR / workflow MCP",
    DOCUMENT_BATCH_COMPLETED: "PaddleOCR / workflow MCP",
    S0_SCOPE_DECISION_RECORDED: "用户 / workflow MCP",
    S1_SCOPE_DECISION_RECORDED: "用户 / workflow MCP",
    S1_SCOPE_RECONCILED: "本体工程工作流",
    STAGE_PASSED: "workflow MCP",
    STAGE_FAILED: "workflow MCP",
    STAGE_RETRY_STARTED: "Harness",
    HUMAN_GATE_BLOCKED: "workflow MCP",
    HUMAN_DECISION_RECORDED: "用户 / Harness",
    COMPETENCY_QUESTION_REVIEW_REQUIRED: "本体工程工作流",
    COMPETENCY_QUESTION_REVIEW_APPROVED: "用户 / 本体工程工作流",
    FIRST_VERSION_COMPLETED: "workflow MCP",
    ONTOLOGY_DESIGN_COMPLETED: "Harness / workflow MCP",
    ONTOLOGY_BUILD_COMPLETED: "Protégé MCP / workflow MCP",
    QUALITY_VALIDATION_COMPLETED: "本体编辑器、图谱服务与本体工程工作流",
    ONTOLOGY_PACKAGE_PUBLISHED: "用户 / workflow MCP",
    REALTIME_DEPLOYMENT_QUEUED: "本体工程工作流",
    REALTIME_DEPLOYMENT_VERIFYING: "本体工程工作流",
    REALTIME_DEPLOYMENT_READY: "本体工程工作流",
    REALTIME_DEPLOYMENT_FAILED: "本体工程工作流",
    ONTOLOGY_PUBLICATION_DEFERRED: "用户 / 本体工程工作流",
    ONTOLOGY_PUBLICATION_RESUMED: "用户 / 本体工程工作流",
    ONTOLOGY_RELEASE_REVOKED: "用户 / 本体工程工作流",
    REVISION_PROJECT_CREATED_FROM_RELEASE: "用户 / 本体工程工作流",
    PROJECT_ARCHIVED: "用户 / 本体工程工作流",
    PROJECT_RESTORED: "用户 / 本体工程工作流",
    STAGE_REOPENED_FOR_CORRECTION: "用户 / workflow MCP",
    REVISION_COMPLETED: "workflow MCP",
  };

  const engineeringProjectIdFromPage = () => {
    if (window.__ORION_NATIVE_PLUGIN__ === true) return window.__ORION_SHELL_STATE__?.get?.().projectId ?? engineeringSelectedProjectId ?? "";
    const matches = document.body.innerText.match(/\b[a-z][a-z0-9-]*-[a-f0-9]{8}\b/gi);
    return matches?.at(-1) ?? "";
  };

  const sessionEngineeringRoot = () => {
    const header = sessionTabList()?.closest("header");
    return header?.parentElement?.parentElement ?? null;
  };

  const engineeringRoot = () => {
    if (window.__ORION_NATIVE_PLUGIN__ === true) return pluginEngineeringRoot;
    const frame = document.querySelector("[data-dsh-frame]");
    return frame?.querySelector(':scope > [class*="centerCol"]')
      ?? sessionEngineeringRoot();
  };

  const artifactUrl = (projectId, path) =>
    `${workflowApiBase}/artifact?${new URLSearchParams({ project_id: projectId, path })}`;

  const stageGateReceiptRequests = new Map();
  const stageGateNames = {
    "STATUS": "候选状态规范", "EVIDENCE": "来源证据可追溯", "UNIQUE": "候选标识唯一",
    "RULE": "规则候选完整", "HYBRID-EVIDENCE": "混合来源证据一致", "RULE-COVERAGE": "业务规则覆盖",
    "RULE-CONTRACT": "规则契约完整", "UNSUPPORTED-OPERATOR": "规则算子受支持", "INPUT-INCOMPLETE": "规则输入完整",
    "SOURCE-NOT-BOUND": "规则来源已绑定", "TEST-EVIDENCE-FORBIDDEN": "证据范围合规", "PLATFORM-CAPABILITY": "平台执行能力匹配",
    "DOCUMENT-IDENTITY": "资料身份一致", "EVIDENCE-TRACE": "证据可追溯", "QUALITY": "资料质量", "SCOPE": "来源范围合规",
    "STRUCTURED-MARKDOWN": "资料结构化完整", "TOOL-TRACE": "工具轨迹可追溯", "READONLY": "只读访问",
    "CATALOG-READBACK": "元数据回读", "EVIDENCE-EXECUTION": "证据实际执行", "PRODUCTION-PROFILE": "真实来源画像",
    "RECONCILIATION": "来源事实核对", "REQUIRED": "必需内容完整", "SOURCE-LINEAGE": "来源血缘完整",
    "CHINESE": "中文业务表达", "MAPPING": "映射验证", "PRODUCTION-REASONING": "真实来源推理",
    "REASONING-POLICY": "推理策略合规", "RUNTIME-COVERAGE": "运行候选覆盖", "CQ": "业务问题验证",
    "CQ-CONTRACT-COVERAGE": "验收问题契约覆盖", "CQ-PRODUCTION": "验收问题真实来源", "CQ-REASONING": "验收问题推理",
    "LOGIC": "逻辑一致性", "MAPPING-COVERAGE": "映射覆盖", "PRODUCTION-LOGIC": "真实来源逻辑验证",
    "RELATION-COVERAGE": "关系覆盖", "UNIQUE-IRI": "本体标识唯一", "DESIGN-COVERAGE": "批准设计覆盖",
    "PROTEGE-EVIDENCE": "Protégé 构建回执", "RDF-PARSE": "本体文件可解析", "REASONER": "推理器验证", "SHAPES": "约束文件完整",
    "HERMIT": "HermiT 一致性", "SHACL": "SHACL 约束", "SEMANTICA": "Semantica 验证",
    "HUMAN-APPROVAL": "负责人批准", "INTEGRITY": "交付完整性", "MANIFEST": "文件校验清单", "PACKAGE-COMPLETE": "工程包完整",
    "PRODUCTION-EVIDENCE": "真实来源证据", "PRODUCTION-READINESS": "运行就绪", "RECOVERY-SNAPSHOT": "恢复快照",
    "RUNTIME": "运行时验证", "SEMANTICA-SYNC": "Semantica 同步", "CQ-LINEAGE": "验收问题血缘",
  };
  const renderStageGateReceipt = (receipt, stage, url) => {
    if (receipt.stage !== stage || !Array.isArray(receipt.gates)) return "";
    const gates = receipt.gates.filter((gate) => gate && typeof gate === "object" && typeof gate.id === "string" && gate.id.startsWith(`G-${stage}-`));
    if (!gates.length) return "";
    const labels = { PASSED: "通过", FAILED: "未通过", PENDING: "待检查", RUNNING: "检查中", NOT_APPLICABLE: "不适用", BLOCKED: "受阻", PASSED_WITH_NOTES: "带说明通过" };
    const counts = new Map();
    gates.forEach((gate) => { const label = labels[gate.status] ?? "未记录"; counts.set(label, (counts.get(label) ?? 0) + 1); });
    const priority = (gate) => ["FAILED", "BLOCKED"].includes(gate.status) ? 0 : ["PASSED", "NOT_APPLICABLE"].includes(gate.status) ? 2 : 1;
    const rows = [...gates].sort((a, b) => priority(a) - priority(b)).map((gate) => {
      const name = gate.name_zh ?? gate.name ?? stageGateNames[gate.id.replace(/^G-S[0-7]-/u, "")] ?? gate.id;
      const detail = gate.message ?? gate.reason_zh ?? gate.reason ?? gate.detail;
      const refs = engineeringContractList(gate.evidence_refs);
      return `<div class="owa-gate-item"><div><strong>${escapeHtml(name)}</strong><em data-result="${escapeHtml(gate.status ?? "UNKNOWN")}">${escapeHtml(labels[gate.status] ?? "未记录")}</em></div><details><summary>查看依据</summary><p>${escapeHtml(gate.id)}</p>${detail ? `<p>${escapeHtml(typeof detail === "string" ? detail : JSON.stringify(detail))}</p>` : ""}${refs.map((ref) => `<p>${escapeHtml(ref)}</p>`).join("")}<a href="${escapeHtml(url)}" target="_blank" rel="noopener">查看原始门禁回执</a></details></div>`;
    });
    return `<div class="owa-gate-receipt-summary"><strong>${[...counts].map(([label, count]) => `${label} ${count} 项`).join(" · ")}</strong><small>最近检查${receipt.checked_at ? ` · ${escapeHtml(chinaTimeLabel(receipt.checked_at))}` : " · 时间未记录"}</small></div><div class="owa-gate-items">${rows.slice(0, 4).join("")}</div>${rows.length > 4 ? `<details class="owa-gate-more"><summary>查看其余 ${rows.length - 4} 项检查</summary><div class="owa-gate-items">${rows.slice(4).join("")}</div></details>` : ""}`;
  };
  const hydrateStageGateReceipts = (target, projectId, stage, path, revision, fallback) => {
    if (!target) return;
    const url = artifactUrl(projectId, path);
    const key = `${url}:${revision ?? ""}`;
    if (!stageGateReceiptRequests.has(key)) {
      if (stageGateReceiptRequests.size >= 24) stageGateReceiptRequests.delete(stageGateReceiptRequests.keys().next().value);
      stageGateReceiptRequests.set(key, requestJson(url).catch((error) => { stageGateReceiptRequests.delete(key); throw error; }));
    }
    stageGateReceiptRequests.get(key).then((receipt) => {
      if (!target.isConnected) return;
      target.innerHTML = renderStageGateReceipt(receipt, stage, url) || `<p class="owa-gate-empty">${escapeHtml(fallback)}</p>`;
      const failures = receipt?.stage === stage && Array.isArray(receipt.gates)
        ? receipt.gates.filter((gate) => typeof gate?.id === "string" && gate.id.startsWith(`G-${stage}-`)
          && ["FAILED", "BLOCKED"].includes(gate.status)) : [];
      if (failures.length) {
        const disclosure = target.closest(".owa-quality-section");
        if (disclosure) {
          disclosure.dataset.disclosureAttention = "true";
          disclosure.classList.add("needs-attention");
          disclosure.open = true;
          const label = disclosure.querySelector(":scope > summary > span");
          if (label) label.textContent = `${failures.length} 项检查需要处理`;
        }
      }
    }).catch(() => {
      if (target.isConnected) target.innerHTML = '<p class="owa-gate-empty">本阶段检查回执暂时无法读取，请刷新后重试。</p>';
    });
  };

  const eventStage = (event) =>
    event.details?.stage ?? event.current_stage ?? (event.event_type === "FIRST_VERSION_COMPLETED" ? "S3" : "S0");

  const eventDetail = (event) => {
    if (event.event_type === "STAGE_FAILED") return event.details?.message ?? "阶段输入未通过门禁";
    if (event.event_type === "STAGE_EXECUTION_ATTEMPTED") return `输入内容校验码 ${String(event.details?.input_fingerprint ?? "—").replace(/^sha256:/, "").slice(0, 12)}（完整值见审计凭证）`;
    if (event.event_type === "DOCUMENT_EVIDENCE_RECORDED") return `${event.details?.document_count ?? 0} 份资料、${event.details?.evidence_count ?? 0} 条证据已通过 S0 门禁`;
    if (event.event_type === "DOCUMENT_BATCH_COMPLETED") return `${event.details?.document_count ?? 0} 份资料已通过集中路由形成结构化 Markdown、证据包和复核报告，并继续进入本体工程`;
    if (event.event_type === "S0_SCOPE_DECISION_RECORDED") return `${({ DOCUMENT_ONLY: "仅资料处理", DATABASE_ONLY: "仅数据库处理", HYBRID: "资料与数据库混合处理" })[event.details?.intake_mode] ?? "仅数据库处理"} · ${event.details?.rationale ?? "无需文档处理"}`;
    if (event.event_type === "S1_SCOPE_DECISION_RECORDED") return `S1 已留痕跳过并进入 ${event.details?.next_stage ?? "S2"} · ${event.details?.rationale ?? "本项目不连接数据库"}`;
    if (event.event_type === "S1_SCOPE_RECONCILED") return `根据当前数据库结构新增 ${event.details?.added_count ?? 0} 张、移除 ${event.details?.removed_count ?? 0} 张初始范围表，并保留前后差异`;
    if (event.event_type === "HUMAN_GATE_BLOCKED") return `等待处理 ${event.details?.confirmation_id ?? "高影响语义"}`;
    if (event.event_type === "HUMAN_DECISION_RECORDED") return `已确认 ${event.details?.confirmation_id ?? "建模决定"}`;
    if (event.event_type === "COMPETENCY_QUESTION_REVIEW_REQUIRED") return `等待负责人确认 ${event.details?.question_count ?? 0} 个业务验收问题`;
    if (event.event_type === "COMPETENCY_QUESTION_REVIEW_APPROVED") return `${event.details?.question_count ?? 0} 个业务问题已确认，其中 ${event.details?.changed_item_count ?? 0} 项经过调整`;
    if (event.event_type === "STAGE_PASSED") return `${event.details?.stage ?? event.current_stage ?? "阶段"} 证据与门禁检查通过`;
    if (event.event_type === "FIRST_VERSION_COMPLETED") return "映射已评审，正式映射文件（mapping.yaml）已生成";
    if (event.event_type === "ONTOLOGY_DESIGN_COMPLETED") return `${event.details?.mapping_coverage_count ?? "全部"} 条映射已落实到本体施工图`;
    if (event.event_type === "ONTOLOGY_BUILD_COMPLETED") return `${event.details?.ttl_triple_count ?? "—"} 条本体三元组已通过解析与设计覆盖检查`;
    if (event.event_type === "QUALITY_VALIDATION_COMPLETED") return `${event.details?.materialized_triple_count ?? "—"} 条实例三元组的质量验收已完成；各项实际执行和证据复用范围见 S6 报告`;
    if (event.event_type === "ONTOLOGY_PACKAGE_PUBLISHED") return `版本 ${event.details?.release_version ?? "—"} 已生成可校验工程包`;
    if (event.event_type === "REALTIME_DEPLOYMENT_QUEUED") return `版本 ${event.details?.release_version ?? "—"} 已进入运行时部署队列`;
    if (event.event_type === "REALTIME_DEPLOYMENT_VERIFYING") return `版本 ${event.details?.release_version ?? "—"} 正在验证接口、查询模板与预期结果`;
    if (event.event_type === "REALTIME_DEPLOYMENT_READY") return `版本 ${event.details?.release_version ?? "—"} 的运行时与查询用例已经验证通过`;
    if (event.event_type === "REALTIME_DEPLOYMENT_FAILED") return event.details?.degraded_reason ?? "发布包已保留，但运行时未通过验证";
    if (event.event_type === "ONTOLOGY_PUBLICATION_DEFERRED") return `暂不发布：${event.details?.reason ?? "负责人要求继续复核"}`;
    if (event.event_type === "ONTOLOGY_PUBLICATION_RESUMED") return `恢复发布审批：${event.details?.reason ?? "前序问题已处理"}`;
    if (event.event_type === "ONTOLOGY_RELEASE_REVOKED") return `版本 ${event.details?.release_version ?? "—"} 已撤回，原包保留供审计`;
    if (event.event_type === "REVISION_PROJECT_CREATED_FROM_RELEASE") return `基于 ${event.details?.source_project_id ?? "原项目"} v${event.details?.source_release_version ?? "—"} 从 ${event.details?.stage ?? "指定阶段"} 开始修订`;
    if (event.event_type === "PROJECT_ARCHIVED") return `保留全部产物和审计记录并归档 · ${event.details?.reason ?? "负责人要求暂时收起工程"}`;
    if (event.event_type === "PROJECT_RESTORED") return `恢复归档前状态并继续原阶段 · ${event.details?.reason ?? "负责人要求继续工程"}`;
    if (event.event_type === "STAGE_REOPENED_FOR_CORRECTION") return `${event.details?.revision_id ?? "调整"} · ${event.details?.reason ?? "重新验证"}`;
    if (event.event_type === "REVISION_COMPLETED") return `${event.details?.revision_id ?? "调整"} 已完成重新验证并生成差异报告`;
    if (event.event_type === "PROJECT_CREATED") return "项目范围、处理路径和初始审计记录已登记";
    return "工作流状态已更新";
  };

  const eventArtifact = (event, artifacts) => {
    const stage = eventStage(event);
    const preferred = {
      S0: event.event_type === "DOCUMENT_EVIDENCE_RECORDED" || event.event_type === "S0_SCOPE_DECISION_RECORDED"
        ? "00-document-evidence/document-evidence-report.html"
        : "00-document-evidence/gate-results.json",
      S1: "01-data-understanding/gate-results.json",
      S2: "02-semantic-recognition/gate-results.json",
      S3: event.event_type === "FIRST_VERSION_COMPLETED"
        ? "03-mapping-review/mapping.yaml"
        : event.event_type === "HUMAN_DECISION_RECORDED"
          ? "03-mapping-review/decisions.jsonl"
          : "03-mapping-review/gate-results.json",
      S4: "04-ontology-design/ontology-design.yaml",
      S5: "05-ontology-build/ontology.ttl",
      S6: "06-quality-validation/quality-summary.json",
      S7: event.event_type === "ONTOLOGY_PACKAGE_PUBLISHED"
        ? "07-release/publication.json"
        : "07-release/gate-results.json",
    }[stage];
    if (event.details?.revision_id) {
      const revisionReport = `revisions/${event.details.revision_id}/change-report.html`;
      if (artifacts.some((item) => item.path === revisionReport)) return revisionReport;
    }
    if (preferred && artifacts.some((item) => item.path === preferred)) return preferred;
    return artifacts.find((item) => item.path.startsWith(stagePrefixes[stage] ?? ""))?.path ?? "events/agent-trace.jsonl";
  };

  const defaultEngineeringStage = (payload) => {
    if (payload.state?.current_stage) return payload.state.current_stage;
    return [...stageDefinitions]
      .reverse()
      .find(([id]) => payload.state?.stage_statuses?.[id] === "PASSED")?.[0] ?? "S0";
  };

  const reachedEngineeringStageDefinitions = (payload) => {
    const stageStatuses = payload.state?.stage_statuses ?? payload.project?.stage_statuses ?? {};
    const currentStage = payload.state?.current_stage ?? payload.project?.current_stage ?? null;
    return engineeringStageDefinitions(payload).filter(([id]) => (
      id === currentStage || (stageStatuses[id] ?? "PENDING") !== "PENDING"
    ));
  };

  const defaultVisibleEngineeringStage = (payload) => {
    const reachedStages = reachedEngineeringStageDefinitions(payload);
    const currentStage = payload.state?.current_stage ?? payload.project?.current_stage ?? null;
    if (currentStage && reachedStages.some(([id]) => id === currentStage)) return currentStage;
    return reachedStages.at(-1)?.[0] ?? "S0";
  };

  const stageStatusLabel = (status) => ({
    PASSED: "已通过",
    RUNNING: "阶段进行中",
    BLOCKED_HUMAN: "待人工确认",
    FAILED: "校验失败",
    INVALIDATED: "已失效·待重跑",
    PENDING: "待开始",
    NOT_APPLICABLE: "当前项目不适用",
    DEFERRED: "已暂缓发布",
    REVOKED: "已撤回",
  }[status] ?? status ?? "待开始");

  const stageStatusDisplay = (stage, status, intakeMode, state = {}) => {
    const execution = status === "RUNNING" ? currentStageExecution(state, stage) : null;
    if (execution?.status === "FAILED") return "本次执行失败·待修复";
    if (execution?.status === "INTERRUPTED") return "本次执行中断·待恢复";
    if (execution?.status === "RUNNING") return "本次执行中";
    return status === "NOT_APPLICABLE"
      && ((stage === "S1" && intakeMode === "DOCUMENT_ONLY")
        || (stage === "S0" && intakeMode === "DATABASE_ONLY"))
      ? "已按范围跳过" : stageStatusLabel(status);
  };

  const projectStatusLabel = (status) => ({
    IN_PROGRESS: "进行中",
    BLOCKED_HUMAN: "待人工确认",
    PUBLISHED: "已发布",
    PACKAGE_PUBLISHED: "发布包已生成",
    RUNTIME_VERIFYING: "运行时验证中",
    RUNTIME_FAILED: "运行时验证失败",
    PACKAGE_READY_RUNTIME_BLOCKED: "发布包就绪，运行时待恢复",
    QA_FAILED: "质量验证失败",
    STAGE_FAILED: "阶段执行失败",
    INITIALIZING: "正在初始化",
    RELEASE_DEFERRED: "已暂缓发布",
    RELEASE_REVOKED: "已撤回",
    ARCHIVED: "已归档",
    S1_S3_READY: "第一版已完成",
  }[status] ?? status ?? "进行中");

  const rememberEngineeringProject = (projectId) => {
    if (!projectId) return;
    if (projectId !== engineeringSelectedProjectId) closeEngineeringReview();
    engineeringSelectedProjectId = projectId;
    try {
      window.localStorage.setItem("orion.engineering.selectedProjectId", projectId);
    } catch (_error) {
      // The selector still works for the current page when storage is unavailable.
    }
  };

  const rememberEngineeringStage = (stage) => {
    if (stage !== selectedEngineeringStage) closeEngineeringReview();
    selectedEngineeringStage = /^S[0-7]$/.test(stage ?? "") ? stage : null;
    try {
      if (selectedEngineeringStage) {
        window.localStorage.setItem("orion.engineering.selectedStage", selectedEngineeringStage);
      } else {
        window.localStorage.removeItem("orion.engineering.selectedStage");
      }
    } catch (_error) {
      // Stage navigation remains usable when storage is unavailable.
    }
  };

  const engineeringContextScrollKey = (projectId, stage) =>
    `orion.engineering.contextScroll.${projectId || "unknown"}.${stage || "unknown"}`;

  const restoreEngineeringContextScroll = (panel, projectId, stage) => {
    const context = panel.querySelector(".owa-engineering-context");
    if (!context || !projectId || !stage) return;
    const key = engineeringContextScrollKey(projectId, stage);
    try {
      const saved = Number(window.sessionStorage.getItem(key));
      if (Number.isFinite(saved) && saved > 0) context.scrollTop = saved;
      context.addEventListener("scroll", () => {
        window.sessionStorage.setItem(key, String(context.scrollTop));
      }, { passive: true });
    } catch (_error) {
      // 滚动位置记忆不可用时，当前页面和局部轮询仍照常工作。
    }
  };

  const unavailableWorkflowClient = () => Promise.reject(
    new Error("工程工作流模块未加载，请刷新页面后重试。"),
  );
  const postWorkflowConfirmation = (payload) => engineeringWorkflowClient?.confirmation(payload)
    ?? unavailableWorkflowClient();
  const postWorkflowAction = (tool, argumentsPayload) => engineeringWorkflowClient?.action(tool, argumentsPayload)
    ?? unavailableWorkflowClient();
  const retryEngineeringContinuation = (projectId) => {
    const receipt = engineeringContinuations.get(projectId);
    if (!receipt?.retryable || !receipt.event_id || receipt.retrying) return Promise.resolve();
    engineeringContinuations.set({ ...receipt, retrying: true });
    renderEngineeringPanel();
    return (engineeringWorkflowClient?.retryContinuation(projectId, receipt.event_id) ?? unavailableWorkflowClient())
      .then((result) => { engineeringContinuations.set(result.continuation); })
      .catch((error) => {
        engineeringContinuations.set({ ...receipt, retrying: false, detail: `决定已保存；衔接重试未完成：${error.message}` });
      })
      .finally(() => { renderEngineeringPanel(); });
  };

  const submitEngineeringAction = (tool, argumentsPayload, { stayInProjectCenter = false } = {}) => {
    engineeringReviewInFlight = true;
    engineeringReviewMessage = null;
    renderEngineeringPanel();
    return postWorkflowAction(tool, argumentsPayload).then((result) => {
      engineeringReviewInFlight = false;
      engineeringContinuations.set(result.continuation);
      engineeringReviewOpen = false;
      engineeringData = result.dashboard ?? engineeringData;
      if (result.dashboard) engineeringDataEtag = null;
      rememberEngineeringProject(
        engineeringData?.project?.project_id ?? engineeringData?.state?.project_id,
      );
      if (stayInProjectCenter) engineeringWorkspaceMode = "projects";
      else {
        engineeringWorkspaceMode = "stage";
        const releaseStageActions = new Set([
          "publish_ontology_package",
          "defer_ontology_publication",
          "resume_ontology_publication",
          "revoke_ontology_release",
          "retry_release_runtime_deployment",
        ]);
        const nextProjectId = engineeringData?.project?.project_id
          ?? engineeringData?.state?.project_id;
        const nextStage = releaseStageActions.has(tool)
          ? "S7"
          : defaultEngineeringStage(engineeringData);
        rememberEngineeringStage(nextStage);
        window.__ORION_SHELL_STATE__?.navigate({
          primary: "ontology",
          section: "engineering",
          projectId: nextProjectId,
          stage: nextStage,
        });
      }
      engineeringLastFetchAt = Date.now();
      renderEngineeringPanel();
      window.__ORION_ONTOLOGY_CENTER__?.refresh?.();
      return result;
    }).catch((error) => {
      engineeringReviewInFlight = false;
      engineeringReviewMessage = error instanceof Error ? error.message : "工程操作保存失败";
      renderEngineeringPanel();
      throw error;
    });
  };

  const showEngineeringReviewFieldError = (form, field, message) => {
    const error = form.querySelector("[data-cq-review-error]");
    for (const input of form.querySelectorAll('[aria-invalid="true"]')) {
      input.removeAttribute("aria-invalid");
      input.removeAttribute("aria-describedby");
    }
    if (error) {
      error.textContent = message;
      error.hidden = !message;
    }
    if (field && message) {
      field.setAttribute("aria-invalid", "true");
      field.setAttribute("aria-describedby", "owa-cq-review-error");
      field.focus({ preventScroll: true });
      field.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  };

  const engineeringWorkStage = (payload = {}) => {
    const current = payload.state && Object.hasOwn(payload.state, "current_stage")
      ? payload.state.current_stage : payload.project?.current_stage;
    return /^S[0-7]$/.test(current ?? "") ? current : null;
  };

  const engineeringNeedsHumanReview = (payload) => {
    const state = payload?.state ?? {};
    return Boolean(state.blocking && (state.project_status === "BLOCKED_HUMAN"
      || state.stage_statuses?.[state.current_stage] === "BLOCKED_HUMAN"
      || state.blocking.type === "COMPETENCY_QUESTION_REVIEW"));
  };

  const engineeringCurrentActionCopy = (payload, viewedStage) => {
    const stage = engineeringWorkStage(payload);
    if (!stage) return "";
    const viewing = viewedStage !== stage ? `当前查看 ${viewedStage} 的阶段记录；` : "";
    const blocking = payload.state?.blocking;
    if (blocking?.type === "COMPETENCY_QUESTION_REVIEW") {
      return `${viewing}工程当前在 ${stage} 等待确认。请审阅本体设计、映射、规则和业务问题；确认后才会继续后续构建与验收。`;
    }
    if (blocking && engineeringNeedsHumanReview(payload)) return `${viewing}工程当前在 ${stage} 等待业务确认。请处理待确认事项，或返回工程对话核对依据与影响。`;
    return viewing ? `${viewing}工程当前阶段是 ${stage}。返回本体工作台将接续 ${stage}，不会执行正在查看的历史阶段。` : "";
  };

  const openProjectWorkSession = ({ intent = "continue", detail = "", freshSession = false } = {}) => {
    const projectId = engineeringData?.project?.project_id
      ?? engineeringData?.state?.project_id
      ?? engineeringSelectedProjectId;
    if (!projectId || !window.__ORION_CHAT_MODES__?.openWorkbench) {
      engineeringStageNotice = "暂时无法打开工程工作会话，请刷新页面后重试。";
      renderEngineeringPanel();
      return false;
    }
    const stage = engineeringWorkStage(engineeringData);
    const viewedStage = selectedEngineeringStage;
    if (intent === "publish" && stage !== "S7") intent = "discuss";
    window.__ORION_CHAT_MODES__.openWorkbench(projectId, {
      projectName: engineeringData?.project?.project_name ?? projectId,
      stage,
      viewedStage,
      intent,
      detail,
      freshSession,
    });
    return true;
  };

  const documentApiRequest = (method, path, payload = null, confirmed = false) =>
    new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      request.open(method, `${documentIngestionApiBase}${path}`, true);
      request.setRequestHeader("Accept", "application/json");
      if (payload !== null) request.setRequestHeader("Content-Type", "application/json");
      if (confirmed) request.setRequestHeader("x-orion-document-confirmation", "1");
      request.onload = () => {
        let response = {};
        try {
          response = JSON.parse(request.responseText || "{}");
        } catch (_error) {
          response = {};
        }
        if (request.status < 200 || request.status >= 300) {
          reject(new Error(response.detail ?? `HTTP ${request.status}`));
          return;
        }
        resolve(response);
      };
      request.onerror = () => reject(new Error("资料路由请求未能到达本机服务"));
      request.send(payload === null ? null : JSON.stringify(payload));
    });

  const uploadDocumentFile = (uploadId, file, onProgress) =>
    new Promise((resolve, reject) => {
      const request = new XMLHttpRequest();
      const uploadName = file.webkitRelativePath || file.name;
      const query = new URLSearchParams({ name: uploadName });
      request.open("PUT", `${documentIngestionApiBase}/uploads/${encodeURIComponent(uploadId)}/file?${query}`, true);
      request.setRequestHeader("Accept", "application/json");
      request.upload.onprogress = (event) => {
        if (event.lengthComputable) onProgress?.(event.loaded, event.total);
      };
      request.onload = () => {
        let response = {};
        try {
          response = JSON.parse(request.responseText || "{}");
        } catch (_error) {
          response = {};
        }
        if (request.status < 200 || request.status >= 300) {
          reject(new Error(response.detail ?? `上传 ${file.webkitRelativePath || file.name} 失败`));
          return;
        }
        resolve(response);
      };
      request.onerror = () => reject(new Error(`上传 ${file.name} 时连接中断`));
      request.send(file);
    });

  const engineeringDocumentJobMarkup = () => {
    const view = window.__ORION_DOCUMENT_JOB_VIEW__;
    if (!view || !engineeringDocumentJob) return "";
    const normalized = view.normalizePage(engineeringDocumentJob, engineeringDocumentPage);
    engineeringDocumentPage = normalized.page;
    const projectId = engineeringDocumentJob.project_id;
    return view.build({
      job: engineeringDocumentJob,
      page: engineeringDocumentPage,
      busy: engineeringDocumentInFlight,
      message: engineeringDocumentMessage,
      apiBase: documentIngestionApiBase,
      s0ReportUrl: projectId
        ? artifactUrl(projectId, "00-document-evidence/document-evidence-report.html")
        : "#",
    });
  };

  const refreshEngineeringDocumentJobView = () => {
    const target = document.querySelector(".owa-document-router-dialog [data-document-job-view]");
    if (!target || !engineeringDocumentJob || !window.__ORION_DOCUMENT_JOB_VIEW__) return false;
    const body = target.closest(".owa-document-router-body");
    const scrollTop = body?.scrollTop ?? 0;
    const nextMarkup = engineeringDocumentJobMarkup();
    if (target.innerHTML !== nextMarkup) target.innerHTML = nextMarkup;
    if (body) body.scrollTop = scrollTop;
    return true;
  };

  const refreshEngineeringDocumentStartState = (message = engineeringDocumentMessage) => {
    const form = document.querySelector(".owa-document-router-dialog .owa-document-router-form");
    if (!form) return false;
    const status = form.querySelector("[data-document-start-status]");
    const startButton = form.querySelector('[data-engineering-action="start-document-job"]');
    if (status) {
      status.textContent = message ?? "";
      status.hidden = !message;
    }
    if (startButton) {
      startButton.disabled = engineeringDocumentInFlight;
      startButton.textContent = engineeringDocumentInFlight ? "正在准备资料…" : "开始处理资料";
    }
    return true;
  };

  const renderEngineeringDocumentState = () => {
    if (!refreshEngineeringDocumentJobView()) renderEngineeringPanel();
  };

  const commitEngineeringDocumentJob = ({
    rationale,
    acceptWarnings = false,
    automatic = false,
    structuredDataAction = "DOCUMENT_ONLY",
  }) => {
    if (!engineeringDocumentJob?.job_id || engineeringDocumentInFlight) return Promise.resolve(null);
    engineeringDocumentInFlight = true;
    engineeringDocumentMessage = automatic
      ? "资料质量门禁已通过，正在自动把证据和工具轨迹写入 S0…"
      : "正在把复核结果、证据和工具轨迹写入 S0…";
    renderEngineeringDocumentState();
    return documentApiRequest(
      "POST",
      `/jobs/${encodeURIComponent(engineeringDocumentJob.job_id)}/commit`,
      {
        rationale,
        accept_warnings: acceptWarnings,
        structured_data_action: structuredDataAction,
      },
      true,
    ).then((result) => {
      engineeringDocumentInFlight = false;
      engineeringDocumentMessage = null;
      engineeringDocumentJob = result.job;
      engineeringLatestDocumentJob = result.job;
      if (result.dashboard) {
        engineeringData = result.dashboard;
        engineeringDataEtag = null;
        const committedProjectId = result.dashboard.project?.project_id
          ?? result.dashboard.state?.project_id;
        const committedStage = result.dashboard.state?.current_stage ?? "S0";
        rememberEngineeringProject(committedProjectId);
        engineeringWorkspaceMode = "stage";
        rememberEngineeringStage(committedStage);
        engineeringSection = "summary";
        engineeringLastFetchAt = Date.now();
        window.__ORION_SHELL_STATE__?.navigate({
          primary: "ontology",
          section: "engineering",
          projectId: committedProjectId,
          stage: committedStage,
        });
      }
      renderEngineeringPanel();
      return result;
    }).catch((error) => {
      engineeringDocumentInFlight = false;
      engineeringDocumentMessage = error instanceof Error ? error.message : "S0 资料提交失败";
      if (automatic) engineeringDocumentDialogOpen = true;
      renderEngineeringDocumentState();
      throw error;
    });
  };

  const pollDocumentJob = (jobId, { requireDialog = true, autoOpenReview = false } = {}) => {
    const token = ++engineeringDocumentPollToken;
    const poll = () => {
      if ((requireDialog && !engineeringDocumentDialogOpen) || token !== engineeringDocumentPollToken) return;
      documentApiRequest("GET", `/jobs/${encodeURIComponent(jobId)}`)
        .then((job) => {
          if ((requireDialog && !engineeringDocumentDialogOpen) || token !== engineeringDocumentPollToken) return;
          engineeringDocumentJob = job;
          engineeringLatestDocumentJob = job;
          if (autoOpenReview && ["READY_FOR_REVIEW", "FAILED", "TIMED_OUT"].includes(job.status)) {
            engineeringDocumentDialogOpen = true;
            engineeringDocumentPage = 1;
            engineeringDocumentMessage = job.status === "READY_FOR_REVIEW"
              ? "资料解析完成；请确认表格是仅作为资料，还是全量导入结构化数据区。"
              : "资料任务未完成，请查看失败文件并选择重试或取消。";
          }
          renderEngineeringDocumentState();
          if (["QUEUED", "RUNNING"].includes(job.status)) window.setTimeout(poll, 1500);
        })
        .catch((error) => {
          engineeringDocumentMessage = error instanceof Error ? error.message : "任务状态读取失败";
          renderEngineeringDocumentState();
        });
    };
    window.setTimeout(poll, 350);
  };

  const startEngineeringDocumentJob = async ({
    projectName,
    domain,
    rationale,
    intakeMode,
    cqMode,
    initialCompetencyQuestions,
    sourceMode,
    sourcePath,
    projectId,
    projectRequestId,
    expectedRevision = null,
    reuseContentCache = false,
    files = [],
    autoOpenReview = false,
    onProgress = null,
  }) => {
    const currentProjectId = engineeringData?.project?.project_id ?? engineeringData?.state?.project_id;
    const documentProjectRevision = expectedRevision
      ?? (projectId && projectId === currentProjectId ? engineeringData?.state?.revision : null);
    engineeringDocumentFiles = files;
    engineeringDocumentInFlight = true;
    engineeringDocumentPage = 1;
    const setProgress = (message) => {
      engineeringDocumentMessage = message;
      onProgress?.(message);
      if (!refreshEngineeringDocumentStartState(message)) renderEngineeringPanel();
    };
    try {
      setProgress(sourceMode === "upload" ? "正在创建安全上传批次…" : "正在启动 S0 资料任务…");
      let resolvedSourcePath = sourcePath;
      if (sourceMode === "upload") {
        const upload = await documentApiRequest("POST", "/uploads", {});
        resolvedSourcePath = upload.source_path;
        for (let index = 0; index < files.length; index += 1) {
          const file = files[index];
          setProgress(`正在上传 ${file.name}（${index + 1}/${files.length}）`);
          await uploadDocumentFile(upload.upload_id, file, (loaded, total) => {
            const percent = Math.round((loaded / Math.max(1, total)) * 100);
            engineeringDocumentMessage = `正在上传 ${file.name}：${percent}%`;
            onProgress?.(engineeringDocumentMessage);
            const message = document.querySelector(".owa-document-router-dialog .owa-review-message");
            if (message) message.textContent = engineeringDocumentMessage;
          });
        }
      }
      setProgress("资料已接收，正在启动解析与证据生成任务…");
      const job = await documentApiRequest("POST", "/jobs", {
        project_name: projectName,
        domain,
        intake_rationale: rationale,
        intake_mode: intakeMode,
        cq_mode: cqMode,
        initial_competency_questions: initialCompetencyQuestions,
        source_path: resolvedSourcePath,
        independent_project: true,
        project_id: projectId,
        project_request_id: projectRequestId,
        ...(Number.isInteger(documentProjectRevision) ? { expected_revision: documentProjectRevision } : {}),
        reuse_content_cache: reuseContentCache === true,
      });
      engineeringDocumentJob = { ...job, files: [], warnings: [] };
      engineeringLatestDocumentJob = engineeringDocumentJob;
      engineeringDocumentMessage = null;
      engineeringDocumentInFlight = false;
      renderEngineeringPanel();
      pollDocumentJob(job.job_id, { requireDialog: !autoOpenReview, autoOpenReview });
      return job;
    } catch (error) {
      engineeringDocumentInFlight = false;
      engineeringDocumentMessage = error instanceof Error ? error.message : "资料上传或任务启动失败";
      onProgress?.(engineeringDocumentMessage);
      renderEngineeringPanel();
      throw error;
    }
  };

  const ensureEngineeringDocumentSummary = (force = false) => {
    if (!engineeringViewOpen || engineeringInteractionOpen()) return;
    const currentTime = Date.now();
    if (
      engineeringDocumentSummaryInFlight
      || (!force && currentTime - engineeringDocumentSummaryLastFetchAt < 3000)
    ) return;
    engineeringDocumentSummaryInFlight = true;
    engineeringDocumentSummaryLastFetchAt = currentTime;
    const currentProjectId = engineeringData?.project?.project_id ?? engineeringData?.state?.project_id;
    const activeJobId = engineeringLatestDocumentJob?.project_id === currentProjectId
      && ["QUEUED", "RUNNING"].includes(engineeringLatestDocumentJob?.status)
      ? engineeringLatestDocumentJob.job_id
      : null;
    const path = activeJobId
      ? `/jobs/${encodeURIComponent(activeJobId)}`
      : `/jobs?${new URLSearchParams({ limit: "1", project_id: currentProjectId ?? "" })}`;
    documentApiRequest("GET", path)
      .then((payload) => {
        const nextJob = activeJobId ? payload : payload.jobs?.[0] ?? null;
        const nextFingerprint = JSON.stringify(nextJob);
        const dataChanged = nextFingerprint !== engineeringDocumentSummaryFingerprint;
        engineeringLatestDocumentJob = nextJob;
        engineeringDocumentSummaryFingerprint = nextFingerprint;
        if (dataChanged && !engineeringInteractionOpen()) {
          renderEngineeringPanel();
        }
      })
      .catch(() => {
        // 工程主页面仍可使用；资料批次读取失败时不打断当前操作。
      })
      .finally(() => {
        engineeringDocumentSummaryInFlight = false;
      });
  };

  const renderEngineeringLoading = (panel) => {
    panel.innerHTML = `
      <div class="owa-engineering-loading">
        <strong>正在读取本体工程状态</strong>
        <span>从本机工作流资产中加载阶段、事件与产物…</span>
      </div>`;
  };

  const captureEngineeringUiState = (panel) => {
    const active = panel.contains(document.activeElement) ? document.activeElement : null;
    const disclosureScope = `${panel.dataset.projectId ?? ""}:${panel.dataset.visibleStage ?? ""}`;
    const disclosures = [...panel.querySelectorAll("details[data-engineering-disclosure]")];
    if (disclosures.length) {
      const saved = engineeringDisclosureState.get(disclosureScope) ?? {};
      disclosures.forEach((item) => { saved[item.dataset.engineeringDisclosure] = item.open; });
      engineeringDisclosureState.set(disclosureScope, saved);
      if (engineeringDisclosureState.size > 64) engineeringDisclosureState.delete(engineeringDisclosureState.keys().next().value);
    }
    return {
      disclosureScope,
      activeDisclosure: active?.tagName === "SUMMARY" ? active.parentElement?.dataset?.engineeringDisclosure : null,
      panelScrollTop: panel.scrollTop,
      stageNavScrollTop: panel.querySelector(".owa-engineering-stage-rail > nav")?.scrollTop ?? null,
      documentBodyScrollTop: panel.querySelector(".owa-document-router-body")?.scrollTop ?? null,
      truthDetailsOpen: panel.querySelector(".owa-engineering-truth-disclosure")?.open ?? null,
      activeName: active?.getAttribute("name") ?? null,
      activeAction: active?.dataset?.engineeringAction ?? null,
      selectionStart: typeof active?.selectionStart === "number" ? active.selectionStart : null,
      selectionEnd: typeof active?.selectionEnd === "number" ? active.selectionEnd : null,
    };
  };

  const restoreEngineeringUiState = (panel, state) => {
    const disclosureScope = `${panel.dataset.projectId ?? ""}:${panel.dataset.visibleStage ?? ""}`;
    const savedDisclosures = engineeringDisclosureState.get(disclosureScope) ?? {};
    panel.querySelectorAll("details[data-engineering-disclosure]").forEach((item) => {
      if (item.dataset.disclosureAttention === "true") item.open = true;
      else if (Object.hasOwn(savedDisclosures, item.dataset.engineeringDisclosure)) item.open = savedDisclosures[item.dataset.engineeringDisclosure];
    });
    panel.scrollTop = state.panelScrollTop;
    const stageNavigation = panel.querySelector(".owa-engineering-stage-rail > nav");
    if (stageNavigation && state.stageNavScrollTop !== null) {
      stageNavigation.scrollTop = state.stageNavScrollTop;
    }
    const documentBody = panel.querySelector(".owa-document-router-body");
    if (documentBody && state.documentBodyScrollTop !== null) {
      documentBody.scrollTop = state.documentBodyScrollTop;
    }
    const truthDetails = panel.querySelector(".owa-engineering-truth-disclosure");
    if (truthDetails?.dataset.disclosureAttention === "true") {
      truthDetails.open = true;
    } else if (truthDetails && state.truthDetailsOpen !== null && state.disclosureScope === disclosureScope) {
      truthDetails.open = state.truthDetailsOpen;
    }
    const fileInput = panel.querySelector('input[name="document-files"]');
    if (fileInput && engineeringDocumentFiles.length && typeof DataTransfer === "function") {
      try {
        const transfer = new DataTransfer();
        engineeringDocumentFiles.forEach((file) => transfer.items.add(file));
        fileInput.files = transfer.files;
      } catch (_error) {
        // The selected File objects remain in memory even if a browser blocks FileList restoration.
      }
    }
    const active = state.activeDisclosure && state.disclosureScope === disclosureScope
      ? [...panel.querySelectorAll("details[data-engineering-disclosure]")]
        .find((item) => item.dataset.engineeringDisclosure === state.activeDisclosure)?.querySelector("summary")
      : state.activeName
      ? [...panel.querySelectorAll("[name]")].find((element) => element.getAttribute("name") === state.activeName)
      : state.activeAction
        ? [...panel.querySelectorAll("[data-engineering-action]")].find((element) => element.dataset.engineeringAction === state.activeAction)
        : null;
    if (!active) return;
    active.focus({ preventScroll: true });
    if (
      state.selectionStart !== null
      && state.selectionEnd !== null
      && typeof active.setSelectionRange === "function"
    ) {
      active.setSelectionRange(state.selectionStart, state.selectionEnd);
    }
  };

  const renderEngineeringPanel = () => {
    const panel = document.querySelector(".owa-engineering-view");
    if (!panel || !engineeringViewOpen) return;
    if (!engineeringData) {
      if (engineeringError) {
        panel.innerHTML = `<div class="owa-engineering-loading is-error" role="alert"><strong>${engineeringError.startsWith("当前环境未找到工程") ? "工程不存在或当前环境不可用" : "暂时无法读取工程状态"}</strong><span>${escapeHtml(engineeringError)}</span><button type="button" data-engineering-action="retry">重新读取</button><button type="button" data-engineering-action="open-project-center">回工程中心</button></div>`;
      } else {
        renderEngineeringLoading(panel);
      }
      return;
    }
    const uiState = captureEngineeringUiState(panel);

    const {
      project = {},
      state = {},
      events = [],
      artifacts = [],
      mappingStats = {},
      stageSummaries = {},
      stageDecisions = {},
      confirmations = [],
      competencyQuestionReview = null,
      publication = null,
      releaseDecision = null,
      releaseRevocation = null,
      releaseContract: dashboardReleaseContract = null,
      release_contract: workflowReleaseContract = null,
      storageStatus = null,
      releaseRuntime = null,
      release_runtime: workflowReleaseRuntime = null,
      realtime_deployment: workflowDeployment = null,
      revisions = [],
      projects = [],
    } = engineeringData;
    const releaseVersion = String(publication?.release_version ?? "").replace(/^v/iu, "");
    const releaseContract = dashboardReleaseContract ?? workflowReleaseContract;
    const runtimeStatus = releaseRuntime ?? workflowReleaseRuntime ?? workflowDeployment ?? state.release_runtime_status ?? null;
    const projectId = project.project_id ?? state.project_id ?? "";
    const projectArchived = state.project_status === "ARCHIVED";
    const intakeMode = project.intake_mode ?? state.intake_mode ?? "HYBRID";
    const intakeModeLabel = ({
      DOCUMENT_ONLY: "资料",
      DATABASE_ONLY: "数据库",
      HYBRID: "资料 + 数据库",
    })[intakeMode] ?? "资料 + 数据库";
    const stageDefinitions = engineeringStageDefinitions(engineeringData);
    const lifecycleV2 = usesLifecycleV2(engineeringData);
    const reachedStageDefinitions = reachedEngineeringStageDefinitions(engineeringData);
    const reachedStageIds = new Set(reachedStageDefinitions.map(([id]) => id));
    const activeStage = selectedEngineeringStage && reachedStageIds.has(selectedEngineeringStage)
      ? selectedEngineeringStage
      : defaultVisibleEngineeringStage(engineeringData);
    if (selectedEngineeringStage !== activeStage) rememberEngineeringStage(activeStage);
    const definition = stageDefinitions.find(([id]) => id === activeStage) ?? stageDefinitions[0];
    const activeContract = engineeringStageContract(engineeringData, activeStage);
    const stageStatus = state.stage_statuses?.[activeStage] ?? "PENDING";
    const activeStageEvents = events.filter((event) => eventStage(event) === activeStage);
    const allStageArtifacts = artifacts.filter((item) => item.path.startsWith(stagePrefixes[activeStage] ?? ""));
    const invalidatedStageArtifacts = allStageArtifacts.filter((item) => item.lifecycle_status === "INVALIDATED");
    const stageArtifacts = allStageArtifacts.filter((item) => item.lifecycle_status !== "INVALIDATED");
    const blocking = state.blocking;
    if (engineeringReviewOpen && (engineeringWorkspaceMode !== "stage"
      || projectId !== engineeringSelectedProjectId
      || (engineeringReviewOpen === "APPROVED_READ_ONLY" ? activeStage !== "S4" : activeStage !== state.current_stage))) {
      closeEngineeringReview();
    }
    const reviewPrompt = engineeringPendingReviewPrompt(engineeringData, {
      open: engineeringViewOpen, workspace: engineeringWorkspaceMode,
      projectId: engineeringSelectedProjectId, stage: activeStage,
    });
    const reviewOpenedAutomatically = !engineeringReviewOpen
      && engineeringReviewPrompts.shouldOpen(reviewPrompt, engineeringReviewPromptBusy());
    if (reviewOpenedAutomatically) {
      engineeringReviewOpen = true;
      engineeringReviewConfirmationId = reviewPrompt.confirmationId;
      engineeringReviewMessage = null;
    }
    if (engineeringReviewOpen) engineeringReviewPrompts.mark(reviewPrompt);
    const pendingConfirmations = confirmations.filter((item) => item.status !== "RESOLVED");
    const activeConfirmation = pendingConfirmations.find(
      (item) => item.id === engineeringReviewConfirmationId,
    ) ?? pendingConfirmations[0] ?? null;
    const failedEvents = activeStageEvents.filter((event) => event.event_type === "STAGE_FAILED");
    const activeSummary = stageSummaries[activeStage] ?? {};
    const materializationProgressSummary = s6MaterializationProgressSummary(state, activeStage);
    const qualityProgressSummary = s6QualityProgressSummary(state, activeStage);
    const activeDecisions = stageDecisions[activeStage] ?? [];
    const workflowCurrentStage = defaultEngineeringStage(engineeringData);
    const workflowCurrentDefinition = stageDefinitions.find(([id]) => id === workflowCurrentStage) ?? stageDefinitions[0];
    const workflowCurrentStatus = state.stage_statuses?.[workflowCurrentStage] ?? "PENDING";
    const summary = activeSummary.headline
      ?? (activeStage === "S3" && mappingStats.total
        ? `${mappingStats.total} 条映射已完成评审`
        : `${stageArtifacts.length} 个阶段产物 · ${activeStageEvents.length} 条执行记录`);
    const stageRail = reachedStageDefinitions.map(([id, name]) => {
      const status = state.stage_statuses?.[id] ?? "PENDING";
      const count = events.filter((event) => eventStage(event) === id).length;
      return `<button type="button" class="owa-engineering-stage is-${escapeHtml(status.toLowerCase())}${engineeringWorkspaceMode === "stage" && id === activeStage ? " is-selected" : ""}${id === workflowCurrentStage ? " is-workflow-current" : ""}" data-engineering-stage="${id}" aria-pressed="${engineeringWorkspaceMode === "stage" && id === activeStage}" title="${escapeHtml(`${id} ${name} · ${stageStatusDisplay(id, status, intakeMode, state)}${id === workflowCurrentStage ? " · 工程当前阶段" : ""}`)}" data-status-code="${escapeHtml(status)}">
        <span class="owa-stage-code">${id}</span>
        <span class="owa-stage-copy"><span class="owa-stage-name">${escapeHtml(name)}${id === workflowCurrentStage ? '<em class="owa-stage-current-badge">当前</em>' : ""}</span><span class="owa-stage-status">${escapeHtml(stageStatusDisplay(id, status, intakeMode, state))}</span></span>
        <span class="owa-stage-count">${count ? `${count} 条事件` : ""}</span>
      </button>`;
    }).join("");

    const featuredArtifactNames = {
      S0: ["document-evidence-report.html", "source-scope.json", "README.md", "scope-decision.json", "document-register.json", "evidence-index.json"],
      S1: ["data-understanding-report.html", "source-understanding.json", "scope-decision.json", "scope-reconciliation.json", "schema-snapshot.json", "evidence-sql.json"],
      S2: ["business-semantics-report.html", "README.md", "ontology-candidates.yaml", "business-rule-candidates.json"],
      S3: ["mapping-review-report.html", "README.md", "mapping.yaml", "automatic-decisions.json"],
      S4: ["joint-design-report.html", "ontology-design-report.html", "joint-design-baseline.json", "README.md", "ontology-design.yaml", "gate-results.json"],
      S5: ["ontology-build-report.html", "README.md", "ontology.ttl", "shapes.ttl"],
      S6: ["quality-validation-report.html", "README.md", "quality-summary.json", "semantica-report.json"],
      S7: ["release-revocation-report.html", "release-resumption-report.html", "release-decision-report.html", "release-report.html", "package-contract.json", "README.md", "publication.json"],
    }[activeStage] ?? [];
    const preferredArtifacts = featuredArtifactNames
      .map((name) => stageArtifacts.find((item) => item.path.endsWith(`/${name}`)))
      .filter(Boolean);
    const featuredArtifacts = preferredArtifacts.slice(0, 4);
    for (const item of stageArtifacts.slice().reverse()) {
      if (featuredArtifacts.length >= 4) break;
      if (!featuredArtifacts.some((featured) => featured.path === item.path)) featuredArtifacts.push(item);
    }
    const stageReportCandidates = {
      S0: ["document-evidence-report.html"],
      S1: ["data-understanding-report.html"],
      S2: ["business-semantics-report.html"],
      S3: ["mapping-review-report.html"],
      S4: ["joint-design-report.html", "ontology-design-report.html"],
      S5: ["ontology-build-report.html"],
      S6: ["quality-validation-report.html"],
      S7: ["release-revocation-report.html", "release-resumption-report.html", "release-decision-report.html", "release-report.html"],
    }[activeStage] ?? [];
    const stageReport = stageReportCandidates
      .map((name) => stageArtifacts.find((item) => item.path.endsWith(`/${name}`)))
      .find(Boolean);
    const artifactFormatLabels = {
      HTML: "HTML 报告",
      HTM: "HTML 报告",
      YAML: "YAML 配置",
      YML: "YAML 配置",
      JSON: "JSON 数据",
      MD: "Markdown 文档",
      TTL: "Turtle 本体",
      OWL: "OWL 本体",
      RDF: "RDF 本体",
      CSV: "CSV 数据",
      SQL: "SQL 证据",
      ZIP: "ZIP 发布包",
    };
    const artifactRows = featuredArtifacts.map((item, index) => {
      const eventTime = activeStageEvents.at(-(index + 1))?.at;
      const fileName = item.path.split("/").at(-1) ?? item.path;
      const fileExtension = fileName.includes(".") ? fileName.split(".").at(-1).toUpperCase() : "FILE";
      const formatLabel = artifactFormatLabels[fileExtension] ?? `${fileExtension} 文件`;
      const purpose = item.purpose ?? (index === 0 ? "当前阶段正式产物" : "保留本阶段的工程结果与审计信息。");
      const artifactType = item.artifact_type ?? "工程产物";
      return `<details class="owa-engineering-artifact" data-engineering-disclosure="artifact:${escapeHtml(item.path)}">
        <summary>
          <i class="owa-engineering-icon is-page owa-artifact-icon" aria-hidden="true"></i>
          <span class="owa-artifact-copy"><strong>${escapeHtml(item.display_name ?? fileName)}</strong></span>
          <span class="owa-artifact-labels"><span class="owa-artifact-type">${escapeHtml(artifactType)}</span><span class="owa-artifact-format">.${escapeHtml(fileExtension)}</span></span>
          <span class="owa-artifact-disclosure" aria-hidden="true">展开</span>
        </summary>
        <div class="owa-artifact-detail">
          <dl><div><dt>用途</dt><dd>${escapeHtml(purpose)}</dd></div><div><dt>格式</dt><dd>${escapeHtml(formatLabel)}</dd></div><div><dt>文件</dt><dd><code>${escapeHtml(item.path)}</code></dd></div>${eventTime ? `<div><dt>形成时间</dt><dd>${escapeHtml(chinaTimeLabel(eventTime, { seconds: false }))}</dd></div>` : ""}</dl>
          <a href="${escapeHtml(artifactUrl(projectId, item.path))}" target="_blank" rel="noopener">打开产物</a>
        </div>
      </details>`;
    }).join("");
    const stageReadme = stageArtifacts.find((item) => item.path.endsWith("/README.md"));
    const toolDisplayLabels = {
      orion__file__sha256_identity: "文件身份与内容校验",
      orion__pdf__direct_text_extract: "PDF 文字层直接提取",
      orion__pdf__render_page_for_recognition: "PDF 页面图像生成",
      mcp__paddleocr__pp_structurev3: "PaddleOCR 版面结构识别",
      mcp__paddleocr_ocr__ocr: "PaddleOCR 基础文字识别回退",
      orion__office__docx_to_markdown: "Word 结构化转换",
      orion__office__xlsx_to_markdown: "Excel 结构与数据转换",
      orion__text__direct_to_markdown: "文本直接结构化",
      orion__email__eml_to_markdown: "邮件正文与附件清单解析",
      workflow_mcp: "本体工程工作流",
      protege_mcp: "本体编辑与推理工具",
      semantica_mcp: "知识图谱验证工具",
      chat2db_mcp: "数据库只读摸排工具",
    };

    const executorLabel = (executor) => ({
      ORION_WORKFLOW: "本体工程工作流",
      Harness: "工程运行框架",
      "workflow MCP": "本体工程工作流",
      "Harness / MCP": "工程框架与工具服务",
      "PaddleOCR / workflow MCP": "文档识别与本体工程工作流",
      Codex: "Codex 协作助手",
    }[executor] ?? String(executor ?? "本体工程工作流").replaceAll("workflow MCP", "本体工程工作流"));
    const activityRows = activeStageEvents.slice().reverse().map((event) => {
      const failed = event.event_type === "STAGE_FAILED";
      const blocked = event.event_type === "HUMAN_GATE_BLOCKED";
      const artifact = eventArtifact(event, artifacts);
      const rawExecutor = event.actor ?? eventExecutors[event.event_type] ?? "Harness";
      const eventCode = String(event.event_id ?? "—").replace(/^EVT-/, "");
      const hashCode = String(event.event_hash ?? "").replace(/^sha256:/, "");
      const eventTools = (Array.isArray(event.details?.tool_calls) ? event.details.tool_calls : [])
        .map((tool) => `<span>${escapeHtml(toolDisplayLabels[tool] ?? tool)}</span>`).join("");
      return `<article class="owa-engineering-activity${failed ? " is-failed" : blocked ? " is-blocked" : ""}" role="listitem">
        <span class="owa-activity-time"><i aria-hidden="true"></i><time datetime="${escapeHtml(event.at ?? "")}">${escapeHtml(chinaTimeLabel(event.at))}</time></span>
        <span class="owa-activity-copy"><strong>${escapeHtml(eventLabels[event.event_type] ?? event.event_type)}</strong><small>${escapeHtml(eventDetail(event))}</small>${eventTools ? `<span class="owa-activity-tools">${eventTools}</span>` : ""}</span>
        <span class="owa-activity-meta">
          <span class="owa-activity-actor"><b>执行方</b><em title="原始执行标识：${escapeHtml(rawExecutor)}">${escapeHtml(executorLabel(rawExecutor))}</em></span>
          <details class="owa-audit-proof"><summary><b>审计凭证</b><span>事件 ${escapeHtml(eventCode.slice(0, 8) || "—")}</span></summary><div><strong>这是什么</strong><p>用于证明这一步何时发生、由谁执行，以及记录之后没有被静默改写。</p><code>事件编号（Event ID）：${escapeHtml(event.event_id ?? "—")}\n内容校验码（SHA-256）：${escapeHtml(event.event_hash ?? "旧记录暂无")}</code>${hashCode ? `<small>当前简称：${escapeHtml(hashCode.slice(0, 12))}</small>` : ""}</div></details>
          <a href="${escapeHtml(artifactUrl(projectId, artifact))}" target="_blank" rel="noopener">查看证据</a>
        </span>
      </article>`;
    }).join("");

    const decisionStatusLabel = (status) => ({
      RESOLVED: "已确认",
      AUTO_ACCEPTED: "已采用",
      APPLIED: "已采用",
      PASSED: "已通过",
      PENDING: "待批准",
    }[status] ?? status ?? "已记录");
    const decisionRows = activeDecisions.map((item) => `<article class="owa-engineering-decision is-key-decision">
      <div class="owa-decision-card">
        <header><div><span>${escapeHtml(item.kind ?? "工程决定")}</span><strong>${escapeHtml(item.topic ?? item.id ?? "建模决定")}</strong></div><em class="is-${item.status === "PENDING" ? "pending" : "resolved"}">${escapeHtml(decisionStatusLabel(item.status))}</em></header>
        <div class="owa-decision-body"><div><span>决定</span><p>${escapeHtml(item.decision ?? "尚未形成决定")}</p></div><div><span>依据</span><p>${escapeHtml(item.reason ?? "未单独记录决策依据")}</p></div></div>
      </div>
    </article>`).join("");

    const metricNotes = (activeSummary.metrics ?? []).filter((item) => item.detail)
      .map((item) => `<div><dt>${escapeHtml(item.label ?? "阶段指标")}</dt><dd>${escapeHtml(item.detail)}</dd></div>`).join("");
    const metricRows = (activeSummary.metrics ?? []).map((item, index) => `<article class="owa-stage-metric is-tone-${index % 4}">
      <span>${escapeHtml(item.label ?? "阶段指标")}</span>
      <strong>${escapeHtml(item.value ?? "—")}</strong>
    </article>`).join("");

    const runtimeAvailable = ["ONTOP_READY", "DOCUMENT_RUNTIME_READY"].includes(runtimeStatus?.state);
    const runtimeNotApplicable = runtimeStatus?.state === "NOT_APPLICABLE";
    const runtimeReady = runtimeAvailable || runtimeNotApplicable;
    const runtimeFailed = ["AUTOMATION_DISABLED", "WAITING_CONFIGURATION", "DEPLOYMENT_FAILED", "DOCUMENT_RUNTIME_FAILED", "AUTOMATION_FAILED_TO_QUEUE"].includes(runtimeStatus?.state);
    const s6Evidence = activeStage === "S6" ? renderS6ValidationEvidence(activeSummary, projectId, artifacts) : { rows: "", receipt: "", deployment: "" };
    const gateArtifact = stageArtifacts.find((item) => item.path.endsWith("/gate-results.json"));
    const gateFallback = stageStatus === "PASSED" ? "阶段已通过，未提供有效的分项检查回执。" : stageStatus === "NOT_APPLICABLE" ? "本阶段不适用。" : "当前阶段尚无有效的分项检查回执。";
    const qualityRows = s6Evidence.rows || `<div data-stage-gate-receipts><p class="owa-gate-empty">${gateArtifact ? "正在读取本阶段检查回执…" : gateFallback}</p></div>`;

    const historyRows = failedEvents.map((event) => `<div class="owa-history-row"><time>${escapeHtml(chinaTimeLabel(event.at))}</time><strong>${escapeHtml(event.details?.gate ?? "阶段门禁")}</strong><span>${escapeHtml(event.details?.message ?? "输入未通过校验")}</span></div>`).join("");
    const currentExecution = stageStatus === "RUNNING" ? currentStageExecution(state, activeStage) : null;
    const qualityNeedsAttention = ["FAILED", "INTERRUPTED"].includes(currentExecution?.status)
      || ["FAILED", "BLOCKED", "BLOCKED_HUMAN"].includes(stageStatus)
      || (activeSummary.validation_plan?.checks ?? []).some((check) => ["FAILED", "BLOCKED"].includes(check.status));
    const qualityLabel = qualityNeedsAttention ? "有检查需要处理" : stageStatus === "PASSED" ? "阶段已通过" : stageStatus === "NOT_APPLICABLE" ? "本阶段不适用" : "查看检查进度";
    const stageDiagnosticTitle = { S3: "语义与问题依据核对", S6: "实例与映射核对" }[activeStage];
    const stageDiagnostics = stageDiagnosticTitle ? `<details class="owa-stage-disclosure" data-engineering-disclosure="stage-diagnostics"><summary><strong>${stageDiagnosticTitle}</strong><span>展开核对已有产物</span></summary><div class="owa-stage-disclosure-body"><section class="owa-business-quality" data-business-quality aria-label="${stageDiagnosticTitle}"></section><p>发现问题后，可在当前工程对话中核对依据和修订方案；修订仍须经过原有阶段校验与确认。</p><button type="button" data-engineering-action="conversation" data-diagnostic-stage="${activeStage}">接续工程处理这些问题</button>${stageStatus === "PASSED" ? '<button type="button" data-engineering-action="reopen-stage">重新核对本阶段</button>' : ""}</div></details>` : "";
    const summarySection = `<section class="owa-engineering-summary" aria-label="阶段概览">
      <div class="owa-stage-summary-heading"><div><div class="owa-stage-result-toolbar"><span class="owa-stage-result-label">本阶段结果</span>${stageReport ? `<a href="${escapeHtml(artifactUrl(projectId, stageReport.path))}" target="_blank" rel="noopener" title="查看本阶段总结报告">阶段报告 ↗</a>` : '<span class="owa-stage-report-pending">报告待生成</span>'}</div><strong>${escapeHtml(activeSummary.headline ?? summary)}</strong><details class="owa-stage-outcome" data-engineering-disclosure="outcome"><summary>结果说明</summary><p>${escapeHtml(activeSummary.outcome ?? (["PASSED", "NOT_APPLICABLE"].includes(stageStatus) ? "本阶段已形成结果，并保留执行记录、门禁证据和正式产物。" : "本阶段尚未通过验收，当前执行记录和指标不代表完成。"))}</p></details>${materializationProgressSummary ? `<p aria-label="来源分段提取进度">${escapeHtml(materializationProgressSummary)}</p>` : ""}${qualityProgressSummary ? `<p aria-label="当前质量验收进度">${escapeHtml(qualityProgressSummary)}</p>` : ""}${renderS7ValidationProgress(runtimeStatus, activeStage)}</div></div>
      <div class="owa-stage-metric-grid">${metricRows || `<div class="owa-engineering-empty">${["PASSED", "NOT_APPLICABLE"].includes(stageStatus) ? "本阶段结果详见质量门禁与阶段报告。" : "阶段指标将在完成相应检查后显示。"}</div>`}</div>
      <details class="owa-quality-section owa-stage-disclosure${qualityNeedsAttention ? " needs-attention" : ""}" data-engineering-disclosure="quality" data-disclosure-attention="${qualityNeedsAttention}" open><summary><strong>质量门禁</strong><span>${qualityLabel}</span></summary><div class="owa-stage-disclosure-body">${s6Evidence.receipt}${qualityRows}${activeStage === "S6" && !s6Evidence.rows ? '<p class="owa-s6-evidence-notice">当前报告未提供逐项执行方式；请查看阶段报告中的实际证据，不能据阶段状态推断每项均重新执行。</p>' : ""}${s6Evidence.deployment}${stageDiagnostics}</div></details>
      ${activeStage === "S3" ? '<details class="owa-stage-disclosure" data-engineering-disclosure="business-preview"><summary><strong>业务能力试运行</strong><span>单项样例 · 实例与规则依据</span></summary><div class="owa-stage-disclosure-body"><section class="owa-business-preview" data-business-preview aria-label="业务能力试运行"></section></div></details>' : ""}
      ${renderEngineeringStageContract(engineeringData, activeStage)}
      <details class="owa-stage-supporting-details owa-stage-disclosure" data-engineering-disclosure="stage-details"><summary><strong>执行明细</strong><span>耗时 · 指标口径</span></summary><div class="owa-stage-supporting-body">
      ${renderStageTiming(state, activeStage)}
      ${metricNotes ? `<details class="owa-stage-disclosure owa-stage-metric-notes" data-engineering-disclosure="metric-notes"><summary><strong>指标口径</strong><span>查看统计说明</span></summary><dl class="owa-stage-disclosure-body">${metricNotes}</dl></details>` : ""}
      </div></details>
      <div class="owa-event-stage-group${engineeringHistoryExpanded ? " is-expanded" : ""}"${failedEvents.length ? "" : " hidden"}>
        <button type="button" class="owa-history-toggle" data-engineering-action="toggle-history" aria-expanded="${engineeringHistoryExpanded}"><span>历史校验 <em>${failedEvents.length} 次</em></span><span class="owa-disclosure">${engineeringHistoryExpanded ? "收起" : "展开"}</span></button>
        ${engineeringHistoryExpanded ? `<div class="owa-history-list">${historyRows || '<div class="owa-engineering-empty">暂无失败校验记录。</div>'}</div>` : ""}
      </div>
    </section>`;
    const decisionsSection = `<section class="owa-engineering-decisions" aria-label="决策摘要">
      <div class="owa-decision-summary-heading"><div><strong>关键决定</strong><span>只展示改变范围、语义、工具、门禁或发布状态的决定</span></div><em><b>${activeDecisions.length}</b> 项</em></div>
      ${decisionRows || '<div class="owa-engineering-empty">本阶段主要执行确定性处理，没有新增需要展示的关键决定。</div>'}
    </section>`;
    const activitySection = `<section class="owa-engineering-activity-list" aria-label="执行记录">
      <div class="owa-activity-summary"><div><strong>本阶段执行说明</strong><span>按时间倒序展示</span></div><p>${escapeHtml(activeContract?.purpose ?? activityStageExplanations[activeStage] ?? "记录本阶段实际发生的操作、结果和审计证据。")}</p><em><b>${escapeHtml(activeStageEvents.length)}</b> 条</em></div>
      <div class="owa-activity-timeline" role="list">${activityRows || '<div class="owa-engineering-empty">该阶段尚无执行记录。</div>'}</div>
    </section>`;
    const friendlyRevisionReason = (value) => String(value ?? "阶段调整")
      .replaceAll("purchaseOrderHasSupplier", "订单关联供应商（purchaseOrderHasSupplier）")
      .replaceAll("source_table/source_column/target_table/target_column", "来源表（source_table）/来源字段（source_column）/目标表（target_table）/目标字段（target_column）")
      .replaceAll("旧 Mapping", "旧映射（Mapping）")
      .replaceAll(" Mapping", " 映射（Mapping）");
    const revisionRows = revisions.map((revision, index) => {
      const summary = revision.diff_summary ?? {};
      const affectedStages = [...new Set([
        ...(Array.isArray(revision.required_revalidation_stages) ? revision.required_revalidation_stages : []),
        revision.target_stage,
      ].filter(Boolean))].sort((left, right) => stageDefinitions.findIndex(([id]) => id === left) - stageDefinitions.findIndex(([id]) => id === right));
      const stageName = (stageId) => stageDefinitions.find(([id]) => id === stageId)?.[1] ?? "阶段";
      const stagePills = affectedStages.map((stageId) => `<span>${escapeHtml(stageId)} ${escapeHtml(stageName(stageId))}</span>`).join("");
      const createdAt = String(revision.created_at ?? "");
      const createdAtLabel = createdAt ? createdAt.replace("T", " ").slice(0, 16) : "时间未记录";
      const reportLink = revision.report_path
        ? `<a class="owa-revision-report-link" href="${escapeHtml(artifactUrl(projectId, revision.report_path))}" target="_blank" rel="noopener"><span>查看完整差异报告</span><small>前后版本、文件清单与 SHA-256 校验</small></a>`
        : '<span class="owa-revision-report-pending">差异报告生成中</span>';
      return `<article class="owa-revision-card">
        <header>
          <div><span>留痕调整 ${index + 1}</span><strong>${escapeHtml(revision.target_stage ?? "阶段")} ${escapeHtml(stageName(revision.target_stage))}重新核对</strong></div>
          <em class="is-${revision.status === "COMPLETED" ? "resolved" : "pending"}" title="技术状态码：${escapeHtml(revision.status ?? "IN_PROGRESS")}">${escapeHtml(revision.status === "COMPLETED" ? "已完成" : "重新验证中")}</em>
        </header>
        <p class="owa-revision-reason">${escapeHtml(friendlyRevisionReason(revision.reason))}</p>
        <div class="owa-revision-meta"><span>发起人 <b>${escapeHtml(executorLabel(revision.requested_by ?? "未记录"))}</b></span><time datetime="${escapeHtml(createdAt)}">${escapeHtml(createdAtLabel)}</time><code title="完整调整编号">${escapeHtml(revision.revision_id ?? "未编号")}</code></div>
        <div class="owa-revision-metrics" aria-label="本次变更摘要">
          <article class="owa-revision-metric is-added"><span>新增</span><strong>${escapeHtml(summary.added ?? 0)}</strong><small>新形成的文件或资产</small></article>
          <article class="owa-revision-metric is-modified"><span>修改</span><strong>${escapeHtml(summary.modified ?? 0)}</strong><small>内容校验码已变化</small></article>
          <article class="owa-revision-metric is-removed"><span>移除</span><strong>${escapeHtml(summary.removed ?? 0)}</strong><small>从当前版本移出的内容</small></article>
          <article class="owa-revision-metric is-affected"><span>受影响阶段</span><strong>${escapeHtml(affectedStages.length)}</strong><div>${stagePills || "<small>未记录阶段范围</small>"}</div></article>
        </div>
        <footer>${reportLink}</footer>
      </article>`;
    }).join("");
    const revisionsSection = `<section class="owa-engineering-decisions" aria-label="变更对比">
      <div class="owa-decision-summary-heading"><div><strong>变更对比</strong><span>旧资产和旧报告保留；新增、修改、移除均按内容校验码（SHA-256）判定</span></div><em><b>${revisions.length}</b> 次留痕</em></div>
      ${revisionRows || '<div class="owa-engineering-empty">尚未发生阶段重开或工程调整。</div>'}
    </section>`;
    const sectionContent = engineeringSection === "decisions"
      ? decisionsSection
      : engineeringSection === "activity"
        ? activitySection
        : engineeringSection === "revisions"
          ? revisionsSection
          : summarySection;

    const publishedArtifact = stageArtifacts.find((item) => item.path.endsWith("/publication.json"));
    const packagePublished = Boolean(publication || publishedArtifact);
    const releaseReady = packagePublished
      && state.project_status === "PUBLISHED"
      && stageStatus === "PASSED"
      && ["ONTOP_READY", "DOCUMENT_RUNTIME_READY", "NOT_APPLICABLE"].includes(runtimeStatus?.state ?? "NOT_APPLICABLE");
    const activeStageIndex = stageDefinitions.findIndex(([id]) => id === activeStage);
    const nextDefinition = reachedStageDefinitions.find(([id]) => (
      stageDefinitions.findIndex(([stageId]) => stageId === id) > activeStageIndex
    ));
    const s0ModeGuidance = intakeMode === "DATABASE_ONLY"
      ? {
          purpose: "本项目没有待处理资料，S0 不做文字识别，但必须记录为什么从数据库开始、由谁决定以及使用哪个数据源。",
          tasks: ["记录不处理资料的业务原因", "登记决定人和数据库来源", "生成 S0 范围判定报告并进入 S1"],
        }
      : intakeMode === "DOCUMENT_ONLY"
        ? {
            purpose: "本项目以 PDF、Word、Excel 等文件资料建设本体；S0 先把原始文件整理成后续阶段可复核、可追溯的正式证据。",
            tasks: ["登记原文件和内容校验码", "生成结构化文本与原文定位证据", "S1 留痕跳过后从 S2 继续到 S7"],
          }
        : stageGuidance.S0;
    const guidance = activeStage === "S0"
      ? s0ModeGuidance
      : activeStage === "S7" && releaseRevocation
      ? {
          purpose: "已发布版本已明确撤回，正式包保留供审计，但不再作为当前正式资产。",
          tasks: ["查看撤回原因与审计事件", "保留原发布包和完整性清单", "需要继续时基于原版本建立新修订"],
        }
      : activeStage === "S7" && packagePublished
      ? {
          purpose: releaseReady
            ? "发布包与运行时均已验证，可以作为当前正式本体使用。"
            : "负责人已经批准并生成发布包；只有运行时验证通过后，才会标记为正式可用。",
          tasks: releaseReady
            ? ["已汇总 S0～S6 的质量证据", "负责人已明确批准发布", "发布包完整性与运行时查询均已验证"]
            : ["发布包与完整性清单已经生成", "等待或修复运行时部署", "验证接口、查询模板和预期结果"],
        }
      : stageGuidance[activeStage] ?? { purpose: definition[2], tasks: [] };
    const documentOnlyS0 = !lifecycleV2 && activeStage === "S0" && intakeMode === "DOCUMENT_ONLY";
    const stageFinished = ["PASSED", "NOT_APPLICABLE"].includes(stageStatus);
    const activeDocumentJob = ["S0", "S1"].includes(activeStage) ? state.document_ingestion_job : null;
    const documentJobCopy = activeDocumentJob
      ? `${({ QUEUED: "资料已排队，等待执行器", RUNNING: "正在解析资料", READY_FOR_REVIEW: "解析完成，等待复核提交", FAILED: "资料解析失败，需处理失败项", TIMED_OUT: "资料解析超时，待恢复", CANCELLED: "资料任务已取消", COMMITTED: "资料已提交" })[activeDocumentJob.status] ?? activeDocumentJob.status}；文件 ${activeDocumentJob.completed_files ?? 0}/${activeDocumentJob.total_files ?? 0}，失败 ${activeDocumentJob.failed_files ?? 0}。${activeDocumentJob.message ?? ""}`
      : null;
    const inProgressCopy = {
      S0: "当前 S0 还没有完成：请继续整理原文件、结构化文本、原文定位证据和质量检查。解析与质量校验通过后会自动写入 S0；只有截断、降级或失败才需要处理。",
      S1: "S1 会自动以只读方式盘点数据源、业务表、字段分布、主外键关系和数据质量；只有范围冲突或关键数据含义不明确时才需要你选择。",
      S2: "S1 完成后，S2 会自动读取 S0/S1 证据并识别业务对象、属性、关系和规则候选；遇到证据不足或执行失败时才需要人工处理。",
      S3: "S3 会自动生成和检查映射；只有高影响业务含义存在多个合理选项时，才会停下来让你选择并说明各自影响。",
      S4: "S4 会自动形成本体施工图；需要业务负责人决定 CQ 验收问题时，页面会给出候选问题和调整入口，不要求填写底层建模参数。",
      S5: "S5 会根据已确认的施工图自动生成本体文件和数据约束，并保留真实工具执行证据。",
      S6: "S6 会自动完成逻辑、约束、映射、业务问题和真实实例验证；失败时只展示原因、影响和重试入口。",
    };
    const nextStepCopy = projectArchived
      ? "这个工程已经归档。全部报告、产物和执行记录仍可查看；恢复后会从归档前保存的阶段继续。"
      : engineeringCurrentActionCopy(engineeringData, activeStage)
        ? engineeringCurrentActionCopy(engineeringData, activeStage)
      : documentJobCopy
        ? documentJobCopy
      : currentExecution?.status === "FAILED"
        ? "本次执行已失败，阶段尚未完成。请根据下方失败回执，在当前工程中修复后重新验证；已有有效产物保留。"
      : activeStage === "S6" && currentExecution?.status === "INTERRUPTED"
        ? "本次执行已中断。回到本体工作台恢复 S6，平台会检查并复用有效的候选图检查点。"
      : !stageFinished && activeStage !== "S7"
      ? lifecycleV2 ? `继续 ${activeStage} ${definition[1]}：${definition[2]}。平台核验输入与结果，需要业务判断时再交给负责人处理。`
        : inProgressCopy[activeStage] ?? "当前阶段还没有完成，请继续本阶段工作并通过质量门禁。"
      : documentOnlyS0
        ? "S0 资料证据已经完成；S1 将以范围报告留痕跳过，下一步从 S2 识别业务对象、关系、属性和规则候选。"
        : nextDefinition
          ? `下一阶段 ${nextDefinition[0]} ${nextDefinition[1]}：${nextDefinition[2]}`
          : activeStage !== "S7"
            ? "下一阶段尚未进入实际执行；进入后会自动出现在工程详情中。"
          : releaseRevocation
            ? "版本已撤回；原发布包只作为历史审计保留，需要继续时请建立新修订。"
            : releaseReady
            ? "正式发布已经完成，发布包与运行时查询均已验证。"
            : packagePublished
              ? runtimeFailed
                ? "发布包已保留，但运行时验证失败；请根据失败回执修复后重新验证，当前尚不能作为正式可用版本。"
                : "发布包已生成，正在等待运行时接口、查询模板和预期结果验证完成。"
            : "S1～S6 已完成，等待负责人明确批准正式发布。";
    const competencyQuestionGateActions = `<button type="button" class="owa-engineering-primary" data-engineering-action="gate">审阅并确认设计</button><button type="button" data-engineering-action="conversation-review">返回对话讨论</button>`;
    const mappingGateActions = `<button type="button" class="owa-engineering-primary" data-engineering-action="gate">处理待确认事项</button><button type="button" data-engineering-action="conversation-review">返回对话讨论</button>`;
    const formalActionStage = engineeringWorkStage(engineeringData);
    const historicalActionStage = formalActionStage && formalActionStage !== activeStage
      ? stageDefinitions.find(([id]) => id === formalActionStage) : null;
    const humanReviewRequired = engineeringNeedsHumanReview(engineeringData);
    const action = projectArchived
      ? `<button type="button" class="owa-engineering-primary" data-engineering-action="open-project-center">到工程中心恢复</button>`
      : historicalActionStage
        ? `<button type="button" class="owa-engineering-primary" data-engineering-action="next-stage" data-next-stage="${historicalActionStage[0]}">前往当前 ${historicalActionStage[0]} ${escapeHtml(historicalActionStage[1])}</button>`
      : humanReviewRequired && blocking?.type === "COMPETENCY_QUESTION_REVIEW"
      ? competencyQuestionGateActions
      : humanReviewRequired
        ? mappingGateActions
          : activeStage === "S7" && releaseRevocation
          ? `<a class="owa-engineering-primary" href="${escapeHtml(artifactUrl(projectId, "07-release/release-revocation-report.html"))}" target="_blank" rel="noopener">查看撤回总结报告</a><button type="button" data-engineering-action="revision-from-release">基于原版本重新修订</button>`
          : activeStage === "S7" && releaseReady
            ? `<div class="owa-current-actions"><button type="button" class="owa-engineering-primary" data-engineering-action="open-release-qa" data-release-version="${escapeHtml(releaseVersion)}" data-release-name="${escapeHtml(displayOntologyName(project.project_name ?? projectId))}">发起本体问答</button><a href="${escapeHtml(artifactUrl(projectId, "07-release/release-report.html"))}" target="_blank" rel="noopener">查看全流程发布报告</a><details class="owa-stage-maintenance" data-engineering-disclosure="maintenance"><summary>版本维护</summary><div><button type="button" data-engineering-action="revision-from-release" data-revision-stage="S4">创建新修订</button><button type="button" class="is-danger" data-engineering-action="revoke-release">撤回该版本</button></div></details></div>`
            : activeStage === "S7" && packagePublished
              ? window.__ORION_RELEASE_RUNTIME__.runtimeRepairActions({ projectStatus: state.project_status, runtimeState: runtimeStatus?.state, reportUrl: artifactUrl(projectId, "07-release/release-report.html"), busy: engineeringReviewInFlight })
            : activeStage === "S7" && stageStatus === "DEFERRED"
              ? `<button type="button" class="owa-engineering-primary" data-engineering-action="resume-release">恢复发布评审</button><button type="button" data-engineering-action="reopen-stage">重新核对前序工作</button>`
              : activeStage === "S7"
              ? `<button type="button" class="owa-engineering-primary" data-engineering-action="publish-release">确认并发布</button><button type="button" data-engineering-action="conversation">进入发布讨论</button><button type="button" data-engineering-action="defer-release">暂不发布</button><button type="button" data-engineering-action="reopen-stage">重新核对前序工作</button>`
                : !stageFinished
                  ? `<button type="button" class="owa-engineering-primary" data-engineering-action="open-stage-session-direct">${stageStatus === "FAILED" ? "返回本体工作台诊断" : "返回本体工作台"}</button><button type="button" data-engineering-action="recover-project-session" title="对话过长或反复中断时，从已保存的工程状态恢复；原会话保留">从工程检查点恢复新会话</button>`
                : nextDefinition
                  ? `<button type="button" class="owa-engineering-primary" data-engineering-action="next-stage" data-next-stage="${nextDefinition[0]}">查看 ${nextDefinition[0]} ${escapeHtml(nextDefinition[1])}</button>${stageStatus === "PASSED" ? '<button type="button" data-engineering-action="reopen-stage">重新核对本阶段</button>' : ""}`
                  : `<button type="button" class="owa-engineering-primary" data-engineering-action="conversation">打开工程工作会话</button>`;
    const invalidatedArtifactNotice = invalidatedStageArtifacts.length
      ? `<span class="owa-context-invalidated">${invalidatedStageArtifacts.length} 个旧产物已失效，仅可在“变更对比”中作为历史查看</span>`
      : "";
    const rightArtifacts = `${invalidatedArtifactNotice}${artifactRows || '<span class="owa-context-empty">当前阶段尚未生成当前产物</span>'}`;
    const projectModeLabel = (mode) => ({
      DOCUMENT_ONLY: "资料整理",
      DATABASE_ONLY: "数据建模",
      HYBRID: "资料与数据混合",
    })[mode] ?? "资料与数据混合";
    const projectUpdatedLabel = (value) => {
      return chinaDateTimeLabel(value);
    };
    const projectLineage = window.__ORION_PROJECT_LINEAGE__;
    const projectGroups = projectLineage?.groupProjects?.(projects) ?? projects.map((item) => ({
      rootProjectId: item.project_id,
      displayName: displayOntologyName(item.project_name ?? item.project_id),
      current: item,
      members: [item],
      activeMembers: item.project_status === "ARCHIVED" ? [] : [item],
      projectIds: [item.project_id],
      versionCount: 1,
      allArchived: item.project_status === "ARCHIVED",
      needsAttention: item.project_status === "BLOCKED_HUMAN" || item.stage_liveness?.state === "SUSPECTED_INTERRUPTED",
      activeWork: !["PUBLISHED", "PACKAGE_PUBLISHED", "RELEASE_REVOKED", "ARCHIVED"].includes(item.project_status),
      published: item.project_status === "PUBLISHED",
      projectKind: item.project_kind ?? "BUSINESS",
      updatedAt: item.updated_at ?? "",
    }));
    const projectVersionLabel = (item) => projectLineage?.projectVersionLabel?.(item) ?? "建设记录";
    const completedStageCount = (item) => stageDefinitions.filter(([id]) => (
      ["PASSED", "NOT_APPLICABLE"].includes(item.stage_statuses?.[id])
    )).length;
    const currentStageLabel = (item) => {
      if (item.current_stage) return `当前在 ${item.current_stage}`;
      if (completedStageCount(item) === stageDefinitions.length || item.project_status === "PUBLISHED") return "流程已完成";
      const itemStageStatuses = item.stage_statuses ?? {};
      const highestCompletedIndex = stageDefinitions.reduce(
        (highest, [id], index) => ["PASSED", "NOT_APPLICABLE"].includes(itemStageStatuses[id]) ? index : highest,
        -1,
      );
      const nextPendingStage = stageDefinitions[highestCompletedIndex + 1]?.[0];
      return nextPendingStage ? `待进入 ${nextPendingStage}` : "尚未开始";
    };
    const versionSourceLabel = (item) => item.based_on_release_version
      ? `基于 v${String(item.based_on_release_version).replace(/^v/iu, "")} 修订`
      : "初始建设";
    const projectStatusTone = (item) => String(item.project_status ?? "IN_PROGRESS")
      .toLowerCase()
      .replace(/[^a-z0-9_-]/g, "");
    const normalizeProjectSearch = (value) => String(value ?? "")
      .normalize("NFKC")
      .toLocaleLowerCase("zh-CN")
      .replace(/[\s·•/\\()[\]（）_—-]+/gu, "");
    const fuzzyProjectMatch = (value, query) => {
      const searchable = normalizeProjectSearch(value);
      if (!query || searchable.includes(query)) return true;
      let cursor = 0;
      for (const character of searchable) {
        if (character === query[cursor]) cursor += 1;
        if (cursor === query.length) return true;
      }
      return false;
    };
    const renderProjectRevisionRow = (item) => {
      const itemProjectId = item.project_id ?? "";
      const archived = item.project_status === "ARCHIVED";
      return `<article class="owa-project-revision-row${itemProjectId === projectId ? " is-current" : ""}${archived ? " is-archived" : ""}">
        <div class="owa-project-revision-identity"><strong>${escapeHtml(projectVersionLabel(item))}</strong><span>${escapeHtml(versionSourceLabel(item))}${itemProjectId === projectId ? " · 当前打开" : ""}</span><code title="工程编号">${escapeHtml(itemProjectId)}</code></div>
        <div class="owa-project-revision-state"><em class="is-${escapeHtml(projectStatusTone(item))}">${escapeHtml(projectStatusLabel(item.project_status))}</em></div>
        <div class="owa-project-revision-progress"><strong>${escapeHtml(currentStageLabel(item))}</strong><span>${completedStageCount(item)} / ${stageDefinitions.length} 阶段</span></div>
        <time datetime="${escapeHtml(item.updated_at ?? "")}">${escapeHtml(projectUpdatedLabel(item.updated_at))}</time>
        <button type="button" data-engineering-action="open-project" data-project-id="${escapeHtml(itemProjectId)}">${archived ? "查看" : "打开"}</button>
      </article>`;
    };
    const renderProjectCard = (group) => {
      const item = group.current ?? {};
      const itemProjectId = item.project_id ?? "";
      const itemProjectName = group.displayName ?? displayOntologyName(item.project_name ?? itemProjectId);
      const itemIntakeMode = item.intake_mode ?? "HYBRID";
      const itemStageStatuses = item.stage_statuses ?? {};
      const completedStages = completedStageCount(item);
      const stageProgress = engineeringStageDefinitions(item).filter(([id]) => (
        id === item.current_stage || (itemStageStatuses[id] ?? "PENDING") !== "PENDING"
      )).map(([id, name]) => {
        const status = itemStageStatuses[id] ?? "PENDING";
        const tone = String(status).toLowerCase().replace(/[^a-z0-9_-]/g, "");
        return `<button type="button" class="owa-project-stage is-${escapeHtml(tone)}" data-engineering-action="open-project-stage" data-project-id="${escapeHtml(itemProjectId)}" data-project-stage="${id}" title="${escapeHtml(`${id} ${name}：${stageStatusDisplay(id, status, itemIntakeMode, item)}`)}"><strong>${id}</strong><span>${escapeHtml(stageStatusDisplay(id, status, itemIntakeMode, item))}</span></button>`;
      }).join("");
      const familyId = String(group.rootProjectId ?? itemProjectId);
      const revisionsId = `owa-project-revisions-${familyId.replace(/[^a-z0-9_-]/giu, "-")}`;
      const expanded = engineeringExpandedProjectFamilies.has(familyId);
      const archived = item.project_status === "ARCHIVED";
      const projectKindLabel = group.projectKind === "ACCEPTANCE_TEST" ? "验收 / 测试工程" : "业务工程";
      const revisionCount = Math.max(0, group.versionCount - 1);
      const revisionSummary = revisionCount ? ` · ${revisionCount} 次修订` : "";
      const legacyReleaseLabel = item.release_contract?.status === "LEGACY_UNVERIFIED"
        ? '<small class="owa-release-contract-notice is-legacy"><i class="owa-engineering-icon is-warning" aria-hidden="true"></i><span>旧版验收信息不完整</span><b>交付受限</b></small>'
        : item.release_contract?.status === "PACKAGE_INTEGRITY_FAILED"
          ? `<small class="owa-release-contract-notice is-legacy" title="${escapeHtml(item.release_contract.message ?? "")}"><i class="owa-engineering-icon is-warning" aria-hidden="true"></i><span>发布包完整性异常</span><b>交付受限</b></small>`
        : "";
      const suspectedReason = item.stage_liveness?.evidence === "OWNER_LOCK_RELEASED"
        ? "执行器锁已释放"
        : item.stage_liveness?.evidence === "HEARTBEAT_STALE"
          ? "执行器心跳已超时"
          : "阶段缺少持续执行证据";
      const suspectedInterruptedLabel = !archived && item.stage_liveness?.state === "SUSPECTED_INTERRUPTED"
        ? `<small class="owa-release-contract-notice is-legacy is-interrupted" title="${escapeHtml(`${item.current_stage ?? "当前阶段"} 仍标记为运行中，但${suspectedReason}；请先打开工程回读状态，确认无活跃执行器后再恢复。`)}"><i class="owa-engineering-icon is-warning" aria-hidden="true"></i><span>疑似中断</span><b>${escapeHtml(item.current_stage ?? "")} 需处理</b></small>`
        : "";
      const openLabel = !archived && item.parent_project_id ? "继续修订" : "打开工程";
      return `<article class="owa-project-card${group.projectIds?.includes(projectId) ? " is-current" : ""}${archived ? " is-archived" : ""}">
        <header><div><span>${escapeHtml(projectModeLabel(itemIntakeMode))} · ${escapeHtml(projectKindLabel)}</span><h3>${escapeHtml(itemProjectName)}</h3><small>${escapeHtml(itemProjectId)}${escapeHtml(revisionSummary)}</small>${legacyReleaseLabel}${suspectedInterruptedLabel}</div><em class="is-${escapeHtml(projectStatusTone(item))}">${escapeHtml(projectStatusLabel(item.project_status))}</em></header>
        <div class="owa-project-progress-heading"><span>全链路进度</span><strong>${completedStages} / ${stageDefinitions.length} 个阶段完成</strong><small>${escapeHtml(currentStageLabel(item))}</small></div>
        <div class="owa-project-stage-grid" aria-label="${escapeHtml(itemProjectName)} 已到达的阶段">${stageProgress}</div>
        ${group.versionCount > 1 ? `<section class="owa-project-revision-group"><button type="button" class="owa-project-revision-toggle" data-engineering-action="toggle-project-family" data-project-family-id="${escapeHtml(familyId)}" aria-expanded="${expanded}" aria-controls="${escapeHtml(revisionsId)}"><span><strong>修订记录</strong><small>当前 ${escapeHtml(projectVersionLabel(item))}</small></span><b>${expanded ? "收起" : `${revisionCount} 次修订`}</b></button>${expanded ? `<div class="owa-project-revision-list" id="${escapeHtml(revisionsId)}" role="region" aria-label="${escapeHtml(itemProjectName)}的修订记录">${group.members.map(renderProjectRevisionRow).join("")}</div>` : ""}</section>` : ""}
        <footer><span>最近更新：${escapeHtml(projectUpdatedLabel(item.updated_at))}${group.projectIds?.includes(projectId) ? " · 当前打开" : ""}</span><div><button type="button" class="owa-project-action is-open" data-engineering-action="open-project" data-project-id="${escapeHtml(itemProjectId)}"><i class="owa-engineering-icon is-play" aria-hidden="true"></i><span>${openLabel}</span></button>${archived ? `<button type="button" class="owa-project-action is-restore" data-engineering-action="restore-project-target" data-project-id="${escapeHtml(itemProjectId)}" data-project-name="${escapeHtml(item.project_name ?? itemProjectId)}" data-project-revision="${escapeHtml(item.revision ?? 0)}"><i class="owa-engineering-icon is-refresh" aria-hidden="true"></i><span>恢复工程</span></button>` : `<button type="button" class="owa-project-action is-muted is-archive" data-engineering-action="archive-project-target" data-project-id="${escapeHtml(itemProjectId)}" data-project-name="${escapeHtml(item.project_name ?? itemProjectId)}" data-project-revision="${escapeHtml(item.revision ?? 0)}"><i class="owa-engineering-icon is-archive" aria-hidden="true"></i><span>移入回收站</span></button>`}</div></footer>
      </article>`;
    };
    const activeProjectGroups = projectGroups.filter((group) => !group.allArchived);
    const archivedProjectGroups = projectGroups.filter((group) => group.allArchived);
    const businessProjectGroups = activeProjectGroups.filter((group) => group.projectKind !== "ACCEPTANCE_TEST");
    const acceptanceProjectGroups = activeProjectGroups.filter((group) => group.projectKind === "ACCEPTANCE_TEST");
    const waitingProjectGroups = activeProjectGroups.filter((group) => group.needsAttention);
    const publishedProjectGroups = activeProjectGroups.filter((group) => group.published);
    const normalizedProjectSearch = normalizeProjectSearch(engineeringProjectSearch);
    const groupMatchesSearch = (group) => !normalizedProjectSearch || group.members.some((item) => {
      const currentStageName = engineeringStageDefinitions(item).find(([id]) => id === item.current_stage)?.[1] ?? "";
      const searchCorpus = [
        group.displayName,
        item.project_name,
        item.internal_project_name,
        item.project_id,
        item.domain,
        item.release_version,
        item.suggested_release_version,
        item.based_on_release_version,
        projectStatusLabel(item.project_status),
        item.current_stage,
        currentStageName,
        "工程中心 工程详情 全链路进度 修订记录 继续修订",
      ].filter(Boolean).join(" ");
      return fuzzyProjectMatch(searchCorpus, normalizedProjectSearch);
    });
    const { groups: filteredProjectGroups, recordCount: filteredRecordCount } = window.__ORION_PROJECT_LINEAGE__.selectProjectGroups(projectGroups, {
      kind: engineeringProjectKindFilter,
      status: engineeringProjectStatusFilter,
      matchesSearch: groupMatchesSearch,
    });
    const filteredArchivedProjectGroups = archivedProjectGroups.filter(groupMatchesSearch).sort((left, right) => (
      (Date.parse(right.updatedAt ?? right.current?.updated_at ?? "") || 0)
      - (Date.parse(left.updatedAt ?? left.current?.updated_at ?? "") || 0)
    ));
    const projectPageCount = Math.max(1, Math.ceil(filteredProjectGroups.length / ENGINEERING_PROJECT_PAGE_SIZE));
    engineeringProjectPage = Math.min(Math.max(1, engineeringProjectPage), projectPageCount);
    const projectPageStart = (engineeringProjectPage - 1) * ENGINEERING_PROJECT_PAGE_SIZE;
    const visibleProjectGroups = filteredProjectGroups.slice(projectPageStart, projectPageStart + ENGINEERING_PROJECT_PAGE_SIZE);
    const projectPageItems = Array.from({ length: projectPageCount }, (_item, index) => index + 1)
      .filter((page) => projectPageCount <= 7 || page === 1 || page === projectPageCount || Math.abs(page - engineeringProjectPage) <= 1)
      .reduce((items, page, index, pages) => {
        if (index && page - pages[index - 1] > 1) items.push('<span aria-hidden="true">…</span>');
        items.push(`<button type="button" data-engineering-action="project-page" data-project-page="${page}" aria-label="第 ${page} 页" ${page === engineeringProjectPage ? 'aria-current="page"' : ""}>${page}</button>`);
        return items;
      }, [])
      .join("");
    const projectPagination = projectPageCount > 1 ? `<nav class="owa-project-pagination" aria-label="当前工程分页">
      <span>第 ${engineeringProjectPage} / ${projectPageCount} 页</span>
      <div><button type="button" data-engineering-action="project-page" data-project-page="${engineeringProjectPage - 1}" ${engineeringProjectPage === 1 ? "disabled" : ""}>上一页</button>${projectPageItems}<button type="button" data-engineering-action="project-page" data-project-page="${engineeringProjectPage + 1}" ${engineeringProjectPage === projectPageCount ? "disabled" : ""}>下一页</button></div>
    </nav>` : "";
    const projectKindFilter = `<div class="owa-project-center-tools"><label><span>搜索工程</span><input type="search" name="project-search" value="${escapeHtml(engineeringProjectSearch)}" placeholder="搜索名称、业务域、编号、版本、状态或阶段" autocomplete="off" enterkeyhint="search"></label><nav class="owa-project-kind-filter" aria-label="工程类型筛选"><span>工程类型</span><div>
      <button type="button" data-engineering-action="project-kind-filter" data-project-kind="BUSINESS" aria-pressed="${engineeringProjectKindFilter === "BUSINESS"}">业务工程 ${businessProjectGroups.length}</button>
      <button type="button" data-engineering-action="project-kind-filter" data-project-kind="ACCEPTANCE_TEST" aria-pressed="${engineeringProjectKindFilter === "ACCEPTANCE_TEST"}">验收 / 测试 ${acceptanceProjectGroups.length}</button>
      <button type="button" data-engineering-action="project-kind-filter" data-project-kind="ALL" aria-pressed="${engineeringProjectKindFilter === "ALL"}">全部 ${activeProjectGroups.length}</button>
    </div></nav></div>`;
    const selectedProjectKindLabel = engineeringProjectStatusFilter === "WAITING"
      ? "待我处理工程"
      : engineeringProjectStatusFilter === "PUBLISHED"
        ? "已发布工程"
        : engineeringProjectKindFilter === "BUSINESS"
          ? "业务工程"
          : engineeringProjectKindFilter === "ACCEPTANCE_TEST"
            ? "验收 / 测试工程"
            : "全部当前工程";
    const summaryFilterSelected = (kind, status = "ALL") => (
      engineeringProjectKindFilter === kind && engineeringProjectStatusFilter === status
    );
    const projectCenter = `<main class="owa-project-center-workspace" aria-label="工程中心">
      <header class="owa-project-center-hero"><div><span><i class="owa-engineering-icon is-tools" aria-hidden="true"></i>工程详情</span><h1>工程中心</h1><p>查看、回收和恢复本体工程，并核验每个工程从 S0 到 S7 的真实进度与阶段产物。</p></div><button type="button" class="owa-project-create-button" data-engineering-action="open-new-project"><i class="owa-engineering-icon is-plus" aria-hidden="true"></i><span>新建工程</span></button></header>
      <section class="owa-project-center-stats" aria-label="按工程统计筛选列表"><button type="button" class="is-all${summaryFilterSelected("ALL") ? " is-selected" : ""}" data-engineering-action="project-summary-filter" data-project-kind="ALL" data-project-status="ALL" aria-pressed="${summaryFilterSelected("ALL")}"><i class="owa-engineering-icon is-folder" aria-hidden="true"></i><div><span>全部工程</span><strong>${activeProjectGroups.length}</strong></div></button><button type="button" class="is-active${summaryFilterSelected("BUSINESS") ? " is-selected" : ""}" data-engineering-action="project-summary-filter" data-project-kind="BUSINESS" data-project-status="ALL" aria-pressed="${summaryFilterSelected("BUSINESS")}"><i class="owa-engineering-icon is-play" aria-hidden="true"></i><div><span>业务工程</span><strong>${businessProjectGroups.length}</strong></div></button><button type="button" class="is-waiting${summaryFilterSelected("ALL", "WAITING") ? " is-selected" : ""}" data-engineering-action="project-summary-filter" data-project-kind="ALL" data-project-status="WAITING" aria-pressed="${summaryFilterSelected("ALL", "WAITING")}"><i class="owa-engineering-icon is-warning" aria-hidden="true"></i><div><span>待我处理</span><strong>${waitingProjectGroups.length}</strong></div></button><button type="button" class="is-published${summaryFilterSelected("ALL", "PUBLISHED") ? " is-selected" : ""}" data-engineering-action="project-summary-filter" data-project-kind="ALL" data-project-status="PUBLISHED" aria-pressed="${summaryFilterSelected("ALL", "PUBLISHED")}"><i class="owa-engineering-icon is-check" aria-hidden="true"></i><div><span>已发布</span><strong>${publishedProjectGroups.length}</strong></div></button><button type="button" class="is-archive${summaryFilterSelected("ACCEPTANCE_TEST") ? " is-selected" : ""}" data-engineering-action="project-summary-filter" data-project-kind="ACCEPTANCE_TEST" data-project-status="ALL" aria-pressed="${summaryFilterSelected("ACCEPTANCE_TEST")}"><i class="owa-engineering-icon is-archive" aria-hidden="true"></i><div><span>验收 / 测试</span><strong>${acceptanceProjectGroups.length}</strong></div></button></section>
      ${projectKindFilter}
      <section class="owa-project-center-section" data-project-list-anchor><header><div><h2><i class="owa-engineering-icon is-folder" aria-hidden="true"></i>${escapeHtml(selectedProjectKindLabel)}</h2><p>当前条件下共 ${filteredProjectGroups.length} 个工程、${filteredRecordCount} 次建设记录；修订归入原工程卡片，展开“修订记录”即可查看。</p></div><span>${filteredProjectGroups.length} 个工程</span></header><div class="owa-project-card-list">${visibleProjectGroups.map(renderProjectCard).join("") || '<div class="owa-project-center-empty">当前条件下没有工程，请调整状态、类型或搜索词。</div>'}</div>${projectPagination}</section>
      <section class="owa-project-center-section is-recycle"><header><div><h2><i class="owa-engineering-icon is-archive" aria-hidden="true"></i>回收站</h2><p>这里只显示整条建设链都已归档的工程；历史产物和审计记录仍然保留。</p></div><span>${filteredArchivedProjectGroups.length} 个工程</span></header><div class="owa-project-card-list">${filteredArchivedProjectGroups.map(renderProjectCard).join("") || '<div class="owa-project-center-empty">回收站为空。</div>'}</div></section>
      <aside class="owa-project-governance-note"><i class="owa-engineering-icon is-warning" aria-hidden="true"></i><div><strong>为什么没有“永久删除”</strong><p>本体工程要求全程可追踪、可审计。永久删除会破坏历史报告与证据链，所以正式工程只支持移入回收站和恢复。</p></div></aside>
    </main>`;
    const cqQuestions = competencyQuestionReview?.approved_questions
      ?? competencyQuestionReview?.draft_questions
      ?? [];
    const jointDesignReview = lifecycleV2 && competencyQuestionReview?.review_scope === "JOINT_DESIGN";
    const jointDesignCanApprove = !lifecycleV2 || (jointDesignReview && Boolean(competencyQuestionReview?.joint_design_fingerprint));
    const jointDesignReport = artifacts.find((item) => item.lifecycle_status !== "INVALIDATED"
      && /^04-ontology-design\/(?:joint-design|ontology-design)-report\.html$/.test(item.path));
    const cqItems = cqQuestions.map((item, index) => `<article class="owa-cq-item">
      <div><strong>业务问题 ${index + 1}</strong><button type="button" data-engineering-action="remove-cq">移除此题</button></div>
      <input type="hidden" name="cq-id" value="${escapeHtml(item.id ?? `CQ-${String(index + 1).padStart(3, "0")}`)}">
      <label><span>业务人员要问什么</span><textarea name="cq-question" rows="2">${escapeHtml(item.question ?? "")}</textarea></label>
      <label><span>怎样算回答正确</span><textarea name="cq-expected" rows="2">${escapeHtml(item.expected ?? "")}</textarea></label>
      ${typeof item.answer_contract?.answer_scope_zh === "string" && item.answer_contract.answer_scope_zh.trim() ? `<p class="owa-cq-answer-scope"><strong>回答范围：</strong>${escapeHtml(item.answer_contract.answer_scope_zh)}</p>` : ""}
      <details><summary>查看系统生成的技术查询（只读）</summary><pre>${escapeHtml(item.sparql ?? "将在保存时自动生成")}</pre></details>
    </article>`).join("");
    const s4ReviewDrawer = `<div class="owa-review-backdrop" data-engineering-action="close-review" aria-hidden="true"></div><aside class="owa-review-drawer" role="dialog" aria-modal="true" aria-label="${jointDesignReview ? "S4 联合设计整体确认" : "S4 业务问题评审"}">
      <header><div><span>S4 人工评审</span><h2>${jointDesignReview ? "确认本体、映射、规则与验收问题" : "确认本体必须回答的业务问题"}</h2><p>${jointDesignReview ? "请审阅整套设计及其来源依据。整体确认后进入 S5，后续变更需重新校准和留痕。" : "可以采用建议，也可以修改、增删；确认后的问题会进入 S6 真实执行验证。"}</p></div><button type="button" data-engineering-action="close-review" aria-label="关闭评审抽屉">关闭</button></header>
      <div class="owa-review-body">${jointDesignReview ? `${renderJointDesignSummary(competencyQuestionReview)}${jointDesignReport ? `<a href="${escapeHtml(artifactUrl(projectId, jointDesignReport.path))}" target="_blank" rel="noopener">查看完整联合设计报告</a>` : ""}` : `<section class="owa-review-question"><h3>这一步解决什么</h3><p>业务问题就是本体的验收题。不是让您编写技术语句，而是先确认“用户以后到底要问什么、怎样算回答正确”。</p></section>`}
        <form class="owa-cq-review-form"><div class="owa-cq-list">${cqItems}</div>
          <button type="button" data-engineering-action="add-cq">增加一个业务问题</button>
          <label class="owa-review-rationale"><span>本次确认或退回的理由（必填）</span><textarea name="rationale" required aria-required="true" rows="3" placeholder="例如：这些问题覆盖了供应商、订单和风险场景。"></textarea></label>
          <p class="owa-review-message" id="owa-cq-review-error" data-cq-review-error role="alert" ${engineeringReviewMessage ? "" : "hidden"}>${escapeHtml(engineeringReviewMessage ?? "")}</p>
          <div class="owa-review-actions"><button type="button" data-engineering-action="conversation-review">去对话页讨论</button><button type="button" data-engineering-action="return-cq-review">退回 S3 调整</button><button type="button" class="is-primary" data-engineering-action="submit-cq-review" ${engineeringReviewInFlight || !jointDesignCanApprove ? "disabled" : ""}>${engineeringReviewInFlight ? "正在保存…" : jointDesignReview ? "确认整套联合设计" : "确认这些业务问题"}</button></div>
        </form>
      </div></aside>`;
    const s3ReviewDrawer = activeConfirmation
      ? `<div class="owa-review-backdrop" data-engineering-action="close-review" aria-hidden="true"></div><aside class="owa-review-drawer" role="dialog" aria-modal="true" aria-label="S3 高影响语义评审">
            <header><div><span>S3 人工评审</span><h2>${escapeHtml(activeConfirmation.title ?? "业务语义确认")}</h2><p>第 ${pendingConfirmations.indexOf(activeConfirmation) + 1} / ${pendingConfirmations.length} 题 · 选择会同步写入${lifecycleV2 ? "语义与候选映射" : "正式映射"}、阶段总结报告和审计链</p></div><button type="button" data-engineering-action="close-review" aria-label="关闭评审抽屉">关闭</button></header>
            <div class="owa-review-body">
              <section class="owa-review-question"><h3>需要你决定什么</h3><p>${escapeHtml(activeConfirmation.business_question ?? activeConfirmation.question)}</p></section>
              <section><h3>数据库已经确认的事实</h3>${(activeConfirmation.evidence?.database_facts ?? []).map((fact) => `<article class="owa-review-evidence"><p>${escapeHtml(fact.summary)}</p><small>证据来源：${escapeHtml((fact.source_refs ?? []).join("、"))}</small></article>`).join("") || '<p class="owa-review-muted">暂无数据库事实。</p>'}</section>
              <section><h3>业务资料与访谈</h3>${[...(activeConfirmation.evidence?.business_materials ?? []), ...(activeConfirmation.evidence?.customer_interviews ?? [])].map((item) => `<article class="owa-review-evidence"><p>${escapeHtml(item.summary ?? item)}</p></article>`).join("") || '<p class="owa-review-muted">本题没有额外业务资料或访谈记录；不会用不存在的材料替你做决定。</p>'}</section>
              <section class="owa-review-ai"><h3>人工智能辅助判断</h3><p>${escapeHtml(activeConfirmation.evidence?.ai_inference ?? "未提供辅助判断")}</p><small>这是建议，不是数据库事实，也不会代替你的最终选择。</small></section>
              <form class="owa-review-form" data-confirmation-id="${escapeHtml(activeConfirmation.id)}">
                <fieldset><legend>请选择一种建模方案</legend>${(activeConfirmation.options ?? []).map((option) => `<label class="owa-review-option"><input type="radio" name="s3-option" value="${escapeHtml(option.id)}" ${option.id === activeConfirmation.recommended_option_id ? "checked" : ""}><span><strong>${escapeHtml(option.label)}${option.recommended ? '<em>建议，尚未确认</em>' : ""}</strong><p>${escapeHtml(option.summary)}</p><small><b>会带来什么影响：</b>${escapeHtml(option.impact)}</small></span></label>`).join("")}</fieldset>
                <label class="owa-review-rationale"><span>你的决定理由（采用建议方案时可留空）</span><textarea name="rationale" rows="3" placeholder="如选择另一方案，请用一句话说明原因，便于后续审计和复盘。"></textarea></label>
                ${engineeringReviewMessage ? `<p class="owa-review-message">${escapeHtml(engineeringReviewMessage)}</p>` : ""}
                <div class="owa-review-actions"><button type="button" data-engineering-action="conversation-review">去对话页讨论</button><button type="button" class="is-primary" data-engineering-action="submit-review" ${engineeringReviewInFlight ? "disabled" : ""}>${engineeringReviewInFlight ? "正在保存…" : "确认并保存本题"}</button></div>
              </form>
            </div>
          </aside>`
      : `<div class="owa-review-backdrop" data-engineering-action="close-review"></div><aside class="owa-review-drawer" role="dialog"><header><div><span>S3 人工评审</span><h2>所有问题已处理</h2><p>工程状态正在重新读取。</p></div><button type="button" data-engineering-action="close-review">关闭</button></header></aside>`;
    const reviewDrawer = engineeringReviewOpen === "APPROVED_READ_ONLY"
      ? renderApprovedS4Review(competencyQuestionReview)
      : engineeringReviewOpen
      ? blocking?.type === "COMPETENCY_QUESTION_REVIEW"
        ? s4ReviewDrawer
        : s3ReviewDrawer
      : "";
    const managedProjectName = displayOntologyName(
      engineeringActionDialog?.projectName ?? project.project_name ?? projectId,
    );
    const actionDialogConfig = engineeringActionDialog ? ({
      "publish-release": {
        eyebrow: "发布审批",
        title: "确认发布本体工程包",
        description: "只有负责人在这里明确批准后才会生成正式版本；版本号和说明都会写入审计记录。",
        confirm: "明确批准并发布",
        placeholder: "说明本次版本包含的业务范围、质量结论和已知边界。",
        needsReleaseVersion: true,
      },
      "defer-release": {
        eyebrow: "发布决定",
        title: "确认暂不发布",
        description: "本次不会生成发布包。暂缓原因会写入审计记录，并生成一份中文总结报告。",
        confirm: "确认暂不发布",
        placeholder: "例如：业务范围仍需复核，本次先不发布。",
      },
      "resume-release": {
        eyebrow: "发布决定",
        title: "恢复发布评审",
        description: "恢复后仍需要负责人再次明确批准，系统不会因为恢复评审而自动发布。",
        confirm: "确认恢复评审",
        placeholder: "例如：前序问题已经核对完成，可以重新进入发布评审。",
      },
      "revoke-release": {
        eyebrow: "发布控制",
        title: `撤回版本 ${publication?.release_version ?? "当前版本"}`,
        description: "撤回不会删除原发布包，只会标记该版本不再推荐使用，并生成撤回总结报告。",
        confirm: "确认撤回该版本",
        placeholder: "例如：发现业务定义需要修正，应先撤回后再建立修订。",
        danger: true,
      },
      "revision-from-release": {
        eyebrow: "版本修订",
        title: "选择重新核对的起点",
        description: "系统会保留原发布版本，建立新的修订项目，并带你进入所选工作节点继续执行。",
        confirm: "创建修订并进入重跑",
        placeholder: "说明为什么需要重新修订，以及希望修正什么。",
        needsStage: true,
      },
      "reopen-stage": {
        eyebrow: "阶段调整",
        title: "选择需要重新核对的工作节点",
        description: "先从后端预览受影响的阶段、当前产物和发布状态；只有二次确认后才会回退。",
        confirm: "预览回退影响",
        placeholder: "说明本次需要重新核对的原因和预期变化。",
        needsStage: true,
      },
      "archive-project": {
        eyebrow: "工程管理",
        title: `将“${managedProjectName}”移入回收站`,
        description: "不会删除任何文件、报告、发布包或执行记录。工程只会从使用中列表移到回收站，并可随时恢复。",
        confirm: "确认移入回收站",
        placeholder: "例如：当前工程暂时停止，后续需要时再恢复。",
      },
      "restore-project": {
        eyebrow: "工程管理",
        title: `恢复“${managedProjectName}”`,
        description: "恢复后会回到归档前保存的阶段和状态，原执行记录保持不变。",
        confirm: "确认恢复工程",
        placeholder: "例如：资料已补齐，继续完成当前本体工程。",
      },
    })[engineeringActionDialog.type] : null;
    const defaultActionStage = engineeringActionDialog?.stage
      ?? (engineeringActionDialog?.type === "revision-from-release" ? "S3" : activeStage === "S7" ? "S6" : activeStage);
    const rollbackPreview = engineeringActionDialog?.type === "reopen-stage"
      ? engineeringActionDialog.preview
      : null;
    const actionStageOptions = actionDialogConfig?.needsStage
      ? stageDefinitions.filter(([id]) => id !== "S7" && !(!lifecycleV2 && intakeMode === "DOCUMENT_ONLY" && id === "S1")).map(([id, name, description]) => `<label class="owa-action-stage-option"><input type="radio" name="action-stage" value="${id}" ${id === defaultActionStage ? "checked" : ""} ${rollbackPreview ? "disabled" : ""}><span><strong>${id} ${escapeHtml(name)}</strong><small>${escapeHtml(description)}</small></span></label>`).join("")
      : "";
    const rollbackImpact = rollbackPreview ? `<section class="owa-rollback-impact" aria-label="回退影响预览">
      <header><div><span>后端影响预览</span><strong>回退到 ${escapeHtml(rollbackPreview.target_stage)}</strong></div><em>有效至 ${escapeHtml(String(rollbackPreview.expires_at ?? "").replace("T", " ").slice(0, 19))}</em></header>
      <div class="owa-rollback-impact-grid"><article><span>受影响下游</span><strong>${escapeHtml((rollbackPreview.affected_downstream ?? []).filter((item) => item.disposition === "INVALIDATED").length)}</strong><small>${escapeHtml((rollbackPreview.affected_downstream ?? []).map((item) => `${item.stage}：${item.disposition === "INVALIDATED" ? "现结果失效" : "继续等待上游"}`).join("；") || "无下游阶段")}</small></article><article><span>当前产物</span><strong>${escapeHtml(rollbackPreview.artifact_impact?.current_count ?? 0)}</strong><small>旧产物保留到 revision 历史，但不再作为当前结果</small></article><article><span>工程 revision</span><strong>${escapeHtml(rollbackPreview.project_revision)}</strong><small>${escapeHtml(rollbackPreview.version_impact?.active_revision_id ? `当前调整 ${rollbackPreview.version_impact.active_revision_id}` : "将新建留痕调整")}</small></article></div>
      <p class="${rollbackPreview.release_impact?.published ? "is-danger" : ""}">${escapeHtml(rollbackPreview.release_impact?.message ?? "确认后受影响下游进入待重跑状态。")}</p>
      ${rollbackPreview.commit_allowed ? '<label class="owa-intake-confirm"><input type="checkbox" name="rollback-impact-confirmed"><span><strong>我已核对上述影响，确认执行回退</strong><small>提交后旧产物仅作为历史保留，目标阶段必须重新执行。</small></span></label>' : ""}
    </section>` : "";
    const actionDialog = actionDialogConfig ? `<div class="owa-review-backdrop" data-engineering-action="close-action-dialog" aria-hidden="true"></div><section class="owa-action-dialog" role="dialog" aria-modal="true" aria-label="${escapeHtml(actionDialogConfig.title)}">
      <header><div><span>${escapeHtml(actionDialogConfig.eyebrow)}</span><h2>${escapeHtml(actionDialogConfig.title)}</h2><p>${escapeHtml(actionDialogConfig.description)}</p></div><button type="button" data-engineering-action="close-action-dialog" aria-label="关闭操作窗口">关闭</button></header>
      <form class="owa-action-dialog-form" data-action-dialog-type="${escapeHtml(engineeringActionDialog.type)}">
        ${actionStageOptions ? `<fieldset><legend>从哪个工作节点开始重新核对</legend><div class="owa-action-stage-grid">${actionStageOptions}</div></fieldset>` : ""}
        ${actionDialogConfig.needsReleaseVersion ? `<label class="owa-action-reason"><span>正式版本号</span><input name="action-release-version" value="${escapeHtml(engineeringActionDialog.releaseVersion ?? "1.0.0")}" placeholder="例如：1.0.0"><small>必须使用语义化版本号；已发布版本不能被覆盖。</small></label>` : ""}
        <label class="owa-action-reason"><span>请说明原因</span><textarea name="action-reason" rows="4" placeholder="${escapeHtml(actionDialogConfig.placeholder)}">${escapeHtml(engineeringActionDialog.reason ?? "")}</textarea><small>请按实际情况填写；原因会进入执行记录和对应的中文总结报告。</small></label>
        ${rollbackImpact}
        ${engineeringActionDialog.message ? `<p class="owa-review-message">${escapeHtml(engineeringActionDialog.message)}</p>` : ""}
        <div class="owa-review-actions"><button type="button" data-engineering-action="close-action-dialog">取消</button><button type="submit" class="is-primary${actionDialogConfig.danger ? " is-danger" : ""}" data-engineering-action="submit-action-dialog" ${engineeringReviewInFlight || (rollbackPreview && !rollbackPreview.commit_allowed) ? "disabled" : ""}>${engineeringReviewInFlight ? "正在保存…" : rollbackPreview ? "二次确认并执行回退" : escapeHtml(actionDialogConfig.confirm)}</button></div>
      </form>
    </section>` : "";
    const documentJob = engineeringDocumentJob;
    const documentIntakeMode = documentJob?.intake_mode ?? engineeringDocumentDraft?.intakeMode ?? "DOCUMENT_ONLY";
    const documentIntakeDefinition = intakeModeDefinitions.find((item) => item.id === documentIntakeMode)
      ?? intakeModeDefinitions[0];
    const documentExistingProject = Boolean(documentJob?.project_id ?? engineeringDocumentDraft?.projectId);
    const documentFilePreview = engineeringDocumentFiles.slice(0, 5).map((file) => `<li><span>${escapeHtml(file.webkitRelativePath || file.name)}</span><small>${escapeHtml(Math.max(1, Math.ceil(Number(file.size || 0) / 1024)))} KB</small></li>`).join("");
    const documentSkippedNames = engineeringDocumentSkippedFiles.slice(0, 3).map((item) => item.path || item.name).filter(Boolean);
    const documentUploadArea = `<section class="owa-document-continue-upload" aria-label="补充当前工程资料">
      <header><div><strong>${engineeringDocumentFiles.length ? `已选择 ${engineeringDocumentFiles.length} 份资料` : "添加需要处理的资料"}</strong><p>支持单个或多个文件，也可以重复添加多个文件夹。</p></div>${engineeringDocumentFiles.length || engineeringDocumentSkippedFiles.length ? '<button type="button" data-engineering-action="clear-document-files">清空</button>' : ""}</header>
      <div class="owa-document-picker-actions"><label><span>添加文件</span><input type="file" name="document-files" multiple accept=".pdf,.docx,.xlsx,.xlsm,.csv,.tsv,.txt,.md,.html,.htm,.xml,.json,.yaml,.yml,.eml,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp"></label><label><span>添加文件夹</span><input type="file" name="document-folders" webkitdirectory directory multiple></label></div>
      ${documentFilePreview ? `<ul class="owa-document-selected-files">${documentFilePreview}${engineeringDocumentFiles.length > 5 ? `<li class="is-more">另有 ${engineeringDocumentFiles.length - 5} 份资料</li>` : ""}</ul>` : '<p class="owa-document-empty-files">还没有选择资料。</p>'}
      ${engineeringDocumentSkippedFiles.length ? `<p class="owa-document-skipped-files"><strong>已自动跳过 ${engineeringDocumentSkippedFiles.length} 项</strong><span>${escapeHtml(documentSkippedNames.join("、"))}${engineeringDocumentSkippedFiles.length > documentSkippedNames.length ? " 等" : ""}</span></p>` : ""}
    </section>`;
    const selectedCqMode = engineeringDocumentDraft?.cqMode ?? "USER_PLUS_AI";
    const documentProjectFields = documentExistingProject ? `<section class="owa-document-current-project"><span>当前工程</span><strong>${escapeHtml(engineeringDocumentDraft?.projectName ?? "")}</strong><small>${escapeHtml(engineeringDocumentDraft?.domain ?? "未填写业务领域")} · S0 资料整理</small></section>` : `<div class="owa-document-form-grid"><label><span>新工程名称</span><input name="document-project-name" value="${escapeHtml(engineeringDocumentDraft?.projectName ?? "")}" placeholder="例如：供应商制度资料本体工程"></label><label><span>业务领域</span><input name="document-domain" value="${escapeHtml(engineeringDocumentDraft?.domain ?? "")}" placeholder="例如：供应商管理"></label></div><label><span>建设目标</span><textarea name="document-rationale" rows="3" placeholder="说明资料来源和希望解决的问题。">${escapeHtml(engineeringDocumentDraft?.rationale ?? "")}</textarea></label>
      <fieldset class="owa-cq-intake"><legend>业务问题怎么确定</legend>
        <label><input type="radio" name="document-cq-mode" value="USER_PLUS_AI" ${selectedCqMode === "USER_PLUS_AI" ? "checked" : ""}><span><strong>我先提供，系统补充（推荐）</strong><small>保留你的验收题，再从资料与 Mapping 中补齐遗漏场景。</small></span></label>
        <label><input type="radio" name="document-cq-mode" value="USER_PROVIDED" ${selectedCqMode === "USER_PROVIDED" ? "checked" : ""}><span><strong>只采用我提供的问题</strong><small>系统只负责生成技术查询，不额外增加业务问题。</small></span></label>
        <label><input type="radio" name="document-cq-mode" value="AI_GENERATED" ${selectedCqMode === "AI_GENERATED" ? "checked" : ""}><span><strong>由系统从材料中生成</strong><small>适合暂时没有 CQ 的材料，S4 仍可人工修改。</small></span></label>
      </fieldset>
      <label><span>初始业务问题（无需写 SPARQL）</span><textarea name="document-cq-input" rows="4" placeholder="每行一个：业务问题｜怎样算回答正确">${escapeHtml(engineeringDocumentDraft?.cqInput ?? "")}</textarea><small>示例：哪些供应商正在影响关键订单？｜返回供应商、受影响订单和可追溯原因</small></label>`;
    const reuseContentCache = engineeringDocumentDraft?.reuseContentCache === true;
    const documentJobBody = documentJob ? `<div data-document-job-view>${engineeringDocumentJobMarkup()}</div>` : engineeringDocumentInFlight && engineeringDocumentDraft === null ? `<div class="owa-document-loading"><strong>正在读取最近资料批次</strong><span>正在回读任务状态、逐文件结果和复核报告…</span></div>` : `<form class="owa-document-router-form${documentExistingProject ? " is-existing-project" : ""}">
        <input type="hidden" name="document-source-mode" value="upload">
        ${documentProjectFields}
        ${documentUploadArea}
        <label class="owa-intake-confirm"><input type="checkbox" name="document-reuse-content-cache" ${reuseContentCache ? "checked" : ""}><span><strong>复用相同文件的历史解析缓存</strong><small>默认关闭；关闭时会重新执行 Word/PDF/Excel 的 S0 解析。原件内容寻址去重仍保留，但不会跳过本次解析。</small></span></label>
        <p class="owa-document-review-note">启动前会先统计表格规模；大 Excel/CSV 自动走 HYBRID + IMPORT，Word/PDF 保留在文档证据链路。</p>
        <p class="owa-review-message" role="status" data-document-start-status ${engineeringDocumentMessage ? "" : "hidden"}>${escapeHtml(engineeringDocumentMessage ?? "")}</p>
        <div class="owa-review-actions"><button type="button" data-engineering-action="close-document-router">返回工程</button><button type="button" class="is-primary" data-engineering-action="start-document-job" ${engineeringDocumentInFlight ? "disabled" : ""}>${engineeringDocumentInFlight ? "正在准备资料…" : "开始处理资料"}</button></div>
      </form>`;
    const documentDialog = engineeringDocumentDialogOpen ? `<div class="owa-review-backdrop" data-engineering-action="close-document-router" aria-hidden="true"></div><section class="owa-action-dialog owa-document-router-dialog" role="dialog" aria-modal="true" aria-label="S0 资料处理">
      <header><div><span>S0 资料整理</span><h2>${documentExistingProject ? "继续处理当前工程资料" : "新建资料工程"}</h2><p>${documentExistingProject ? "补充文件或文件夹，系统将继续自动解析、校验并推进当前工程。" : "添加资料并启动 S0；正常结果将自动继续。"}</p></div><button type="button" data-engineering-action="close-document-router" aria-label="关闭资料处理窗口">关闭</button></header>
      <div class="owa-document-router-body">${documentJobBody}</div>
    </section>` : "";
    const selectedStageChanged = panel.dataset.visibleStage !== activeStage;
    const releaseContractNotice = publication && releaseContract?.status === "LEGACY_UNVERIFIED"
      ? '<aside class="owa-project-governance-note" role="status"><i class="owa-engineering-icon is-warning" aria-hidden="true"></i><div><strong>该历史版本暂不支持直接交付</strong><p>此版本没有完整记录当前使用的业务问题验收信息。</p><small>如需继续使用，请在右侧“当前操作”创建新修订，并从 S4 重新核对。</small></div></aside>'
      : publication && releaseContract?.status === "PACKAGE_INTEGRITY_FAILED"
        ? `<aside class="owa-project-governance-note" role="status"><i class="owa-engineering-icon is-warning" aria-hidden="true"></i><div><strong>发布包完整性异常，暂不支持交付</strong><p>${escapeHtml(releaseContract.message ?? "请核对发布包文件并重新校验。")}</p></div></aside>`
      : "";
    const storageCurrent = ["SYNCED", "CONNECTED"].includes(storageStatus?.status)
      && !["MISSING", "STALE", "PROJECT_MISMATCH"].includes(storageStatus?.sync_status);
    const storageRevision = storageStatus?.target?.project_revision;
    const runtimeStateLabel = ({
      ONTOP_READY: "Ontop 已就绪",
      DOCUMENT_RUNTIME_READY: "文档运行时已就绪",
      NOT_APPLICABLE: "本版本无需运行时",
      DEPLOYMENT_QUEUED: "等待部署",
      DEPLOYING: "正在部署",
      RUNTIME_VERIFYING: "正在验证",
      WAITING_CONFIGURATION: "等待配置",
      DEPLOYMENT_FAILED: "部署失败",
      DOCUMENT_RUNTIME_FAILED: "文档运行时失败",
      AUTOMATION_FAILED_TO_QUEUE: "排队失败",
      AUTOMATION_DISABLED: "自动部署未启用",
    })[runtimeStatus?.state] ?? (publication ? "尚未回读" : "尚未发布");
    const truthNeedsAttention = !storageCurrent || (Boolean(publication) && !runtimeReady);
    const truthState = !storageCurrent || runtimeFailed ? "warning" : publication && !runtimeReady ? "pending" : "ready";
    const usesOntop = intakeMode === "DATABASE_ONLY" || intakeMode === "HYBRID";
    const usesFuseki = intakeMode === "DOCUMENT_ONLY" || intakeMode === "HYBRID";
    const ontopPackaged = publication?.realtime_query_capability === "PACKAGED_ARTIFACT_VERIFIED";
    const fusekiPackaged = publication?.document_runtime_capability === "CURRENT_VERSION_SEARCH_PACKAGED";
    const ontopReady = usesOntop && ontopPackaged && runtimeStatus?.state === "ONTOP_READY";
    const fusekiReady = usesFuseki && fusekiPackaged && runtimeAvailable;
    const currentDocumentCount = Number(runtimeStatus?.current_document_count ?? 0);
    const availableServiceCopy = usesOntop && usesFuseki
      ? "业务数据与文档证据均可用于问答查询。"
      : usesOntop
        ? "业务数据可以正常用于问答查询。"
        : usesFuseki
          ? "文档证据可以正常用于问答查询。"
          : "发布后的问答查询服务正常。";
    const truthTitle = !storageCurrent
      ? "后台同步尚未完成"
      : publication && runtimeFailed
        ? "发布包已生成，问答服务需要处理"
      : publication && runtimeAvailable
        ? `本体${releaseVersion ? ` v${releaseVersion}` : ""} 已发布，可正常使用`
        : publication && runtimeNotApplicable
          ? `本体${releaseVersion ? ` v${releaseVersion}` : ""} 已发布`
        : publication
          ? `本体${releaseVersion ? ` v${releaseVersion}` : ""} 已发布，问答服务正在准备`
          : "工程进度已保存";
    const truthDescription = !storageCurrent
      ? "当前工程记录与后台同步回执尚未对齐，请查看下方版本与同步详情。"
      : publication && runtimeAvailable
        ? `工程内容已保存，${availableServiceCopy}`
        : publication && runtimeNotApplicable
          ? "工程内容已保存；当前版本没有启用在线问答服务。"
        : publication
          ? "工程内容已保存；服务准备完成后即可用于问答查询。"
          : "当前修改已经保存；完成发布后，将显示正式版本和可用状态。";
    const storageDetail = storageRevision == null
      ? "尚无后台保存记录"
      : `后台第 ${escapeHtml(storageRevision)} 次 / 当前第 ${escapeHtml(state.revision ?? 0)} 次`;
    const runtimeCheckedAt = runtimeStatus?.updated_at || runtimeStatus?.verified_at;
    const serviceStatusLabel = (packaged, ready) => !publication
      ? "尚未发布"
      : !packaged
        ? "当前版本未启用"
        : ready
          ? "已就绪"
          : runtimeFailed
            ? "需要处理"
            : "正在准备";
    const serviceStatusClass = (packaged, ready) => ready
      ? "ready"
      : publication && packaged && runtimeFailed
        ? "warning"
        : "pending";
    const ontopDetail = usesOntop ? `<div class="is-${serviceStatusClass(ontopPackaged, ontopReady)}"><span>业务数据查询</span><strong>Ontop ${escapeHtml(serviceStatusLabel(ontopPackaged, ontopReady))}</strong><small>从数据库实时读取结构化业务数据</small></div>` : "";
    const fusekiDetail = usesFuseki ? `<div class="is-${serviceStatusClass(fusekiPackaged, fusekiReady)}"><span>文档证据查询</span><strong>Fuseki ${escapeHtml(serviceStatusLabel(fusekiPackaged, fusekiReady))}</strong><small>${fusekiReady && currentDocumentCount > 0 ? `已收录 ${escapeHtml(currentDocumentCount)} 份当前资料` : "保存文档正文、页码和来源关系"}</small></div>` : "";
    const truthStrip = `<section class="owa-engineering-truth-strip is-${truthState}" aria-label="当前可用状态" role="status" aria-live="polite">
      <div class="owa-engineering-truth-summary">
        <span>工程状态</span>
        <strong>${escapeHtml(truthTitle)}</strong>
        ${truthNeedsAttention ? `<small>${escapeHtml(truthDescription)}</small>` : ""}
      </div>
      <details class="owa-engineering-truth-disclosure" data-disclosure-attention="${truthNeedsAttention}" ${truthNeedsAttention ? "open" : ""}>
        <summary>查看技术详情</summary>
        ${!truthNeedsAttention ? `<p class="owa-engineering-truth-description">${escapeHtml(truthDescription)}</p>` : ""}
        <div class="owa-engineering-truth-details">
          <div><span>工程记录</span><strong>第 ${escapeHtml(state.revision ?? 0)} 次变更</strong><small>${escapeHtml(chinaDateTimeLabel(state.updated_at))}</small></div>
          <div class="is-${storageCurrent ? "ready" : "warning"}"><span>后台保存</span><strong>${storageCurrent ? "已保存" : "尚未完成"}</strong><small>${storageDetail}</small></div>
          ${ontopDetail}${fusekiDetail}
        </div>
        <small class="owa-engineering-truth-runtime-note">运行时回读：${escapeHtml(runtimeStateLabel)}${runtimeCheckedAt ? ` · ${escapeHtml(chinaDateTimeLabel(runtimeCheckedAt))}` : ""}</small>
      </details>
    </section>`;
    const stageWorkspace = `<main class="owa-engineering-workspace" aria-label="当前查看 ${escapeHtml(activeStage)} ${escapeHtml(definition[1])}">
          <header class="owa-engineering-hero">
            <div class="owa-engineering-breadcrumb"><span class="owa-engineering-project-name" title="${escapeHtml(displayOntologyName(project.project_name ?? projectId))}">${escapeHtml(displayOntologyName(project.project_name ?? projectId))}</span><i>/</i><strong>${escapeHtml(activeStage)} ${escapeHtml(definition[1])}</strong><span class="owa-intake-mode" title="建设来源由工程真实接入内容确定"><span>建设来源</span><strong>${escapeHtml(intakeModeLabel)}</strong></span></div>
            <div class="owa-engineering-title"><h1>${escapeHtml(activeStage)} ${escapeHtml(definition[1])}</h1><span class="is-${escapeHtml(stageStatus.toLowerCase())}"><i></i>${escapeHtml(stageStatusDisplay(activeStage, stageStatus, intakeMode, state))}</span>${activeStage === "S4" && (stageStatus === "PASSED" || competencyQuestionReview?.status === "APPROVED") ? '<button type="button" class="owa-approved-review-trigger" data-engineering-action="view-approved-s4" aria-label="查看已批准评审（只读）"><svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h5"/></svg><span>查看评审记录</span><small>只读</small></button>' : ""}</div>
            <div class="owa-engineering-stage-intro"><p>${escapeHtml(definition[2])}</p></div>
          </header>${truthStrip}${releaseContractNotice}${renderEngineeringContinuation(engineeringContinuations.get(projectId))}
          <nav class="owa-engineering-tabs" aria-label="工程视图">
            <button type="button" class="${engineeringSection === "summary" ? "is-active" : ""}" data-engineering-section="summary">阶段概览</button>
            <button type="button" class="${engineeringSection === "decisions" ? "is-active" : ""}" data-engineering-section="decisions">决策摘要 <span>${activeDecisions.length}</span></button>
            <button type="button" class="${engineeringSection === "activity" ? "is-active" : ""}" data-engineering-section="activity">执行记录 <span>${activeStageEvents.length}</span></button>
            <button type="button" class="${engineeringSection === "revisions" ? "is-active" : ""}" data-engineering-section="revisions">变更对比 <span>${revisions.length}</span></button>
          </nav>
          <div class="owa-engineering-content">${engineeringSection === "summary" ? `<div class="owa-stage-overview-layout">${sectionContent}<aside class="owa-stage-overview-context" aria-label="当前操作与当前产物"><section class="owa-next-step"><h2>${humanReviewRequired ? "需要你确认" : qualityNeedsAttention || runtimeFailed && activeStage === "S7" ? "需要处理" : "下一步"}</h2><p>${escapeHtml(nextStepCopy)}</p>${activeStage === "S7" ? window.__ORION_RELEASE_RUNTIME__.releaseBadges({ packagePublished, runtimeState: runtimeStatus?.state, revoked: Boolean(releaseRevocation) }) : ""}${engineeringStageNotice ? `<p class="owa-stage-notice">${escapeHtml(engineeringStageNotice)}</p>` : ""}${action}</section><section class="owa-related-artifacts"><details class="owa-stage-artifacts-disclosure owa-stage-disclosure" data-engineering-disclosure="artifacts" open><summary><strong>阶段产物</strong><span>${featuredArtifacts.length} 项产物</span></summary><div class="owa-stage-artifacts-body">${rightArtifacts}${stageReadme ? `<a class="owa-all-artifacts" href="${escapeHtml(artifactUrl(projectId, stageReadme.path))}" target="_blank" rel="noopener">查看阶段产物说明</a>` : ""}</div></details></section></aside></div>` : sectionContent}</div>
        </main>`;
    panel.innerHTML = `
      <div class="owa-engineering-shell${engineeringStageNavCollapsed ? " is-stage-nav-collapsed" : ""}${engineeringWorkspaceMode === "projects" ? " is-project-center" : " is-context-free"}">
        <aside class="owa-engineering-stage-rail" aria-label="本体工程导航">
          <button type="button" class="owa-project-center-entry${engineeringWorkspaceMode === "projects" ? " is-selected" : ""}" data-engineering-action="open-project-center" aria-pressed="${engineeringWorkspaceMode === "projects"}"><span class="owa-project-center-short" aria-hidden="true"><i class="owa-engineering-icon is-tools"></i></span><span class="owa-project-center-copy"><strong>工程中心</strong><small>${activeProjectGroups.length} 个当前工程</small></span></button>
          <header><span>已到达阶段</span></header>
          <nav>${stageRail}</nav>
          <button type="button" class="owa-stage-nav-toggle" data-engineering-action="toggle-nav" aria-expanded="${!engineeringStageNavCollapsed}" title="${engineeringStageNavCollapsed ? "展开阶段导航" : "收起阶段导航"}"><span>${engineeringStageNavCollapsed ? "展开" : "收起"}</span><strong>阶段导航</strong></button>
        </aside>
        ${engineeringWorkspaceMode === "projects" ? projectCenter : stageWorkspace}
      </div>${reviewDrawer}${actionDialog}${documentDialog}`;
    panel.dataset.projectId = projectId;
    panel.dataset.visibleStage = activeStage;
    const diagnosticsDisclosure = panel.querySelector('[data-engineering-disclosure="stage-diagnostics"]');
    let diagnosticsLoaded = false;
    const loadStageDiagnostics = () => {
      if (!diagnosticsDisclosure?.open || diagnosticsLoaded) return;
      diagnosticsLoaded = true;
      window.__ORION_BUSINESS_QUALITY__?.mount(
        diagnosticsDisclosure.querySelector("[data-business-quality]"), projectId, state.revision, false,
        window.__ORION_BUSINESS_QUALITY__.snapshotKey(engineeringData), activeStage,
      );
    };
    diagnosticsDisclosure?.addEventListener("toggle", loadStageDiagnostics);
    if (!s6Evidence.rows && gateArtifact) {
      hydrateStageGateReceipts(panel.querySelector("[data-stage-gate-receipts]"), projectId, activeStage, gateArtifact.path, state.revision, gateFallback);
    }
    restoreEngineeringUiState(panel, uiState);
    loadStageDiagnostics();
    window.__ORION_BUSINESS_PREVIEW__?.mount(panel.querySelector("[data-business-preview]"), projectId, state.revision);
    if (selectedStageChanged) {
      const selectedStageButton = panel.querySelector(".owa-engineering-stage.is-selected");
      const stageNavigation = selectedStageButton?.closest("nav");
      if (stageNavigation?.clientHeight) {
        const navigationRect = stageNavigation.getBoundingClientRect();
        const selectedRect = selectedStageButton.getBoundingClientRect();
        if (selectedRect.top < navigationRect.top) {
          stageNavigation.scrollTop -= navigationRect.top - selectedRect.top + 8;
        } else if (selectedRect.bottom > navigationRect.bottom) {
          stageNavigation.scrollTop += selectedRect.bottom - navigationRect.bottom + 8;
        }
      }
    }
    restoreEngineeringContextScroll(panel, projectId, activeStage);
    if (reviewOpenedAutomatically) {
      panel.querySelector('.owa-review-drawer [data-engineering-action="close-review"]')?.focus({ preventScroll: true });
    }
  };

  const setEngineeringViewOpen = (open, restoreTab = null) => {
    engineeringViewOpen = open;
    const root = engineeringRoot();
    const tabList = sessionTabList();
    const engineeringTab = tabList?.querySelector(".owa-engineering-tab");
    const engineeringPanel = root?.querySelector(":scope > .owa-engineering-view");
    root?.classList.toggle("owa-engineering-active", open);
    if (engineeringPanel) engineeringPanel.hidden = !open;
    engineeringTab?.classList.toggle("is-active", open);
    engineeringTab?.setAttribute("aria-selected", String(open));
    const nativeTabs = [...(tabList?.querySelectorAll('[role="tab"]') ?? [])].filter(
      (tab) => tab !== engineeringTab,
    );
    if (open) {
      const activeNative = nativeTabs.find((tab) => tab.getAttribute("aria-selected") === "true");
      if (activeNative) {
        lastNativeTabLabel = activeNative.textContent?.trim() || lastNativeTabLabel;
        nativeActiveTabClass = [...activeNative.classList].find((className) => className.includes("tabActive")) ?? nativeActiveTabClass;
      }
    }
    for (const tab of nativeTabs) {
      if (open) {
        tab.setAttribute("aria-selected", "false");
        for (const className of [...tab.classList]) {
          if (className.includes("tabActive")) tab.classList.remove(className);
        }
      }
    }
    if (!open) {
      const target = restoreTab ?? nativeTabs.find((tab) => tab.textContent?.trim() === lastNativeTabLabel) ?? nativeTabs[0];
      for (const tab of nativeTabs) {
        const selected = tab === target;
        tab.setAttribute("aria-selected", String(selected));
        if (selected && nativeActiveTabClass) tab.classList.add(nativeActiveTabClass);
        if (!selected && nativeActiveTabClass) tab.classList.remove(nativeActiveTabClass);
      }
    }
    if (open) {
      renderEngineeringPanel();
      ensureEngineeringData(true);
    }
  };

  const ensureEngineeringNavigation = () => {
    const tabList = sessionTabList();
    const root = engineeringRoot();
    if (!root) return;
    for (const nativeContent of [...root.children]) {
      if (nativeContent.matches(".owa-engineering-view, .owa-ontology-registry-view")) continue;
      nativeContent.classList.add("owa-native-session-content");
    }

    let panel = root.querySelector(":scope > .owa-engineering-view");
    if (!panel) {
      panel = document.createElement("section");
      panel.className = "owa-engineering-view";
      panel.setAttribute("aria-label", "AHS 本体工程控制台");
      root.appendChild(panel);
      renderEngineeringLoading(panel);
      const commitProjectSearch = (value, immediate = false) => {
        engineeringProjectSearch = value;
        engineeringProjectPage = 1;
        if (engineeringProjectSearchTimer !== null) {
          window.clearTimeout(engineeringProjectSearchTimer);
          engineeringProjectSearchTimer = null;
        }
        if (immediate) {
          renderEngineeringPanel();
          return;
        }
        engineeringProjectSearchTimer = window.setTimeout(() => {
          engineeringProjectSearchTimer = null;
          renderEngineeringPanel();
        }, 160);
      };
      panel.addEventListener("click", (event) => {
        const section = event.target.closest("[data-engineering-section]")?.dataset.engineeringSection;
        if (section) {
          engineeringSection = section;
          renderEngineeringPanel();
          return;
        }
        const stage = event.target.closest("[data-engineering-stage]")?.dataset.engineeringStage;
        if (stage) {
          engineeringWorkspaceMode = "stage";
          rememberEngineeringStage(stage);
          engineeringSection = "summary";
          engineeringHistoryExpanded = false;
          window.__ORION_SHELL_STATE__?.navigate({
            primary: "ontology",
            section: "engineering",
            projectId: engineeringData?.project?.project_id ?? engineeringSelectedProjectId,
            stage,
          });
          renderEngineeringPanel();
          return;
        }
        const actionControl = event.target.closest("[data-engineering-action]");
        const action = actionControl?.dataset.engineeringAction;
        if (action === "project-summary-filter") {
          const control = event.target.closest("[data-project-kind][data-project-status]");
          const requestedKind = control?.dataset.projectKind;
          const requestedStatus = control?.dataset.projectStatus;
          if (["BUSINESS", "ACCEPTANCE_TEST", "ALL"].includes(requestedKind) && ["ALL", "WAITING", "PUBLISHED"].includes(requestedStatus)) {
            engineeringProjectKindFilter = requestedKind;
            engineeringProjectStatusFilter = requestedStatus;
            engineeringProjectPage = 1;
            try {
              window.localStorage.setItem("orion.engineering.projectKindFilter", requestedKind);
              window.localStorage.setItem("orion.engineering.projectStatusFilter", requestedStatus);
            } catch (_error) {
              // 当前页面仍可筛选；本地存储不可用时只是不记住选择。
            }
            renderEngineeringPanel();
            window.requestAnimationFrame(() => {
              panel.querySelector("[data-project-list-anchor]")?.scrollIntoView({ block: "start" });
            });
          }
          return;
        }
        if (action === "toggle-project-family") {
          const familyId = actionControl?.dataset.projectFamilyId;
          if (!familyId) return;
          if (engineeringExpandedProjectFamilies.has(familyId)) {
            engineeringExpandedProjectFamilies.delete(familyId);
          } else {
            engineeringExpandedProjectFamilies.add(familyId);
          }
          renderEngineeringPanel();
          return;
        }
        if (action === "project-kind-filter") {
          const requestedKind = event.target.closest("[data-project-kind]")?.dataset.projectKind;
          if (["BUSINESS", "ACCEPTANCE_TEST", "ALL"].includes(requestedKind)) {
            engineeringProjectKindFilter = requestedKind;
            engineeringProjectStatusFilter = "ALL";
            engineeringProjectPage = 1;
            try {
              window.localStorage.setItem("orion.engineering.projectKindFilter", requestedKind);
              window.localStorage.setItem("orion.engineering.projectStatusFilter", "ALL");
            } catch (_error) {
              // 当前页面仍可筛选；本地存储不可用时只是不记住选择。
            }
            renderEngineeringPanel();
          }
          return;
        }
        if (action === "project-page") {
          const requestedPage = Number(event.target.closest("[data-project-page]")?.dataset.projectPage);
          if (Number.isInteger(requestedPage) && requestedPage > 0) engineeringProjectPage = requestedPage;
          renderEngineeringPanel();
          window.requestAnimationFrame(() => {
            panel.querySelector("[data-project-list-anchor]")?.scrollIntoView({ block: "start" });
          });
          return;
        }
        if (action === "document-page") {
          engineeringDocumentPage = Number(
            event.target.closest("[data-document-page]")?.dataset.documentPage,
          ) || 1;
          refreshEngineeringDocumentJobView();
          return;
        }
        if (action === "toggle-nav") {
          engineeringStageNavCollapsed = !engineeringStageNavCollapsed;
          try {
            window.localStorage.setItem("orion.engineering.stageNavCollapsed", String(engineeringStageNavCollapsed));
          } catch (_error) {
            // The control still works when storage is unavailable.
          }
          renderEngineeringPanel();
          return;
        }
        if (action === "open-project-center") {
          engineeringWorkspaceMode = "projects";
          engineeringSection = "summary";
          engineeringHistoryExpanded = false;
          engineeringStageNotice = null;
          window.__ORION_SHELL_STATE__?.navigate({
            primary: "ontology",
            section: "engineering",
            projectId: null,
            stage: null,
          });
          renderEngineeringPanel();
          return;
        }
        if (action === "open-document-router") {
          const currentProject = engineeringData?.project ?? {};
          const currentState = engineeringData?.state ?? {};
          const currentProjectId = currentProject.project_id ?? currentState.project_id ?? engineeringSelectedProjectId ?? "";
          engineeringDocumentReturnAction = action;
          engineeringDocumentPage = 1;
          engineeringDocumentDraft = {
            projectName: currentProject.project_name ?? currentState.project_name ?? "",
            domain: currentProject.domain ?? "",
            rationale: currentProject.intake_rationale ?? currentState.intake_rationale ?? "补充当前工程 S0 资料",
            intakeMode: currentProject.intake_mode ?? currentState.intake_mode ?? "DOCUMENT_ONLY",
            sourceMode: "upload",
            sourcePath: "",
            independent: true,
            projectId: currentProjectId,
            projectRequestId: currentProject.creation_request_id ?? currentState.creation_request_id ?? engineeringRequestId("engineering-document"),
          };
          engineeringDocumentJob = null;
          engineeringDocumentFiles = [];
          engineeringDocumentSkippedFiles = [];
          engineeringDocumentMessage = null;
          engineeringDocumentInFlight = false;
          engineeringDocumentDialogOpen = true;
          renderEngineeringPanel();
          return;
        }
        if (action === "resume-document-job") {
          engineeringDocumentReturnAction = action;
          engineeringDocumentPage = 1;
          engineeringDocumentDraft = null;
          engineeringDocumentJob = null;
          engineeringDocumentFiles = [];
          engineeringDocumentSkippedFiles = [];
          engineeringDocumentMessage = "正在读取最近的资料批次…";
          engineeringDocumentInFlight = true;
          engineeringDocumentDialogOpen = true;
          renderEngineeringPanel();
          const currentProjectId = engineeringData?.project?.project_id ?? engineeringData?.state?.project_id;
          documentApiRequest("GET", `/jobs?${new URLSearchParams({ limit: "1", project_id: currentProjectId ?? "" })}`)
            .then((payload) => {
              engineeringDocumentInFlight = false;
              const latest = payload.jobs?.[0] ?? null;
              if (!latest) {
                const currentProject = engineeringData?.project ?? {};
                const currentState = engineeringData?.state ?? {};
                engineeringDocumentDraft = {
                  projectName: currentProject.project_name ?? currentState.project_name ?? "",
                  domain: currentProject.domain ?? "",
                  rationale: currentProject.intake_rationale ?? currentState.intake_rationale ?? "补充当前工程 S0 资料",
                  intakeMode: currentProject.intake_mode ?? currentState.intake_mode ?? "DOCUMENT_ONLY",
                  sourceMode: "upload",
                  sourcePath: "",
                  independent: true,
                  projectId: currentProjectId,
                  projectRequestId: currentProject.creation_request_id ?? currentState.creation_request_id ?? engineeringRequestId("engineering-document"),
                };
                engineeringDocumentMessage = "当前工程还没有成功启动资料批次，请重新选择资料。";
                renderEngineeringPanel();
                return;
              }
              engineeringDocumentJob = latest;
              engineeringLatestDocumentJob = latest;
              engineeringDocumentMessage = null;
              renderEngineeringPanel();
              if (["QUEUED", "RUNNING"].includes(latest.status)) pollDocumentJob(latest.job_id);
            })
            .catch((error) => {
              engineeringDocumentInFlight = false;
              engineeringDocumentMessage = error instanceof Error ? error.message : "最近资料批次读取失败";
              renderEngineeringPanel();
            });
          return;
        }
        if (action === "reveal-document-results") {
          if (!engineeringDocumentJob?.job_id) return;
          documentApiRequest(
            "POST",
            `/jobs/${encodeURIComponent(engineeringDocumentJob.job_id)}/reveal`,
            {},
          ).then((result) => {
            engineeringDocumentMessage = `已在 Finder 中打开：${result.path}`;
            renderEngineeringDocumentState();
          }).catch((error) => {
            engineeringDocumentMessage = error instanceof Error ? error.message : "未能打开本地结果目录";
            renderEngineeringDocumentState();
          });
          return;
        }
        if (action === "copy-document-results-path") {
          const path = event.target.closest("[data-document-path]")?.dataset.documentPath ?? "";
          if (!path) return;
          if (!navigator.clipboard?.writeText) {
            engineeringDocumentMessage = `本地目录：${path}`;
            renderEngineeringDocumentState();
            return;
          }
          navigator.clipboard.writeText(path).then(() => {
            engineeringDocumentMessage = "本地结构化结果目录已复制。";
            renderEngineeringDocumentState();
          }).catch(() => {
            engineeringDocumentMessage = `本地目录：${path}`;
            renderEngineeringDocumentState();
          });
          return;
        }
        if (action === "close-document-router") {
          const returnAction = engineeringDocumentReturnAction;
          engineeringDocumentReturnAction = null;
          engineeringDocumentDialogOpen = false;
          engineeringDocumentPollToken += 1;
          engineeringDocumentInFlight = false;
          engineeringDocumentMessage = null;
          renderEngineeringPanel();
          if (returnAction) {
            window.requestAnimationFrame(() => {
              panel.querySelector(`[data-engineering-action="${returnAction}"]`)?.focus({ preventScroll: true });
            });
          }
          return;
        }
        if (action === "clear-document-files") {
          engineeringDocumentFiles = [];
          engineeringDocumentSkippedFiles = [];
          engineeringDocumentMessage = null;
          renderEngineeringPanel();
          return;
        }
        if (action === "start-document-job") {
          const form = event.target.closest(".owa-document-router-form");
          if (!form) return;
          const projectId = engineeringDocumentDraft?.projectId ?? "";
          const projectName = form.querySelector('[name="document-project-name"]')?.value.trim()
            || engineeringDocumentDraft?.projectName
            || "";
          const domain = form.querySelector('[name="document-domain"]')?.value.trim()
            || engineeringDocumentDraft?.domain
            || "";
          const rationale = form.querySelector('[name="document-rationale"]')?.value.trim()
            || engineeringDocumentDraft?.rationale
            || "";
          const cqMode = form.querySelector('[name="document-cq-mode"]:checked')?.value
            ?? engineeringDocumentDraft?.cqMode
            ?? "USER_PLUS_AI";
          const cqInput = form.querySelector('[name="document-cq-input"]')?.value.trim()
            ?? engineeringDocumentDraft?.cqInput
            ?? "";
          const malformedCqLines = [];
          const initialCompetencyQuestions = cqMode === "AI_GENERATED" ? [] : cqInput
            .split(/\r?\n/)
            .map((line) => line.trim())
            .filter(Boolean)
            .map((line, index) => {
              const separator = line.search(/[|｜]/);
              if (separator < 0) {
                malformedCqLines.push(index + 1);
                return null;
              }
              const question = line.slice(0, separator).trim();
              const expected = line.slice(separator + 1).trim();
              if (!question || !expected) malformedCqLines.push(index + 1);
              return question && expected ? { question, expected, priority: "MEDIUM" } : null;
            })
            .filter(Boolean);
          const sourceMode = form.querySelector('[name="document-source-mode"]:checked')?.value
            ?? form.querySelector('[name="document-source-mode"]')?.value
            ?? engineeringDocumentDraft?.sourceMode
            ?? "upload";
          const sourcePath = form.querySelector('[name="document-source-path"]')?.value.trim()
            ?? engineeringDocumentDraft?.sourcePath
            ?? "";
          const independent = true;
          const missing = [];
          if (!projectName) missing.push("请填写新工程名称");
          if (!domain) missing.push("请填写业务领域");
          if (!rationale) missing.push("请说明资料来源或处理目的");
          if (sourceMode === "local" && !sourcePath) missing.push("请填写白名单内相对路径");
          if (sourceMode === "upload" && engineeringDocumentFiles.length === 0) missing.push("请选择至少一份文件");
          if (malformedCqLines.length) missing.push(`业务问题第 ${malformedCqLines.join("、")} 行请使用“问题｜正确结果”格式`);
          if (cqMode === "USER_PROVIDED" && initialCompetencyQuestions.length === 0) missing.push("只采用人工问题时，请至少填写一个业务问题");
          const intakeMode = engineeringDocumentDraft?.intakeMode ?? "DOCUMENT_ONLY";
          const reuseContentCache = form.querySelector('[name="document-reuse-content-cache"]')?.checked === true;
          const projectRequestId = engineeringDocumentDraft?.projectRequestId
            ?? engineeringRequestId("engineering-create");
          engineeringDocumentDraft = { projectName, domain, rationale, intakeMode, cqMode, cqInput, sourceMode, sourcePath, independent, projectId, projectRequestId, reuseContentCache };
          if (missing.length) {
            engineeringDocumentMessage = missing.join("；");
            renderEngineeringPanel();
            return;
          }
          engineeringDocumentInFlight = true;
          engineeringDocumentPage = 1;
          engineeringDocumentMessage = sourceMode === "upload" ? "正在创建上传批次…" : "正在启动资料路由任务…";
          if (!refreshEngineeringDocumentStartState(engineeringDocumentMessage)) renderEngineeringPanel();
          startEngineeringDocumentJob({
            projectName,
            domain,
            rationale,
            intakeMode,
            cqMode,
            initialCompetencyQuestions,
            sourceMode,
            sourcePath,
            projectId,
            projectRequestId,
            reuseContentCache,
            files: engineeringDocumentFiles,
          }).catch((error) => {
            engineeringDocumentInFlight = false;
            engineeringDocumentMessage = error instanceof Error ? error.message : "资料批处理未能启动";
            renderEngineeringPanel();
          });
          return;
        }
        if (action === "retry-document-job") {
          if (!engineeringDocumentJob?.job_id) return;
          engineeringDocumentInFlight = true;
          engineeringDocumentMessage = "正在重新启动失败文件，不会重复处理成功文件…";
          renderEngineeringDocumentState();
          documentApiRequest("POST", `/jobs/${encodeURIComponent(engineeringDocumentJob.job_id)}/retry`, {})
            .then((job) => {
              engineeringDocumentJob = { ...engineeringDocumentJob, ...job };
              engineeringLatestDocumentJob = engineeringDocumentJob;
              engineeringDocumentMessage = null;
              engineeringDocumentInFlight = false;
              renderEngineeringDocumentState();
              pollDocumentJob(job.job_id);
            })
            .catch((error) => {
              engineeringDocumentInFlight = false;
              engineeringDocumentMessage = error instanceof Error ? error.message : "任务重试失败";
              renderEngineeringDocumentState();
            });
          return;
        }
        if (action === "cancel-document-job") {
          if (!engineeringDocumentJob?.job_id || engineeringDocumentInFlight) return;
          engineeringDocumentInFlight = true;
          engineeringDocumentMessage = "正在取消真实后台资料任务…";
          renderEngineeringDocumentState();
          documentApiRequest("POST", `/jobs/${encodeURIComponent(engineeringDocumentJob.job_id)}/cancel`, {})
            .then((result) => {
              engineeringDocumentInFlight = false;
              engineeringDocumentMessage = null;
              engineeringDocumentJob = result.job;
              engineeringLatestDocumentJob = result.job;
              renderEngineeringDocumentState();
            })
            .catch((error) => {
              engineeringDocumentInFlight = false;
              engineeringDocumentMessage = error instanceof Error ? error.message : "资料任务取消失败";
              renderEngineeringDocumentState();
            });
          return;
        }
        if (action === "commit-document-job") {
          const form = event.target.closest(".owa-document-commit-form");
          const rationaleField = form?.querySelector('[name="document-review-rationale"]');
          const rationaleError = form?.querySelector("#document-review-rationale-error");
          const rationale = rationaleField?.value.trim() ?? "";
          const acceptWarnings = Boolean(form?.querySelector('[name="document-accept-warnings"]')?.checked);
          const structuredDataAction = form?.querySelector('[name="document-structured-data-action"]:checked')?.value
            ?? "DOCUMENT_ONLY";
          if (!rationale) {
            engineeringDocumentMessage = "请填写人工复核结论。";
            rationaleField?.setAttribute("aria-invalid", "true");
            if (rationaleError) rationaleError.hidden = false;
            rationaleField?.focus({ preventScroll: false });
            rationaleField?.scrollIntoView({ block: "center", behavior: "smooth" });
            return;
          }
          rationaleField?.removeAttribute("aria-invalid");
          if (rationaleError) rationaleError.hidden = true;
          commitEngineeringDocumentJob({ rationale, acceptWarnings, structuredDataAction }).catch(() => {
            // 提交函数已经保留错误并刷新当前资料状态。
          });
          return;
        }
        if (action === "open-new-project") {
          window.__ORION_CHAT_MODES__?.openNewSession?.();
          return;
        }
        if (action === "recover-project-session") {
          openProjectWorkSession({ freshSession: true, intent: "diagnose",
            detail: "请根据平台回读的当前失败回执和恢复建议继续修订与验证。仅执行已有授权范围；需要业务决定时展示依据与选项。" });
          return;
        }
        if (action === "open-stage-session-direct") {
          const currentStage = engineeringWorkStage(engineeringData);
          const currentStatus = engineeringData?.state?.stage_statuses?.[currentStage]
            ?? engineeringData?.project?.stage_statuses?.[currentStage];
          openProjectWorkSession({
            intent: currentStatus === "FAILED" ? "diagnose" : "continue",
            detail: currentStatus === "FAILED"
              ? "读取最近失败事件，说明原因、影响和可执行的重试方案。"
              : "直接执行当前阶段并把真实进度、产物和下一项有效操作回写工程页面。",
          });
          return;
        }
        if (action === "open-project" || action === "open-project-stage") {
          const control = event.target.closest("[data-project-id]");
          const targetProjectId = control?.dataset.projectId;
          const targetStage = control?.dataset.projectStage ?? null;
          if (!targetProjectId) return;
          const currentProjectId = engineeringData?.project?.project_id ?? panel.dataset.projectId;
          rememberEngineeringProject(targetProjectId);
          engineeringWorkspaceMode = "stage";
          rememberEngineeringStage(targetStage);
          engineeringSection = "summary";
          engineeringHistoryExpanded = false;
          engineeringStageNotice = null;
          window.__ORION_SHELL_STATE__?.navigate({
            primary: "ontology",
            section: "engineering",
            projectId: targetProjectId,
            stage: targetStage,
          });
          if (targetProjectId === currentProjectId) {
            if (!selectedEngineeringStage) rememberEngineeringStage(defaultEngineeringStage(engineeringData));
            renderEngineeringPanel();
            return;
          }
          engineeringData = null;
          engineeringDataFingerprint = null;
          engineeringDataEtag = null;
          engineeringError = null;
          engineeringLastFetchAt = 0;
          renderEngineeringPanel();
          ensureEngineeringData(true);
          return;
        }
        if (action === "archive-project-target" || action === "restore-project-target") {
          const control = event.target.closest("[data-project-id]");
          engineeringActionDialog = {
            type: action === "archive-project-target" ? "archive-project" : "restore-project",
            projectId: control?.dataset.projectId,
            projectName: control?.dataset.projectName,
            projectRevision: Number(control?.dataset.projectRevision ?? 0),
            fromProjectCenter: true,
          };
          engineeringReviewMessage = null;
          renderEngineeringPanel();
          return;
        }
        if (action === "switch-project") {
          const projectId = event.target.closest("[data-project-id]")?.dataset.projectId;
          if (!projectId || projectId === engineeringSelectedProjectId) return;
          rememberEngineeringProject(projectId);
          engineeringData = null;
          engineeringDataFingerprint = null;
          engineeringDataEtag = null;
          engineeringError = null;
          rememberEngineeringStage(null);
          engineeringSection = "summary";
          engineeringStageNotice = null;
          engineeringLastFetchAt = 0;
          window.__ORION_SHELL_STATE__?.navigate({
            primary: "ontology",
            section: "engineering",
            projectId,
            stage: null,
          });
          renderEngineeringPanel();
          ensureEngineeringData(true);
          return;
        }
        if (action === "toggle-history") {
          engineeringHistoryExpanded = !engineeringHistoryExpanded;
          renderEngineeringPanel();
          return;
        }
        if (action === "next-stage") {
          const nextStage = event.target.closest("[data-next-stage]")?.dataset.nextStage;
          if (nextStage) {
            rememberEngineeringStage(nextStage);
            engineeringSection = "summary";
            engineeringHistoryExpanded = false;
            window.__ORION_SHELL_STATE__?.navigate({
              primary: "ontology",
              section: "engineering",
              projectId: engineeringData?.project?.project_id ?? engineeringSelectedProjectId,
              stage: nextStage,
            });
            renderEngineeringPanel();
          }
          return;
        }
        if (action === "refresh" || action === "retry") {
          engineeringLastFetchAt = 0;
          ensureEngineeringData(true);
        }
        if (action === "retry-runtime-deployment") {
          submitEngineeringAction(window.__ORION_RELEASE_RUNTIME__.retryTool, { project_id: panel.dataset.projectId }).catch(() => {});
          return;
        }
        if (action === "retry-continuation") {
          retryEngineeringContinuation(panel.dataset.projectId);
          return;
        }
        if (action === "view-approved-s4") {
          if (panel.dataset.visibleStage !== "S4") return;
          engineeringReviewOpen = "APPROVED_READ_ONLY";
          engineeringReviewMessage = null;
          renderEngineeringPanel();
          return;
        }
        if (action === "gate") {
          const reviewStage = engineeringWorkStage(engineeringData);
          if (!reviewStage || !engineeringData?.state?.blocking) return;
          rememberEngineeringStage(reviewStage);
          engineeringWorkspaceMode = "stage";
          engineeringSection = "summary";
          window.__ORION_SHELL_STATE__?.navigate({
            primary: "ontology", section: "engineering",
            projectId: engineeringData?.project?.project_id ?? engineeringData?.state?.project_id,
            stage: reviewStage,
          });
          engineeringReviewOpen = true;
          engineeringReviewConfirmationId = engineeringData?.state?.blocking?.confirmation_id ?? null;
          engineeringReviewMessage = null;
          renderEngineeringPanel();
          return;
        }
        if (action === "close-review") {
          closeEngineeringReview();
          renderEngineeringPanel();
          return;
        }
        if (action === "add-cq") {
          const list = event.target.closest(".owa-cq-review-form")?.querySelector(".owa-cq-list");
          if (!list) return;
          const item = document.createElement("article");
          item.className = "owa-cq-item";
          item.innerHTML = `<div><strong>新增业务问题</strong><button type="button" data-engineering-action="remove-cq">移除此题</button></div>
            <input type="hidden" name="cq-id" value="CQ-NEW-${Date.now()}">
            <label><span>业务人员要问什么</span><textarea name="cq-question" rows="2" placeholder="例如：哪些供应商正在影响关键订单？"></textarea></label>
            <label><span>怎样算回答正确</span><textarea name="cq-expected" rows="2" placeholder="说明应返回哪些对象和判断依据"></textarea></label>
            <details><summary>技术查询语句</summary><p>保存时由系统依据正式 Mapping 自动生成，无需人工填写。</p></details>`;
          list.appendChild(item);
          item.querySelector("textarea")?.focus();
          return;
        }
        if (action === "remove-cq") {
          const item = event.target.closest(".owa-cq-item");
          const list = item?.parentElement;
          if (!item || !list) return;
          if (list.querySelectorAll(".owa-cq-item").length <= 1) {
            engineeringReviewMessage = "至少保留一个业务问题。";
            renderEngineeringPanel();
            return;
          }
          item.remove();
          return;
        }
        if (action === "submit-cq-review" || action === "return-cq-review") {
          const form = event.target.closest(".owa-cq-review-form");
          if (!form) return;
          if (action === "submit-cq-review" && usesLifecycleV2(engineeringData)
            && (engineeringData?.competencyQuestionReview?.review_scope !== "JOINT_DESIGN"
              || !engineeringData?.competencyQuestionReview?.joint_design_fingerprint)) {
            engineeringReviewMessage = "当前缺少完整联合设计及其版本凭证，请回到工作台重新生成后确认。";
            renderEngineeringPanel();
            return;
          }
          const questions = [...form.querySelectorAll(".owa-cq-item")].map((item, index) => ({
            id: item.querySelector('[name="cq-id"]')?.value || `CQ-${String(index + 1).padStart(3, "0")}`,
            question: item.querySelector('[name="cq-question"]')?.value?.trim() ?? "",
            expected: item.querySelector('[name="cq-expected"]')?.value?.trim() ?? "",
          }));
          const rationaleField = form.querySelector('textarea[name="rationale"]');
          const rationale = rationaleField?.value?.trim() ?? "";
          showEngineeringReviewFieldError(form, null, "");
          if (!rationale) {
            showEngineeringReviewFieldError(form, rationaleField, "尚未提交：请填写本次确认或退回的理由（必填）。");
            return;
          }
          if (action === "submit-cq-review" && questions.some((item) => !item.question || !item.expected)) {
            const missingField = [...form.querySelectorAll('[name="cq-question"], [name="cq-expected"]')]
              .find((field) => !field.value.trim());
            showEngineeringReviewFieldError(form, missingField, "尚未提交：请补全此业务问题及正确结果；技术查询由系统生成。");
            return;
          }
          submitEngineeringAction("resolve_competency_question_review", {
            project_id: engineeringData?.project?.project_id ?? panel.dataset.projectId,
            questions,
            decision: action === "submit-cq-review" ? "APPROVED" : "RETURN_TO_S3",
            rationale,
            expected_revision: Number(engineeringData?.state?.revision ?? 0),
          }).catch(() => {});
          return;
        }
        if (action === "submit-review") {
          const form = event.target.closest(".owa-review-form");
          const option = form?.querySelector('input[name="s3-option"]:checked')?.value;
          let rationale = form?.querySelector('textarea[name="rationale"]')?.value?.trim() ?? "";
          const confirmation = (engineeringData?.confirmations ?? []).find(
            (item) => item.id === form?.dataset.confirmationId,
          );
          const selectedOption = (confirmation?.options ?? []).find((item) => item.id === option);
          if (!rationale && selectedOption?.recommended) {
            rationale = `根据已展示的数据库事实，采用建议方案：${selectedOption.label}。`;
          }
          if (!option || !rationale) {
            engineeringReviewMessage = "选择非建议方案时，请说明理由。";
            renderEngineeringPanel();
            return;
          }
          engineeringReviewInFlight = true;
          engineeringReviewMessage = null;
          renderEngineeringPanel();
          postWorkflowConfirmation({
            project_id: engineeringData?.project?.project_id ?? panel.dataset.projectId,
            confirmation_id: form.dataset.confirmationId,
            selected_option_id: option,
            rationale,
            expected_revision: Number(engineeringData?.state?.revision ?? 0),
          }).then((result) => {
            engineeringReviewInFlight = false;
            engineeringContinuations.set(result.continuation);
            engineeringData = result.dashboard ?? engineeringData;
            if (result.dashboard) engineeringDataEtag = null;
            engineeringLastFetchAt = Date.now();
            const remaining = (engineeringData?.confirmations ?? []).filter((item) => item.status !== "RESOLVED");
            engineeringReviewConfirmationId = remaining[0]?.id ?? null;
            engineeringReviewOpen = remaining.length > 0;
            engineeringReviewMessage = remaining.length > 0 ? "上一题已保存，继续处理下一题。" : null;
            renderEngineeringPanel();
          }).catch((error) => {
            engineeringReviewInFlight = false;
            engineeringReviewMessage = error instanceof Error ? error.message : "本题决定保存失败";
            renderEngineeringPanel();
          });
          return;
        }
        if (["publish-release", "defer-release", "resume-release", "revoke-release", "revision-from-release", "reopen-stage", "archive-project", "restore-project"].includes(action)) {
          engineeringActionDialog = {
            type: action,
            requestId: engineeringRequestId("engineering-action"),
            stage: action === "revision-from-release"
              ? actionControl?.dataset.revisionStage ?? "S3"
              : selectedEngineeringStage === "S7" ? "S6" : selectedEngineeringStage,
          };
          engineeringReviewMessage = null;
          renderEngineeringPanel();
          return;
        }
        if (action === "close-action-dialog") {
          engineeringActionDialog = null;
          renderEngineeringPanel();
          return;
        }
        if (action === "submit-action-dialog") {
          event.preventDefault();
          const form = event.target.closest(".owa-action-dialog-form");
          const dialogType = form?.dataset.actionDialogType;
          const reason = form?.querySelector('textarea[name="action-reason"]')?.value?.trim() ?? "";
          const releaseVersion = form?.querySelector('[name="action-release-version"]')?.value?.trim() ?? "";
          const targetStage = form?.querySelector('input[name="action-stage"]:checked')?.value;
          const version = engineeringData?.publication?.release_version
            ?? engineeringData?.releaseRevocation?.release_version;
          if (!reason) {
            engineeringActionDialog = { ...engineeringActionDialog, reason, message: "请说明原因。" };
            renderEngineeringPanel();
            return;
          }
          if (["revision-from-release", "reopen-stage"].includes(dialogType) && !targetStage) {
            engineeringActionDialog = { ...engineeringActionDialog, reason, message: "请选择一个需要重新核对的工作节点。" };
            renderEngineeringPanel();
            return;
          }
          if (["revoke-release", "revision-from-release"].includes(dialogType) && !version) {
            engineeringActionDialog = { ...engineeringActionDialog, reason, message: "当前没有可用于本次操作的已发布版本。" };
            renderEngineeringPanel();
            return;
          }
          const project_id = engineeringActionDialog?.projectId
            ?? engineeringData?.project?.project_id
            ?? panel.dataset.projectId;
          if (dialogType === "reopen-stage" && !engineeringActionDialog?.preview) {
            engineeringReviewInFlight = true;
            engineeringActionDialog = {
              ...engineeringActionDialog,
              stage: targetStage,
              reason,
              message: "正在从后端计算下游、产物、版本和发布影响…",
            };
            renderEngineeringPanel();
            postWorkflowAction("preview_stage_rollback", {
              project_id,
              target_stage: targetStage,
            }).then((result) => {
              engineeringReviewInFlight = false;
              engineeringActionDialog = {
                ...engineeringActionDialog,
                stage: targetStage,
                reason,
                preview: result.workflow,
                message: null,
              };
              renderEngineeringPanel();
            }).catch((error) => {
              engineeringReviewInFlight = false;
              engineeringActionDialog = {
                ...engineeringActionDialog,
                stage: targetStage,
                reason,
                preview: null,
                message: error instanceof Error ? error.message : "回退影响预览失败。",
              };
              renderEngineeringPanel();
            });
            return;
          }
          if (
            dialogType === "reopen-stage"
            && engineeringActionDialog?.preview
            && !form?.querySelector('[name="rollback-impact-confirmed"]')?.checked
          ) {
            engineeringActionDialog = {
              ...engineeringActionDialog,
              reason,
              message: "请先核对影响清单并勾选二次确认。",
            };
            renderEngineeringPanel();
            return;
          }
          const expectedRevision = engineeringActionDialog?.projectRevision
            ?? engineeringData?.state?.revision
            ?? 0;
          const requests = {
            "publish-release": ["publish_ontology_package", { project_id, release_version: releaseVersion, approval_decision: "APPROVED", release_notes: reason, expected_revision: expectedRevision }],
            "defer-release": ["defer_ontology_publication", { project_id, reason }],
            "resume-release": ["resume_ontology_publication", { project_id, reason }],
            "revoke-release": ["revoke_ontology_release", { project_id, release_version: version, reason, expected_revision: expectedRevision }],
            "revision-from-release": ["create_revision_from_release", { project_id, release_version: version, target_stage: targetStage, reason, expected_revision: expectedRevision, request_id: engineeringActionDialog?.requestId }],
            "reopen-stage": ["reopen_stage_for_correction", {
              project_id,
              stage: targetStage,
              reason,
              preview_token: engineeringActionDialog?.preview?.preview_token,
              project_revision: engineeringActionDialog?.preview?.project_revision,
            }],
            "archive-project": ["archive_ontology_project", { project_id, reason, expected_revision: expectedRevision }],
            "restore-project": ["restore_ontology_project", { project_id, reason, expected_revision: expectedRevision }],
          };
          const request = requests[dialogType];
          if (!request) return;
          const stayInProjectCenter = engineeringActionDialog?.fromProjectCenter === true;
          submitEngineeringAction(request[0], request[1], { stayInProjectCenter }).then(() => {
            engineeringActionDialog = null;
            renderEngineeringPanel();
          }).catch((error) => {
            engineeringActionDialog = {
              ...engineeringActionDialog,
              reason,
              message: error instanceof Error ? error.message : "操作保存失败，请重新检查后再试。",
            };
            renderEngineeringPanel();
          });
          return;
        }
        if (action === "open-release-qa") {
          // S7 completion hands off to the release-bound QA entry; the server
          // re-validates publication identity before binding the new session.
          window.__ORION_CHAT_MODES__?.openOntology?.({
            sourceProjectId: engineeringData?.project?.project_id ?? panel.dataset.projectId,
            name: actionControl?.dataset.releaseName || panel.dataset.projectId,
            version: actionControl?.dataset.releaseVersion || "正式发布",
          });
          return;
        }
        if (action === "conversation-review" || action === "conversation") {
          const confirmationId = engineeringData?.state?.blocking?.confirmation_id ?? engineeringReviewConfirmationId ?? "当前问题";
          engineeringReviewOpen = false;
          const diagnosticStage = actionControl?.dataset.diagnosticStage;
          const detail = ["S3", "S6"].includes(diagnosticStage)
            ? `回读本工程当前阶段、草稿和 get_business_quality 的当前有效产物核对结果，聚焦 ${diagnosticStage === "S3" ? "语义、来源依据和业务问题缺口" : "实例与映射验收问题"}。先区分已经解决的历史记录、可修订的技术问题、缺少的资料与必须由我决定的业务口径。沿现有修订和阶段校验流程继续处理，保留已完成工作；若需重开已通过阶段或人工确认，先说明影响并等待批准。不要把未核验项当作失败，也不要为补齐资料编造事实。`
            : action === "conversation-review"
            ? engineeringData?.state?.blocking?.type === "COMPETENCY_QUESTION_REVIEW"
              ? "逐条解释这些业务问题解决什么需求，允许我修改、增加或删除，再由我确认；不要替我自动提交。"
              : `处理人工语义评审问题 ${confirmationId}，展示来源事实、两个方案的影响和建议理由，再让我选择；不要替我自动提交。`
            : engineeringWorkStage(engineeringData) === "S2"
              ? "说明 S2 正在依据哪些 S0/S1 证据识别业务对象、属性、关系和规则候选，并回读真实进度、产物和风险；不要再次询问我是否开始。"
              : "解释当前状态、剩余风险和下一项有效操作；任何人工门禁都要停下等待我确认。";
          openProjectWorkSession({
            intent: action === "conversation-review"
              ? "review"
              : engineeringWorkStage(engineeringData) === "S7" ? "publish" : "discuss",
            detail,
          });
          return;
        }
      });
      panel.addEventListener("input", (event) => {
        if (!event.target.matches('input[name="project-search"]')) return;
        engineeringProjectSearch = event.target.value;
        if (event.isComposing || engineeringProjectSearchComposing) return;
        commitProjectSearch(event.target.value);
      });
      panel.addEventListener("compositionstart", (event) => {
        if (!event.target.matches('input[name="project-search"]')) return;
        engineeringProjectSearchComposing = true;
      });
      panel.addEventListener("compositionend", (event) => {
        if (!event.target.matches('input[name="project-search"]')) return;
        engineeringProjectSearchComposing = false;
        commitProjectSearch(event.target.value, true);
      });
      panel.addEventListener("change", (event) => {
        if (event.target.matches('input[name="document-files"], input[name="document-folders"]')) {
          const policy = window.__ORION_DOCUMENT_FILE_POLICY__;
          const incomingFiles = [...(event.target.files ?? [])];
          if (policy?.merge) {
            const merged = policy.merge(engineeringDocumentFiles, incomingFiles, engineeringDocumentSkippedFiles);
            engineeringDocumentFiles = merged.files;
            engineeringDocumentSkippedFiles = merged.skipped;
          } else {
            engineeringDocumentFiles = [...engineeringDocumentFiles, ...incomingFiles];
          }
          engineeringDocumentMessage = null;
          renderEngineeringPanel();
        }
      });
    }

    let engineeringTab = tabList?.querySelector(":scope > .owa-engineering-tab");
    if (tabList && !engineeringTab) {
      const template = [...tabList.querySelectorAll(':scope > [role="tab"]')][0];
      engineeringTab = document.createElement("button");
      engineeringTab.type = "button";
      engineeringTab.role = "tab";
      engineeringTab.className = `${template?.className ?? ""} owa-engineering-tab`.trim();
      for (const className of [...engineeringTab.classList]) {
        if (className.includes("tabActive")) engineeringTab.classList.remove(className);
      }
      engineeringTab.textContent = "工程";
      engineeringTab.setAttribute("aria-selected", "false");
      engineeringTab.addEventListener("click", () => setEngineeringViewOpen(true));
      tabList.appendChild(engineeringTab);
    }
    for (const tab of tabList?.querySelectorAll(':scope > [role="tab"]') ?? []) {
      if (tab === engineeringTab || tab.dataset.owaEngineeringBound === "true") continue;
      tab.dataset.owaEngineeringBound = "true";
      tab.addEventListener("click", () => setEngineeringViewOpen(false, tab));
    }
    root.classList.toggle("owa-engineering-active", engineeringViewOpen);
    panel.hidden = !engineeringViewOpen;
  };

  const engineeringInteractionOpen = () => (
    engineeringActionDialog
    || engineeringReviewOpen
    || engineeringDocumentDialogOpen
  );

  const engineeringReviewPromptBusy = () => Boolean(
    engineeringInteractionOpen() || engineeringReviewInFlight || engineeringDocumentInFlight || (mcpControlOpen && document.querySelector(".owa-mcp-control-panel")?.checkVisibility()) || engineeringError
    || document.visibilityState === "hidden"
    || document.activeElement?.matches('input, textarea, select, [contenteditable="true"]')
    || [...document.querySelectorAll('dialog[open], [role="dialog"][aria-modal="true"]')].some((dialog) => (
      !dialog.classList.contains("owa-review-drawer") && !dialog.closest("[hidden]") && dialog.getClientRects().length > 0
    ))
  );
  const engineeringReviewPromptPending = () => engineeringReviewPrompts.shouldOpen(
    engineeringPendingReviewPrompt(engineeringData, {
      open: engineeringViewOpen, workspace: engineeringWorkspaceMode,
      projectId: engineeringSelectedProjectId,
      stage: selectedEngineeringStage ?? defaultVisibleEngineeringStage(engineeringData),
    }), engineeringReviewPromptBusy(),
  );

  const ensureEngineeringData = (force = false) => {
    if (!engineeringViewOpen) return;
    if (engineeringInteractionOpen()) return;
    const now = Date.now();
    if (engineeringRequestInFlight || (!force && now - engineeringLastFetchAt < 2500)) return;
    engineeringRequestInFlight = true;
    engineeringLastFetchAt = now;
    const projectId = engineeringWorkspaceMode === "projects" ? null : (engineeringSelectedProjectId
      ?? engineeringData?.project?.project_id
      ?? engineeringData?.state?.project_id
      ?? engineeringProjectIdFromPage());
    const query = projectId ? `?${new URLSearchParams({ project_id: projectId })}` : "";
    const requestGeneration = ++engineeringRequestGeneration;
    requestEngineeringStatus(`${workflowApiBase}/status${query}`, force ? null : engineeringDataEtag)
      .then((result) => {
        if (requestGeneration !== engineeringRequestGeneration) return;
        if (projectId && engineeringSelectedProjectId !== projectId) return;
        if (result.notModified) {
          const recoveredFromError = Boolean(engineeringError);
          engineeringDataEtag = result.etag ?? engineeringDataEtag;
          engineeringError = null;
          if (!engineeringInteractionOpen() && (force || recoveredFromError || engineeringReviewPromptPending())) {
            renderEngineeringPanel();
          }
          return;
        }
        const payload = result.payload;
        if (projectId && (payload.project?.project_id ?? payload.state?.project_id) !== projectId) {
          throw new Error(`当前环境未找到工程 ${projectId}。请回工程中心选择当前环境中的工程。`);
        }
        const nextFingerprint = JSON.stringify(payload);
        const dataChanged = nextFingerprint !== engineeringDataFingerprint;
        const recoveredFromError = Boolean(engineeringError);
        engineeringData = payload;
        rememberEngineeringProject(payload.project?.project_id ?? payload.state?.project_id);
        engineeringDataFingerprint = nextFingerprint;
        engineeringDataEtag = result.etag ?? null;
        engineeringError = null;
        if (!selectedEngineeringStage) rememberEngineeringStage(defaultEngineeringStage(payload));
        if (!engineeringInteractionOpen() && (force || dataChanged || recoveredFromError)) {
          renderEngineeringPanel();
        }
      })
      .catch((error) => {
        if (requestGeneration !== engineeringRequestGeneration) return;
        const message = error instanceof Error ? error.message : "工作流状态接口不可用";
        const nextError = projectId && message === "HTTP 404"
          ? `当前环境未找到工程 ${projectId}。请回工程中心选择当前环境中的工程。`
          : message;
        const errorChanged = nextError !== engineeringError;
        if (nextError.startsWith("当前环境未找到工程")) {
          engineeringData = null;
          engineeringDataFingerprint = null;
          engineeringDataEtag = null;
        }
        engineeringError = nextError;
        if (!engineeringInteractionOpen() && (force || errorChanged)) renderEngineeringPanel();
      })
      .finally(() => {
        if (requestGeneration === engineeringRequestGeneration) {
          engineeringRequestInFlight = false;
        }
      });
  };

  const sessionTabList = () => window.__ORION_NATIVE_PLUGIN__ === true ? null :
    Array.from(document.querySelectorAll('[role="tablist"]')).find((tabList) => {
      const labels = Array.from(tabList.querySelectorAll('[role="tab"]')).map((tab) =>
        tab.textContent?.trim(),
      );
      return labels.includes("对话") && labels.includes("轨迹");
    });

  const mcpStateLabel = (state) => ({
    ready: "正常",
    warning: "需关注",
    offline: "不可用",
  })[state] ?? "检查中";

  const mcpActionLabel = (action) => ({
    start: "启动",
    stop: "停止",
    restart: "重启",
    repair: "一键恢复",
  })[action] ?? action;

  const mcpRiskLabel = (risk) => ({
    read: "只读",
    compute: "计算",
    high: "高风险",
  })[risk] ?? "未分类";

  const mcpToolChineseNames = {
    text2sql: "自然语言转 SQL",
    execute_sql: "执行 SQL",
    get_tables_schema: "获取数据表结构",
    list_all_databases: "查看全部数据库",
    list_all_datasources: "查看全部数据源",
    list_all_schemas: "查看全部 Schema",
    list_all_tables: "查看全部数据表",
    pp_structurev3: "解析文档版面与表格",
    ocr: "识别图片文字",
    add_relationship: "添加图谱关系",
    export_graph: "导出知识图谱",
    extract_entities: "提取实体",
    extract_relations: "提取关系",
    find_precedents: "查找历史先例",
    get_causal_chain: "获取因果链",
    get_graph_analytics: "获取图谱分析",
    get_graph_summary: "获取图谱摘要",
    update_node: "更新节点属性",
    delete_node: "归档节点",
    import_ontology: "导入本体",
    query_decisions: "查询历史决策",
    record_decision: "记录决策",
    run_reasoning: "运行图谱推理",
    search_graph: "搜索知识图谱",
    create_ontology_project: "新建本体项目",
    generate_ontology_design: "生成本体设计",
    get_project_revision_history: "查看工程调整历史",
    get_ontology_workflow_status: "查看本体流程状态",
    list_ontology_projects: "查看本体项目",
    compile_mapping_runtime: "编译映射运行时",
    generate_mapping_skeleton: "生成映射草稿",
    prepare_mapping_review: "准备映射复核",
    publish_ontology_package: "发布本体包",
    record_document_evidence: "记录资料与证据",
    record_s0_scope_decision: "记录 S0 接入范围判定",
    record_data_understanding: "记录数据理解",
    record_ontology_build: "记录本体构建",
    preflight_stage_submission: "提交前聚合预检",
    commit_preflight_stage_submission: "提交已验证载荷",
    record_ontology_design: "记录本体设计",
    record_quality_validation: "记录质量校验",
    record_semantic_candidates: "记录语义候选项",
    reopen_stage_for_correction: "重新打开阶段进行修正",
    resume_active_revision: "恢复当前工程修订",
    resolve_mapping_confirmation: "处理映射确认",
    resolve_mapping_option: "按评审选项保存建模决定",
    retry_failed_stage: "重试失败阶段",
    retry_release_runtime_deployment: "重试运行时部署",
    shacl_validate: "执行 SHACL 校验",
    sparql_query: "执行 SPARQL 查询",
    sparql_schema: "查看 SPARQL 数据结构",
    sparql_validate: "校验 SPARQL 查询",
    semantic_diff: "比较语义差异",
  };

  const mcpToolChineseActions = {
    accept: "接受",
    add: "添加",
    analyze: "分析",
    apply: "应用",
    cancel: "取消",
    commit: "提交",
    create: "创建",
    delete: "删除",
    deprecate: "弃用",
    diff: "比较",
    discard: "放弃",
    execute: "执行",
    explain: "解释",
    export: "导出",
    extract: "提取",
    get: "获取",
    import: "导入",
    inspect: "检查",
    list: "查看",
    load: "加载",
    materialize: "物化",
    merge: "合并",
    move: "移动",
    prepare: "准备",
    preview: "预览",
    propose: "建议",
    query: "查询",
    rebase: "变基",
    redo: "重做",
    remove: "移除",
    rename: "重命名",
    run: "运行",
    save: "保存",
    search: "搜索",
    set: "设置",
    start: "启动",
    summarize: "汇总",
    undo: "撤销",
    validate: "校验",
    verify: "验证",
    write: "写入",
  };

  const mcpToolChineseObjects = {
    active_ontology: "当前本体",
    annotation: "注释",
    audit_log: "审计日志",
    axiom: "公理",
    axioms_for_entity: "实体相关公理",
    catalog: "目录配置",
    causal_chain: "因果链",
    change: "变更",
    change_impact: "变更影响",
    change_set: "变更集",
    changes: "变更",
    class: "类",
    classes: "类列表",
    competency_question: "能力问题",
    competency_questions: "能力问题",
    data_understanding: "数据理解",
    dl_query: "DL 查询",
    entailment: "逻辑蕴含",
    entities: "实体",
    entity: "实体",
    entity_context: "实体上下文",
    explanations: "推理解释",
    external_term: "外部术语",
    external_terms: "外部术语",
    governance: "治理规则",
    graph: "知识图谱",
    import: "导入项",
    import_lock: "导入锁定文件",
    imports: "导入项",
    inconsistency: "不一致原因",
    inferred_superclasses: "推理得到的父类",
    job: "任务",
    job_artifact: "任务产物",
    jobs: "任务列表",
    label: "显示名称",
    mapping: "映射",
    mapping_confirmation: "映射确认",
    mapping_review: "映射复核",
    mappings: "映射",
    materialization: "推理物化结果",
    model_revision: "模型版本",
    module: "本体模块",
    ontologies: "本体列表",
    ontology: "本体",
    ontology_annotation: "本体注释",
    ontology_build: "本体构建",
    ontology_context: "本体上下文",
    ontology_design: "本体设计",
    ontology_document: "本体文档",
    ontology_id: "本体标识",
    ontology_package: "本体包",
    ontology_project: "本体项目",
    ontology_projects: "本体项目",
    ontology_workflow_status: "本体流程状态",
    prefix: "命名空间前缀",
    project_policy: "项目策略",
    project_policy_template: "项目策略模板",
    properties: "属性",
    property: "属性",
    quality_validation: "质量校验",
    reasoner: "推理机",
    reasoner_capabilities: "推理机能力",
    reasoners: "推理机列表",
    release: "发布包",
    release_gate: "发布门禁",
    reuse_proposal: "复用建议",
    rule: "规则",
    rules: "规则",
    semantic_candidates: "语义候选项",
    sssom: "SSSOM 映射",
    subclass_of: "子类关系",
    term: "术语",
    term_reuse: "术语复用",
    terms: "术语",
    unsatisfiable_classes: "不可满足类",
  };

  const mcpToolChineseTokens = {
    all: "全部",
    analytics: "分析",
    correction: "修正",
    database: "数据库",
    databases: "数据库",
    datasource: "数据源",
    datasources: "数据源",
    decision: "决策",
    decisions: "决策",
    design: "设计",
    entity: "实体",
    failed: "失败",
    graph: "图谱",
    lock: "锁定",
    ontology: "本体",
    policy: "策略",
    proposal: "建议",
    quality: "质量",
    relations: "关系",
    relationship: "关系",
    reuse: "复用",
    schema: "结构",
    schemas: "Schema",
    stage: "阶段",
    summary: "摘要",
    table: "数据表",
    tables: "数据表",
    validation: "校验",
    workflow: "流程",
  };

  const mcpToolChineseMeta = (tool) => {
    const rawName = String(tool.rawName ?? "");
    let name = mcpToolChineseNames[rawName];
    if (!name) {
      const [action, ...objectTokens] = rawName.split("_");
      const actionLabel = mcpToolChineseActions[action];
      const objectKey = objectTokens.join("_");
      const objectLabel = mcpToolChineseObjects[objectKey]
        ?? objectTokens.map((token) => mcpToolChineseTokens[token] ?? token).join("");
      name = actionLabel && objectLabel ? `${actionLabel}${objectLabel}` : `工具：${rawName}`;
    }
    const note = ({
      read: `用途：${name}，主要读取、搜索或检查现有信息。`,
      compute: `用途：${name}，会执行解析、推理或计算，不直接改写业务数据。`,
      high: `用途：${name}，可能新增、修改或删除内容，执行前请确认影响范围。`,
    })[tool.risk] ?? `用途：${name}。`;
    return { name, note };
  };

  const ocrPresets = {
    "paddleocr-structure": {
      policy: {
        use_doc_orientation_classify: true,
        use_doc_unwarping: false,
        use_textline_orientation: false,
        use_table_recognition: true,
        use_formula_recognition: false,
        use_chart_recognition: false,
        use_seal_recognition: false,
      },
      scan: {
        use_doc_orientation_classify: true,
        use_doc_unwarping: true,
        use_textline_orientation: true,
        use_table_recognition: true,
        use_formula_recognition: false,
        use_chart_recognition: false,
        use_seal_recognition: false,
      },
      table: {
        use_doc_orientation_classify: true,
        use_doc_unwarping: false,
        use_textline_orientation: false,
        use_table_recognition: true,
        use_formula_recognition: false,
        use_chart_recognition: false,
        use_seal_recognition: false,
      },
    },
    "paddleocr-ocr": {
      screenshot: {
        use_doc_orientation_classify: true,
        use_doc_unwarping: false,
        use_textline_orientation: true,
      },
      scan: {
        use_doc_orientation_classify: true,
        use_doc_unwarping: true,
        use_textline_orientation: true,
      },
    },
  };

  const ocrSettingLabels = {
    use_doc_orientation_classify: "自动旋转页面",
    use_doc_unwarping: "页面弯曲矫正",
    use_textline_orientation: "文字行方向识别",
    use_table_recognition: "表格识别",
    use_formula_recognition: "公式识别",
    use_chart_recognition: "图表识别",
    use_seal_recognition: "印章识别",
  };

  const renderMcpToolManager = (service) => {
    const hasToolInventory = Array.isArray(service.tools);
    const enabledToolCount = Number.isFinite(service.enabledToolCount)
      ? service.enabledToolCount
      : null;
    const query = mcpControlToolSearch.trim().toLowerCase();
    const toolsWithChinese = (hasToolInventory ? service.tools : []).map((tool) => ({
      ...tool,
      chinese: mcpToolChineseMeta(tool),
    }));
    const visible = toolsWithChinese.filter((tool) => (
      !query || `${tool.rawName} ${tool.description} ${tool.chinese.name} ${tool.chinese.note}`.toLowerCase().includes(query)
    ));
    const rows = visible.map((tool) => `
      <div class="owa-mcp-tool-row${tool.enabled ? "" : " is-disabled"}">
        <div class="owa-mcp-tool-copy">
          <div class="owa-mcp-tool-title"><strong>${escapeHtml(tool.chinese.name)}</strong><span class="owa-mcp-risk is-${escapeHtml(tool.risk)}">${escapeHtml(mcpRiskLabel(tool.risk))}</span></div>
          <code>${escapeHtml(tool.rawName)}</code>
          <p class="owa-mcp-tool-note">${escapeHtml(tool.chinese.note)}</p>
          <details class="owa-mcp-tool-original"><summary>工具原始说明${tool.inputKeys?.length ? ` · ${escapeHtml(tool.inputKeys.length)} 个参数` : ""}</summary><p>${escapeHtml(tool.description || "该 MCP 未提供说明")}</p>${tool.inputKeys?.length ? `<small>参数：${escapeHtml(tool.inputKeys.join("、"))}</small>` : ""}</details>
        </div>
        <button type="button" class="owa-mcp-switch${tool.enabled ? " is-on" : ""}" role="switch" aria-checked="${String(tool.enabled)}" data-mcp-tool-name="${escapeHtml(tool.name)}" data-mcp-tool-enabled="${String(!tool.enabled)}" data-mcp-service-id="${escapeHtml(service.id)}" ${mcpControlSettingsInFlight ? "disabled" : ""}><span></span>${tool.enabled ? "已启用" : "已停用"}</button>
      </div>`).join("");
    const emptyCopy = !hasToolInventory
      ? "当前 3081 仍在使用旧后端，尚未返回工具清单；重启 3081 后即可逐项管理。"
      : service.toolCount
        ? "没有匹配的工具"
        : "服务尚未挂载工具，恢复服务后再管理";
    const countCopy = enabledToolCount === null
      ? "工具策略等待后端生效"
      : `${enabledToolCount} / ${service.toolCount} 已启用`;
    return `
      <div class="owa-mcp-detail-heading"><div><span class="owa-mcp-eyebrow">工具权限</span><h3>${escapeHtml(service.label)} · 工具管理</h3><p>停用后由运行守卫强制阻断；页面不显示密钥和连接凭据。</p></div><span>${escapeHtml(countCopy)}</span></div>
      <label class="owa-mcp-tool-search"><span>搜索工具</span><input type="search" value="${escapeHtml(mcpControlToolSearch)}" placeholder="中文用途、英文名称或参数" data-mcp-tool-search></label>
      <div class="owa-mcp-tool-list">${rows || `<div class="owa-mcp-detail-empty">${escapeHtml(emptyCopy)}</div>`}</div>`;
  };

  const renderOcrSettings = (service) => {
    const settings = service.settings;
    if (!settings) {
      if (service.id === "paddleocr-structure" || service.id === "paddleocr-ocr") {
        return `<div class="owa-mcp-detail-heading"><div><span class="owa-mcp-eyebrow">OCR DEFAULTS</span><h3>${escapeHtml(service.label)} · 常用设置</h3><p>当前 3081 仍在使用旧后端，尚未返回 OCR 设置；重启 3081 后即可选择常用场景和识别参数。</p></div><span>OCR 设置等待后端生效</span></div>`;
      }
      return `<div class="owa-mcp-detail-heading"><div><span class="owa-mcp-eyebrow">服务设置</span><h3>${escapeHtml(service.label)} · 设置</h3><p>该服务由 ${escapeHtml(service.ownership)} 管理。当前页面只开放工具策略，不修改外部服务的凭据或数据。</p></div><span>服务配置只读</span></div>`;
    }
    const presetOptions = service.id === "paddleocr-structure"
      ? [["policy", "政策文件"], ["scan", "扫描文件"], ["table", "表格文件"], ["custom", "自定义"]]
      : [["screenshot", "截图识字"], ["scan", "扫描文件"], ["custom", "自定义"]];
    const toggles = Object.entries(settings.runtimeParams).map(([key, value]) => `
      <label class="owa-mcp-setting-toggle"><input type="checkbox" name="${escapeHtml(key)}" ${value ? "checked" : ""}><span></span><b>${escapeHtml(ocrSettingLabels[key] ?? key)}</b></label>`).join("");
    return `
      <div class="owa-mcp-detail-heading"><div><span class="owa-mcp-eyebrow">OCR DEFAULTS</span><h3>${escapeHtml(service.label)} · 常用设置</h3><p>这些参数写入 Agent 默认调用模板；具体任务中明确给出的参数仍可覆盖默认值。</p></div><span>保存后下一次请求生效</span></div>
      <form class="owa-mcp-settings-form" data-mcp-ocr-form="${escapeHtml(service.id)}">
        <div class="owa-mcp-setting-grid"><label><span>使用场景</span><select name="preset" data-mcp-ocr-preset="${escapeHtml(service.id)}">${presetOptions.map(([value, label]) => `<option value="${value}" ${settings.preset === value ? "selected" : ""}>${label}</option>`).join("")}</select></label><label><span>输出模式</span><select name="outputMode"><option value="simple" ${settings.outputMode === "simple" ? "selected" : ""}>简洁结果</option><option value="detailed" ${settings.outputMode === "detailed" ? "selected" : ""}>详细结果</option></select></label></div>
        ${service.id === "paddleocr-structure" ? `<label class="owa-mcp-setting-toggle is-wide"><input type="checkbox" name="returnImages" ${settings.returnImages ? "checked" : ""}><span></span><b>返回文档图片</b><em>关闭可减少上下文和传输体积</em></label>` : ""}
        <div class="owa-mcp-setting-toggles">${toggles}</div>
        <div class="owa-mcp-setting-note">安全边界：这里只保存识别参数，不开放任意目录，也不会自动读取或写入文件。</div>
        <button type="button" class="owa-mcp-save-settings" data-mcp-save-ocr="${escapeHtml(service.id)}" ${mcpControlSettingsInFlight ? "disabled" : ""}>${mcpControlSettingsInFlight ? "保存中…" : "保存常用设置"}</button>
      </form>`;
  };

  const renderMcpDetailPanel = (services) => {
    const service = services.find((item) => item.id === mcpControlSelectedService);
    if (!service) return "";
    const content = mcpControlSelectedSection === "settings"
      ? renderOcrSettings(service)
      : renderMcpToolManager(service);
    return `<section class="owa-mcp-detail-panel" id="owa-mcp-service-detail" aria-live="polite"><nav><button type="button" data-mcp-detail-section="tools" data-mcp-detail-service="${escapeHtml(service.id)}" class="${mcpControlSelectedSection === "tools" ? "is-active" : ""}">工具</button><button type="button" data-mcp-detail-section="settings" data-mcp-detail-service="${escapeHtml(service.id)}" class="${mcpControlSelectedSection === "settings" ? "is-active" : ""}">设置</button><button type="button" class="owa-mcp-detail-close" data-mcp-detail-close aria-label="关闭详情">×</button></nav><div class="owa-mcp-detail-body">${content}</div></section>`;
  };

  const revealMcpDetailPanel = (panel) => {
    window.requestAnimationFrame(() => {
      panel.querySelector("#owa-mcp-service-detail")?.scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
        block: "nearest",
      });
    });
  };

  const renderMcpControlPanel = () => {
    const panel = document.querySelector(".owa-mcp-control-panel");
    if (!panel || !mcpControlOpen) return;
    if (!mcpControlData) {
      panel.innerHTML = mcpControlError
        ? `<div class="owa-mcp-empty is-error"><strong>无法读取 MCP 实时状态</strong><span>${escapeHtml(mcpControlError)}</span><button type="button" data-mcp-control-action="refresh">重新检查</button></div>`
        : '<div class="owa-mcp-empty"><span class="owa-mcp-spinner" aria-hidden="true"></span><strong>正在逐个验证 6 个 MCP…</strong><span>包括后端协议、进程和 Harness 工具挂载。</span></div>';
      return;
    }
    const { summary, services, checkedAt } = mcpControlData;
    const checkedTime = checkedAt
      ? new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(checkedAt))
      : "—";
    const cards = services.map((service) => {
      const enabledToolCount = Number.isFinite(service.enabledToolCount)
        ? service.enabledToolCount
        : null;
      const availableActions = service.state === "offline"
        ? service.controls.filter((action) => action === "start")
        : service.controls.filter((action) => action === "restart" || action === "stop");
      const actions = availableActions.map((action) => {
        const busy = mcpControlActionInFlight === `${service.id}:${action}`;
        return `<button type="button" class="owa-mcp-action${action === "stop" ? " is-danger" : ""}" data-mcp-service="${escapeHtml(service.id)}" data-mcp-action="${escapeHtml(action)}" ${mcpControlActionInFlight ? "disabled" : ""}>${busy ? "处理中…" : escapeHtml(mcpActionLabel(action))}</button>`;
      }).join("");
      const version = service.serverVersion ? ` · v${escapeHtml(service.serverVersion)}` : "";
      return `<article class="owa-mcp-card is-${escapeHtml(service.state)}">
        <header><div><span class="owa-mcp-dot" aria-hidden="true"></span><h3>${escapeHtml(service.label)}</h3></div><span class="owa-mcp-badge is-${escapeHtml(service.state)}">${escapeHtml(mcpStateLabel(service.state))}</span></header>
        <div class="owa-mcp-meta"><span>${escapeHtml(service.transport)}</span><span>${escapeHtml(service.toolCount)} 个工具</span>${enabledToolCount !== null && enabledToolCount !== service.toolCount ? `<span>${escapeHtml(enabledToolCount)} 个已启用</span>` : ""}${service.pid ? `<span>PID ${escapeHtml(service.pid)}</span>` : ""}</div>
        <code>${escapeHtml(service.endpoint)}</code>
        <p>${escapeHtml(service.detail)}</p>
        <footer><span>${escapeHtml(service.ownership)}${version}</span><div><button type="button" class="owa-mcp-action${mcpControlSelectedService === service.id && mcpControlSelectedSection === "tools" ? " is-selected" : ""}" data-mcp-detail-service="${escapeHtml(service.id)}" data-mcp-detail-section="tools" aria-controls="owa-mcp-service-detail" aria-expanded="${String(mcpControlSelectedService === service.id && mcpControlSelectedSection === "tools")}">工具</button><button type="button" class="owa-mcp-action${mcpControlSelectedService === service.id && mcpControlSelectedSection === "settings" ? " is-selected" : ""}" data-mcp-detail-service="${escapeHtml(service.id)}" data-mcp-detail-section="settings" aria-controls="owa-mcp-service-detail" aria-expanded="${String(mcpControlSelectedService === service.id && mcpControlSelectedSection === "settings")}">设置</button>${actions}</div></footer>
      </article>`;
    }).join("");
    panel.innerHTML = `
      <section class="owa-mcp-overview" aria-label="MCP 总体状态">
        <div class="owa-mcp-overview-copy"><h2>全局 MCP 服务</h2><p>核验协议、进程和真实工具注册。“已挂载”仍需通过可用性检查，页面不展示任何密钥。</p></div>
        <div class="owa-mcp-summary"><span><strong>${escapeHtml(summary.total)}</strong>全部</span><span class="is-ready"><strong>${escapeHtml(summary.ready)}</strong>正常</span><span class="is-warning"><strong>${escapeHtml(summary.warning)}</strong>需关注</span><span class="is-offline"><strong>${escapeHtml(summary.offline)}</strong>不可用</span></div>
        <div class="owa-mcp-toolbar"><span>最近检查 ${escapeHtml(checkedTime)} · 每 10 秒自动刷新</span><div><button type="button" data-mcp-control-action="refresh" ${mcpControlRequestInFlight || mcpControlActionInFlight ? "disabled" : ""}>${mcpControlRequestInFlight ? "检查中…" : "立即检查"}</button><button type="button" class="is-primary" data-mcp-control-action="repair" ${summary.warning + summary.offline === 0 || mcpControlActionInFlight ? "disabled" : ""}>${mcpControlActionInFlight === "all:repair" ? "恢复中…" : summary.warning + summary.offline === 0 ? "全部正常" : "一键恢复异常 MCP"}</button></div></div>
        ${mcpControlMessage ? `<div class="owa-mcp-message">${escapeHtml(mcpControlMessage)}</div>` : ""}
      </section>
      ${renderMcpDetailPanel(services)}
      <section class="owa-mcp-grid" aria-label="MCP 服务列表">${cards}</section>`;
  };

  const ensureMcpControlData = (force = false) => {
    if (!mcpControlOpen) return;
    // Native main panels can stay mounted while another panel is selected.
    if (!document.querySelector(".owa-mcp-control-panel")?.checkVisibility()) return;
    const now = Date.now();
    if (mcpControlRequestInFlight || (!force && now - mcpControlLastFetchAt < 10000)) return;
    mcpControlRequestInFlight = true;
    mcpControlLastFetchAt = now;
    renderMcpControlPanel();
    requestJson(`${mcpControlApiBase}/status`)
      .then((payload) => {
        mcpControlData = payload;
        mcpControlError = null;
      })
      .catch((error) => {
        mcpControlError = error instanceof Error ? error.message : "MCP 状态接口不可用";
      })
      .finally(() => {
        mcpControlRequestInFlight = false;
        renderMcpControlPanel();
      });
  };

  const postMcpSettings = async (payload) => {
    if (mcpControlSettingsInFlight) return;
    mcpControlSettingsInFlight = true;
    mcpControlMessage = "正在保存 MCP 设置…";
    renderMcpControlPanel();
    try {
      const response = await fetch(`${mcpControlApiBase}/settings`, {
        method: "POST",
        headers: {
          accept: "application/json",
          "content-type": "application/json",
          "x-orion-mcp-control": "1",
        },
        body: JSON.stringify(payload),
      });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.detail ?? `HTTP ${response.status}`);
      mcpControlMessage = result.detail ?? "MCP 设置已保存";
      mcpControlData = null;
      mcpControlLastFetchAt = 0;
      window.setTimeout(() => ensureMcpControlData(true), 150);
    } catch (error) {
      mcpControlMessage = `设置保存失败：${error instanceof Error ? error.message : "未知错误"}`;
    } finally {
      mcpControlSettingsInFlight = false;
      renderMcpControlPanel();
    }
  };

  const saveMcpToolPolicy = (toolName, enabled) => postMcpSettings({
    kind: "tool-policy",
    toolName,
    enabled,
    confirmation: `tool-policy:${toolName}:${String(enabled)}`,
  });

  const saveOcrSettings = (serviceId) => {
    const form = document.querySelector(`[data-mcp-ocr-form="${CSS.escape(serviceId)}"]`);
    if (!form) return;
    const runtimeParams = {};
    for (const key of Object.keys(ocrSettingLabels)) {
      const input = form.elements.namedItem(key);
      if (input instanceof HTMLInputElement) runtimeParams[key] = input.checked;
    }
    const returnImages = form.elements.namedItem("returnImages");
    postMcpSettings({
      kind: "ocr-settings",
      serviceId,
      settings: {
        preset: form.elements.namedItem("preset")?.value ?? "custom",
        outputMode: form.elements.namedItem("outputMode")?.value ?? "simple",
        returnImages: returnImages instanceof HTMLInputElement && returnImages.checked,
        runtimeParams,
      },
      confirmation: `ocr-settings:${serviceId}`,
    });
  };

  const applyOcrPreset = (serviceId, preset, form) => {
    const values = ocrPresets[serviceId]?.[preset];
    if (!values) return;
    for (const [key, value] of Object.entries(values)) {
      const input = form.elements.namedItem(key);
      if (input instanceof HTMLInputElement) input.checked = value;
    }
  };

  const runMcpControlAction = async (serviceId, action) => {
    const bulkRepair = serviceId === "all" && action === "repair";
    const service = bulkRepair
      ? { id: "all", label: "异常 MCP", controls: ["repair"] }
      : mcpControlData?.services?.find((item) => item.id === serviceId);
    if (!service || !service.controls.includes(action) || mcpControlActionInFlight) return;
    const actionName = mcpActionLabel(action);
    const confirmationCopy = bulkRepair
      ? "将修复 Chat2DB 持久配置、接管已运行的文档解析 PID，并启动离线 OCR。不会停止 Protégé 或 3081；OCR 模型加载可能需要 1～3 分钟。"
      : action === "stop" || action === "restart"
        ? "该服务会短暂不可用，正在执行的调用可能失败。"
        : "启动可能需要等待模型或桌面应用加载。";
    if (!window.confirm(`确认${actionName} ${service.label}？\n\n${confirmationCopy}`)) return;
    mcpControlActionInFlight = `${serviceId}:${action}`;
    mcpControlMessage = `${service.label} 正在${actionName}…`;
    renderMcpControlPanel();
    try {
      const response = await fetch(`${mcpControlApiBase}/action`, {
        method: "POST",
        headers: {
          accept: "application/json",
          "content-type": "application/json",
          "x-orion-mcp-control": "1",
        },
        body: JSON.stringify({
          serviceId,
          action,
          confirmation: `${serviceId}:${action}`,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload.detail ?? `HTTP ${response.status}`);
      mcpControlMessage = payload.ok === false
        ? payload.detail ?? "部分服务仍需单独检查。"
        : `${service.label} 已完成${actionName}，状态将重新验证。`;
      mcpControlData = null;
      mcpControlLastFetchAt = 0;
      window.setTimeout(() => ensureMcpControlData(true), 900);
    } catch (error) {
      mcpControlMessage = `${service.label} ${actionName}失败：${error instanceof Error ? error.message : "未知错误"}`;
    } finally {
      mcpControlActionInFlight = null;
      renderMcpControlPanel();
    }
  };

  // The native plugin slot owns this container and its lifetime. The existing
  // MCP controller continues to own requests, actions and service policy.
  window.__ORION_MCP_CONTROL__ = {
    mount(container) {
      const panel = document.createElement("div");
      panel.className = "owa-mcp-control-panel";
      panel.id = "owa-mcp-control-panel";
      panel.addEventListener("click", (event) => {
        if (event.target.closest("[data-mcp-detail-close]")) {
          mcpControlSelectedService = null;
          renderMcpControlPanel();
          return;
        }
        const detailButton = event.target.closest("[data-mcp-detail-service][data-mcp-detail-section]");
        if (detailButton) {
          mcpControlSelectedService = detailButton.dataset.mcpDetailService;
          mcpControlSelectedSection = detailButton.dataset.mcpDetailSection;
          mcpControlToolSearch = "";
          renderMcpControlPanel();
          revealMcpDetailPanel(panel);
          return;
        }
        const toolSwitch = event.target.closest("[data-mcp-tool-name][data-mcp-tool-enabled]");
        if (toolSwitch) {
          saveMcpToolPolicy(
            toolSwitch.dataset.mcpToolName,
            toolSwitch.dataset.mcpToolEnabled === "true",
          );
          return;
        }
        const saveOcrButton = event.target.closest("[data-mcp-save-ocr]");
        if (saveOcrButton) {
          saveOcrSettings(saveOcrButton.dataset.mcpSaveOcr);
          return;
        }
        const globalAction = event.target.closest("[data-mcp-control-action]")?.dataset.mcpControlAction;
        if (globalAction === "refresh") {
          mcpControlLastFetchAt = 0;
          mcpControlMessage = null;
          ensureMcpControlData(true);
          return;
        }
        if (globalAction === "repair") {
          runMcpControlAction("all", "repair");
          return;
        }
        const actionButton = event.target.closest("[data-mcp-service][data-mcp-action]");
        if (actionButton) runMcpControlAction(actionButton.dataset.mcpService, actionButton.dataset.mcpAction);
      });
      panel.addEventListener("input", (event) => {
        if (!event.target.matches("[data-mcp-tool-search]")) return;
        mcpControlToolSearch = event.target.value;
        renderMcpControlPanel();
        requestAnimationFrame(() => {
          const input = panel.querySelector("[data-mcp-tool-search]");
          input?.focus();
          input?.setSelectionRange?.(input.value.length, input.value.length);
        });
      });
      panel.addEventListener("change", (event) => {
        const preset = event.target.closest("[data-mcp-ocr-preset]");
        if (preset) {
          applyOcrPreset(preset.dataset.mcpOcrPreset, preset.value, preset.form);
          return;
        }
        const form = event.target.closest("[data-mcp-ocr-form]");
        if (form && event.target instanceof HTMLInputElement && event.target.type === "checkbox") {
          const presetSelect = form.elements.namedItem("preset");
          if (presetSelect) presetSelect.value = "custom";
        }
      });
      container.appendChild(panel);
      mcpControlOpen = true;
      renderMcpControlPanel();
      ensureMcpControlData(true);
      if (mcpControlTimer) window.clearInterval(mcpControlTimer);
      mcpControlTimer = window.setInterval(() => ensureMcpControlData(), 10000);
      return () => {
        mcpControlOpen = false;
        if (mcpControlTimer) window.clearInterval(mcpControlTimer);
        mcpControlTimer = null;
        panel.remove();
      };
    },
  };
  window.dispatchEvent(new CustomEvent("orion:mcp-control-ready"));

  window.__ORION_ENGINEERING_BRIDGE__ = {
    mount(root) {
      if (!(root instanceof HTMLElement)) throw new TypeError("工程页面需要自己的容器");
      if (pluginEngineeringRoot && pluginEngineeringRoot !== root) throw new Error("工程页面已经挂载");
      pluginEngineeringRoot = root;
      ensureEngineeringNavigation();
      const timer = window.setInterval(() => {
        if (document.visibilityState === "visible") refreshEngineeringRuntime();
      }, 3000);
      return () => {
        window.clearInterval(timer);
        if (pluginEngineeringRoot !== root) return;
        engineeringRequestGeneration += 1;
        setEngineeringViewOpen(false);
        pluginEngineeringRoot = null;
        root.replaceChildren();
      };
    },
    open({ workspace = "projects", projectId = null, stage = null } = {}) {
      const currentProjectId = engineeringData?.project?.project_id
        ?? engineeringData?.state?.project_id
        ?? engineeringSelectedProjectId;
      const projectChanged = Boolean(projectId && projectId !== currentProjectId);
      const nextWorkspaceMode = workspace === "stage" ? "stage" : "projects";
      const workspaceChanged = nextWorkspaceMode !== engineeringWorkspaceMode;
      const stageChanged = Boolean(nextWorkspaceMode === "stage" && stage && stage !== selectedEngineeringStage);
      const viewChanged = projectChanged || workspaceChanged || stageChanged;
      if (projectId) rememberEngineeringProject(projectId);
      if (workspace === "stage" && projectId) rememberEngineeringStage(stage);
      engineeringWorkspaceMode = nextWorkspaceMode;
      if (viewChanged) {
        engineeringSection = "summary";
        engineeringHistoryExpanded = false;
        engineeringStageNotice = null;
      }
      if (projectChanged || workspaceChanged) {
        engineeringRequestGeneration += 1;
        engineeringRequestInFlight = false;
        engineeringData = null;
        engineeringDataFingerprint = null;
        engineeringDataEtag = null;
        engineeringError = null;
        engineeringLastFetchAt = 0;
      }
      setEngineeringViewOpen(true);
    },
    close() {
      setEngineeringViewOpen(false);
    },
    getRoot: engineeringRoot,
    getState: () => ({
      open: engineeringViewOpen,
      workspace: engineeringWorkspaceMode,
      projectId: engineeringSelectedProjectId,
      stage: selectedEngineeringStage,
    }),
  };
  window.dispatchEvent(new CustomEvent("orion:engineering-ready"));

  const polling = window.__ORION_POLLING__;
  const refreshEngineeringRuntime = () => {
    engineeringRoot()?.querySelectorAll("[data-stage-timing-base]").forEach((element) => {
      const base = Number(element.dataset.stageTimingBase);
      const at = Number(element.dataset.stageTimingAt);
      if (Number.isFinite(base) && Number.isFinite(at)) element.textContent = stageTimingDuration(base + Math.max(0, (Date.now() - at) / 1000));
    });
    engineeringRoot()?.querySelectorAll("[data-s7-query-started-at]").forEach((element) => {
      const started = Number(element.dataset.s7QueryStartedAt);
      if (Number.isFinite(started)) element.textContent = s7ValidationDuration(Math.max(0, (Date.now() - started) / 1000));
    });
    ensureEngineeringData();
    ensureEngineeringDocumentSummary();
  };
  // Native plugins own their slot container and its polling lifecycle. The
  // legacy observer below stays available only to the existing 3081 assembly.
  if (window.__ORION_NATIVE_PLUGIN__ === true) return;
  if (polling?.everyVisible) {
    polling.everyVisible(refreshEngineeringRuntime, 3000);
  } else {
    window.setInterval(refreshEngineeringRuntime, 3000);
  }
  window.__ORION_HARNESS_ADAPTER__.start(() => {
    ensureMcpControlData();
    ensureEngineeringNavigation();
    ensureEngineeringData();
    ensureEngineeringDocumentSummary();
  });
})();
