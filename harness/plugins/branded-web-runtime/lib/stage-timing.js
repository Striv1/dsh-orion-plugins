import { appendFile, lstat, mkdir, readFile, readdir, realpath } from "node:fs/promises";
import { join, resolve, sep } from "node:path";
import { toolResultBlocks } from "./session-logs.js";

const stages = Array.from({ length: 8 }, (_, i) => `S${i}`);
const validStage = (value) => stages.includes(value);
const timestamp = (value) => typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;
const seconds = (ms) => Math.round(ms) / 1000;
const completions = new Set(["STAGE_PASSED", "FIRST_VERSION_COMPLETED", "ONTOLOGY_DESIGN_COMPLETED",
  "ONTOLOGY_BUILD_COMPLETED", "QUALITY_VALIDATION_COMPLETED", "REALTIME_DEPLOYMENT_READY"]);
const starts = new Set(["PROJECT_CREATED", "STAGE_STARTED", "STAGE_RETRY_STARTED", "STAGE_REOPENED_FOR_CORRECTION"]);
const unionMs = (intervals) => {
  let total = 0; let end = -Infinity;
  for (const [start, stop] of intervals.filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b) && b >= a).sort((a, b) => a[0] - b[0])) {
    total += Math.max(0, stop - Math.max(start, end)); end = Math.max(end, stop);
  }
  return total;
};

