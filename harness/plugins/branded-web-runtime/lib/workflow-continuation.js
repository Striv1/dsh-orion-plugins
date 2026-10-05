import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname } from "node:path";
import { harnessSessionEvents, toolResultBlock } from "./session-logs.js";

const CREATE_TOOL = "mcp__orion_workflow__create_ontology_project";
const NOTICE_PLUGIN = "orion-workflow-continuation";
// Session format V4 (Harness 0.1.7-rc.2) rejects the retired kind:"plugin"
// wrapper and migrates plugin X to the producer-owned kind "plugin:X". V3
// (0.1.6-alpha.2) keeps the wrapper. Choose by the live session's format.
export function continuationNoticeSource(session, summary) {
  return Number(session?.header?.version) >= 4
    ? { kind: `plugin:${NOTICE_PLUGIN}`, form: "notice", summary }
    : { kind: "plugin", plugin: NOTICE_PLUGIN, form: "notice", summary };
}
export const isContinuationNoticeSource = (source) => source?.form === "notice" && (
  (source.kind === "plugin" && source.plugin === NOTICE_PLUGIN) || source.kind === `plugin:${NOTICE_PLUGIN}`);
const SESSION_ID = /^session-[a-f0-9-]{36}$/;
const PROJECT_ID = /^[a-z][a-z0-9-]{2,100}$/;
export const CONTINUATION_ACTIONS = new Set([
  "record_s0_scope_decision", "resolve_mapping_option", "resolve_competency_question_review",
  "retry_failed_stage", "retry_release_runtime_deployment", "resume_active_revision", "resume_ontology_publication",
  "reopen_stage_for_correction", "publish_ontology_package",
]);

// A mention, status lookup, or recent conversation is not ownership evidence.
export function creationBindingFromEvents(sessionId, events, projectId) {
  if (!SESSION_ID.test(sessionId) || !PROJECT_ID.test(projectId)) return null;
  const calls = new Set();
  for (const event of events) {
    if (event.type === "tool/call" && event.data?.name === CREATE_TOOL) {
      calls.add(event.data.callId ?? event.data.id);
    }
    if (event.type !== "tool/result") continue;
    const block = toolResultBlock(event);
    if (!calls.has(block?.toolCallId)) continue;
    calls.delete(block.toolCallId);
    if (event.data?.error || block.isError === true) continue;
    const text = (block.content ?? []).filter((item) => item.type === "text").map((item) => item.text).join("\n");
    const returned = /(?:项目|project_id)["`]*\s*[：:]\s*[`"*]*([a-z][a-z0-9-]{2,100})/u.exec(text)?.[1];
    if (returned === projectId) return {
      session_id: sessionId, project_id: projectId, source: "verified-create-tool-result",
      tool_call_id: block.toolCallId, result_sequence: event.seq,
    };
  }
  return null;
}

const messageObserved = (events, id) => events.some((event) => (
  event.type === "user/message" && (event.data?.id ?? event.data?.message?.id) === id
) || (
  event.type === "agent/inbox/spliced" && event.data?.inserted?.some((message) => message.id === id)
));

// Native v2 cancellation evidence (also emitted by the legacy runtime), never
// inferred from model text, status=idle, or a workbench approval notification.
export const isUserCancellation = (event) => event?.type === "turn/end"
  && event.data?.reason?.kind === "aborted" && event.data.reason.reason?.kind === "user";

export function continuationCancellation(events, record) {
  const index = events.findLastIndex(isUserCancellation);
  if (index < 0) return null;
  const cancellation = events[index];
  const cancelledAt = Number(cancellation.time);
  const eventAt = Date.parse(record.workflow_event_at ?? "");
  if (!Number.isFinite(cancelledAt) || !Number.isFinite(eventAt) || eventAt <= cancelledAt) return {
    status: "CANCELLED_USER",
    detail: "用户已停止原会话；停止前的工作台续跑通知已取消，重试不会重新启动模型。",
  };
  // A later source.kind=user message may only ask for status. Neither it nor
  // an ordinary approval proves permission to resume; the persona must handle
  // that new intent in the user's own turn. Only a fresh, audited explicit
  // workbench resume action can authorize this background wake.
  const explicitResume = ["resume_active_revision", "resume_ontology_publication"].includes(record.action)
    && typeof record.recorded_actor === "string" && record.recorded_actor.trim()
    && record.recorded_actor !== "ORION_WORKFLOW";
  if (explicitResume) return null;
  return {
    status: "WAITING_USER_RESUME",
    detail: "决定已保存；原会话已由用户停止。普通确认、状态询问和衔接重试不代表恢复授权，请在原会话明确继续或使用适用的正式恢复操作；本通知未启动模型。",
  };
}

