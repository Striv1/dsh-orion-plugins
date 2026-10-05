import { createHash, randomUUID } from "node:crypto";
import { constants as fsConstants, createReadStream, createWriteStream } from "node:fs";
import { chmod, copyFile, link, lstat, mkdir, readFile, readdir, rename, stat, unlink, writeFile } from "node:fs/promises";
import { basename, dirname, extname, join, normalize, resolve, sep } from "node:path";
import { Transform } from "node:stream";
import { pipeline } from "node:stream/promises";

import { MIME, jsonResponse, readRequestJson } from "./http-response.js";
import { readJson } from "./runtime-files.js";
import {
  DOCUMENT_ACTIVE_STATUSES,
  DOCUMENT_JOB_ID_PATTERN,
  documentJobDirectory,
  documentRuntime,
  findReusableDocumentJob,
  interruptDocumentJob,
  loadDocumentJob,
  requireWorkflowActor,
  startDocumentJob,
} from "./document-jobs.js";

const DOCUMENT_CONFIRMATION_HEADER = "x-orion-document-confirmation";
const DOCUMENT_UPLOAD_ID_PATTERN = /^UPLOAD-[A-Z0-9-]{8,80}$/;
const DOCUMENT_UPLOAD_EXTENSIONS = new Set([
  ".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".tsv",
  ".txt", ".md", ".html", ".htm", ".xml", ".json", ".yaml", ".yml", ".eml",
  ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp",
]);
const MAX_DOCUMENT_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024;
const DOCUMENT_RETRYABLE_STATUSES = new Set(["FAILED", "CANCELLED", "TIMED_OUT"]);

const streamedFileSha256 = async (path) => {
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(path)) digest.update(chunk);
  return digest.digest("hex");
};

