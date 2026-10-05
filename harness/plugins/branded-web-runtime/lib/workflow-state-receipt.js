export const WORKFLOW_STATE_RECEIPT_PREFIX = 'ORION_WORKFLOW_RECEIPT_V1 ';
const stages = Array.from({ length: 8 }, (_, index) => `S${index}`);
const boundedText = (value, limit) => typeof value === 'string' && value.trim().length > 0
  && value.length <= limit && !/[\r\n\u0000]/u.test(value);

// Only an exact leading transport receipt is eligible. Never interpret a
// matching string inside a source document or a truncated JSON body as state.
export function parseWorkflowStateReceipt(text) {
  if (typeof text !== 'string' || !text.startsWith(WORKFLOW_STATE_RECEIPT_PREFIX)) return null;
  const line = text.split('\n', 1)[0];
  if (line.length > 4096) return null;
  let value;
  try { value = JSON.parse(line.slice(WORKFLOW_STATE_RECEIPT_PREFIX.length)); }
  catch { return null; }
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || !boundedText(value.project_id, 120) || !Number.isSafeInteger(value.revision) || value.revision < 0
    || !(stages.includes(value.current_stage) || (value.current_stage === null && ['PUBLISHED', 'S1_S3_READY'].includes(value.project_status)))
    || !boundedText(value.project_status, 80)) return null;
  const statuses = value.stage_statuses;
  if (!statuses || typeof statuses !== 'object' || Array.isArray(statuses)
    || Object.keys(statuses).length !== stages.length
    || !stages.every(stage => Object.hasOwn(statuses, stage) && boundedText(statuses[stage], 80))) return null;
  if (value.project_name !== undefined && !boundedText(value.project_name, 200)) return null;
  if (value.current_stage === null) {
    const required = value.project_status === 'PUBLISHED' ? stages : stages.slice(0, 4);
    if (!required.every(stage => ['PASSED', 'SKIPPED', 'NOT_APPLICABLE'].includes(statuses[stage]))) return null;
  }
  if (value.stage_names !== undefined && (!value.stage_names || typeof value.stage_names !== 'object'
    || Array.isArray(value.stage_names) || !Object.entries(value.stage_names)
      .every(([stage, name]) => stages.includes(stage) && boundedText(name, 120)))) return null;
  // Copy a narrow receipt; next_action, approval fields and arbitrary nested
  // payloads cannot acquire meaning through this progress-only transport.
  return {
    project_id: value.project_id, revision: value.revision, current_stage: value.current_stage,
    project_status: value.project_status, stage_statuses: { ...statuses },
    ...(value.project_name !== undefined ? { project_name: value.project_name } : {}),
    ...(value.stage_names !== undefined ? { stage_names: { ...value.stage_names } } : {}),
  };
}
