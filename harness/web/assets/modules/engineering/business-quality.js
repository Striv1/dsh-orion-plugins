(() => {
  if (window.__ORION_BUSINESS_QUALITY__) return;
  const cache = new Map();
  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const list = (value) => Array.isArray(value) ? value : [];
  const labels = { CHECKED: "已执行诊断", REVIEW_REQUIRED: "有问题待核对", NOT_ASSESSED: "尚未评估" };
  const render = (report, stage = "") => {
    const scope = { S3: ["semantics", "questions", "scope"], S6: ["mapping", "scope"] }[stage];
    const sections = list(report.sections).filter((section) => !scope || scope.includes(section.id)).slice(0, 20).map((section) => {
      const issues = list(section.issues);
      const totalIssues = Math.max(issues.length, Number(section.issue_count) || 0);
      const total = Math.max(issues.length, Number(section.issue_count) || 0);
      return `<section class="owa-business-quality-section"><header><h3>${escape(section.title)}</h3><span>${escape(labels[section.status] ?? "查看诊断结果")}</span></header><p>${escape(section.summary)}</p>
        <dl>${list(section.metrics).slice(0, 20).map((metric) => `<div><dt>${escape(metric.label)}</dt><dd>${escape(metric.value)}</dd></div>`).join("")}</dl>
        ${issues.length ? `<ul>${issues.slice(0, 100).map((issue) => `<li><strong>${escape({ ERROR: "需处理", WARNING: "需核对", INFO: "提示" }[issue.severity] ?? "提示")}${issue.subject ? ` · ${escape(issue.subject)}` : ""}</strong><p>${escape(issue.message)}</p>${list(issue.source_refs).length ? `<small>依据：${list(issue.source_refs).slice(0, 8).map((ref) => escape(typeof ref === "string" ? ref : ref?.path ?? ref?.id ?? "来源记录")).join("；")}</small>` : ""}</li>`).join("")}</ul>` : ""}
        ${total > Math.min(issues.length, 100) ? `<p>本项共 ${total} 条问题，当前展示前 ${Math.min(issues.length, 100)} 条。</p>` : ""}</section>`;
    }).join("");
    return `<header class="owa-business-quality-heading"><div><h2>${escape({ S3: "语义与问题依据", S6: "实例与映射" }[stage] ?? "产物核对")}</h2><p>工程第 ${escape(report.revision)} 次变更</p></div><button type="button" data-quality-refresh>重新读取</button></header>
      <p class="owa-business-quality-notice">这是当前工程的诊断视图，不改变阶段状态或审批结果；已执行诊断不等于业务验收通过。</p>
      ${sections || '<p class="owa-engineering-empty">尚无可供诊断的业务建模或映射成果。</p>'}
      ${list(report.limitations).length ? `<aside class="owa-business-quality-notice"><strong>评估范围</strong><ul>${list(report.limitations).slice(0, 20).map((item) => `<li>${escape(item)}</li>`).join("")}</ul></aside>` : ""}`;
  };
  // Stage saves advance revision; auxiliary reports can change independently.
  const snapshotKey = (payload = {}) => JSON.stringify([
    payload.state?.current_stage, payload.state?.stage_statuses,
    payload.state?.artifact_lifecycle, payload.artifacts ?? [],
    payload.stageSummaries ?? {},
  ]);
  const invalidate = (projectId) => {
    for (const key of cache.keys()) {
      if (JSON.parse(key)[0] === projectId) cache.delete(key);
    }
  };
  const mount = async (target, projectId, revision, force = false, snapshot = "", stage = "") => {
    if (!target) return;
    const key = JSON.stringify([projectId, revision, snapshot]);
    const token = {};
    target.__qualityRequest = token;
    target.setAttribute("aria-busy", "true");
    target.innerHTML = '<p role="status">正在读取业务质量诊断…</p>';
    let entry = cache.get(key);
    if (!entry || (force && entry.settled)) {
      entry = { settled: false };
      entry.promise = Promise.resolve().then(() => {
        if (!window.__ORION_ONTOLOGY_API__?.businessQuality) throw new Error("诊断服务暂不可用，请重新读取。");
        return window.__ORION_ONTOLOGY_API__.businessQuality(projectId);
      }).then((report) => {
        if (report?.schema_version !== 1 || report.project_id !== projectId || String(report.revision) !== String(revision)) {
          throw new Error("工程版本已变化或诊断响应不匹配，请刷新工程后重试。");
        }
        return { report };
      }).catch((error) => ({ error: error.message || "诊断读取失败，请重试。" }))
        .finally(() => { entry.settled = true; });
      cache.set(key, entry);
      while (cache.size > 8) cache.delete(cache.keys().next().value);
    }
    const result = await entry.promise;
    if (!target.isConnected || target.__qualityRequest !== token) return;
    target.innerHTML = result.error
      ? `<p role="alert">${escape(result.error)}</p><button type="button" data-quality-refresh>重试</button>`
      : render(result.report, stage);
    target.setAttribute("aria-busy", "false");
    target.querySelector("[data-quality-refresh]")?.addEventListener("click", () => mount(target, projectId, revision, true, snapshot, stage));
  };
  window.__ORION_BUSINESS_QUALITY__ = { render, mount, snapshotKey, invalidate };
})();