export function summarizeStageTiming({ projectId, events = [], executions = [], nativeRecords = [], now = Date.now() }) {
  const byStage = Object.fromEntries(stages.map((stage) => [stage, { stage, attempts: [], waits: [] }]));
  let active = null; let wait = null;
  const runtime = { package_published_at: null, ready_at: null, attempts: [], recovery_intervals: [] };
  let runtimeAttempt = null; let recoveryStart = null;
  const closeWait = (at) => { if (wait) { byStage[wait.stage].waits.push([wait.start, at]); wait = null; } };
  const close = (at, status) => { if (active) { active.ended_at = at; active.status = status; active = null; } closeWait(at); };
  const begin = (stage, at, event, restart = false) => {
    if (active?.stage === stage && !restart) return;
    close(at, "LEFT_STAGE");
    active = { stage, started_at: at, ended_at: null, status: "OPEN", event_id: event.event_id ?? null };
    byStage[stage].attempts.push(active);
  };
  const ordered = events.filter((e) => e.project_id === projectId && timestamp(e.at) !== null)
    .sort((a, b) => timestamp(a.at) - timestamp(b.at) || (a.sequence ?? 0) - (b.sequence ?? 0));
  for (const event of ordered) {
    const at = timestamp(event.at); const stage = event.details?.stage ?? event.current_stage;
    if (at > now) continue;
    if (event.event_type === "ONTOLOGY_PACKAGE_PUBLISHED") runtime.package_published_at = event.at;
    if (event.event_type === "REALTIME_DEPLOYMENT_QUEUED") {
      if (runtimeAttempt?.ended_at === null) runtimeAttempt.ended_at = at;
      runtimeAttempt = { started_at: at, ended_at: null, status: "OPEN", event_id: event.event_id ?? null };
      runtime.attempts.push(runtimeAttempt);
    }
    if (event.event_type === "REALTIME_DEPLOYMENT_FAILED") {
      if (runtimeAttempt) { runtimeAttempt.ended_at = at; runtimeAttempt.status = "FAILED"; }
      recoveryStart ??= at;
    }
    if (event.event_type === "REALTIME_DEPLOYMENT_READY") {
      runtime.ready_at = event.at;
      if (runtimeAttempt) { runtimeAttempt.ended_at = at; runtimeAttempt.status = "COMPLETED"; }
      if (recoveryStart !== null) { runtime.recovery_intervals.push([recoveryStart, at]); recoveryStart = null; }
    }
    if (starts.has(event.event_type) && validStage(stage)) {
      begin(stage, at, event, ["STAGE_RETRY_STARTED", "STAGE_REOPENED_FOR_CORRECTION"].includes(event.event_type));
    }
    const legacyFinalPackage = event.event_type === "ONTOLOGY_PACKAGE_PUBLISHED" && event.project_status === "PUBLISHED";
    if ((completions.has(event.event_type) || legacyFinalPackage) && validStage(stage)) {
      if (active?.stage === stage) close(at, "COMPLETED");
      // The transition is evidence of the next stage's entry, not its completion.
      if (validStage(event.current_stage) && event.current_stage !== stage) begin(event.current_stage, at, event);
    }
    if (event.project_status === "BLOCKED_HUMAN" && validStage(stage) && !wait) wait = { stage, start: at };
    if (wait && event.project_status !== "BLOCKED_HUMAN") closeWait(at);
  }
  if (wait) byStage[wait.stage].waits.push([wait.start, now]);
  if (recoveryStart !== null) runtime.recovery_intervals.push([recoveryStart, now]);
  const validExecutions = new Map();
  for (const execution of executions) {
    if (execution?.project_id !== projectId || !validStage(execution.stage) || !execution.execution_id) continue;
    const previous = validExecutions.get(execution.execution_id);
    if (!previous || (timestamp(execution.heartbeat_at) ?? 0) >= (timestamp(previous.heartbeat_at) ?? 0)) validExecutions.set(execution.execution_id, execution);
  }
  return {
    schema_version: 1, project_id: projectId, observed_at: new Date(now).toISOString(),
    scope_zh: "正式阶段尝试区间与已记录运行观察。墙钟、执行、工具、模型步骤和审批等待可能重叠，不能相加；未知不等于零。",
    native_scope_zh: "仅记录公共事件中可关联已验证工程/阶段上下文的步骤；工具为调用至返回墙钟，模型步骤含请求与排队，不是供应商纯生成耗时。",
    stages: stages.map((stage) => {
      const item = byStage[stage];
      const intervals = item.attempts.map((a) => [a.started_at, a.ended_at ?? now]);
      const runs = [...validExecutions.values()].filter((e) => e.stage === stage);
      const managedIntervals = runs.flatMap((e) => {
        const start = timestamp(e.started_at); const end = timestamp(e.completed_at) ?? timestamp(e.heartbeat_at);
        return start === null || end === null ? [] : [[start, end]];
      });
      const records = nativeRecords.filter((r) => r.schema_version === 1 && r.project_id === projectId && r.stage === stage
        && /^session-[a-f0-9-]{36}$/.test(r.session_id ?? "") && Number.isInteger(r.revision)
        && Number.isFinite(r.started_at_ms) && Number.isFinite(r.ended_at_ms) && r.ended_at_ms >= r.started_at_ms
        && r.ended_at_ms <= now && ["TOOL_WALL", "MODEL_STEP_WALL"].includes(r.kind));
      const observed = (kind) => records.some((r) => r.kind === kind)
        ? seconds(unionMs(records.filter((r) => r.kind === kind).map((r) => [r.started_at_ms, r.ended_at_ms]))) : null;
      return {
        stage, wall_clock_seconds: intervals.length ? seconds(unionMs(intervals)) : null,
        wall_clock_status: intervals.length ? (item.attempts.some((a) => a.ended_at === null) ? "OPEN_INTERVAL" : "RECORDED_INTERVALS") : "UNKNOWN",
        managed_execution_seconds: managedIntervals.length ? seconds(unionMs(managedIntervals)) : null,
        managed_execution_scope: "已记录运行区间合并；中断记录仅计至最后心跳，非CPU时间；历史覆盖可能不完整。",
        tool_wall_seconds: observed("TOOL_WALL"), model_step_wall_seconds: observed("MODEL_STEP_WALL"),
        model_generation_seconds: null, model_generation_status: "UNKNOWN_PROVIDER_DURATION_NOT_RECORDED",
        human_wait_seconds: item.waits.length ? seconds(unionMs(item.waits)) : null,
        human_wait_open: wait?.stage === stage,
        human_wait_scope: "仅明确BLOCKED_HUMAN至解除门禁，可能由授权代办确认；无记录时未知。",
        rework_count: item.attempts.length ? Math.max(0, item.attempts.length - 1) : null,
        rework_wall_seconds: item.attempts.length ? seconds(unionMs(intervals.slice(1))) : null,
        execution_attempt_count: runs.length || null,
        native_timing_status: records.length ? "OBSERVED_PARTIAL" : "UNKNOWN_NO_ATTRIBUTABLE_EVENTS",
        native_record_count: records.length,
        attempt_count: item.attempts.length,
        attempts: item.attempts.slice(-20).map((a) => ({ ...a, started_at: new Date(a.started_at).toISOString(),
          ended_at: a.ended_at === null ? null : new Date(a.ended_at).toISOString() })),
        omitted_attempt_count: Math.max(0, item.attempts.length - 20),
        ...(stage === "S7" ? { runtime: {
          scope_zh: "包生成是中间节点；S7 至正式运行就绪（含明确不适用）才结束。运行恢复包含失败后的修复等待及重试，与阶段墙钟重叠。",
          package_published_at: runtime.package_published_at, ready_at: runtime.ready_at,
          status: runtime.ready_at ? "COMPLETED" : runtime.package_published_at || runtime.attempts.length ? "OPEN" : "UNKNOWN",
          wall_clock_seconds: runtime.attempts.length ? seconds((timestamp(runtime.ready_at) ?? now) - runtime.attempts[0].started_at) : null,
          retry_count: runtime.attempts.length ? runtime.attempts.length - 1 : null,
          recovery_wall_seconds: runtime.recovery_intervals.length ? seconds(unionMs(runtime.recovery_intervals)) : null,
          recovery_open: recoveryStart !== null,
          attempts: runtime.attempts.slice(-20).map((a) => ({ ...a, started_at: new Date(a.started_at).toISOString(), ended_at: a.ended_at === null ? null : new Date(a.ended_at).toISOString() })),
        } } : {}),
      };
    }),
  };
}

