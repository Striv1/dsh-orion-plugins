import { execFileSync, spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import { appendFile, mkdir, open, readdir, rename, stat, unlink, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";

import { readJson } from "./runtime-files.js";

const DOCUMENT_JOB_ID_PATTERN = /^JOB-[A-Z0-9-]{8,80}$/;
const DOCUMENT_ACTIVE_STATUSES = new Set(["QUEUED", "RUNNING"]);
const DOCUMENT_DEFAULT_CONCURRENCY = 1;
const DOCUMENT_DEFAULT_WATCHDOG_INTERVAL_MS = 1000;
const DOCUMENT_DEFAULT_HEARTBEAT_INTERVAL_MS = 5000;
const DOCUMENT_DEFAULT_EXIT_GRACE_MS = 5000;
const DOCUMENT_DEFAULT_RECOVERY_LIMIT = 1;

export function requireWorkflowActor(config) {
  const actor = config.workflowActor;
  if (typeof actor !== "string" || !actor.trim() || actor.length > 128 || /[\u0000-\u001f\u007f]/u.test(actor)) {
    const error = new Error("业务写入需要明确配置操作人。");
    error.code = "ACTOR_REQUIRED";
    throw error;
  }
  return actor.trim();
}

const boundedInteger = (value, fallback, minimum, maximum) => {
  const parsed = Number(value);
  return Number.isInteger(parsed) ? Math.max(minimum, Math.min(maximum, parsed)) : fallback;
};

const atomicJsonWrite = async (path, payload) => {
  await mkdir(dirname(path), { recursive: true });
  const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`;
  await writeFile(temporary, `${JSON.stringify(payload, null, 2)}\n`, "utf8");
  await rename(temporary, path);
};

const schedulerOwnerIsAlive = (pid) => {
  const numericPid = Number(pid ?? 0);
  if (!Number.isInteger(numericPid) || numericPid <= 1) return false;
  try {
    process.kill(numericPid, 0);
    return true;
  } catch (error) {
    return error?.code === "EPERM";
  }
};

async function acquireDocumentSchedulerLock(inputRoot, clock = () => Date.now()) {
  await mkdir(inputRoot, { recursive: true });
  const lockPath = join(inputRoot, ".orion-document-scheduler.lock");
  const token = `SCHED-${randomUUID().replaceAll("-", "").toUpperCase()}`;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    let handle;
    try {
      handle = await open(lockPath, "wx", 0o600);
      const lock = {
        schema_version: 1,
        pid: process.pid,
        token,
        acquired_at: new Date(clock()).toISOString(),
      };
      await handle.writeFile(`${JSON.stringify(lock, null, 2)}\n`, "utf8");
      await handle.sync();
      await handle.close();
      handle = null;
      let released = false;
      return async () => {
        if (released) return true;
        const current = await readJson(lockPath, null);
        if (current?.token !== token || Number(current?.pid) !== process.pid) return false;
        try {
          await unlink(lockPath);
          released = true;
          return true;
        } catch (error) {
          if (error?.code === "ENOENT") return false;
          throw error;
        }
      };
    } catch (error) {
      await handle?.close().catch(() => {});
      if (error?.code !== "EEXIST") throw error;
      const existing = await readJson(lockPath, null);
      if (schedulerOwnerIsAlive(existing?.pid)) return null;
      if (!existing) {
        const lockStat = await stat(lockPath).catch(() => null);
        if (lockStat && clock() - lockStat.mtimeMs < 5000) return null;
      }
      const confirmation = await readJson(lockPath, null);
      if (
        existing
        && confirmation
        && (existing.token !== confirmation.token || Number(existing.pid) !== Number(confirmation.pid))
      ) {
        return null;
      }
      try {
        await unlink(lockPath);
      } catch (unlinkError) {
        if (unlinkError?.code !== "ENOENT") return null;
      }
    }
  }
  return null;
}

const writeDocumentWorkerFence = async (jobDir, workerInvocationId, details = {}) => {
  const fence = {
    schema_version: 1,
    worker_invocation_id: workerInvocationId,
    written_at: new Date().toISOString(),
    writer_pid: process.pid,
    ...details,
  };
  await atomicJsonWrite(join(jobDir, "worker-fence.json"), fence);
  return fence;
};

const rotateDocumentWorkerFence = (jobDir, reason, actor = null) => writeDocumentWorkerFence(
  jobDir,
  `FENCED-${randomUUID().replaceAll("-", "").slice(0, 20).toUpperCase()}`,
  { reason, actor },
);

const documentRuntime = (config) => {
  const workflowCwd = config.workflowCwd ?? dirname(resolve(config.workflowHome));
  return {
    workflowCwd,
    workflowPython: config.workflowPython ?? join(workflowCwd, ".venv", "bin", "python"),
    workflowHome: resolve(config.workflowHome),
    inputRoot: resolve(config.documentIngestionRoot),
  };
};

const documentJobDirectory = (root, jobId) => {
  if (!DOCUMENT_JOB_ID_PATTERN.test(jobId)) throw new Error("资料任务编号不合法");
  return join(root, ".orion-s0-jobs", jobId);
};

async function loadDocumentJob(root, jobId) {
  const jobDir = documentJobDirectory(root, jobId);
  const status = await readJson(join(jobDir, "status.json"), null);
  if (!status) return status;
  const [request, runner] = await Promise.all([
    readJson(join(jobDir, "request.json"), {}),
    readJson(join(jobDir, "runner.json"), null),
  ]);
  const effectiveStatus = status.status === "QUEUED" && ["STARTING", "RUNNING"].includes(runner?.state)
    ? "RUNNING"
    : status.status;
  return {
    ...status,
    status: effectiveStatus,
    project_id: status.project_id ?? (String(request.project_id ?? "").trim() || null),
    project_name: status.project_name ?? (String(request.project_name ?? "").trim() || null),
    domain: status.domain ?? (String(request.domain ?? "").trim() || null),
    project_request_id: status.project_request_id
      ?? (String(request.project_request_id ?? "").trim() || null),
    storage_paths: {
      input_root: resolve(root),
      job_directory: jobDir,
      structured_markdown_directory: join(jobDir, "structured-markdown"),
    },
    queue_position: effectiveStatus === "QUEUED" ? runner?.queue_position ?? null : null,
    queue_concurrency: runner?.queue_concurrency ?? null,
    queued_at: status.queued_at ?? runner?.queued_at ?? status.created_at ?? null,
    started_at: status.started_at ?? runner?.started_at ?? null,
    heartbeat_at: runner?.heartbeat_at ?? null,
    deadline_at: runner?.deadline_at ?? status.deadline_at ?? null,
    worker_invocation_id: runner?.worker_invocation_id ?? null,
    worker_pid: runner?.pid ?? null,
    worker_attempt: runner?.attempt ?? null,
    recovery_count: runner?.recovery_count ?? 0,
  };
}

async function appendDocumentJobEvent(jobDir, eventType, status, details = {}) {
  const event = {
    event_id: `JOBEVT-${randomUUID().replaceAll("-", "").slice(0, 16).toUpperCase()}`,
    event_type: eventType,
    job_id: status.job_id,
    status: status.status,
    at: new Date().toISOString(),
    details,
  };
  await appendFile(join(jobDir, "job-events.jsonl"), `${JSON.stringify(event)}\n`, "utf8");
  return event;
}

function documentRunnerIdentityMatches(runner, jobDir) {
  const pid = Number(runner?.pid ?? 0);
  if (!Number.isInteger(pid) || pid <= 1) return false;
  const requestPath = join(resolve(jobDir), "request.json");
  try {
    const command = execFileSync(
      "/bin/ps",
      ["-ww", "-p", String(pid), "-o", "command="],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], timeout: 2000 },
    ).trim();
    return command.includes("services.ingestion.s0_batch_executor")
      && command.includes(requestPath);
  } catch {
    return false;
  }
}

function stopDocumentRunner(runner, jobDir) {
  const pid = Number(runner?.pid ?? 0);
  if (!documentRunnerIdentityMatches(runner, jobDir)) return false;
  try {
    process.kill(-pid, "SIGTERM");
    return true;
  } catch {
    try {
      process.kill(pid, "SIGTERM");
      return true;
    } catch {
      return false;
    }
  }
}

async function interruptDocumentJob(root, jobId, terminalStatus, message, actor) {
  const jobDir = documentJobDirectory(root, jobId);
  const statusPath = join(jobDir, "status.json");
  const current = await readJson(statusPath, null);
  if (!current) throw new Error("资料任务不存在");
  if (!DOCUMENT_ACTIVE_STATUSES.has(current.status)) {
    const error = new Error(`当前任务状态为 ${current.status}，不能取消或标记超时。`);
    error.code = "STATE_CONFLICT";
    throw error;
  }
  const runner = await readJson(join(jobDir, "runner.json"), null);
  const fence = await rotateDocumentWorkerFence(
    jobDir,
    terminalStatus === "TIMED_OUT" ? "JOB_TIMED_OUT" : "JOB_CANCELLED",
    actor,
  );
  const runnerStopped = stopDocumentRunner(runner, jobDir);
  const successfulPaths = new Set(
    (current.files ?? []).filter((item) => item.status === "SUCCEEDED").map((item) => String(item.relative_path)),
  );
  const discoveredPaths = (current.discovered_paths ?? []).map(String);
  const retryablePaths = [...new Set([
    ...discoveredPaths.filter((path) => !successfulPaths.has(path)),
    ...(current.files ?? [])
      .filter((item) => item.relative_path && item.status !== "SUCCEEDED")
      .map((item) => String(item.relative_path)),
  ])];
  const files = (current.files ?? []).map((item) => item.status === "SUCCEEDED" ? item : {
    ...item,
    status: terminalStatus,
    error: message,
  });
  const terminal = {
    ...current,
    status: terminalStatus,
    message,
    files,
    failed_files: retryablePaths.length,
    retryable_paths: retryablePaths,
    current_file: null,
    current_page: null,
    progress_phase: terminalStatus,
    finished_at: new Date().toISOString(),
    interrupted_by: actor,
    runner_stopped: runnerStopped,
  };
  await atomicJsonWrite(statusPath, terminal);
  if (runner) {
    await atomicJsonWrite(join(jobDir, "runner.json"), {
      ...runner,
      state: terminalStatus,
      queue_position: null,
      worker_invocation_id: fence.worker_invocation_id,
      stopped_at: terminal.finished_at,
      stopped_by: actor,
    });
  }
  await appendDocumentJobEvent(
    jobDir,
    terminalStatus === "TIMED_OUT" ? "DOCUMENT_JOB_TIMED_OUT" : "DOCUMENT_JOB_CANCELLED",
    terminal,
    { actor, runner_stopped: runnerStopped, retryable_paths: retryablePaths },
  );
  return terminal;
}

const timestampValue = (value) => {
  const parsed = Date.parse(String(value ?? ""));
  return Number.isFinite(parsed) ? parsed : null;
};

const documentSchedulerSettings = (overrides = {}) => ({
  concurrency: boundedInteger(
    overrides.concurrency ?? process.env.ORION_S0_JOB_CONCURRENCY,
    DOCUMENT_DEFAULT_CONCURRENCY,
    1,
    8,
  ),
  intervalMs: boundedInteger(
    overrides.intervalMs ?? process.env.ORION_S0_JOB_WATCHDOG_MS,
    DOCUMENT_DEFAULT_WATCHDOG_INTERVAL_MS,
    10,
    60000,
  ),
  heartbeatMs: boundedInteger(
    overrides.heartbeatMs ?? process.env.ORION_S0_JOB_HEARTBEAT_MS,
    DOCUMENT_DEFAULT_HEARTBEAT_INTERVAL_MS,
    10,
    60000,
  ),
  exitGraceMs: boundedInteger(
    overrides.exitGraceMs ?? process.env.ORION_S0_JOB_EXIT_GRACE_MS,
    DOCUMENT_DEFAULT_EXIT_GRACE_MS,
    0,
    60000,
  ),
  recoveryLimit: boundedInteger(
    overrides.recoveryLimit ?? process.env.ORION_S0_JOB_RECOVERY_LIMIT,
    DOCUMENT_DEFAULT_RECOVERY_LIMIT,
    0,
    3,
  ),
});

const documentRunnerIsAlive = (pid, jobDir) => {
  const numericPid = Number(pid ?? 0);
  if (!Number.isInteger(numericPid) || numericPid <= 1) return false;
  try {
    process.kill(numericPid, 0);
    return documentRunnerIdentityMatches({ pid: numericPid }, jobDir);
  } catch (error) {
    return error?.code === "EPERM"
      && documentRunnerIdentityMatches({ pid: numericPid }, jobDir);
  }
};

async function readDocumentJobRecord(root, jobId) {
  const jobDir = documentJobDirectory(root, jobId);
  const status = await readJson(join(jobDir, "status.json"), null);
  if (!status) return null;
  // Terminal jobs only need runner reconciliation; their source request can be
  // large and is never used by the watchdog again unless the job is requeued.
  const [request, runner] = await Promise.all([
    DOCUMENT_ACTIVE_STATUSES.has(status.status)
      ? readJson(join(jobDir, "request.json"), {})
      : {},
    readJson(join(jobDir, "runner.json"), {}),
  ]);
  return { jobId, jobDir, request, runner, status };
}

async function listDocumentJobRecords(root) {
  const jobsRoot = join(root, ".orion-s0-jobs");
  const records = [];
  for (const entry of await readdir(jobsRoot, { withFileTypes: true }).catch(() => [])) {
    if (!entry.isDirectory() || !DOCUMENT_JOB_ID_PATTERN.test(entry.name)) continue;
    const record = await readDocumentJobRecord(root, entry.name);
    if (record) records.push(record);
  }
  return records;
}

const retryableDocumentPaths = (status) => {
  const successfulPaths = new Set(
    (status.files ?? [])
      .filter((item) => item.status === "SUCCEEDED" && item.relative_path)
      .map((item) => String(item.relative_path)),
  );
  return [...new Set([
    ...(status.discovered_paths ?? [])
      .map(String)
      .filter((path) => !successfulPaths.has(path)),
    ...(status.files ?? [])
      .filter((item) => item.relative_path && item.status !== "SUCCEEDED")
      .map((item) => String(item.relative_path)),
    ...(status.retryable_paths ?? []).map(String),
  ])];
};

async function failDocumentJob(record, message, eventType = "DOCUMENT_JOB_RECOVERY_FAILED") {
  const at = new Date().toISOString();
  const fence = await rotateDocumentWorkerFence(record.jobDir, eventType, "orion-document-scheduler");
  const retryablePaths = retryableDocumentPaths(record.status);
  const terminal = {
    ...record.status,
    status: "FAILED",
    message,
    files: (record.status.files ?? []).map((item) => item.status === "SUCCEEDED" ? item : {
      ...item,
      status: "FAILED",
      error: item.error ?? message,
    }),
    failed_files: retryablePaths.length,
    retryable_paths: retryablePaths,
    current_file: null,
    current_page: null,
    progress_phase: "FAILED",
    finished_at: at,
  };
  await atomicJsonWrite(join(record.jobDir, "status.json"), terminal);
  await atomicJsonWrite(join(record.jobDir, "runner.json"), {
    ...record.runner,
    state: "FAILED",
    queue_position: null,
    worker_invocation_id: fence.worker_invocation_id,
    stopped_at: at,
  });
  await appendDocumentJobEvent(record.jobDir, eventType, terminal, {
    retryable_paths: retryablePaths,
    worker_invocation_id: record.runner.worker_invocation_id ?? null,
  });
  return terminal;
}

function createDocumentJobScheduler(config, options = {}) {
  const runtime = documentRuntime(config);
  const settings = documentSchedulerSettings(options);
  const processAlive = options.processAlive ?? documentRunnerIsAlive;
  const clock = options.clock ?? (() => Date.now());
  const logger = options.logger;
  const spawnWorker = options.spawnWorker ?? ((specification) => spawn(
    specification.command,
    specification.arguments,
    specification.options,
  ));
  let timer = null;
  let inFlight = null;
  let stopped = true;

  const recoverRecord = async (record) => {
    const latest = await readDocumentJobRecord(runtime.inputRoot, record.jobId);
    if (!latest || !DOCUMENT_ACTIVE_STATUSES.has(latest.status.status)) return null;
    record = latest;
    const recoveryCount = Math.max(0, Number(record.runner.recovery_count ?? 0));
    if (recoveryCount >= settings.recoveryLimit) {
      await failDocumentJob(
        record,
        `后台执行器已退出，且自动恢复已达到 ${settings.recoveryLimit} 次上限；已保留成功文件和可重试清单。`,
      );
      return null;
    }
    const successfulFiles = (record.status.files ?? []).filter((item) => item.status === "SUCCEEDED");
    const retryablePaths = retryableDocumentPaths(record.status);
    if (successfulFiles.length && !retryablePaths.length) {
      await failDocumentJob(
        record,
        "后台执行器已退出；现有文件均显示成功，但任务未形成完整终态，为避免重复 OCR 已停止自动恢复。",
      );
      return null;
    }
    const retryBaseStatus = successfulFiles.length ? {
      ...record.status,
      status: "FAILED",
      files: (record.status.files ?? []).map((item) => item.status === "SUCCEEDED" ? item : {
        ...item,
        status: "FAILED",
        error: item.error ?? "后台执行器意外退出，等待安全恢复。",
      }),
      retryable_paths: retryablePaths,
    } : null;
    const requestPayload = {
      ...record.request,
      retry_failed_only: successfulFiles.length > 0,
      retry_failed_paths: successfulFiles.length ? retryablePaths : [],
      retry_started_at: new Date(clock()).toISOString(),
      recovery_reason: "WORKER_PROCESS_NOT_ALIVE",
    };
    await startDocumentJob(config, requestPayload, record.jobId, null, {
      eventType: "DOCUMENT_JOB_RECOVERY_QUEUED",
      previousRunner: record.runner,
      recoveryCount: recoveryCount + 1,
      retryBaseStatus,
    });
    return readDocumentJobRecord(runtime.inputRoot, record.jobId);
  };

  const launchRecord = async (record) => {
    const latest = await readDocumentJobRecord(runtime.inputRoot, record.jobId);
    if (
      !latest
      || !DOCUMENT_ACTIVE_STATUSES.has(latest.status.status)
      || latest.status.status === "RUNNING"
      || ["STARTING", "RUNNING"].includes(latest.runner.state)
    ) {
      return false;
    }
    record = latest;
    const requestPath = join(record.jobDir, "request.json");
    const workerInvocationId = `WRK-${randomUUID().replaceAll("-", "").slice(0, 20).toUpperCase()}`;
    const attempt = Math.max(0, Number(record.runner.attempt ?? 0)) + 1;
    const startedAtMs = clock();
    const startedAt = new Date(startedAtMs).toISOString();
    const maxRuntimeSeconds = boundedInteger(
      record.request.max_runtime_seconds,
      1800,
      1,
      14400,
    );
    const deadlineAt = new Date(startedAtMs + maxRuntimeSeconds * 1000).toISOString();
    const requestPayload = {
      ...record.request,
      worker_job_id: record.jobId,
      worker_invocation_id: workerInvocationId,
      worker_attempt: attempt,
      worker_started_at: startedAt,
      worker_deadline_at: deadlineAt,
      worker_timeout_seconds: maxRuntimeSeconds,
      mcp_invocation_context: `${record.jobId}:${workerInvocationId}`,
    };
    const startingRunner = {
      ...record.runner,
      state: "STARTING",
      pid: null,
      queue_position: null,
      queue_concurrency: settings.concurrency,
      started_at: startedAt,
      heartbeat_at: startedAt,
      deadline_at: deadlineAt,
      max_runtime_seconds: maxRuntimeSeconds,
      timeout_policy: "ADAPTIVE_WORKLOAD",
      worker_invocation_id: workerInvocationId,
      attempt,
    };
    await atomicJsonWrite(join(record.jobDir, "runner.json"), startingRunner);
    await atomicJsonWrite(requestPath, requestPayload);
    await writeDocumentWorkerFence(record.jobDir, workerInvocationId, {
      reason: "WORKER_LAUNCH",
      attempt,
    });
    const claimed = await readDocumentJobRecord(runtime.inputRoot, record.jobId);
    if (
      stopped
      || !claimed
      || !DOCUMENT_ACTIVE_STATUSES.has(claimed.status.status)
      || claimed.runner.worker_invocation_id !== workerInvocationId
    ) {
      if (
        stopped
        && claimed
        && DOCUMENT_ACTIVE_STATUSES.has(claimed.status.status)
        && claimed.runner.worker_invocation_id === workerInvocationId
      ) {
        await atomicJsonWrite(join(record.jobDir, "runner.json"), {
          ...claimed.runner,
          state: "QUEUED",
          started_at: null,
          heartbeat_at: null,
          deadline_at: null,
          worker_invocation_id: null,
          attempt: Math.max(0, attempt - 1),
        });
      }
      return false;
    }
    let child;
    try {
      child = spawnWorker({
        command: runtime.workflowPython,
        arguments: [
          "-m",
          "services.ingestion.s0_batch_executor",
          "run",
          "--request-file",
          requestPath,
        ],
        options: {
          cwd: runtime.workflowCwd,
          detached: true,
          stdio: "ignore",
          env: {
            ...(config.workflowEnvironment ?? process.env),
            PYTHONPYCACHEPREFIX: config.workflowEnvironment?.PYTHONPYCACHEPREFIX ?? "/private/tmp/orion-document-batch-pycache",
            ORION_DOCUMENT_JOB_ID: record.jobId,
            ORION_WORKER_INVOCATION_ID: workerInvocationId,
            ORION_WORKER_DEADLINE_AT: deadlineAt,
            ORION_WORKER_TIMEOUT_SECONDS: String(maxRuntimeSeconds),
            ORION_MCP_INVOCATION_PREFIX: `${record.jobId}:${workerInvocationId}`,
          },
        },
        jobId: record.jobId,
        workerInvocationId,
      });
      if (!Number.isInteger(Number(child?.pid)) || Number(child.pid) <= 1) {
        throw new Error("批处理执行器未返回有效进程编号");
      }
    } catch (error) {
      await failDocumentJob(
        { ...record, request: requestPayload },
        `批处理执行器未能启动：${error instanceof Error ? error.message : String(error)}`,
        "DOCUMENT_JOB_START_FAILED",
      );
      return false;
    }
    const runner = {
      ...startingRunner,
      state: "RUNNING",
      pid: Number(child.pid),
    };
    await atomicJsonWrite(join(record.jobDir, "runner.json"), runner);
    const afterSpawn = await readDocumentJobRecord(runtime.inputRoot, record.jobId);
    if (!afterSpawn || !DOCUMENT_ACTIVE_STATUSES.has(afterSpawn.status.status)) {
      stopDocumentRunner(runner, record.jobDir);
      if (afterSpawn) {
        await atomicJsonWrite(join(record.jobDir, "runner.json"), {
          ...afterSpawn.runner,
          state: afterSpawn.status.status,
          stopped_at: new Date(clock()).toISOString(),
        });
      }
      return false;
    }
    await appendDocumentJobEvent(record.jobDir, "DOCUMENT_JOB_STARTED", {
      ...record.status,
      status: "RUNNING",
    }, {
      attempt,
      deadline_at: deadlineAt,
      job_id: record.jobId,
      server_timeout_seconds: maxRuntimeSeconds,
      worker_invocation_id: workerInvocationId,
    });
    child.once?.("error", async (error) => {
      const latest = await readDocumentJobRecord(runtime.inputRoot, record.jobId).catch(() => null);
      if (
        latest
        && DOCUMENT_ACTIVE_STATUSES.has(latest.status.status)
        && latest.runner.worker_invocation_id === workerInvocationId
      ) {
        await failDocumentJob(
          latest,
          `批处理执行器异常：${error instanceof Error ? error.message : String(error)}`,
          "DOCUMENT_JOB_WORKER_ERROR",
        ).catch(() => {});
      }
    });
    child.once?.("exit", () => {
      void wake();
    });
    child.unref?.();
    return true;
  };

  const runTickUnlocked = async () => {
    const nowMs = clock();
    const queue = [];
    let running = 0;
    for (let record of await listDocumentJobRecords(runtime.inputRoot)) {
      if (!DOCUMENT_ACTIVE_STATUSES.has(record.status.status)) {
        if (record.runner.state && record.runner.state !== record.status.status) {
          await atomicJsonWrite(join(record.jobDir, "runner.json"), {
            ...record.runner,
            state: record.status.status,
            queue_position: null,
            finished_at: record.status.finished_at ?? new Date(nowMs).toISOString(),
          });
        }
        continue;
      }
      const wasStarted = record.status.status === "RUNNING"
        || ["STARTING", "RUNNING"].includes(record.runner.state);
      if (!wasStarted) {
        queue.push(record);
        continue;
      }
      const recommendedRuntime = boundedInteger(
        record.status.recommended_runtime_seconds,
        Number(record.runner.max_runtime_seconds ?? record.request.max_runtime_seconds ?? 1800),
        1,
        14400,
      );
      const startedAtMs = timestampValue(record.runner.started_at ?? record.status.started_at);
      let runner = record.runner;
      if (startedAtMs !== null) {
        const adaptiveDeadlineMs = startedAtMs + recommendedRuntime * 1000;
        const currentDeadlineMs = timestampValue(runner.deadline_at);
        if (currentDeadlineMs === null || adaptiveDeadlineMs > currentDeadlineMs) {
          runner = {
            ...runner,
            deadline_at: new Date(adaptiveDeadlineMs).toISOString(),
            max_runtime_seconds: recommendedRuntime,
            timeout_policy: "ADAPTIVE_WORKLOAD",
          };
          await atomicJsonWrite(join(record.jobDir, "runner.json"), runner);
          record = { ...record, runner };
        }
      }
      const deadlineMs = timestampValue(runner.deadline_at);
      if (deadlineMs !== null && nowMs >= deadlineMs) {
        try {
          await interruptDocumentJob(
            runtime.inputRoot,
            record.jobId,
            "TIMED_OUT",
            "后台资料任务已超过服务端截止时间；成功文件和证据已保留，可安全重试未完成文件。",
            "orion-document-watchdog",
          );
        } catch (error) {
          if (error?.code !== "STATE_CONFLICT") throw error;
        }
        continue;
      }
      let alive = false;
      try {
        alive = processAlive(runner.pid, record.jobDir) === true;
      } catch {
        alive = false;
      }
      if (alive) {
        running += 1;
        const heartbeatMs = timestampValue(runner.heartbeat_at);
        if (heartbeatMs === null || nowMs - heartbeatMs >= settings.heartbeatMs) {
          await atomicJsonWrite(join(record.jobDir, "runner.json"), {
            ...runner,
            state: "RUNNING",
            heartbeat_at: new Date(nowMs).toISOString(),
            queue_concurrency: settings.concurrency,
          });
        }
        continue;
      }
      const exitReferenceMs = timestampValue(
        runner.heartbeat_at ?? runner.started_at ?? record.status.started_at ?? record.status.created_at,
      );
      if (exitReferenceMs !== null && nowMs - exitReferenceMs < settings.exitGraceMs) {
        running += 1;
        continue;
      }
      const recovered = await recoverRecord(record);
      if (recovered) queue.push(recovered);
    }
    queue.sort((left, right) => {
      const leftTime = String(left.runner.queued_at ?? left.status.queued_at ?? left.status.created_at ?? "");
      const rightTime = String(right.runner.queued_at ?? right.status.queued_at ?? right.status.created_at ?? "");
      return leftTime.localeCompare(rightTime) || left.jobId.localeCompare(right.jobId);
    });
    while (!stopped && running < settings.concurrency && queue.length) {
      const record = queue.shift();
      if (await launchRecord(record)) running += 1;
    }
    for (const [index, record] of queue.entries()) {
      if (
        record.runner.state !== "QUEUED"
        || record.runner.queue_position !== index + 1
        || record.runner.queue_concurrency !== settings.concurrency
      ) {
        await atomicJsonWrite(join(record.jobDir, "runner.json"), {
          ...record.runner,
          state: "QUEUED",
          pid: null,
          queue_position: index + 1,
          queue_concurrency: settings.concurrency,
        });
      }
    }
  };

  const runTick = async () => {
    const release = await acquireDocumentSchedulerLock(runtime.inputRoot, clock);
    if (!release) return false;
    try {
      await runTickUnlocked();
      return true;
    } finally {
      await release();
    }
  };

  const tick = () => {
    if (stopped) return Promise.resolve();
    if (inFlight) return inFlight;
    inFlight = runTick()
      .catch((error) => {
        logger?.error?.(`S0 资料任务调度失败：${error instanceof Error ? error.stack ?? error.message : String(error)}`);
      })
      .finally(() => {
        inFlight = null;
      });
    return inFlight;
  };

  const wake = () => tick();
  const start = () => {
    if (timer) return;
    stopped = false;
    timer = setInterval(() => {
      void tick();
    }, settings.intervalMs);
    timer.unref?.();
    void tick();
  };
  const stop = () => {
    stopped = true;
    if (timer) clearInterval(timer);
    timer = null;
    return inFlight ?? Promise.resolve();
  };
  return {
    settings,
    start,
    stop,
    tick,
    wake,
    state: () => ({
      in_flight: Boolean(inFlight),
      started: !stopped,
      timer_count: timer ? 1 : 0,
    }),
  };
}

async function startDocumentJob(
  config,
  requestPayload,
  existingJobId = null,
  scheduler = null,
  options = {},
) {
  const actor = requireWorkflowActor(config);
  const runtime = documentRuntime(config);
  const jobId = existingJobId ?? `JOB-${Date.now()}-${randomUUID().replaceAll("-", "").slice(0, 12).toUpperCase()}`;
  const jobDir = documentJobDirectory(runtime.inputRoot, jobId);
  await mkdir(jobDir, { recursive: true });
  const previousStatus = existingJobId
    ? await readJson(join(jobDir, "status.json"), null)
    : null;
  const previousRunner = options.previousRunner ?? (existingJobId
    ? await readJson(join(jobDir, "runner.json"), null)
    : null);
  if (existingJobId) {
    await rotateDocumentWorkerFence(jobDir, "JOB_REQUEUED", actor);
  }
  if (requestPayload.retry_failed_only === true && (options.retryBaseStatus ?? previousStatus)) {
    await atomicJsonWrite(
      join(jobDir, "retry-base-status.json"),
      options.retryBaseStatus ?? previousStatus,
    );
  }
  const requestPath = join(jobDir, "request.json");
  const queuedAt = new Date().toISOString();
  const payload = {
    ...requestPayload,
    job_id: jobId,
    created_at: requestPayload.created_at ?? new Date().toISOString(),
    input_root: runtime.inputRoot,
    workflow_home: runtime.workflowHome,
    actor,
    structure_endpoint: config.mcpControl?.paddleocrStructureUrl ?? "http://127.0.0.1:10826/mcp",
    ocr_endpoint: config.mcpControl?.paddleocrOcrUrl ?? "http://127.0.0.1:10827/mcp",
    ocr_render_dpi: Number(process.env.ORION_OCR_RENDER_DPI ?? 150),
    structure_render_dpi: Number(process.env.ORION_STRUCTURE_RENDER_DPI ?? 200),
    reuse_content_cache: requestPayload.reuse_content_cache === true,
    max_runtime_seconds: Math.max(1, Math.min(14400, Number(requestPayload.max_runtime_seconds ?? 1800))),
    queued_at: queuedAt,
    worker_job_id: null,
    worker_invocation_id: null,
    worker_attempt: null,
    worker_started_at: null,
    worker_deadline_at: null,
    worker_timeout_seconds: null,
    mcp_invocation_context: null,
  };
  await atomicJsonWrite(requestPath, payload);
  const successfulFiles = requestPayload.retry_failed_only === true
    ? ((options.retryBaseStatus ?? previousStatus)?.files ?? [])
      .filter((item) => item.status === "SUCCEEDED")
    : [];
  const settings = documentSchedulerSettings();
  const runner = {
    state: "QUEUED",
    pid: null,
    queue_position: null,
    queue_concurrency: scheduler?.settings?.concurrency ?? settings.concurrency,
    queued_at: queuedAt,
    started_at: null,
    heartbeat_at: null,
    deadline_at: null,
    max_runtime_seconds: payload.max_runtime_seconds,
    timeout_policy: "ADAPTIVE_WORKLOAD",
    worker_invocation_id: null,
    attempt: Math.max(0, Number(previousRunner?.attempt ?? 0)),
    recovery_count: Math.max(
      0,
      Number(options.recoveryCount ?? previousRunner?.recovery_count ?? 0),
    ),
  };
  await atomicJsonWrite(join(jobDir, "runner.json"), runner);
  const status = {
    job_id: jobId,
    status: "QUEUED",
    message: requestPayload.retry_failed_only === true
      ? `已保留 ${successfulFiles.length} 个成功文件，准备仅重试失败文件。`
      : "任务已进入持久队列，等待后台执行器处理。",
    created_at: payload.created_at,
    queued_at: queuedAt,
    source_path: payload.source_path,
    intake_mode: payload.intake_mode ?? "DOCUMENT_ONLY",
    requested_intake_mode: payload.requested_intake_mode ?? payload.intake_mode ?? "DOCUMENT_ONLY",
    source_preflight: payload.source_preflight ?? null,
    recommended_structured_data_action: payload.recommended_structured_data_action ?? "DOCUMENT_ONLY",
    content_cache_enabled: payload.reuse_content_cache === true,
    project_id: String(payload.project_id ?? "").trim() || null,
    project_name: String(payload.project_name ?? "").trim() || null,
    domain: String(payload.domain ?? "").trim() || null,
    project_request_id: String(payload.project_request_id ?? "").trim() || null,
    retry_failed_only: requestPayload.retry_failed_only === true,
    total_files: requestPayload.retry_failed_only === true ? Number(previousStatus?.total_files ?? 0) : 0,
    completed_files: successfulFiles.length,
    failed_files: 0,
    total_pages: requestPayload.retry_failed_only === true ? Number(previousStatus?.total_pages ?? 0) : 0,
    processed_pages: successfulFiles.reduce((total, item) => total + Number(item.processed_pages ?? 0), 0),
    current_file: null,
    current_file_index: 0,
    current_page: null,
    current_file_pages: 0,
    progress_phase: "QUEUED",
    files: successfulFiles,
    warnings: requestPayload.retry_failed_only === true
      ? ((options.retryBaseStatus ?? previousStatus)?.warnings ?? [])
      : [],
    discovered_paths: requestPayload.retry_failed_only === true
      ? ((options.retryBaseStatus ?? previousStatus)?.discovered_paths ?? [])
      : [],
  };
  await atomicJsonWrite(join(jobDir, "status.json"), status);
  await appendDocumentJobEvent(jobDir, options.eventType ?? "DOCUMENT_JOB_QUEUED", status, {
    queue_concurrency: runner.queue_concurrency,
    recovery_count: runner.recovery_count,
    retry_failed_only: payload.retry_failed_only === true,
  });
  void scheduler?.wake?.();
  return {
    job_id: jobId,
    status: "QUEUED",
    message: "资料批处理已排队，关闭页面后仍会继续调度。",
    intake_mode: payload.intake_mode,
    requested_intake_mode: payload.requested_intake_mode,
    source_preflight: payload.source_preflight,
    recommended_structured_data_action: payload.recommended_structured_data_action,
    content_cache_enabled: payload.reuse_content_cache === true,
    queue_concurrency: runner.queue_concurrency,
    queue_position: null,
  };
}

async function findReusableDocumentJob(root, { projectId, sourcePath, requestId }) {
  const jobsRoot = join(root, ".orion-s0-jobs");
  const candidates = [];
  for (const entry of await readdir(jobsRoot, { withFileTypes: true }).catch(() => [])) {
    if (!entry.isDirectory() || !DOCUMENT_JOB_ID_PATTERN.test(entry.name)) continue;
    const job = await loadDocumentJob(root, entry.name);
    if (!job) continue;
    const exactRequest = requestId && job.project_request_id === requestId;
    const sameActiveSource = DOCUMENT_ACTIVE_STATUSES.has(job.status)
      && projectId
      && job.project_id === projectId
      && job.source_path === sourcePath;
    if (exactRequest || sameActiveSource) candidates.push(job);
  }
  candidates.sort((left, right) => String(right.created_at ?? "").localeCompare(String(left.created_at ?? "")));
  return candidates[0] ?? null;
}

export {
  DOCUMENT_ACTIVE_STATUSES,
  DOCUMENT_JOB_ID_PATTERN,
  acquireDocumentSchedulerLock,
  createDocumentJobScheduler,
  documentJobDirectory,
  documentRuntime,
  findReusableDocumentJob,
  interruptDocumentJob,
  loadDocumentJob,
  startDocumentJob,
};
