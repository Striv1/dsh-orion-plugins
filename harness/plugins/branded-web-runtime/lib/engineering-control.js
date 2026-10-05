import { dirname, join, resolve } from "node:path";
import { readFileSync, mkdirSync, openSync, writeFileSync, fsyncSync, closeSync, renameSync } from "node:fs";
import { isUserCancellation } from "./workflow-continuation.js";

// Only native cancellation is forwarded. Text, approvals and turn/start never
// authorize/resume a project. Python verifies the persisted opt-in session bind.
export function createEngineeringControlBridge({ config, execute, warn = () => {} }) {
  // UI observation only: this never authorizes, resumes or starts execution.
  const observationState = (sessionId, projectId) => {
    if (!config?.workflowHome || !/^session-[a-f0-9-]{36}$/.test(sessionId ?? "")
      || !/^ontology-project-[a-zA-Z0-9-]+$/.test(projectId ?? "")) return null;
    try {
      const state = JSON.parse(readFileSync(join(resolve(config.workflowHome),
        '.engineering-control', projectId, 'tasks.json'), 'utf8'));
      const control = state.controls?.[projectId];
      if (control?.session_id !== sessionId || typeof control.enabled !== 'boolean'
        || !Number.isSafeInteger(control.epoch) || control.epoch < 1) return null;
      return { paused: !control.enabled, epoch: control.epoch };
    } catch { return null; }
  };
  const pending = new Set();
  const launches = new Map();
  const invoke = (...args) => {
    try { return Promise.resolve(execute(...args)); }
    catch (error) { return Promise.reject(error); }
  };
  const ensureController = (sessionId, projectId) => {
    if (!config?.workflowHome || !/^session-[a-f0-9-]{36}$/.test(sessionId ?? "")
      || !/^ontology-project-[a-zA-Z0-9-]+$/.test(projectId ?? "")) return Promise.resolve(null);
    const key = `${sessionId}/${projectId}`;
    if (launches.has(key)) return launches.get(key);
    const cwd = config.workflowCwd ?? dirname(resolve(config.workflowHome));
    const args = ["-m", "scripts.engineering_control", "start-controller",
      "--workflow-home", resolve(config.workflowHome), "--session-id", sessionId,
      "--project-id", projectId];
    if (config.semanticaApiUrl) args.push("--semantica-url", config.semanticaApiUrl);
    // This request only wakes an already authorized durable controller. The
    // trusted Python boundary checks opt-in, session, pause and stage scope.
    const job = invoke(config.workflowPython ?? join(cwd, ".venv", "bin", "python"),
      args, { cwd, timeout: 15000 }).then(({ stdout }) => JSON.parse(stdout)).catch(error => {
      warn(`工程执行器启动检查失败：${error.message}`);
      return { status: "CONTROL_START_FAILED" };
    }).finally(() => { launches.delete(key); pending.delete(job); });
    launches.set(key, job);
    pending.add(job);
    return job;
  };
  const observeCancellation = (sessionId, event) => {
    if (!config?.workflowHome || !isUserCancellation(event)
      || !/^session-[a-f0-9-]{36}$/.test(sessionId ?? "")
      || !Number.isSafeInteger(event.time) || event.time <= 0) return Promise.resolve(null);
    // Synchronous durable intent closes the native callback -> Python spawn
    // window. Executors check this journal before effects and inside commit.
    try {
      const directory = join(resolve(config.workflowHome), '.engineering-control', 'cancellations', sessionId);
      mkdirSync(directory, { recursive: true });
      const target = join(directory, `${event.time}.json`);
      {
        const temporary = `${target}.${process.pid}.tmp`;
        const fd = openSync(temporary, 'w', 0o600);
        try {
          writeFileSync(fd, JSON.stringify({ session_id: sessionId,
            event_time_ms: event.time, source: 'HARNESS_NATIVE_USER_STOP' }));
          fsyncSync(fd);
        } finally { closeSync(fd); }
        renameSync(temporary, target);
        // Persist newly created session/cancellations directory entries too.
        for (const persisted of [directory, dirname(directory), dirname(dirname(directory)), resolve(config.workflowHome)]) {
          const dir = openSync(persisted, 'r');
          try { fsyncSync(dir); } finally { closeSync(dir); }
        }
      }
    } catch (error) {
      warn(`持久暂停意图未能保存：${error.message}`);
      return Promise.resolve({ status: 'CONTROL_SYNC_FAILED' });
    }
    const cwd = config.workflowCwd ?? dirname(resolve(config.workflowHome));
    const job = invoke(config.workflowPython ?? join(cwd, ".venv", "bin", "python"), [
      "-m", "scripts.engineering_control", "pause-session",
      "--workflow-home", resolve(config.workflowHome),
      "--session-id", sessionId, "--event-time-ms", String(event.time),
    ], { cwd, timeout: 15000 }).then(({ stdout }) => JSON.parse(stdout)).catch(error => {
      warn(`持久工程暂停同步失败，需核对执行控制：${error.message}`);
      return { status: "CONTROL_SYNC_FAILED" };
    }).finally(() => pending.delete(job));
    pending.add(job);
    return job;
  };
  return { observationState, observeCancellation, ensureController, drain: () => Promise.allSettled([...pending]) };
}
