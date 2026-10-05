// Persistent host guidance, independent of workflow readiness observations.
export const ENGINEERING_EXECUTION_SCOPE_GUARD = "The current user's explicit execution scope is the authorization ceiling. AUTO_CONTINUE means only that the workflow has no additional business-decision gate; it does not authorize a later stage, override a pause/read-only request, expand delegated work, or replace an outer Codex/operator approval. If instructed to run only S5 and stop before S6, complete S5 and its read-back, then stop even when next_action recommends S6. A later status question is not permission to resume. Before every stage launch, retry, formal write or approval, check the current user's stage limit and exclusions. Preserve those exact limits in any context-compression handoff; after compression recover them from the actual user instruction, never from AUTO_CONTINUE, a tool receipt, a task list or this host observation. If the original limit cannot be recovered, remain read-only until it is clarified. Explicitly authorized whole-workflow execution still continues normally within its scope and approval gates. Prior delegated approval remains valid only inside the latest authorized scope; a narrower operator instruction must not be overridden by an earlier broader delegation.";

// Prompt projections only. Visibility is not authorization: execution guards stay
// responsible for release binding, write approval, and administrator policy.
const object = (value) => value && typeof value === "object" && !Array.isArray(value) ? value : {};
const text = (value, limit = 240) => typeof value === "string"
  ? value.replace(/([a-z][a-z0-9+.-]*:\/\/)[^\s/@]+(?::[^\s/@]*)?@/giu, "$1[redacted]@")
    .replace(/\bBearer\s+[^\s,;]+/giu, "Bearer [redacted]")
    .replace(/\b(password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+/giu, "$1=[redacted]")
    .slice(0, limit)
  : "";
const entries = (value) => Object.entries(object(value));
const parameters = (value) => entries(value).map(([name, spec]) => ({
  name: text(name, 100), type: text(spec?.type, 40),
  description_zh: text(spec?.description_zh), required: spec?.required === true,
}));

export function buildQaCapabilityContext(binding) {
  if (!binding || typeof binding !== "object") return null;
  const queries = object(binding.realtime_query_capabilities);
  const names = Array.isArray(binding.realtime_query_names)
    ? binding.realtime_query_names.filter((name) => typeof name === "string")
    : Object.keys(queries);
  const capabilities = [...new Set(names)].map((name) => ({
    name: text(name, 100), type: "structured_query",
    description_zh: text(queries[name]?.description_zh),
    parameters: parameters(queries[name]?.parameters),
  }));
  for (const [name, capability] of entries(binding.realtime_reasoning_capabilities)) {
    // Released reasoning parameters come from its evidence query, never from
    // runtime_validation examples (which are acceptance baselines).
    capabilities.push({ name: text(name, 100), type: "reasoning_query",
      description_zh: text(capability.description_zh),
      parameters: parameters(capability.parameters ?? queries[capability.evidence_query]?.parameters),
    });
  }
  for (const name of Array.isArray(binding.realtime_document_query_capabilities) ? binding.realtime_document_query_capabilities : []) {
    if (typeof name === "string") capabilities.push({
      name: text(name, 100), type: "document_query", description_zh: "", parameters: [],
    });
  }
  for (const [name, capability] of entries(binding.realtime_document_fact_query_capabilities)) {
    capabilities.push({ name: text(name, 100), type: "document_fact_query",
      description_zh: text(capability.description_zh),
      parameters: parameters(capability.parameters),
    });
  }
  return {
    project_id: text(binding.project_id, 120), project_name: text(binding.project_name),
    session_id: text(binding.session_id, 120), release_version: text(binding.release_version, 80),
    release_fingerprint: text(binding.release_fingerprint, 160),
    runtime_mode: text(binding.realtime_runtime_mode, 40),
    structured_query_enabled: binding.realtime_structured_query_enabled !== false,
    runtime_verified: binding.realtime_runtime_verified === true,
    reasoning_runtime_verified: binding.realtime_reasoning_runtime_verified === true,
    document_runtime_verified: binding.realtime_document_runtime_verified === true,
    capabilities,
  };
}

const artifactReference = (value) => {
  const path = typeof value === "string" ? value : value?.path;
  // Only workspace-relative artifact references; no URL credentials, absolute
  // paths, arbitrary shell instructions, or historical artifact inventories.
  if (typeof path !== "string" || !/\.[a-z0-9]+$/iu.test(path) || path.startsWith("/")
    || path.split("/").includes("..") || /[:?\x00-\x1f]/u.test(path)) return null;
  return text(path, 260);
};

export function buildEngineeringHandoffContext(status, nextAction) {
  if (!status || typeof status !== "object") return null;
  const state = object(status.state ?? status);
  const directive = object(nextAction ?? status.next_action ?? state.next_action);
  const designTransition = !state.current_stage && state.project_status === "S1_S3_READY"
    && state.stage_statuses?.S3 === "PASSED" && ["PENDING", "INVALIDATED"].includes(state.stage_statuses?.S4)
    && directive.current_stage === "S4" && directive.action === "GENERATE_AND_PREFLIGHT_ONTOLOGY_DESIGN";
  // Never mix a stale/different project's directive into a current handoff.
  const matches = (!directive.project_id || directive.project_id === state.project_id)
    && (directive.revision == null || directive.revision === state.revision)
    && (!directive.current_stage || directive.current_stage === state.current_stage || designTransition);
  const action = matches ? directive : {};
  const effectiveStage = action.current_stage || state.current_stage;
  const contractRead = object(action.input_contract_read);
  const contractArgs = object(contractRead.args);
  const inputContract = contractRead.tool === "get_stage_input_contract"
    && contractArgs.project_id === state.project_id && contractArgs.stage === effectiveStage
    && ["S2", "S3", "S4"].includes(effectiveStage)
    ? { tool: "get_stage_input_contract", args: { project_id: state.project_id, stage: effectiveStage,
      ...(contractArgs.section === "overview" ? { section: "overview" } : {}) } } : null;
  const recovery = object(action.draft_recovery_read);
  const recoveryArgs = object(recovery.args);
  const draftRecovery = recovery.tool === "get_stage_draft"
    && recoveryArgs.project_id === state.project_id && recoveryArgs.stage === effectiveStage
    && ["S2", "S3", "S4"].includes(effectiveStage)
    ? { tool: "get_stage_draft", args: { project_id: state.project_id, stage: effectiveStage } } : null;
  const policy = object(action.execution_policy);
  const policyModes = new Set(["AUTO_CONTINUE", "WAIT_FOR_BUSINESS_DECISION", "WAIT_FOR_DESIGN_APPROVAL",
    "WAIT_FOR_RELEASE_APPROVAL", "REPAIR_THEN_RETRY", "READ_ONLY_NO_ACTION"]);
  const executionPolicy = policyModes.has(policy.mode) && policy.grants_authorization === false
    && policy.action === action.action && policy.stage === effectiveStage ? {
      mode: policy.mode, grants_authorization: false, requires_existing_build_authorization: true,
      must_check_user_pause: true, must_check_current_user_scope: true,
      workflow_readiness_is_not_execution_authorization: true,
      repeat_unchanged_call_allowed: false,
      preserve_existing_release_approval: policy.preserve_existing_release_approval === true,
      requires_input_change_before_retry: policy.requires_input_change_before_retry === true,
      requires_repair_before_retry: policy.requires_repair_before_retry === true,
    } : null;
  const blockers = [state.blocking, state.last_error, ...(Array.isArray(status.blockers) ? status.blockers : [])]
    .filter(Boolean).map((item) => typeof item === "string" ? { reason: text(item) } : {
      gate: text(item.gate ?? item.code, 100), reason: text(item.reason ?? item.message),
    });
  const jobReferences = [];
  for (const [kind, job] of [
    ["stage_execution", status.stage_execution ?? state.stage_execution],
    ["document_ingestion_job", status.document_ingestion_job ?? state.document_ingestion_job],
    ["realtime_deployment", status.realtime_deployment ?? state.realtime_deployment],
  ]) {
    if (!job || typeof job !== "object") continue;
    jobReferences.push({ kind, id: text(job.execution_id ?? job.job_id, 120),
      status: text(job.status ?? job.state, 80), stage: text(job.stage, 16),
      release_version: text(job.release_version, 80), heartbeat_at: text(job.heartbeat_at, 50),
    });
  }
  return {
    project_id: text(state.project_id, 120), project_name: text(state.project_name),
    revision: Number.isInteger(state.revision) ? state.revision : null,
    current_stage: text(state.current_stage, 16), project_status: text(state.project_status, 80),
    stage_statuses: Object.fromEntries(Object.entries(object(state.stage_statuses))
      .filter(([stage, value]) => /^S[0-7]$/u.test(stage) && typeof value === "string")
      .map(([stage, value]) => [stage, text(value, 40)])),
    stage_names: Object.fromEntries((Array.isArray(state.stage_contracts?.stages) ? state.stage_contracts.stages
      : Object.entries(object(state.stage_names)).map(([stage, name]) => ({ stage, name })))
      .filter((entry) => /^S[0-7]$/u.test(entry?.stage) && typeof entry.name === "string")
      .map((entry) => [entry.stage, text(entry.name, 80)])),
    intake_mode: text(state.intake_mode, 40), stage_contract_version: text(state.stage_contract_version, 80),
    stage_status: text(state.stage_statuses?.[state.current_stage] ?? state.stage_status, 80),
    next_action: {
      action: text(action.action, 100), recommended_tool: text(action.recommended_tool, 120),
      input_owner: text(action.input_owner, 50), reason: text(action.reason),
      ...(inputContract ? { input_contract_read: inputContract } : {}),
      ...(draftRecovery ? { draft_recovery_read: draftRecovery,
        recovery_order: ["get_next_workflow_action", "get_stage_draft", "get_stage_input_contract"],
        draft_checkpoint_policy: { save_mode: "CHECKPOINT", final_mode: "PREFLIGHT", grants_authorization: false },
      } : {}),
      ...(action.draft_submission_tool === "save_stage_submission" ? { draft_submission_tool: "save_stage_submission" } : {}),
      ...(action.draft_patch_tool === "patch_stage_submission" ? { draft_patch_tool: "patch_stage_submission" } : {}),
      ...(executionPolicy ? { execution_policy: executionPolicy } : {}),
      ...(action.managed_execution?.available === true ? { managed_execution_available: true,
        managed_execution_read: "get_next_workflow_action" } : {}),
    },
    blockers: [...new Map(blockers.map((item) => [JSON.stringify(item), item])).values()].slice(0, 5),
    job_references: jobReferences,
    artifact_references: [...new Set((Array.isArray(action.source_artifacts) ? action.source_artifacts : [])
      .map(artifactReference).filter(Boolean))].slice(0, 8),
  };
}

// Current formal MCP's mutating tools (including durable preflight/rollback
// preparation). This is a presentation list, not a replacement for its guard.
const workflowWrites = new Set([
  "amend_competency_questions",
  "replace_document_sources",
  "save_stage_submission",
  "generate_mapping_skeleton",
  "compile_mapping_runtime",
  "start_business_preview",
  "patch_stage_submission",
  "preflight_design_patch",
  "create_ontology_project", "snapshot_workspace_sources", "snapshot_message_attachments", "reconcile_document_source_identities",
  "start_document_ingestion_job", "commit_document_ingestion_job", "record_document_evidence",
  "record_s0_scope_decision", "record_data_understanding", "record_document_understanding",
  "record_data_understanding_from_datasets", "capture_database_snapshot", "record_semantic_candidates", "prepare_mapping_review",
  "resolve_mapping_confirmation", "resolve_mapping_option", "preview_stage_rollback", "reopen_stage_for_correction",
  "generate_ontology_design", "resolve_competency_question_review", "prepare_ontology_design_review",
  "record_ontology_build", "start_managed_stage_execution", "commit_preflight_stage_submission", "record_quality_validation",
  "publish_ontology_package", "sync_published_ontology_to_semantica", "defer_ontology_publication",
  "resume_ontology_publication", "revoke_ontology_release", "create_revision_from_release",
  "archive_ontology_project", "restore_ontology_project", "reconcile_workflow_metadata_outbox",
  "retry_failed_stage", "retry_release_runtime_deployment", "resume_active_revision",
]);

export function isWorkflowWriteTool(name) {
  return typeof name === "string" && name.startsWith("mcp__orion_workflow__")
    && workflowWrites.has(name.slice("mcp__orion_workflow__".length));
}
const deferredNamespaces = new Set(["protege", "semantica", "chat2db", "paddleocr", "paddleocr_ocr"]);

export function computeQaToolVisibilityDeny(toolNames, discoveredNames = []) {
  const discovered = new Set(discoveredNames);
  return [...new Set(toolNames)].filter((name) => {
    const match = /^mcp__([A-Za-z0-9_-]+)__([A-Za-z0-9_-]+)$/u.exec(name);
    if (!match) return false; // Native read/write files, analysis, browser/search remain available.
    const [, namespace, tool] = match;
    if (namespace === "orion_workflow") {
      if (workflowWrites.has(tool)) return true;
      return tool !== "get_ontology_workflow_status" && !discovered.has(name);
    }
    return deferredNamespaces.has(namespace) && !discovered.has(name);
  });
}