async function countDocumentInputs(root) {
  const counts = { markdown: 0, pdf: 0, word: 0, spreadsheet: 0, image: 0, otherReady: 0 };
  let rootAvailable = false;

  const visit = async (directory) => {
    for (const entry of await readdir(directory, { withFileTypes: true }).catch(() => [])) {
      if (entry.name.startsWith(".orion-s0-")) continue;
      const target = join(directory, entry.name);
      if (entry.isDirectory()) {
        await visit(target);
        continue;
      }
      if (!entry.isFile()) continue;
      const extension = extname(entry.name).toLowerCase();
      if (extension === ".md") counts.markdown += 1;
      if (extension === ".pdf") counts.pdf += 1;
      if (extension === ".docx") counts.word += 1;
      if ([".xlsx", ".xlsm", ".csv", ".tsv"].includes(extension)) counts.spreadsheet += 1;
      if ([".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"].includes(extension)) counts.image += 1;
      if (DOCUMENT_UPLOAD_EXTENSIONS.has(extension) && ![".md", ".pdf", ".docx", ".xlsx", ".xlsm", ".csv", ".tsv", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"].includes(extension)) counts.otherReady += 1;
    }
  };

  try {
    rootAvailable = (await stat(root)).isDirectory();
    if (rootAvailable) await visit(root);
  } catch {
    rootAvailable = false;
  }
  return { ...counts, rootAvailable };
}

const childPath = (root, candidate) => {
  const resolvedRoot = resolve(root);
  const target = resolve(resolvedRoot, normalize(candidate));
  if (target !== resolvedRoot && !target.startsWith(resolvedRoot + sep)) {
    throw new Error("资料路径超出已批准的接入目录");
  }
  return target;
};

const documentUploadDirectory = (root, uploadId) => {
  if (!DOCUMENT_UPLOAD_ID_PATTERN.test(uploadId)) throw new Error("上传批次编号不合法");
  return join(root, ".orion-s0-uploads", uploadId);
};

const documentContentRoot = (root) => join(root, ".orion-s0-content");

const documentContentLocation = (root, digestHex) => {
  if (!/^[a-f0-9]{64}$/u.test(digestHex)) throw new Error("资料内容摘要不合法");
  return join(documentContentRoot(root), "sha256", digestHex.slice(0, 2), digestHex);
};

const documentContentReferencePath = (root, uploadId, requestedName) => {
  if (!DOCUMENT_UPLOAD_ID_PATTERN.test(uploadId)) throw new Error("上传批次编号不合法");
  const key = createHash("sha256").update(requestedName).digest("hex");
  return join(documentContentRoot(root), "references", "uploads", uploadId, `${key}.json`);
};

const atomicPrivateJsonWrite = async (path, payload) => {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${process.pid}.${randomUUID()}.tmp`;
  await writeFile(temporary, `${JSON.stringify(payload, null, 2)}\n`, {
    encoding: "utf8",
    mode: 0o600,
  });
  await rename(temporary, path);
};

const contentFallbackError = (error) => ["EXDEV", "EPERM", "EACCES", "ENOSYS", "ENOTSUP"]
  .includes(error?.code);

async function verifyContentObject(path, digestHex, expectedBytes) {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink()) {
    throw new Error("内容库摘要位置不是受控普通文件");
  }
  if (info.size !== expectedBytes || await streamedFileSha256(path) !== digestHex) {
    throw new Error("内容库中同摘要对象的大小或哈希不一致");
  }
}

async function publishContentObject(partial, contentPath, digestHex, expectedBytes) {
  await mkdir(dirname(contentPath), { recursive: true, mode: 0o700 });
  let deduplicated = false;
  let storageMode = "HARD_LINK";
  try {
    await link(partial, contentPath);
  } catch (error) {
    if (error?.code === "EEXIST") {
      deduplicated = true;
    } else if (contentFallbackError(error)) {
      storageMode = "COPY_FALLBACK";
      try {
        await copyFile(partial, contentPath, fsConstants.COPYFILE_EXCL);
      } catch (copyError) {
        if (copyError?.code !== "EEXIST") throw copyError;
        deduplicated = true;
      }
    } else {
      throw error;
    }
  }
  if (deduplicated) await verifyContentObject(contentPath, digestHex, expectedBytes);
  else await chmod(contentPath, 0o400);
  return { deduplicated, storageMode };
}

async function materializeUploadReference(contentPath, target) {
  try {
    await link(contentPath, target);
    return "HARD_LINK";
  } catch (error) {
    if (!contentFallbackError(error)) throw error;
    await copyFile(contentPath, target, fsConstants.COPYFILE_EXCL);
    await chmod(target, 0o400);
    return "COPY_FALLBACK";
  }
}

// HTTP ingress authentication and same-origin checks belong to the host before
// dispatch. These capabilities connect ingestion to existing workflow and MCP APIs.
export function createDocumentApi({
  executeFile,
  loadWorkflowDashboard,
  probeMcpHttp,
  runWorkflowTool,
  safeProjectId,
}) {
  async function loadRouteCatalog(config) {
    const runtime = documentRuntime(config);
    try {
      const result = await executeFile(
        runtime.workflowPython,
        ["-m", "services.ingestion.unstructured_router"],
        {
          cwd: runtime.workflowCwd,
          env: {
            ...(config.workflowEnvironment ?? process.env),
            PYTHONPYCACHEPREFIX: config.workflowEnvironment?.PYTHONPYCACHEPREFIX ?? "/private/tmp/orion-document-router-pycache",
          },
          timeout: 5000,
          maxBuffer: 2 * 1024 * 1024,
        },
      );
      return JSON.parse(result.stdout || "{}");
    } catch {
      return { router: "ORION_UNSTRUCTURED_ROUTER", version: "unavailable", categories: [] };
    }
  }

  async function inspectDocumentSource(config, source, maxDocumentRows = 10000) {
    const runtime = documentRuntime(config);
    const result = await executeFile(
      runtime.workflowPython,
      [
        "-m", "services.ingestion.source_preflight",
        "--source", source,
        "--max-document-rows", String(maxDocumentRows),
      ],
      {
        cwd: runtime.workflowCwd,
        env: {
          ...(config.workflowEnvironment ?? process.env),
          PYTHONPYCACHEPREFIX: config.workflowEnvironment?.PYTHONPYCACHEPREFIX ?? "/private/tmp/orion-source-preflight-pycache",
        },
        timeout: 60000,
        maxBuffer: 4 * 1024 * 1024,
      },
    );
    const preflight = JSON.parse(result.stdout || "{}");
    if (preflight.schema_version !== 1 || !Array.isArray(preflight.files)) {
      throw new Error("数据源预检没有返回可验证的路线结果。");
    }
    return preflight;
  }

  async function serveDocumentJobArtifact(request, response, root, jobId, relativePath) {
    const jobDir = documentJobDirectory(root, jobId);
    const target = childPath(jobDir, relativePath);
    try {
      const info = await stat(target);
      if (!info.isFile()) throw new Error("not a file");
      const body = await readFile(target);
      response.writeHead(200, {
        "cache-control": "no-store",
        "content-type": MIME[extname(target)] ?? "text/plain; charset=utf-8",
      });
      response.end(request.method === "HEAD" ? undefined : body);
    } catch {
      jsonResponse(response, 404, { detail: "资料任务产物不存在" }, request.method);
    }
  }

  async function serveDocumentIngestionApi(request, response, config, scheduler = null) {
    const incoming = new URL(request.url ?? "/", "http://local");
    if (
      config.documentIngestionApiEnabled !== true ||
      !config.documentIngestionRoot
    ) {
      jsonResponse(
        response,
        404,
        { detail: "当前 Harness 模式未启用文档接入状态 API" },
        request.method,
      );
      return;
    }
    let actor;
    if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method)) {
      try {
        actor = requireWorkflowActor(config);
      } catch (error) {
        jsonResponse(response, 403, { detail: error.message }, request.method);
        return;
      }
    }
    const runtime = documentRuntime(config);
    const pathParts = incoming.pathname.split("/").filter(Boolean);
    if (incoming.pathname === "/orion-document-api/status") {
      const inventory = await countDocumentInputs(runtime.inputRoot);
      const structureEndpoint =
        config.mcpControl?.paddleocrStructureUrl ?? "http://127.0.0.1:10826/mcp";
      const ocrEndpoint =
        config.mcpControl?.paddleocrOcrUrl ?? "http://127.0.0.1:10827/mcp";
      const [structureProbe, ocrProbe, routes] = await Promise.all([
        probeMcpHttp(structureEndpoint),
        probeMcpHttp(ocrEndpoint),
        loadRouteCatalog(config),
      ]);
      const executionReady = inventory.rootAvailable;
      const probeStatus = (probe, { endpoint, tool }) => probe.ok ? {
        status: "CONNECTED",
        name: probe.serverName,
        version: probe.serverVersion,
        endpoint,
        tool,
      } : probe.detail === "MCP initialize 超时" ? {
        status: "BUSY",
        endpoint,
        tool,
        detail: "服务正在处理任务，健康检查暂未在 4 秒内返回。",
      } : {
        status: "UNAVAILABLE",
        endpoint,
        tool,
        detail: probe.detail,
      };
      const anyUnavailable = [structureProbe, ocrProbe].some(
        (probe) => !probe.ok && probe.detail !== "MCP initialize 超时",
      );
      const anyBusy = [structureProbe, ocrProbe].some(
        (probe) => !probe.ok && probe.detail === "MCP initialize 超时",
      );
      jsonResponse(
        response,
        200,
        {
          status: executionReady
            ? structureProbe.ok && ocrProbe.ok
              ? "READY"
              : anyUnavailable
                ? "READY_WITH_MCP_GAP"
                : anyBusy ? "READY_WITH_MCP_BUSY" : "READY_WITH_MCP_GAP"
            : "NOT_READY",
          mode: "batch_execution_with_human_commit",
          webmcp_tool: "inspect-document-ingestion",
          source_inventory: {
            root_configured: true,
            root_available: inventory.rootAvailable,
            markdown_file_count: inventory.markdown,
            pdf_file_count: inventory.pdf,
            word_file_count: inventory.word,
            spreadsheet_file_count: inventory.spreadsheet,
            image_file_count: inventory.image,
            other_ready_file_count: inventory.otherReady,
          },
          runtime_pipeline: {
            browser_multi_file_upload: true,
            approved_local_path_input: executionReady,
            direct_pdf_text_detection: true,
            structured_markdown_generation: executionReady,
            human_review_before_s0_commit: true,
            providers: {
              structure: probeStatus(structureProbe, { endpoint: structureEndpoint, tool: "pp_structurev3" }),
              ocr: probeStatus(ocrProbe, { endpoint: ocrEndpoint, tool: "ocr" }),
            },
          },
          router: routes,
          safety: {
            arbitrary_local_path_access: false,
            local_path_allowlist_required: true,
            upload_extensions: [...DOCUMENT_UPLOAD_EXTENSIONS].sort(),
            maximum_single_upload_bytes: MAX_DOCUMENT_UPLOAD_BYTES,
            automatic_s0_commit: false,
          },
        },
        request.method,
      );
      return;
    }
    if (incoming.pathname === "/orion-document-api/routes") {
      jsonResponse(response, 200, await loadRouteCatalog(config), request.method);
      return;
    }
    if (incoming.pathname === "/orion-document-api/uploads" && request.method === "POST") {
      const uploadId = `UPLOAD-${Date.now()}-${randomUUID().replaceAll("-", "").slice(0, 12).toUpperCase()}`;
      const uploadDir = documentUploadDirectory(runtime.inputRoot, uploadId);
      await mkdir(uploadDir, { recursive: true });
      jsonResponse(response, 201, {
        upload_id: uploadId,
        source_path: `.orion-s0-uploads/${uploadId}`,
        message: "上传批次已创建。",
      }, request.method);
      return;
    }
    if (pathParts[1] === "uploads" && pathParts[2] && pathParts[3] === "file" && request.method === "PUT") {
      let partial = null;
      let createdTarget = null;
      try {
        const uploadId = pathParts[2];
        const uploadDir = documentUploadDirectory(runtime.inputRoot, uploadId);
        const requestedName = String(incoming.searchParams.get("name") ?? "").replaceAll("\\", "/");
        const pathSegments = requestedName.split("/");
        const fileName = pathSegments.at(-1) ?? "";
        if (
          !fileName
          || requestedName.startsWith("/")
          || requestedName.includes("\0")
          || pathSegments.some((segment) => !segment || segment === "." || segment === ".." || basename(segment) !== segment)
        ) {
          jsonResponse(response, 400, { detail: "上传文件名不合法" }, request.method);
          return;
        }
        const uploadExtension = extname(fileName).toLowerCase();
        if (!DOCUMENT_UPLOAD_EXTENSIONS.has(uploadExtension)) {
          jsonResponse(response, 415, {
            detail: `文件“${fileName}”的格式 ${uploadExtension || "（无扩展名）"} 尚未启用；其余支持的资料可继续添加。`,
            file_name: requestedName,
            extension: uploadExtension,
            supported_extensions: [...DOCUMENT_UPLOAD_EXTENSIONS].sort(),
          }, request.method);
          return;
        }
        const declaredLength = Number(request.headers["content-length"] ?? 0);
        if (declaredLength > MAX_DOCUMENT_UPLOAD_BYTES) {
          jsonResponse(response, 413, { detail: "单个文件超过 2GB 上限" }, request.method);
          return;
        }
        await mkdir(uploadDir, { recursive: true });
        const target = childPath(uploadDir, requestedName);
        await mkdir(dirname(target), { recursive: true });
        partial = `${target}.${randomUUID()}.partial`;
        let received = 0;
        const uploadDigest = createHash("sha256");
        const hashingStream = new Transform({
          transform(chunk, _encoding, callback) {
            received += chunk.length;
            if (received > MAX_DOCUMENT_UPLOAD_BYTES) {
              const error = new Error("文件超过 2GB 上限");
              error.code = "FILE_TOO_LARGE";
              callback(error);
              return;
            }
            uploadDigest.update(chunk);
            callback(null, chunk);
          },
        });
        await pipeline(
          request,
          hashingStream,
          createWriteStream(partial, { flags: "wx", mode: 0o600 }),
        );
        const digestHex = uploadDigest.digest("hex");
        const digest = `sha256:${digestHex}`;
        const contentPath = documentContentLocation(runtime.inputRoot, digestHex);
        const contentResult = await publishContentObject(
          partial,
          contentPath,
          digestHex,
          received,
        );
        const linkMode = await materializeUploadReference(contentPath, target);
        createdTarget = target;
        const contentReference = `sha256/${digestHex.slice(0, 2)}/${digestHex}`;
        await atomicPrivateJsonWrite(
          documentContentReferencePath(runtime.inputRoot, uploadId, requestedName),
          {
            schema_version: 1,
            upload_id: uploadId,
            file_name: requestedName,
            batch_source_path: `.orion-s0-uploads/${uploadId}`,
            digest,
            bytes: received,
            content_path: contentReference,
            deduplicated: contentResult.deduplicated,
            content_store_mode: contentResult.storageMode,
            batch_reference_mode: linkMode,
            recorded_at: new Date().toISOString(),
          },
        );
        await unlink(partial).catch(() => {});
        partial = null;
        jsonResponse(response, 201, {
          file_name: requestedName,
          bytes: received,
          source_path: `.orion-s0-uploads/${uploadId}`,
          digest,
          deduplicated: contentResult.deduplicated,
          content_path: contentReference,
          link_mode: linkMode,
        }, request.method);
      } catch (error) {
        if (createdTarget) await unlink(createdTarget).catch(() => {});
        if (partial) await unlink(partial).catch(() => {});
        jsonResponse(response, error?.code === "EEXIST" ? 409 : error?.code === "FILE_TOO_LARGE" ? 413 : 500, {
          detail: error instanceof Error ? error.message : "文件上传失败",
        }, request.method);
      }
      return;
    }
    if (incoming.pathname === "/orion-document-api/jobs" && request.method === "GET") {
      const jobsRoot = join(runtime.inputRoot, ".orion-s0-jobs");
      const jobs = [];
      const rawProjectId = incoming.searchParams.get("project_id");
      const requestedProjectId = rawProjectId ? safeProjectId(rawProjectId) : null;
      if (rawProjectId && !requestedProjectId) {
        jsonResponse(response, 400, { detail: "project_id 不合法" }, request.method);
        return;
      }
      for (const entry of await readdir(jobsRoot, { withFileTypes: true }).catch(() => [])) {
        if (!entry.isDirectory() || !DOCUMENT_JOB_ID_PATTERN.test(entry.name)) continue;
        const job = await loadDocumentJob(runtime.inputRoot, entry.name);
        if (job && (!requestedProjectId || job.project_id === requestedProjectId)) jobs.push(job);
      }
      jobs.sort((left, right) => String(right.created_at ?? "").localeCompare(String(left.created_at ?? "")));
      const requestedLimit = Number(incoming.searchParams.get("limit") ?? jobs.length);
      const limit = Number.isFinite(requestedLimit) ? Math.max(1, Math.min(50, requestedLimit)) : jobs.length;
      jsonResponse(response, 200, { jobs: jobs.slice(0, limit) }, request.method);
      return;
    }
    if (incoming.pathname === "/orion-document-api/jobs" && request.method === "POST") {
      try {
        const payload = await readRequestJson(request);
        const projectName = String(payload.project_name ?? "").trim();
        const domain = String(payload.domain ?? "").trim();
        const rationale = String(payload.intake_rationale ?? "").trim();
        const requestedSource = String(payload.source_path ?? "").trim();
        const intakeMode = String(payload.intake_mode ?? "DOCUMENT_ONLY").trim();
        const cqMode = String(payload.cq_mode ?? "USER_PLUS_AI").trim().toUpperCase();
        const initialCompetencyQuestions = Array.isArray(payload.initial_competency_questions)
          ? payload.initial_competency_questions
          : [];
        const requestedProjectId = String(payload.project_id ?? "").trim();
        if (!projectName || !domain || !rationale) {
          jsonResponse(response, 400, { detail: "请完整填写工程名称、业务领域和接入理由。" }, request.method);
          return;
        }
        if (!["DOCUMENT_ONLY", "HYBRID"].includes(intakeMode)) {
          jsonResponse(response, 400, { detail: "资料路由只支持文件资料建模或混合建模；数据库建模请从 S1 数据接入入口开始。" }, request.method);
          return;
        }
        if (!["USER_PROVIDED", "USER_PLUS_AI", "AI_GENERATED"].includes(cqMode)) {
          jsonResponse(response, 400, { detail: "业务问题方式不合法。" }, request.method);
          return;
        }
        if (cqMode === "USER_PROVIDED" && initialCompetencyQuestions.length === 0) {
          jsonResponse(response, 400, { detail: "只采用人工问题时，至少填写 1 个业务问题。" }, request.method);
          return;
        }
        if (payload.independent_project !== true) {
          jsonResponse(response, 400, { detail: "必须确认这是独立本体工程，不修改当前工程。" }, request.method);
          return;
        }
        if (requestedProjectId) {
          const dashboard = await loadWorkflowDashboard(runtime.workflowHome, requestedProjectId, { environment: config.workflowEnvironment });
          const dashboardMode = String(dashboard?.project?.intake_mode ?? dashboard?.state?.intake_mode ?? "");
          const dashboardStage = String(dashboard?.state?.current_stage ?? dashboard?.project?.current_stage ?? "");
          if (!dashboard) {
            jsonResponse(response, 404, { detail: "新建工程已不存在，不能启动资料任务。" }, request.method);
            return;
          }
          if (dashboardMode !== intakeMode || dashboardStage !== "S0") {
            jsonResponse(response, 409, { detail: "工程路径或当前阶段已变化；请刷新后重新确认资料任务。" }, request.method);
            return;
          }
          if ((dashboard.state?.stage_contract_version ?? dashboard.project?.stage_contract_version) === "s0-s7-stage-contract-v2") {
            if (!Number.isInteger(payload.expected_revision) || payload.expected_revision < 0) {
              jsonResponse(response, 409, { detail: "缺少当前工程修订号；请刷新后重新启动资料任务。" }, request.method);
              return;
            }
            // The same workflow producer validates the source manifest and scope
            // before any content inspection, for both UI and MCP entry points.
            const queued = await runWorkflowTool(config, "start_document_ingestion_job", {
              project_id: requestedProjectId,
              source_path: requestedSource,
              expected_revision: payload.expected_revision,
              actor,
            });
            if (queued.ok !== true || !queued.result?.job_id) {
              jsonResponse(response, 409, { detail: queued.detail ?? "资料来源未通过当前工程范围预检。" }, request.method);
              return;
            }
            void scheduler?.wake?.();
            const job = queued.result;
            jsonResponse(response, DOCUMENT_ACTIVE_STATUSES.has(job.status) ? 202 : 200, {
              ...job,
              requested_intake_mode: intakeMode,
              content_cache_enabled: false,
              reused: job.reused_existing_job === true,
            }, request.method);
            return;
          }
        }
        const source = childPath(runtime.inputRoot, requestedSource);
        const sourceInfo = await stat(source).catch(() => null);
        if (!sourceInfo || (!sourceInfo.isDirectory() && !sourceInfo.isFile())) {
          jsonResponse(response, 400, { detail: "资料来源不存在或不可读取。" }, request.method);
          return;
        }
        const sourcePath = source === runtime.inputRoot
          ? "."
          : source.slice(runtime.inputRoot.length + 1).split(sep).join("/");
        const maxExcelRowsPerSheet = Math.max(
          1,
          Number(payload.max_excel_rows_per_sheet ?? 10000),
        );
        let sourcePreflight;
        let sourceSnapshot = null;
        if (!requestedProjectId && (sourcePath === ".orion-s0-uploads" || sourcePath.startsWith(".orion-s0-uploads/"))) {
          const checked = await runWorkflowTool(config, "preflight_workspace_snapshot", { source_path: sourcePath });
          if (checked.ok !== true || !checked.result?.source_snapshot) {
            jsonResponse(response, 409, { detail: checked.detail ?? "资料批次缺少可验证的选定来源清单。" }, request.method);
            return;
          }
          const { source_snapshot: snapshot, ...preflight } = checked.result;
          sourceSnapshot = snapshot;
          sourcePreflight = preflight;
        } else {
          sourcePreflight = await inspectDocumentSource(config, source, maxExcelRowsPerSheet);
        }
        const requiresStructuredImport = sourcePreflight.requires_structured_import === true;
        const effectiveIntakeMode = requiresStructuredImport ? "HYBRID" : intakeMode;
        if (requestedProjectId && effectiveIntakeMode !== intakeMode) {
          jsonResponse(response, 409, {
            detail: "数据源预检发现大表，但当前工程是 DOCUMENT_ONLY。为保护工程版本，请新建 HYBRID 工程后重新提交。",
            code: "SOURCE_ROUTE_CONFLICT",
            requested_intake_mode: intakeMode,
            recommended_intake_mode: effectiveIntakeMode,
            recommended_structured_data_action: "IMPORT",
            source_preflight: sourcePreflight,
          }, request.method);
          return;
        }
        const projectRequestId = String(payload.project_request_id ?? "").trim()
          || `document-job:${randomUUID()}`;
        const reusableJob = await findReusableDocumentJob(runtime.inputRoot, {
          projectId: requestedProjectId,
          sourcePath,
          requestId: projectRequestId,
        });
        if (reusableJob) {
          jsonResponse(response, DOCUMENT_ACTIVE_STATUSES.has(reusableJob.status) ? 202 : 200, {
            job_id: reusableJob.job_id,
            status: reusableJob.status,
            message: DOCUMENT_ACTIVE_STATUSES.has(reusableJob.status)
              ? "相同资料批次已在运行，已复用现有任务。"
              : "相同幂等请求已有结果，未重复执行 OCR。",
            reused: true,
          }, request.method);
          return;
        }
        const result = await startDocumentJob(config, {
          project_name: projectName,
          domain,
          intake_rationale: rationale,
          intake_mode: effectiveIntakeMode,
          requested_intake_mode: intakeMode,
          cq_mode: cqMode,
          initial_competency_questions: initialCompetencyQuestions,
          source_path: sourcePath,
          project_id: requestedProjectId,
          project_request_id: projectRequestId,
          max_excel_rows_per_sheet: maxExcelRowsPerSheet,
          source_preflight: sourcePreflight,
          ...(sourceSnapshot ? { source_snapshot: sourceSnapshot } : {}),
          recommended_structured_data_action: requiresStructuredImport ? "IMPORT" : "DOCUMENT_ONLY",
          reuse_content_cache: payload.reuse_content_cache === true,
          ...(process.env.ORION_ALLOW_DEVELOPMENT_FAULTS === "1" ? {
            development_fail_once_paths: Array.isArray(payload.development_fail_once_paths)
              ? payload.development_fail_once_paths.map(String)
              : [],
            development_delay_ms: Number(payload.development_delay_ms ?? 0),
            max_runtime_seconds: Number(payload.max_runtime_seconds ?? 1800),
          } : {}),
        }, null, scheduler);
        jsonResponse(response, 202, result, request.method);
      } catch (error) {
        jsonResponse(response, 500, {
          detail: error instanceof Error ? error.message : "资料批处理未能启动",
        }, request.method);
      }
      return;
    }
    if (pathParts[1] === "jobs" && pathParts[2] && pathParts.length === 3 && request.method === "GET") {
      const job = await loadDocumentJob(runtime.inputRoot, pathParts[2]);
      jsonResponse(response, job ? 200 : 404, job ?? { detail: "资料任务不存在" }, request.method);
      return;
    }
    if (pathParts[1] === "jobs" && pathParts[2] && pathParts[3] === "artifact" && request.method === "GET") {
      await serveDocumentJobArtifact(request, response, runtime.inputRoot, pathParts[2], incoming.searchParams.get("path") ?? "");
      return;
    }
    if (pathParts[1] === "jobs" && pathParts[2] && pathParts[3] === "reveal" && request.method === "POST") {
      try {
        const jobDir = documentJobDirectory(runtime.inputRoot, pathParts[2]);
        const target = join(jobDir, "structured-markdown");
        const targetInfo = await stat(target);
        if (!targetInfo.isDirectory()) throw new Error("结构化结果目录不存在");
        await executeFile("/usr/bin/open", [target], { timeout: 5000 });
        jsonResponse(response, 200, { ok: true, path: target }, request.method);
      } catch (error) {
        jsonResponse(response, 404, {
          detail: error instanceof Error ? error.message : "未能在 Finder 中打开结果目录",
        }, request.method);
      }
      return;
    }
    if (pathParts[1] === "jobs" && pathParts[2] && pathParts[3] === "retry" && request.method === "POST") {
      try {
        const job = await loadDocumentJob(runtime.inputRoot, pathParts[2]);
        if (!job || !DOCUMENT_RETRYABLE_STATUSES.has(job.status)) {
          jsonResponse(response, 409, { detail: "只有失败、已取消或已超时任务可以重试。" }, request.method);
          return;
        }
        const requestPayload = await readJson(join(documentJobDirectory(runtime.inputRoot, pathParts[2]), "request.json"), null);
        const failedPaths = [...new Set([
          ...(job.retryable_paths ?? []).map(String),
          ...(job.files ?? [])
            .filter((item) => ["FAILED", "CANCELLED", "TIMED_OUT"].includes(item.status) && item.relative_path)
            .map((item) => String(item.relative_path)),
        ])];
        if (!failedPaths.length) {
          if (Number(job.completed_files ?? 0) === 0 && ["CANCELLED", "TIMED_OUT"].includes(job.status)) {
            const result = await startDocumentJob(config, {
              ...requestPayload,
              retry_failed_only: false,
              retry_started_at: new Date().toISOString(),
            }, pathParts[2], scheduler);
            jsonResponse(response, 202, { ...result, retry_failed_files: 0, retry_scope: "FULL_NO_SUCCESS" }, request.method);
            return;
          }
          jsonResponse(response, 409, { detail: "任务没有安全可重试的文件清单。" }, request.method);
          return;
        }
        const result = await startDocumentJob(config, {
          ...requestPayload,
          retry_failed_only: true,
          retry_failed_paths: failedPaths,
          retry_started_at: new Date().toISOString(),
        }, pathParts[2], scheduler);
        jsonResponse(response, 202, { ...result, retry_failed_files: failedPaths.length }, request.method);
      } catch (error) {
        jsonResponse(response, 500, { detail: error instanceof Error ? error.message : "任务重试失败" }, request.method);
      }
      return;
    }
    if (pathParts[1] === "jobs" && pathParts[2] && pathParts[3] === "cancel" && request.method === "POST") {
      try {
        const job = await interruptDocumentJob(
          runtime.inputRoot,
          pathParts[2],
          "CANCELLED",
          "任务已由用户取消；已完成文件和证据保留，可按原任务重试未完成文件。",
          actor,
        );
        jsonResponse(response, 200, { ok: true, job }, request.method);
      } catch (error) {
        jsonResponse(response, error?.code === "STATE_CONFLICT" ? 409 : 404, {
          detail: error instanceof Error ? error.message : "资料任务取消失败",
        }, request.method);
      }
      return;
    }
    if (pathParts[1] === "jobs" && pathParts[2] && pathParts[3] === "commit" && request.method === "POST") {
      if (request.headers[DOCUMENT_CONFIRMATION_HEADER] !== "1") {
        jsonResponse(response, 403, { detail: "缺少 S0 人工复核确认标记。" }, request.method);
        return;
      }
      try {
        const payload = await readRequestJson(request);
        const rationale = String(payload.rationale ?? "").trim();
        const jobDir = documentJobDirectory(runtime.inputRoot, pathParts[2]);
        const argumentsList = [
          "-m", "services.ingestion.s0_batch_executor", "commit",
          "--job-dir", jobDir,
          "--reviewed-by", actor,
          "--rationale", rationale,
        ];
        if (payload.accept_warnings === true) argumentsList.push("--accept-warnings");
        const structuredDataAction = String(payload.structured_data_action ?? "DOCUMENT_ONLY").trim().toUpperCase();
        if (!["DOCUMENT_ONLY", "IMPORT"].includes(structuredDataAction)) {
          jsonResponse(response, 400, { detail: "结构化数据处理决定不合法。" }, request.method);
          return;
        }
        argumentsList.push("--structured-data-action", structuredDataAction);
        await executeFile(runtime.workflowPython, argumentsList, {
          cwd: runtime.workflowCwd,
          env: {
            ...(config.workflowEnvironment ?? process.env),
            PYTHONPYCACHEPREFIX: config.workflowEnvironment?.PYTHONPYCACHEPREFIX ?? "/private/tmp/orion-document-batch-pycache",
          },
          timeout: Math.max(
            120000,
            Math.min(
              14400000,
              Number(process.env.ORION_STRUCTURED_IMPORT_TIMEOUT_MS ?? 1800000),
            ),
          ),
          maxBuffer: 4 * 1024 * 1024,
        });
        const job = await loadDocumentJob(runtime.inputRoot, pathParts[2]);
        const dashboard = job?.project_id
          ? await loadWorkflowDashboard(runtime.workflowHome, job.project_id, { environment: config.workflowEnvironment })
          : null;
        jsonResponse(response, 200, { ok: true, job, dashboard }, request.method);
      } catch (error) {
        jsonResponse(response, 409, {
          detail: error instanceof Error ? error.message : "S0 复核提交失败",
        }, request.method);
      }
      return;
    }
    jsonResponse(response, 404, { detail: "未知的资料接入接口" }, request.method);
  }

  return serveDocumentIngestionApi;
}
