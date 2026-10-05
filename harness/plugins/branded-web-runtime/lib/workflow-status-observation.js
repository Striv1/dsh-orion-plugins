import { buildEngineeringHandoffContext, isWorkflowWriteTool } from "./agent-mode-context.js";
import { parseWorkflowStateReceipt, WORKFLOW_STATE_RECEIPT_PREFIX } from "./workflow-state-receipt.js";

const statusTools = new Set(["get_ontology_workflow_status", "get_next_workflow_action"]
  .map(name => `mcp__orion_workflow__${name}`));
// These writes affect drafts/preview receipts, never the formal stage. They
// remain write tools for all execution and approval guards.
export const WORKFLOW_DRAFT_OBSERVATION_TOOLS = ["compile_mapping_runtime", "generate_mapping_skeleton",
  "save_stage_submission", "patch_stage_submission", "preflight_stage_submission", "start_business_preview", "get_stage_draft", "get_business_preview"]
  .map(name => `mcp__orion_workflow__${name}`);
const draftTools = new Set(WORKFLOW_DRAFT_OBSERVATION_TOOLS);
const object = value => value && typeof value === "object" && !Array.isArray(value);
const receiptKeys = ["project_id", "revision", "current_stage", "project_status", "stage_statuses", "formal_state_changed"];
const receiptObject = candidate => {
  if (!object(candidate)) return null;
  // An identified operation envelope owns its receipt. Its data/result may be
  // an artifact's contents, which must never become a workflow observation.
  return [candidate, candidate.structuredContent, candidate.structured_content, candidate.result, candidate.data]
    .find(value => object(value) && receiptKeys.some(key => Object.hasOwn(value, key))) ?? null;
};
const jsonCandidates = source => {
  const candidates = [source];
  const fenced = /^```(?:json)?\s*([\s\S]*?)\s*```$/iu.exec(source);
  if (fenced) candidates.push(fenced[1]);
  for (const match of source.matchAll(/\n[\t ]*(?=\{)/gu)) {
    candidates.push(source.slice(match.index + match[0].length).replace(/\s*```\s*$/u, ""));
  }
  return candidates;
};

export const workflowStatusFromObject = (candidate) => {
  const value = receiptObject(candidate);
  if (!value || !["current_stage", "stage_statuses", "project_status"].some(key => Object.hasOwn(value, key))) return null;
  const projectId = typeof value.project_id === "string" ? value.project_id.trim() : "";
  const projectStatus = typeof value.project_status === "string" ? value.project_status.trim().toUpperCase() : null;
  const rawStage = typeof value.current_stage === "string" ? value.current_stage.trim().toUpperCase() : null;
  const currentStage = /^S[0-7]$/u.test(rawStage ?? "") ? rawStage
    : projectStatus === "PUBLISHED" ? "S7"
    : projectStatus === "S1_S3_READY" && value.current_stage === null
      && ["S0", "S1", "S2", "S3"].every(stage => ["PASSED", "SKIPPED", "NOT_APPLICABLE"].includes(value.stage_statuses?.[stage]))
      ? "S4" : null;
  return { currentStage, projectId: projectId || null, projectStatus,
    handoff: buildEngineeringHandoffContext(value, value.action ? value : undefined) };
};

const workflowStatusFromJsonText = (text) => {
  let source = String(text ?? "").trim();
  if (!source) return null;
  let transportReceipt = null;
  if (source.startsWith(WORKFLOW_STATE_RECEIPT_PREFIX)) {
    transportReceipt = parseWorkflowStateReceipt(source);
    if (!transportReceipt) return null;
    source = source.slice(source.indexOf("\n") < 0 ? source.length : source.indexOf("\n") + 1).trim();
  }
  // Harness may flatten MCP TextContent blocks into one text value. The final
  // standalone JSON receipt retains identity/revision even when its preceding
  // human summary changes wording. Parse only complete JSON suffixes.
  for (const candidate of jsonCandidates(source)) {
    try {
      const parsed = workflowStatusFromObject(JSON.parse(candidate));
      if (parsed) {
        if (transportReceipt && (parsed.projectId !== transportReceipt.project_id
          || parsed.currentStage !== (transportReceipt.current_stage ?? (transportReceipt.project_status === "S1_S3_READY" ? "S4" : "S7"))
          || parsed.handoff?.revision !== transportReceipt.revision)) return null;
        return parsed;
      }
    } catch { /* Historical summary-only results retain their conservative path. */ }
  }
  return workflowStatusFromObject(transportReceipt);
};

const workflowStatusFromText = (text) => {
  const source = String(text ?? "").trim();
  if (!source) return null;
  const structured = workflowStatusFromJsonText(source);
  if (structured) return structured;
  const projectId = /(?:项目|project(?:_id)?)\s*[：:]\s*(?:`|\*\*)?([^`*\n]+)(?:`|\*\*)?/iu
    .exec(source)?.[1]?.trim() ?? null;
  const projectStatus = /(?:项目状态|project(?:_status)?)\s*[：:]\s*(?:`|\*\*)?([A-Z0-9_]+)(?:`|\*\*)?/iu
    .exec(source)?.[1]?.trim().toUpperCase() ?? null;
  const rawStage = /(?:当前阶段|current(?:_stage|\s+stage))\s*[：:]\s*(?:`|\*\*)?(S[0-7](?![A-Z0-9_-])|已发布|PUBLISHED)(?:`|\*\*)?/iu
    .exec(source)?.[1]?.trim().toUpperCase() ?? null;
  const currentStage = /^S[0-7]$/u.test(rawStage ?? "")
    ? rawStage
    : rawStage === "已发布" || rawStage === "PUBLISHED" || projectStatus === "PUBLISHED"
      ? "S7"
      : null;
  if (!currentStage && !projectStatus && !projectId) return null;
  return { currentStage, projectId, projectStatus };
};

export const workflowStatusFromContent = (content) => {
  if (!Array.isArray(content)) return null;
  // The formal MCP sends a Markdown summary followed by a standalone JSON
  // block. Parse the JSON block first so revision/jobs are not lost to Markdown.
  for (const block of content) {
    if (block?.type !== "text" || typeof block.text !== "string") continue;
    const parsed = workflowStatusFromJsonText(block.text);
    if (parsed) return parsed;
  }
  const textContent = content
    .filter((block) => block?.type === "text" && typeof block.text === "string")
    .map((block) => block.text)
    .join("\n");
  return workflowStatusFromText(textContent);
};

function workflowReceiptFromResult(result) {
  const structured = receiptObject(result?.structuredContent);
  if (structured) return structured;
  for (const block of result?.content ?? []) {
    if (block?.type !== "text" || typeof block.text !== "string") continue;
    let source = block.text.trim();
    if (source.startsWith(WORKFLOW_STATE_RECEIPT_PREFIX)) {
      // A compact transport receipt belongs to the formal-state path. Do not
      // trust an invalid header's trailing body as a no-change declaration.
      if (!parseWorkflowStateReceipt(source)) return null;
      source = source.slice(source.indexOf("\n") < 0 ? source.length : source.indexOf("\n") + 1).trim();
    }
    for (const candidate of jsonCandidates(source)) {
      try {
        const receipt = receiptObject(JSON.parse(candidate));
        if (receipt) return receipt;
      } catch { /* Incomplete bodies provide no structured no-change evidence. */ }
    }
  }
  return null;
}

export function workflowObservationFromResult(toolName, result, order, call = {}) {
  if (result?.isError === true) return statusTools.has(toolName) ? { kind: "failure", toolName, order } : null;
  let receipt = workflowReceiptFromResult(result);
  for (const block of result?.content ?? []) {
    if (block?.type !== "text" || typeof block.text !== "string" || !block.text.trim().startsWith(WORKFLOW_STATE_RECEIPT_PREFIX)) continue;
    const transport = parseWorkflowStateReceipt(block.text.trim());
    if (!transport || receiptConflicts(transport, receipt)) {
      return { kind: "success", toolName, order, status: null, invalidReceipt: true };
    }
    receipt = { ...transport, ...receipt };
  }
  return { kind: "success", toolName, order, receipt, call,
    status: workflowStatusFromObject(result?.structuredContent) ?? workflowStatusFromContent(result?.content ?? []) };
}

function receiptConflicts(previous, receipt) {
  if (!previous || !receipt) return false;
  for (const key of ["project_id", "revision", "current_stage", "project_status"]) {
    if (!Object.hasOwn(receipt, key)) continue;
    if (key === "revision" && !Number.isInteger(previous.revision)) continue;
    if (key === "project_id" && !previous.project_id) continue;
    // The handoff represents a formally null terminal/transition stage as "".
    if (key === "current_stage" && (receipt[key] ?? "") === (previous[key] ?? "")) continue;
    if (receipt[key] !== previous[key]) return true;
  }
  if (Object.hasOwn(receipt, "stage_statuses")) {
    if (!object(receipt.stage_statuses)) return true;
    for (const [stage, value] of Object.entries(receipt.stage_statuses)) {
      if (Object.hasOwn(previous.stage_statuses ?? {}, stage) && previous.stage_statuses[stage] !== value) return true;
    }
  }
  return false;
}

export function workflowHandoffIdentityMatches(previous, next) {
  return Boolean(previous && next && Number.isInteger(next.revision)
    && previous.project_id === next.project_id && previous.revision === next.revision
    && previous.current_stage === next.current_stage && !receiptConflicts(previous, next));
}

// Ignore carries no authority: it cannot bind a project, clear a refresh gate,
// change tasks/actions, or supersede an in-flight formal observation's order.
export function workflowObservationDisposition(policy, observation) {
  if (observation?.invalidReceipt) return "refresh";
  if (observation?.kind !== "success" || !observation.toolName || statusTools.has(observation.toolName)) return "apply";
  const readOnly = !isWorkflowWriteTool(observation.toolName);
  if (!readOnly && !draftTools.has(observation.toolName)) return "apply";
  const { status, receipt } = observation;
  const previous = policy.handoff ?? { project_id: policy.projectId, current_stage: policy.currentStage };
  // The human summary's UNKNOWN/default empty stage is presentation, not fact.
  const facts = receipt ?? {
    ...(status?.projectId ? { project_id: status.projectId } : {}),
    ...(status?.currentStage ? { current_stage: status.currentStage } : {}),
    ...(status?.projectStatus && status.projectStatus !== "UNKNOWN" ? { project_status: status.projectStatus } : {}),
  };
  if (receipt?.formal_state_changed === true || receiptConflicts(previous, facts)) return "refresh";
  if (readOnly) return "ignore";
  // A write's incomplete result is safe to retain only with an explicit
  // unchanged-state receipt at the last known project/revision. PREFLIGHT
  // save/patch results use writes_performed=false and the paired call revision.
  let args;
  try { args = typeof observation.call?.arguments === "string" ? JSON.parse(observation.call.arguments) : observation.call?.arguments; }
  catch { args = null; }
  const revision = Object.hasOwn(receipt ?? {}, "revision") ? receipt.revision
    : args?.project_id === policy.projectId ? args.expected_revision : null;
  return (receipt?.formal_state_changed === false || receipt?.writes_performed === false)
    && receipt.project_id === policy.projectId && Number.isInteger(revision)
    && revision === policy.handoff?.revision ? "ignore" : "refresh";
}