// Host cancel intentionally keeps the inbox. Remove only this plugin's stale
// notices synchronously at the native cancellation event, before any later wake.
export function discardCancelledWorkflowNotices(agent, event) {
  if (!isUserCancellation(event)) return [];
  const inbox = agent?.inbox;
  if (!inbox || typeof inbox.remove !== "function") return [];
  const messages = [...(inbox.nextTurn ?? []), ...(inbox.nextStep ?? [])];
  const events = harnessSessionEvents(agent.session);
  const cancellationIndex = events.findLastIndex((item) => isUserCancellation(item)
    && (item === event || item.seq === event.seq));
  return messages.filter((message) => isContinuationNoticeSource(message.source)
    && (cancellationIndex < 0 || events.findLastIndex((item) => item.type === "agent/inbox/spliced"
      && item.data?.inserted?.some((inserted) => inserted.id === message.id)) <= cancellationIndex))
    .flatMap((message) => inbox.remove(message.id) ? [message.id] : []);
}

const receipt = (record, status = record.status) => ({
  status, project_id: record.project_id, event_id: record.event_id,
  session_id: record.binding?.session_id ?? null, message_id: record.message?.id ?? null,
  detail: record.detail, retryable: ["RETRYABLE", "UNBOUND"].includes(status),
});

