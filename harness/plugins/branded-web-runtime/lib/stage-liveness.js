// Read-only liveness classification for RUNNING stages in the project index.
// Mirrors services/ontology_engineering/stage_liveness.py without the kernel
// owner-lock probe (Node has no non-blocking flock). A modern runner renews
// every 15 seconds; a stale heartbeat makes an unexpired lease suspect in the
// project list. The Python status payload probes the lock for recovery decisions.
import { join } from "node:path";
import { readJson } from "./runtime-files.js";

export const STAGE_LIVENESS_IDLE_MS = 6 * 60 * 60 * 1000;
export const STAGE_LIVENESS_HEARTBEAT_GRACE_MS = 60 * 1000;

const parse = (value) => {
  const time = Date.parse(String(value ?? ""));
  return Number.isFinite(time) ? time : null;
};

export const stageExecutionPath = (projectRoot, stage) =>
  join(projectRoot, ".stage-executions", `${stage}.json`);

export function classifyStageLiveness(state, execution, { now = Date.now(), idleMs = STAGE_LIVENESS_IDLE_MS } = {}) {
  const stage = String(state?.current_stage ?? "").toUpperCase() || null;
  const status = String(state?.stage_statuses?.[stage] ?? "").toUpperCase();
  const result = { schema_version: 1, stage, state: "NOT_RUNNING", evidence: null, idle_seconds: null };
  if (!stage || status !== "RUNNING") return result;
  if (String(state?.project_status ?? "").toUpperCase() === "ARCHIVED") return { ...result, evidence: "ARCHIVED" };
  const executionStatus = String(execution?.status ?? "").toUpperCase();
  const touches = [parse(state?.updated_at), parse(execution?.heartbeat_at)].filter((item) => item !== null);
  const lastTouch = touches.length ? Math.max(...touches) : null;
  if (lastTouch !== null) {
    result.idle_seconds = Math.max(0, Math.floor((now - lastTouch) / 1000));
    result.last_activity_at = new Date(lastTouch).toISOString();
  }
  const expiry = parse(execution?.lease_expires_at);
  const heartbeat = parse(execution?.heartbeat_at);
  if (executionStatus === "RUNNING" && execution?.kernel_lock_version === 1 && execution?.execution_id &&
      heartbeat !== null && now - heartbeat > STAGE_LIVENESS_HEARTBEAT_GRACE_MS) {
    return { ...result, state: "SUSPECTED_INTERRUPTED", evidence: "HEARTBEAT_STALE",
      execution_id: execution.execution_id };
  }
  if (executionStatus === "RUNNING" && expiry !== null && expiry > now) {
    return { ...result, state: "RUNNING_ACTIVE", evidence: "LEASE_UNEXPIRED" };
  }
  if (execution?.execution_id && executionStatus === "INTERRUPTED") {
    return { ...result, state: "SUSPECTED_INTERRUPTED", evidence: "EXECUTION_INTERRUPTED" };
  }
  if (lastTouch === null || now - lastTouch >= idleMs) {
    return { ...result, state: "SUSPECTED_INTERRUPTED", evidence: "NO_ACTIVITY" };
  }
  return { ...result, state: "RUNNING_IDLE", evidence: "RECENT_ACTIVITY" };
}

export async function loadStageLiveness(projectRoot, state, options = {}) {
  const stage = String(state?.current_stage ?? "").toUpperCase();
  const execution = /^S[0-7]$/.test(stage)
    ? await readJson(stageExecutionPath(projectRoot, stage), null)
    : null;
  return classifyStageLiveness(state, execution, options);
}
