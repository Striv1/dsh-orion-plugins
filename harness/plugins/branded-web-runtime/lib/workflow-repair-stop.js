import { harnessSessionEvents } from "./session-logs.js";

const repairTools = new Set(["preflight_stage_submission", "save_stage_submission", "patch_stage_submission", "compile_mapping_runtime"]);
const previewTools = new Set(["get_business_preview", "start_business_preview"]);
const prefix = "ORION_AUTO_REPAIR_STOP:";
const reasons = {
  REPAIR_NOT_CONVERGING: {
    label: "自动修订未收敛，执行已停止",
    recovery: "已保留草稿与编译/预检诊断；请由平台维护核对字段合同或执行条件，修复后明确恢复。正式阶段与审批未改变。",
  },
  PREVIEW_WORKER_ERROR: {
    label: "平台试运行后台任务异常，执行已停止",
    recovery: "已保留草稿与后台诊断；请由平台维护恢复。不要据此改写业务规则、映射或预期答案，也不要自动重复试运行。正式阶段与审批未改变。",
  },
};
const object = value => value && typeof value === "object" && !Array.isArray(value);
function callArguments(value) {
  try { return typeof value === "string" ? JSON.parse(value) : value; }
  catch { return null; }
}
function workerFailure(receipt, binding) {
  return receipt?.status === "FAILED" && receipt.project_id === binding.project_id
    && receipt.revision === binding.revision && receipt.stage === "S3"
    && receipt.formal_state_changed !== true
    && Array.isArray(receipt.diagnostics) && receipt.diagnostics.some(item => item?.code === "PREVIEW_WORKER_ERROR"
      && item.owner === "PLATFORM" && item.automatic_repair_allowed === false);
}

// Only a paired, current-turn Workflow result can stop its own agent. Never
// inspect user/model prose, artifact contents, nested arbitrary errors or replay.
export function workflowRepairStopCode(policy, observation) {
  const binding = policy.handoff, receipt = observation?.receipt;
  const args = callArguments(observation?.call?.arguments);
  if (policy.replaying || policy.disposed || policy.requiresStatusRefresh || observation?.kind !== "success"
    || observation.invalidReceipt || observation.order < policy.lastObservationOrder
    || !binding?.project_id || !Number.isSafeInteger(binding.revision) || !object(receipt) || !object(args)
    || args.project_id !== binding.project_id || receipt.project_id !== binding.project_id
    || receipt.stage !== policy.currentStage || receipt.formal_state_changed === true
    || (Object.hasOwn(receipt, "revision") && receipt.revision !== binding.revision)) return null;
  const name = observation.toolName?.startsWith("mcp__orion_workflow__")
    ? observation.toolName.slice("mcp__orion_workflow__".length) : "";
  if (repairTools.has(name)) {
    const stop = receipt.automatic_repair_stop;
    const compilation = name === "compile_mapping_runtime";
    const failed = compilation
      ? policy.currentStage === "S3" && receipt.status === "BUSINESS_PLAN_REQUIRES_REPAIR"
        && receipt.revision === binding.revision && receipt.formal_state_changed === false
        && receipt.draft_saved === false && stop?.revision === binding.revision
      : receipt.status === "FAILED" && args.stage === policy.currentStage;
    return failed && args.expected_revision === binding.revision
      && stop?.code === "REPAIR_NOT_CONVERGING" && stop.project_id === binding.project_id
      && (!Object.hasOwn(stop, "revision") || stop.revision === binding.revision)
      && stop.stage === policy.currentStage && stop.formal_state_changed === false ? stop.code : null;
  }
  if (!previewTools.has(name) || policy.currentStage !== "S3" || receipt.revision !== binding.revision
    || (Object.hasOwn(args, "expected_revision") && args.expected_revision !== binding.revision)) return null;
  if (workerFailure(receipt, binding)) return "PREVIEW_WORKER_ERROR";
  if (name !== "get_business_preview" || receipt.status !== "AVAILABLE" || !Array.isArray(receipt.plans)) return null;
  return receipt.plans.some(plan => plan?.status === "FAILED" && plan.id === plan.receipt?.plan_id
    && workerFailure(plan.receipt, binding)
    && plan.receipt.payload_file?.file_name === receipt.payload_file?.file_name
    && plan.receipt.payload_file?.sha256 === receipt.payload_file?.sha256) ? "PREVIEW_WORKER_ERROR" : null;
}

export function stopWorkflowRepair(policy, observation, warn = () => {}) {
  const code = workflowRepairStopCode(policy, observation);
  if (!code || policy.agent?.status !== "running") return false;
  const lifecycle = harnessSessionEvents(policy.agent.session).findLast(event => ["turn/start", "turn/end"].includes(event.type));
  const turn = lifecycle?.data?.turn;
  if (lifecycle?.type !== "turn/start" || !Number.isSafeInteger(turn) || observation.call?.turn !== turn
    || observation.call.resultTurn !== turn || policy.repairStoppedTurn === turn) return false;
  if (typeof policy.agent.cancel !== "function") {
    warn(`${code}：当前 Harness 缺少原生 agent.cancel，未执行自动停止。`);
    return false;
  }
  const reason = `${prefix}${code}；${reasons[code].label}。${reasons[code].recovery}`;
  try {
    // keepInbox avoids both discarding user input and reentrant session writes
    // during tool/result publication. The native loop owns abort/turn/end and
    // its goal driver disarms automatic continuation on that aborted boundary.
    policy.agent.cancel({ kind: "hook", reason }, { keepInbox: true });
    policy.repairStoppedTurn = turn;
    return true;
  } catch {
    warn(`${code}：原生 agent.cancel 调用失败，未确认自动停止。`);
    return false;
  }
}

// The stock UI does not render arbitrary hook reasons as an error card. Keep
// the explanation visible in the existing native progress projection instead.
export function workflowRepairInterruption(event) {
  const cause = event?.data?.reason;
  if (event?.type !== "turn/end" || cause?.kind !== "aborted" || cause.reason?.kind !== "hook") return null;
  for (const [code, detail] of Object.entries(reasons)) {
    if (cause.reason.reason?.startsWith(`${prefix}${code}；`)) return { status: "aborted", ...detail };
  }
  return null;
}