export function createWorkflowContinuation({ file, findBinding, resolveAgent, loadDashboard, readWorkflowEvent }) {
  let chain = Promise.resolve();
  let state;
  const serialized = (operation) => {
    const pending = chain.then(operation);
    chain = pending.catch(() => {});
    return pending;
  };
  const load = async () => {
    if (state) return;
    try { state = JSON.parse(await readFile(file, "utf8")); }
    catch (error) {
      if (error.code !== "ENOENT") throw error;
      state = { version: 1, deliveries: {} };
    }
    if (state.version !== 1 || !state.deliveries || typeof state.deliveries !== "object" || Array.isArray(state.deliveries)) {
      state = null;
      throw new Error("续跑回执文件格式无效，未发送模型消息。");
    }
  };
  const save = async () => {
    await mkdir(dirname(file), { recursive: true });
    const temporary = `${file}.${process.pid}.${randomUUID()}.tmp`;
    await writeFile(temporary, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
    await rename(temporary, file);
  };
  const deliver = async (record) => {
    if (record.status === "CANCELLED_USER") return receipt(record);
    try {
      const event = await readWorkflowEvent(record.project_id, record.event_id);
      if (!event || event.event_hash !== record.event_hash) throw new Error("原工作流事件无法回读，未发送续跑通知。");
      record.workflow_event_at ??= event.at;
      // Re-read even delivered records: QUEUED is transport evidence, not an
      // authorization to survive a later native user cancellation.
      if (record.status === "QUEUED" && record.binding?.session_id) {
        const found = await resolveAgent(record.binding.session_id);
        if (!found?.agent || found.error || found.agent.session?.header?.id !== record.binding.session_id) {
          throw new Error("原会话暂不可读，无法核对用户停止记录。");
        }
        const stopped = continuationCancellation(harnessSessionEvents(found.agent.session), record);
        if (!stopped) return receipt(record, "ALREADY_QUEUED");
        if (record.message?.id) found.agent.inbox?.remove?.(record.message.id);
        Object.assign(record, stopped, { updated_at: new Date().toISOString() });
        await save();
        return receipt(record);
      }
      const dashboard = await loadDashboard(record.project_id);
      const current = dashboard?.state;
      if (!current || current.project_id !== record.project_id) throw new Error("工程状态无法核对。");
      if (!Number.isFinite(Number(current.revision)) || !Number.isFinite(record.revision)
        || Number(current.revision) < record.revision) throw new Error("工程修订早于已保存事件，未发送续跑通知。");
      if (Number(current.revision) > record.revision) {
        record.status = "SUPERSEDED";
        record.detail = "工程已推进到后续阶段，本次旧事件不再触发模型。";
      } else if (current.project_status === "BLOCKED_HUMAN" || current.stage_statuses?.[current.current_stage] === "BLOCKED_HUMAN" || current.blocking) {
        record.status = "SKIPPED_WAITING";
        record.detail = "决定已保存；仍有待处理门禁，模型继续等待工作台确认。";
      } else if (["ARCHIVED", "REVOKED", "RELEASE_DEFERRED", "PUBLICATION_DEFERRED"].includes(current.project_status)) {
        record.status = "SKIPPED_WAITING";
        record.detail = "工程当前暂停或归档，未自动启动模型。";
      } else {
        if (!record.binding) record.binding = await findBinding(record.project_id);
        if (!record.binding?.session_id) {
          record.status = "UNBOUND";
          record.detail = "决定已保存；未找到唯一且可验证的原工程会话。未猜选或新建会话，可从工作台重试衔接。";
        } else {
          const found = await resolveAgent(record.binding.session_id);
          if (!found?.agent || found.error) throw new Error("原工程会话暂时无法恢复，请从工作台重试衔接。");
          const agent = found.agent;
          if (agent.session?.header?.id !== record.binding.session_id || agent.session?.header?.origin === "subagent") {
            throw new Error("目标会话身份不一致，未发送通知。");
          }
          if (!creationBindingFromEvents(record.binding.session_id, harnessSessionEvents(agent.session), record.project_id)) {
            throw new Error("原工程会话的创建依据无法回读，未发送通知。");
          }
          const stopped = continuationCancellation(harnessSessionEvents(agent.session), record);
          if (stopped) {
            Object.assign(record, stopped, { updated_at: new Date().toISOString() });
            if (record.message?.id) agent.inbox?.remove?.(record.message.id);
            await save();
            return receipt(record);
          }
          if (!record.message) record.message = {
            id: randomUUID(), role: "user",
            source: continuationNoticeSource(agent.session, "工作台决定已保存，继续原工程任务"),
            content: [{ type: "text", text: [
              "ORION 工作台工作流通知（平台事件，不是新增用户指令或审批）。",
              `工程：${record.project_id}；已保存操作：${record.action}；事件：${record.event_id}；当时修订：${record.revision}。`,
              "请先回读该工程真实状态、正式决定与产物，再继续当前会话已获授权且工作流允许的任务；不要重做已通过阶段。",
              "此通知不新增 S4 设计或 S7 发布授权。若仍有需要确认的门禁，在本体工作台展示并等待；不得把本通知当成审批。",
            ].join("\n") }],
          };
          // Persist the exact message before delivery. A retry can recover an
          // enqueue committed by Harness even if our final receipt write failed.
          record.status = "PENDING";
          await save();
          const latestEvents = harnessSessionEvents(agent.session);
          const cancelledDuringSave = continuationCancellation(latestEvents, record);
          if (cancelledDuringSave) {
            Object.assign(record, cancelledDuringSave, { updated_at: new Date().toISOString() });
            agent.inbox?.remove?.(record.message.id);
            await save();
            return receipt(record);
          }
          if (!messageObserved(latestEvents, record.message.id)) {
            // A record persisted before a runtime upgrade may still carry the
            // V3 wrapper; V4 would reject it, so lift only the source kind.
            const message = structuredClone(record.message);
            if (isContinuationNoticeSource(message.source)) {
              message.source = continuationNoticeSource(agent.session, message.source.summary);
            }
            await agent.followup(message);
          }
          record.status = "QUEUED";
          record.detail = "决定已保存，已通知原工程模型会话；模型忙时由原生收件箱排队。";
        }
      }
    } catch (error) {
      record.status = "RETRYABLE";
      record.detail = `决定已保存；模型衔接暂未完成：${error.message}`;
    }
    record.updated_at = new Date().toISOString();
    await save();
    return receipt(record);
  };
  return {
    afterAction: (action, projectId, workflow) => serialized(async () => {
      if (!CONTINUATION_ACTIONS.has(action)) return null;
      await load();
      const eventId = workflow?.audit?.last_event_id;
      if (!PROJECT_ID.test(projectId) || !eventId) throw new Error("工作流事件回执缺失，未发送模型通知。");
      const key = `${projectId}:${eventId}`;
      if (!state.deliveries[key]) {
        const event = await readWorkflowEvent(projectId, eventId);
        if (!event?.event_hash) throw new Error("工作流事件未持久化，未发送模型通知。");
        state.deliveries[key] = {
          project_id: projectId, event_id: eventId, event_hash: event.event_hash, action,
          revision: Number(workflow.revision), stage: workflow.current_stage,
          recorded_actor: event.actor, workflow_event_at: event.at, created_at: new Date().toISOString(), status: "PENDING",
        };
        await save();
      }
      return deliver(state.deliveries[key]);
    }),
    retry: (projectId, eventId) => serialized(async () => {
      await load();
      const records = Object.values(state.deliveries).filter((record) => record.project_id === projectId && (!eventId || record.event_id === eventId));
      const record = records.at(-1);
      if (!record) throw new Error("没有可重试的已保存续跑事件。");
      return deliver(record);
    }),
  };
}
