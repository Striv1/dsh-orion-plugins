import { harnessSessionEvents } from "./session-logs.js";
import { workflowRepairInterruption } from "./workflow-repair-stop.js";

const stages = Array.from({ length: 8 }, (_, index) => `S${index}`);
const completed = new Set(["PASSED", "SKIPPED", "NOT_APPLICABLE"]);
const active = new Set(["RUNNING", "IN_PROGRESS", "BLOCKED_HUMAN", "FAILED"]);

// The native todo projection is a view of formal state, never a workflow write.
// Missing stage evidence stays pending, even when a later stage is current.
export function engineeringNativeTodos(handoff) {
  const terminal = handoff?.project_status === "PUBLISHED" && !handoff.current_stage;
  const jointDesignPending = handoff?.project_status === "S1_S3_READY" && !handoff.current_stage;
  if (!handoff?.project_id || (!stages.includes(handoff.current_stage) && !terminal && !jointDesignPending)
    || !Number.isInteger(handoff.revision)) return null;
  const statuses = handoff.stage_statuses ?? {};
  const names = handoff.stage_names ?? {};
  if (!stages.some((stage) => typeof statuses[stage] === "string")) return null;
  const project = handoff.project_name || handoff.project_id;
  return stages.map((stage) => {
    const raw = statuses[stage];
    const isCurrent = stage === handoff.current_stage;
    const done = completed.has(raw);
    const status = done ? "completed" : isCurrent && active.has(raw) ? "in_progress" : "pending";
    const note = {
      SKIPPED: "（不适用，已记录）", NOT_APPLICABLE: "（不适用，已记录）",
      FAILED: "（待修复）", INVALIDATED: "（已失效，待重新验证）", DEFERRED: "（已暂缓）",
    }[raw] ?? (jointDesignPending && stage === "S4" && !done ? "（准备联合设计与人工确认）"
      : isCurrent && handoff.project_status === "BLOCKED_HUMAN" ? "（等待你的确认）"
      : isCurrent && handoff.project_status === "PACKAGE_READY_RUNTIME_BLOCKED"
        ? "（交付包已生成，运行验证待恢复）" : "");
    return { content: `${project} · ${stage}${names[stage] ? ` ${names[stage]}` : ""}${note}`, status };
  });
}

export function engineeringIntakeTodos() {
  const names = ["需求与来源接入（尚未创建正式工程）", "资料与数据理解", "业务语义设计", "数据映射设计",
    "联合设计与人工确认", "本体构建与装配", "实例化、质量与 CQ 验证", "人工批准发布"];
  return stages.map((stage, index) => ({ content: `本体工程准备 · ${stage} ${names[index]}`,
    status: index === 0 ? "in_progress" : "pending" }));
}

// A turn outcome describes model execution, never a change to formal stage state.
// Fixed messages avoid copying provider errors (which may contain credentials).
export function engineeringExecutionInterruption(event) {
  if (event?.type !== "turn/end") return null;
  const repairStop = workflowRepairInterruption(event);
  if (repairStop) return repairStop;
  const kind = event.data?.reason?.kind;
  const label = {
    "max-tokens": "单次输出达到上限，执行已中断",
    error: "模型执行失败，等待恢复",
    interrupted: "执行意外中断，等待恢复",
    blocked: "执行受阻，等待处理",
    aborted: event.data?.reason?.reason?.kind === "user" ? "执行已暂停" : "执行已取消",
  }[kind];
  return label ? { status: kind, label,
    recovery: "正式阶段未因本次中断而完成。继续前回读工程状态和 get_stage_draft 检查点；未保存的推理不算成果。先处理阻塞，再在已有授权范围内恢复；用户停止后须明确恢复意图。" } : null;
}

export function syncEngineeringNativeProgress(agent, handoff, { prebinding = false, paused = false, interruption = null } = {}) {
  let todos = engineeringNativeTodos(handoff) ?? (prebinding ? engineeringIntakeTodos() : null);
  if (!todos) return false;
  if (typeof agent?.session?.append !== "function") throw new Error("NATIVE_TASKS_APPEND_UNAVAILABLE");
  if (paused || interruption) todos = todos.map((item, index) => ({ ...item, content: item.status !== "completed"
    && (item.status === "in_progress" || stages[index] === handoff?.current_stage)
    ? `${item.content}（${paused ? "执行已暂停" : interruption.label}；阶段尚未完成）` : item.content }));
  const events = harnessSessionEvents(agent.session);
  let previous = null;
  for (let index = events.length - 1; index >= 0; index--) {
    if (events[index].type === "turn/start") break;
    if (events[index].type === "todo/write") {
      previous = events[index].data?.todos;
      break;
    }
  }
  if (JSON.stringify(previous) === JSON.stringify(todos)) return false;
  // Public native session event, identical to todo_write's persisted output.
  // No synthetic user message or invented tool-call receipt is produced.
  agent.session.append("todo/write", { todos });
  const persisted = harnessSessionEvents(agent.session).findLast(event => event.type === "todo/write");
  if (JSON.stringify(persisted?.data?.todos) !== JSON.stringify(todos)) {
    throw new Error("NATIVE_TASKS_READBACK_MISMATCH");
  }
  return true;
}
