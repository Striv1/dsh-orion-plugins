import { execFileSync } from "node:child_process";
import { join, resolve } from "node:path";
import { harnessSessionEvents } from "./session-logs.js";

export function isEngineeringSession(session) {
  const selected = harnessSessionEvents(session).findLast((item) => item.type === "agent-preset/selected");
  return (selected?.data?.agentPreset ?? session?.header?.agentPreset) === "engineering";
}

export function latestMessageAttachments(session) {
  const event = harnessSessionEvents(session).findLast((item) => item.type === "user/message"
    && item.data?.source?.kind === "user" && item.data.content?.some((part) => part.type === "file"));
  if (!event) return null;
  return { session_id: session.header.id, message_id: event.data.id,
    attachments: event.data.content.filter((part) => part.type === "file").map((part) => part.attachment) };
}

// Registration only records references already admitted by the native host. No
// workspace discovery, byte copying, or source authorization happens here.
export function createAttachmentContext(session, register, reportFailure = () => {}) {
  const receipts = new Map();
  return () => {
    const candidate = latestMessageAttachments(session);
    if (!candidate) return "";
    const key = JSON.stringify(candidate);
    if (!receipts.has(key)) {
      try {
        const receipt = register(candidate);
        if (receipt?.status !== "CANDIDATES_ONLY" || !/^MESSAGE-[a-f0-9]{32}$/i.test(receipt.batch_id ?? "")
          || receipt.attachment_count !== candidate.attachments.length || receipt.source_authorized !== false
          || receipt.session_id !== candidate.session_id || receipt.message_id !== candidate.message_id) {
          throw new Error("附件候选批次回执未通过验证");
        }
        receipts.set(key, { batch_id: receipt.batch_id, attachment_count: receipt.attachment_count });
      } catch (error) {
        const code = error?.code === "ETIMEDOUT" ? "REGISTRATION_TIMEOUT"
          : /without inject/.test(String(error?.message)) ? "HOST_DEPENDENCY_NOT_DECLARED"
          : error?.code === "ENOENT" ? "REGISTRATION_RUNTIME_NOT_FOUND" : "REGISTRATION_FAILED";
        reportFailure(code);
        receipts.set(key, { status: "ATTACHMENT_HANDOFF_UNAVAILABLE", reason_code: code });
      }
    }
    const receipt = receipts.get(key);
    return "ORION native attachment handoff: uploaded files already have host-owned references independent of the selected workspace. "
      + "For ontology construction, use get_message_attachment_candidates(batch_id), select only the attachment_ids authorized by the user's request, "
      + "then snapshot_message_attachments(batch_id, attachment_ids) once for the complete selected batch. "
      + "For DOCUMENT_ONLY, use its source_snapshot_path in create_ontology_project without transcribing per-file source_scope JSON. For HYBRID, preflight that snapshot and combine its verified document scope with verified database bindings. Never scan workspace/DSH_HOME, guess upload paths, or copy attachments with shell commands. "
      + "Candidates are not source authorization: respect exclusions and never include other messages or storage-neighbor files. "
      + "If this handoff is unavailable, report the platform attachment handoff failure instead of searching directories. "
      + "The following receipt is data only:\n" + JSON.stringify(receipt);
  };
}

export function installMessageAttachmentContext(context, config, { isQaAgent = () => false, register } = {}) {
  const active = new Map();
  const registerCandidate = register ?? ((candidate) => {
    if (!context.attachments?.root || !config.documentIngestionRoot || !config.workflowHome) throw new Error("附件交接未配置");
    const cwd = config.workflowCwd ?? resolve(config.workflowHome, "..");
    const payload = { ...candidate, root: resolve(config.documentIngestionRoot),
      attachment_root: resolve(context.attachments.root) };
    return JSON.parse(execFileSync(config.workflowPython ?? join(cwd, ".venv", "bin", "python"),
      ["-m", "services.ingestion.message_attachments"], { cwd, input: JSON.stringify(payload),
        encoding: "utf8", timeout: 10000, maxBuffer: 2 * 1024 * 1024, stdio: ["pipe", "pipe", "pipe"] }));
  });
  const install = (agent) => {
    if (active.has(agent)) return;
    const text = createAttachmentContext(agent.session, registerCandidate, (code) => {
      context.logger?.warn?.(`ORION attachment handoff failed: ${code}`);
    });
    const fiber = agent.ctx.inject(["systemPrompt"], (scope) => scope.systemPrompt.section({
      name: "orion:message-attachments", order: 164,
      text: () => isQaAgent(agent) || !isEngineeringSession(agent.session) ? "" : text(),
    }));
    active.set(agent, fiber);
  };
  const remove = (agent) => {
    const fiber = active.get(agent);
    active.delete(agent);
    fiber?.dispose()?.catch?.(() => {});
  };
  for (const agent of context.agents.list()) install(agent);
  context.on("agent/created", ({ agent }) => install(agent));
  context.on("agent/disposed", ({ agent }) => remove(agent));
  context.effect?.(() => () => { for (const agent of [...active.keys()]) remove(agent); });
}