async function regularJson(path) {
  try { if (!(await lstat(path)).isFile()) return null; return JSON.parse(await readFile(path, "utf8")); }
  catch (error) { if (["ENOENT", "ENOTDIR"].includes(error.code) || error instanceof SyntaxError) return null; throw error; }
}

export async function loadStageTiming(projectRoot, events, now = Date.now()) {
  const root = join(projectRoot, ".stage-executions");
  if ((await lstat(root).catch(() => null))?.isSymbolicLink()) return summarizeStageTiming({ projectId: projectRoot.split(sep).at(-1), events, now });
  const executions = (await Promise.all(stages.map((stage) => regularJson(join(root, `${stage}.json`))))).filter(Boolean);
  const history = join(root, "history");
  if ((await lstat(history).catch(() => null))?.isDirectory()) {
    for (const name of await readdir(history)) {
      if (!/^EXEC-[A-Za-z0-9]+\.json$/.test(name)) continue;
      const record = await regularJson(join(history, name)); if (record) executions.push(record);
    }
  }
  const nativePath = join(root, "native-timing.jsonl"); let nativeRecords = [];
  if ((await lstat(nativePath).catch(() => null))?.isFile()) {
    nativeRecords = (await readFile(nativePath, "utf8")).split("\n").flatMap((line) => {
      try { return [JSON.parse(line)]; } catch { return []; }
    });
  }
  return summarizeStageTiming({ projectId: projectRoot.split(sep).at(-1), events, executions, nativeRecords, now });
}

// Hook only new public session events. No session-log replay/decompression in status polling.
export function createStageTimingRecorder({ workflowHome, warn = () => {} }) {
  const sessions = new Map(); let writes = Promise.resolve();
  const bindingKey = (b) => b && /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(b.project_id ?? "")
    && validStage(b.current_stage) && Number.isInteger(b.revision) ? `${b.project_id}/${b.current_stage}/${b.revision}` : null;
  const persist = (record) => {
    writes = writes.then(async () => {
      const home = await realpath(resolve(workflowHome)); const project = join(home, record.project_id);
      if (!(await lstat(project)).isDirectory() || await realpath(project) !== project) return;
      const dir = join(project, ".stage-executions"); await mkdir(dir, { recursive: true });
      if (!(await lstat(dir)).isDirectory() || await realpath(dir) !== dir) return;
      const file = join(dir, "native-timing.jsonl"); const stat = await lstat(file).catch(() => null);
      if (stat && !stat.isFile()) return;
      await appendFile(file, JSON.stringify(record) + "\n", { mode: 0o600 });
    }).catch((error) => warn(`阶段耗时记录失败：${String(error)}`));
  };
  return {
    observe({ sessionId, event, binding }) {
      if (!/^session-[a-f0-9-]{36}$/.test(sessionId ?? "") || !Number.isFinite(event.time)) return;
      let state = sessions.get(sessionId);
      if (!state) { state = { step: null, calls: new Map() }; sessions.set(sessionId, state); }
      const key = bindingKey(binding); const data = event.data ?? {};
      const emit = (kind, start, end, scope, evidence) => {
        if (end < start || !scope) return;
        persist({ schema_version: 1, project_id: scope.project_id, stage: scope.current_stage, revision: scope.revision,
          session_id: sessionId, kind, started_at_ms: start, ended_at_ms: end, ...evidence });
      };
      if (event.type === "step/start") state.step = { time: event.time, key, binding, turn: data.turn, step: data.step, seq: event.seq };
      if (event.type === "assistant/message" && state.step) {
        const start = state.step;
        if (start.key && start.key === key && start.turn === data.turn && start.step === data.step)
          emit("MODEL_STEP_WALL", start.time, event.time, start.binding, { start_event_seq: start.seq, end_event_seq: event.seq });
        state.step = null;
      }
      if (event.type === "tool/call") {
        let args; try { args = typeof data.arguments === "string" ? JSON.parse(data.arguments) : data.arguments; } catch { args = null; }
        // Explicit arguments must agree with the last verified formal handoff.
        const agrees = key && (!args?.project_id || args.project_id === binding.project_id)
          && (!args?.stage || args.stage === binding.current_stage);
        if (agrees && typeof data.callId === "string") state.calls.set(data.callId, { time: event.time, binding, key, name: data.name, seq: event.seq });
      }
      if (event.type === "tool/result") {
        for (const item of toolResultBlocks(event)) {
          const start = state.calls.get(item.toolCallId); state.calls.delete(item.toolCallId);
          if (start && start.key === key) emit("TOOL_WALL", start.time, event.time, start.binding,
            { call_id: item.toolCallId, tool_name: String(start.name ?? "").slice(0, 160), start_event_seq: start.seq, end_event_seq: event.seq });
        }
      }
      if (event.type === "turn/end") sessions.delete(sessionId);
    },
    flush: () => writes,
  };
}
