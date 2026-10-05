(() => {
  if (window.__ORION_BUSINESS_PREVIEW__) return;
  const pendingStarts = new Map();
  const selectionStates = new Map();
  const list = (value) => Array.isArray(value) ? value : [];
  const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const labels = Object.freeze({
    NOT_STARTED: "尚未试运行", STARTING: "正在准备", RUNNING: "正在试运行", PASSED: "最近试运行通过",
    FAILED: "试运行未通过", INCONCLUSIVE: "证据不足", INTERRUPTED: "试运行中断", STALE: "结果已过期", UNKNOWN: "状态待确认",
  });
  const busy = (status) => ["STARTING", "RUNNING"].includes(status);
  const retryable = (status) => ["FAILED", "INCONCLUSIVE", "INTERRUPTED"].includes(status);
  const validReference = (value) => object(value)
    && typeof value.file_name === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$/.test(value.file_name)
    && typeof value.sha256 === "string" && /^sha256:[a-f0-9]{64}$/.test(value.sha256);
  const referenceKey = (value) => validReference(value) ? JSON.stringify([value.file_name, value.sha256]) : "";
  const sameReference = (left, right) => referenceKey(left) === referenceKey(right);
  const count = (value) => Number.isSafeInteger(value) && value >= 0 ? value : "未提供";
  const text = (value) => object(value) || Array.isArray(value) ? JSON.stringify(value) : String(value ?? "—");
  const notice = '<p class="owa-business-preview-notice">快照预览，不代表全量验收/发布。试运行不改变正式阶段，也不触发审批。</p>';
  const guidance = '<p>需要调整时，请回到工程对话修订该业务计划，再重新读取并试运行。</p><button type="button" data-engineering-action="conversation" data-diagnostic-stage="S3">回到工程对话修订</button>';

  const validReceipt = (receipt) => object(receipt) && receipt.validation_scope === "S3_BUSINESS_PREVIEW"
    && receipt.formal_state_changed === false && receipt.source_mode === "SNAPSHOT_ONLY"
    && typeof receipt.preview_id === "string" && receipt.preview_id.length > 0
    && typeof receipt.input_fingerprint === "string" && receipt.input_fingerprint.length > 0;
  const receiptMatches = (plan, report) => validReceipt(plan.receipt)
    && plan.receipt.project_id === report?.project_id && String(plan.receipt.revision) === String(report?.revision)
    && plan.receipt.plan_id === plan.id && typeof plan.receipt.case_id === "string"
    && validReference(plan.receipt.payload_file) && sameReference(plan.receipt.payload_file, report?.payload_file);
  const planStatus = (plan, report) => {
    if (!Object.hasOwn(labels, plan.status)) return "UNKNOWN";
    if (["NOT_STARTED", "STALE"].includes(plan.status)) return plan.status;
    if (busy(plan.status) && !plan.receipt) return plan.status;
    return receiptMatches(plan, report) && plan.receipt.status === plan.status ? plan.status : "UNKNOWN";
  };
  const selectedCase = (plan, selections) => {
    const cases = list(plan.case_ids).filter((id) => typeof id === "string" && id);
    return cases.includes(selections.get(plan.id)) ? selections.get(plan.id)
      : cases.includes(plan.receipt?.case_id) ? plan.receipt.case_id : cases[0];
  };
  const differentCase = (plan, selected) => !!selected && !!plan.receipt && plan.receipt.case_id !== selected;
  const exhausted = (plan, report, selected) => !differentCase(plan, selected) && retryable(planStatus(plan, report))
    && Number.isSafeInteger(plan.receipt?.attempt) && Number.isSafeInteger(plan.receipt?.max_attempts)
    && plan.receipt.max_attempts > 0 && plan.receipt.attempt >= plan.receipt.max_attempts;
  const hasRunning = (report) => busy(report?.active_preview?.status)
    || list(report?.plans).some((plan) => busy(planStatus(plan, report)));
  const normalizeReport = (report, projectId, revision) => {
    if (report?.schema_version !== 1 || report.project_id !== projectId || report.stage !== "S3"
      || String(report.revision) !== String(revision)) {
      throw new Error("工程或版本已变化，预览响应不匹配。请刷新工程后重新打开试运行。");
    }
    if (!["AVAILABLE", "NOT_READY"].includes(report.status) || !Array.isArray(report.plans)
      || (report.payload_file !== null && !validReference(report.payload_file))) {
      throw new Error("预览响应格式无法确认，请重新读取；当前没有可确认的通过结果。");
    }
    if (report.active_preview != null && (!object(report.active_preview)
      || report.active_preview.project_id !== projectId || !busy(report.active_preview.status)
      || typeof report.active_preview.preview_id !== "string" || !report.active_preview.preview_id)) {
      throw new Error("执行中的试运行身份无法确认，请重新读取；不会重复启动。");
    }
    const ids = new Set();
    for (const plan of report.plans) {
      if (!object(plan) || typeof plan.id !== "string" || !plan.id || ids.has(plan.id)) {
        throw new Error("业务计划标识缺失或重复，请回到工程对话修订。");
      }
      ids.add(plan.id);
    }
    return report;
  };

  const renderRuleAnswers = (rule) => list(rule?.cq_results).slice(0, 5).map((answer) => {
    const fields = list(answer?.result_fields).filter((field) => typeof field === "string").slice(0, 20);
    const rows = list(answer?.rows).filter(object).slice(0, 5);
    return `<div class="owa-business-preview-rule-answer"><strong>规则得出的业务结果 · ${escape(answer?.question_id)}</strong>
      <p>返回 ${escape(count(answer?.returned_row_count))} 行；以下仅展示最多 5 行。</p>
      ${fields.length && rows.length ? `<div class="owa-business-preview-table" tabindex="0" role="region" aria-label="规则问答结果"><table><thead><tr>${fields.map((field) => `<th scope="col">${escape(field)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${fields.map((field) => `<td>${escape(Object.hasOwn(row, field) ? text(row[field]) : "—")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : "<p>未返回规则结论；请结合该用例的正反例预期和诊断理解。</p>"}</div>`;
  }).join("");
  const renderReceipt = (receipt, status) => {
    if (!object(receipt)) return "";
    const diagnostics = list(receipt.diagnostics).slice(0, 30);
    const details = `<details class="owa-business-preview-technical"><summary>技术详情</summary><pre>${escape(JSON.stringify({
      preview_id: receipt.preview_id, status: receipt.status, input_fingerprint: receipt.input_fingerprint,
      validation_scope: receipt.validation_scope, source_mode: receipt.source_mode,
      diagnostics, ...(receipt.rule ? { rule_trace: list(receipt.rule.trace).slice(0, 25) } : {}),
    }, null, 2))}</pre></details>`;
    if (status === "STALE" || status === "UNKNOWN") return details;
    const fields = list(receipt.result_fields).filter((field) => typeof field === "string").slice(0, 30);
    const rows = list(receipt.rows).filter(object).slice(0, 5);
    return `<p>${escape(receipt.message)}</p>
      <p class="owa-business-preview-meta">计划最近一次回执${receipt.case_id ? ` · 样例 ${escape(receipt.case_id)}` : ""}${receipt.observed_at ? ` · ${escape(receipt.observed_at)}` : ""}${Number.isFinite(receipt.elapsed_seconds) && receipt.elapsed_seconds >= 0 ? ` · 用时 ${escape(receipt.elapsed_seconds)} 秒` : ""}</p>
      ${diagnostics.length ? `<ul class="owa-business-preview-diagnostics">${diagnostics.map((item) => `<li><p>${escape(item?.message || "请查看技术详情")}</p>${item?.repair_path ? `<p>修订位置：<code>${escape(item.repair_path)}</code></p>` : ""}${item?.next_step ? `<p>${escape(item.next_step)}</p>` : ""}</li>`).join("")}</ul>` : ""}
      ${!busy(status) ? `<p>查询返回 ${escape(count(receipt.returned_row_count))} 行，展示前 ${rows.length} 行。</p>${rows.length && fields.length
        ? `<div class="owa-business-preview-table" tabindex="0" role="region" aria-label="试运行实例数据"><table><caption>实例数据（最多 5 行）</caption><thead><tr>${fields.map((field) => `<th scope="col">${escape(field)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${fields.map((field) => `<td>${escape(Object.hasOwn(row, field) ? text(row[field]) : "—")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`
        : '<p class="owa-business-preview-meta">本次未返回可展示的实例行；是否满足样例请查看回执和诊断。</p>'}` : ""}
      ${object(receipt.rule) ? `<p>规则输入事实：${escape(count(receipt.rule.input_fact_count))} 条 · 规则结论：${escape(count(receipt.rule.result_fact_count))} 条</p>${renderRuleAnswers(receipt.rule)}` : ""}
      <div class="owa-business-preview-sources"><strong>证据来源</strong>${list(receipt.source_refs).length ? `<ul>${list(receipt.source_refs).slice(0, 30).map((ref) => `<li>${escape(ref)}</li>`).join("")}</ul>` : "<p>回执尚未提供证据来源。</p>"}</div>${details}`;
  };
  const render = (report, { selections = new Map(), starting = false, error = "", message = "", uncertain = false } = {}) => {
    const plans = list(report?.plans);
    const ready = report?.status === "AVAILABLE" && validReference(report.payload_file);
    return `${notice}<div class="owa-business-preview-toolbar"><p>选择一个业务能力和样例，核对真实实例、规则结论及来源。</p><button type="button" data-preview-refresh${starting ? " disabled" : ""}>重新读取</button></div>
      ${busy(report?.active_preview?.status) ? `<p role="status">本工程已有试运行正在执行，将继续读取进度。即使草稿已变化或计划已移除，也需等该任务结束后再启动。${escape(report.active_preview.message || "")}</p>` : ""}
      ${error ? `<p role="alert" class="owa-business-preview-error">${escape(error)}</p>` : ""}${message ? `<p role="status">${escape(message)}</p>` : ""}
      ${report?.message ? `<p>${escape(report.message)}</p>` : ""}
      ${report && !ready ? '<p>当前草稿尚未就绪，请先在工程对话中生成或修订 S3 业务计划。</p>' : ""}
      ${report && !plans.length ? '<p class="owa-engineering-empty">尚未生成业务计划。请在工程对话中说明业务问题及可核对的样例，再生成 S3 业务计划。</p>' : ""}
      ${plans.map((plan) => {
        const cases = list(plan.case_ids).filter((id) => typeof id === "string" && id);
        const selected = selectedCase(plan, selections);
        const otherCase = differentCase(plan, selected);
        const actualStatus = planStatus(plan, report);
        const status = uncertain || !ready ? "UNKNOWN"
          : otherCase && !["STALE", "UNKNOWN"].includes(actualStatus) && !busy(actualStatus) ? "NOT_STARTED" : actualStatus;
        const atLimit = exhausted(plan, report, selected);
        const disabled = !ready || starting || uncertain || hasRunning(report) || status === "UNKNOWN" || atLimit;
        return `<section class="owa-business-preview-plan" data-preview-status="${status}"><header><h3>${escape(plan.title || plan.id)}</h3><span role="status" class="owa-business-preview-status">${labels[status]}</span></header>
          ${list(plan.question_examples).length ? `<ul class="owa-business-preview-questions">${list(plan.question_examples).slice(0, 3).map((question) => `<li>${escape(question)}</li>`).join("")}</ul>` : ""}
          <div class="owa-business-preview-controls"><label>验收样例 <select data-preview-case="${escape(plan.id)}"${starting || busy(status) ? " disabled" : ""}>${cases.length ? cases.map((id) => `<option value="${escape(id)}"${id === selected ? " selected" : ""}>${escape(id)}</option>`).join("") : '<option value="">默认样例</option>'}</select></label><button type="button" data-preview-start="${escape(plan.id)}"${disabled ? " disabled" : ""}>${busy(status) ? "试运行中…" : retryable(status) ? "重试所选样例" : "试运行"}</button></div>
          ${status === "STALE" ? '<p role="status">草稿或依据已变化，旧结果不再有效。请对当前草稿重新试运行。</p>' : ""}
          ${status === "UNKNOWN" ? '<p role="alert">当前试运行状态无法确认，请重新读取回执后再操作。</p>' : ""}
          ${otherCase ? `<p>最近回执属于样例 ${escape(plan.receipt.case_id || "未标明")}，所选样例 ${escape(selected)} 暂无当前回执。请试运行所选样例。</p>` : ""}
          ${atLimit ? '<p>相同输入已达到试运行次数上限。请先在工程对话中修订该计划或补齐依据，再试运行。</p>' : ""}
          ${cases.length > 1 ? '<p class="owa-business-preview-meta">切换样例不会改变已有回执；需要试运行所选样例才能核对其结果。</p>' : ""}
          ${!otherCase && status !== "NOT_STARTED" ? renderReceipt(plan.receipt, status) : ""}</section>`;
      }).join("")}${guidance}`;
  };

  const request = async (method, projectId, body = null, signal) => {
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) abort();
    const timeout = window.setTimeout(abort, 35000);
    try {
      const response = await fetch(`/orion-workflow-api/business-preview${method === "GET" ? `?${new URLSearchParams({ project_id: projectId })}` : ""}`, {
        method, credentials: "same-origin", cache: "no-store", signal: controller.signal,
        headers: { Accept: "application/json", ...(body ? { "Content-Type": "application/json", "x-orion-workflow-confirmation": "1" } : {}) },
        ...(body ? { body: JSON.stringify(body) } : {}),
      });
      let result;
      try { result = await response.json(); }
      catch { throw new Error("预览服务返回了无法识别的内容，请重新读取。"); }
      if (!response.ok) throw new Error(typeof result?.detail === "string" ? result.detail : `预览请求失败（HTTP ${response.status}）。`);
      if (!object(result)) throw new Error("预览服务未返回有效回执，请重新读取。");
      return result;
    } catch (error) {
      if (controller.signal.aborted && !signal?.aborted) throw new Error("预览请求超时，请重新读取回执确认运行状态。");
      throw error;
    } finally {
      window.clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
    }
  };

  const mount = (target, projectId, revision, { payloadFile } = {}) => {
    if (!target) return null;
    target.__businessPreview?.dispose();
    const disclosure = target.closest('[data-engineering-disclosure="business-preview"]');
    let selections = new Map(), selectionKey = "";
    let report = null, error = "", message = "", uncertain = false;
    let timer = null, reading = null, disposed = false, visible = false, starting = false, pageHidden = false, generation = 0;
    let getController = null;
    const active = () => !disposed && !pageHidden && target.isConnected && document.visibilityState !== "hidden"
      && !target.closest("[hidden]") && (!disclosure || disclosure.open);
    const draw = () => {
      if (!active()) return;
      target.setAttribute("aria-busy", String(starting || !!reading));
      target.innerHTML = render(report, { selections, starting: starting || pendingStarts.has(projectId), error, message, uncertain });
    };
    const clearTimer = () => { if (timer !== null) window.clearTimeout(timer); timer = null; };
    const pause = () => {
      visible = false;
      clearTimer();
      generation++;
      getController?.abort();
      getController = null;
      reading = null;
    };
    const schedule = () => {
      clearTimer();
      if (active() && !starting && !error && (pendingStarts.has(projectId) || hasRunning(report))) {
        timer = window.setTimeout(() => { timer = null; void read(); }, 3000);
      }
    };
    const read = () => {
      if (!active()) return Promise.resolve(null);
      if (reading) return reading;
      const epoch = ++generation;
      getController = new AbortController();
      const signal = getController.signal;
      error = "";
      reading = Promise.resolve().then(async () => {
        const result = normalizeReport(await request("GET", projectId, null, signal), projectId, revision);
        if (disposed || !active() || generation !== epoch) return null;
        if (payloadFile !== undefined && !sameReference(result.payload_file, payloadFile)) {
          throw new Error("S3 草稿指纹已变化，请刷新工程后重新打开试运行。");
        }
        // A new payload at the same workflow revision still invalidates the
        // previous preview. Re-read once before accepting a result for it.
        const changed = report && !sameReference(report.payload_file, result.payload_file);
        const key = JSON.stringify([projectId, revision, referenceKey(result.payload_file)]);
        if (key !== selectionKey) {
          selectionKey = key;
          selections = selectionStates.get(key) ?? new Map();
          selectionStates.set(key, selections);
          while (selectionStates.size > 8) selectionStates.delete(selectionStates.keys().next().value);
        }
        report = changed ? { ...result, plans: result.plans.map((plan) => ({ ...plan, status: "STALE", receipt: undefined })) } : result;
        uncertain = false;
        if (changed) message = "S3 草稿已更新，旧回执已隐藏。请核对当前计划；已有任务结束后可试运行新草稿。";
        else if (message === "正在读取业务能力试运行状态…") message = "";
        return report;
      }).catch((failure) => {
        if (disposed || !active() || generation !== epoch) return null;
        report = null;
        error = failure.message || "预览读取失败，请重新读取。";
        return null;
      }).finally(() => {
        if (generation !== epoch) return;
        reading = null;
        getController = null;
        draw();
        schedule();
      });
      if (!report) { message = "正在读取业务能力试运行状态…"; draw(); }
      return reading;
    };
    const start = async (planId) => {
      const plan = list(report?.plans).find((item) => item.id === planId);
      if (!active() || starting || pendingStarts.has(projectId) || uncertain || !plan
        || report.status !== "AVAILABLE" || !validReference(report.payload_file)
        || hasRunning(report) || planStatus(plan, report) === "UNKNOWN") return;
      const basis = { ...report.payload_file };
      const caseId = selectedCase(plan, selections);
      if (exhausted(plan, report, caseId)) return;
      if (caseId) selections.set(planId, caseId);
      starting = true;
      message = "正在核对当前草稿并请求试运行…";
      clearTimer();
      // Fence the click with a fresh read, including same-revision draft edits.
      getController?.abort(); generation++; reading = null;
      const task = (async () => {
        const current = await read();
        const currentPlan = list(current?.plans).find((item) => item.id === planId);
        if (!active() || !current || !currentPlan || current.status !== "AVAILABLE") return;
        if (!sameReference(basis, current.payload_file) || (caseId && !list(currentPlan.case_ids).includes(caseId))) {
          message = "计划或草稿已变化，请重新选择当前样例后试运行。"; return;
        }
        if (hasRunning(current) || planStatus(currentPlan, current) === "UNKNOWN" || exhausted(currentPlan, current, caseId)) return;
        await request("POST", projectId, {
          project_id: projectId, expected_revision: Number(revision), payload_file: basis, plan_id: planId,
          ...(caseId ? { case_id: caseId } : {}), retry: !differentCase(currentPlan, caseId) && retryable(planStatus(currentPlan, current)),
        });
        // A POST receipt has no mandatory project/revision/payload envelope.
        // Never display it as a passing result; the authoritative GET fences it.
        if (!active()) return;
        message = "试运行请求已返回，正在读取当前草稿的回执…";
        const observed = await read();
        if (observed && sameReference(basis, observed.payload_file)) {
          const observedPlan = observed.plans.find((item) => item.id === planId) ?? {};
          const status = planStatus(observedPlan, observed);
          if (status === "NOT_STARTED" || status === "UNKNOWN" || differentCase(observedPlan, caseId)) {
            uncertain = true;
            message = "尚未读到此次试运行回执，请重新读取确认；不会自动重复发起。";
          } else { message = ""; }
        }
      })().catch((failure) => {
        if (!active()) return;
        uncertain = true;
        error = `${failure.message || "试运行请求未能确认。"} 请先重新读取回执，再决定是否重试。`;
        message = "";
      }).finally(() => {
        if (pendingStarts.get(projectId) === task) pendingStarts.delete(projectId);
        starting = false;
        draw(); schedule();
      });
      pendingStarts.set(projectId, task);
      draw();
      return task;
    };
    const click = (event) => {
      const button = event.target.closest("[data-preview-refresh], [data-preview-start]");
      if (!button || !target.contains(button) || button.disabled) return;
      if (button.hasAttribute("data-preview-refresh")) { message = ""; void read(); }
      else void start(button.dataset.previewStart);
    };
    const change = (event) => {
      const select = event.target;
      if (select.hasAttribute?.("data-preview-case")) {
        selections.set(select.dataset.previewCase, select.value);
        draw();
        [...(target.querySelectorAll?.("[data-preview-case]") ?? [])]
          .find((item) => item.dataset.previewCase === select.dataset.previewCase)?.focus();
      }
    };
    const sync = () => {
      if (!target.isConnected) { dispose(); return; }
      if (!active()) { if (visible) pause(); return; }
      if (!visible) { visible = true; message = ""; void read(); }
    };
    const pageHide = () => { pageHidden = true; pause(); };
    const pageShow = () => { pageHidden = false; sync(); };
    const observer = new MutationObserver(sync);
    const dispose = () => {
      if (disposed) return;
      disposed = true;
      pause(); observer.disconnect();
      target.removeEventListener("click", click); target.removeEventListener("change", change);
      disclosure?.removeEventListener("toggle", sync);
      document.removeEventListener("visibilitychange", sync);
      window.removeEventListener("pagehide", pageHide); window.removeEventListener("pageshow", pageShow);
    };
    target.addEventListener("click", click); target.addEventListener("change", change);
    disclosure?.addEventListener("toggle", sync);
    document.addEventListener("visibilitychange", sync);
    window.addEventListener("pagehide", pageHide); window.addEventListener("pageshow", pageShow);
    observer.observe(document.documentElement, { subtree: true, childList: true, attributes: true, attributeFilter: ["hidden", "open"] });
    target.__businessPreview = { dispose, refresh: read };
    sync();
    return target.__businessPreview;
  };
  window.__ORION_BUSINESS_PREVIEW__ = Object.freeze({ mount, render });
})();
