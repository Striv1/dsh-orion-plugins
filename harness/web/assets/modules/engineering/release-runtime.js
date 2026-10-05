(() => {
  if (window.__ORION_RELEASE_RUNTIME__) return;

  // S7 has two independent facts: the immutable package was approved and
  // published, and the runtime (Ontop / document runtime) was verified.
  // The UI must never merge them into one "已发布" claim.
  const READY_STATES = new Set(["ONTOP_READY", "DOCUMENT_RUNTIME_READY"]);
  const FAILED_STATES = new Set([
    "AUTOMATION_DISABLED",
    "WAITING_CONFIGURATION",
    "DEPLOYMENT_FAILED",
    "DOCUMENT_RUNTIME_FAILED",
    "AUTOMATION_FAILED_TO_QUEUE",
  ]);
  const RETRYABLE_PROJECT_STATUSES = new Set(["PACKAGE_READY_RUNTIME_BLOCKED", "RUNTIME_FAILED"]);

  const runtimePhase = (runtimeState) => {
    const state = String(runtimeState ?? "");
    if (state === "NOT_APPLICABLE") return "not-applicable";
    if (READY_STATES.has(state)) return "verified";
    if (FAILED_STATES.has(state)) return "failed";
    return state ? "verifying" : "unknown";
  };

  const PHASE_LABELS = {
    verified: "运行已验证",
    "not-applicable": "无需运行时",
    failed: "运行验证失败",
    verifying: "运行验证中",
    unknown: "运行状态未知",
  };

  const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));

  const releaseBadges = ({ packagePublished, runtimeState, revoked = false }) => {
    if (!packagePublished) return "";
    if (revoked) return '<div class="owa-release-badges"><span class="owa-release-badge is-revoked">已撤回</span></div>';
    const phase = runtimePhase(runtimeState);
    return `<div class="owa-release-badges" role="status"><span class="owa-release-badge is-published">已发布</span><span class="owa-release-badge is-runtime-${phase}">${escape(PHASE_LABELS[phase])}</span></div>`;
  };

  const canRetryRuntime = (projectStatus) => RETRYABLE_PROJECT_STATUSES.has(String(projectStatus ?? ""));

  const runtimeRepairActions = ({ projectStatus, runtimeState, reportUrl, busy = false }) => {
    const report = `<a class="owa-engineering-primary" href="${escape(reportUrl)}" target="_blank" rel="noopener">查看发布包报告</a>`;
    const retry = canRetryRuntime(projectStatus)
      ? `<button type="button" class="owa-engineering-primary" data-engineering-action="retry-runtime-deployment" ${busy ? "disabled" : ""}>${busy ? "正在重新部署…" : "重试运行时部署"}</button>`
      : runtimePhase(runtimeState) === "failed"
        ? '<button type="button" data-engineering-action="conversation">在对话中诊断运行时</button>'
        : "";
    return `<div class="owa-current-actions">${retry}${report}<button type="button" class="is-danger" data-engineering-action="revoke-release">撤回该版本</button></div>`;
  };

  window.__ORION_RELEASE_RUNTIME__ = Object.freeze({
    runtimePhase,
    releaseBadges,
    canRetryRuntime,
    runtimeRepairActions,
    retryTool: "retry_release_runtime_deployment",
  });
})();
