(() => {
  if (window.__ORION_DOCUMENT_JOB_VIEW__) return;

  const PAGE_SIZE = 10;
  const PAGE_CHIP_LIMIT = 10;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const routeStrategyLabels = {
    DIRECT_TEXT: "文字层直接解析",
    HYBRID_PAGE_ROUTE: "按页混合解析",
    STRUCTURE_REQUIRED: "复杂版面识别",
    PDF_TEXT_LAYER_DIRECT: "文字层直接解析",
    PADDLEOCR_PP_STRUCTURE_V3: "PaddleOCR 版面结构识别",
    PADDLEOCR_OCR_FALLBACK: "PaddleOCR 基础文字识别回退",
    OFFICE_OPEN_XML_WORD: "Word 原生结构解析",
    OFFICE_OPEN_XML_EXCEL: "Excel 原生结构解析",
    DELIMITED_TABLE_DIRECT: "表格直接解析",
    TEXT_OR_MARKUP_DIRECT: "文本直接解析",
    EMAIL_MIME_DIRECT: "邮件结构解析",
    PADDLEOCR_OCR: "PaddleOCR 图片文字识别",
    PADDLEOCR_PP_STRUCTURE_FALLBACK: "PaddleOCR 版面结构回退",
    DUPLICATE_REUSED: "复用同内容文件结果",
  };

  const statusLabel = (job) => ({
    IMPORTING: "S0 已完成，正在全量入库",
    IMPORT_FAILED: "S0 已完成，入库失败待恢复",
    WORKFLOW_GATE_FAILED: "已入库，S1 门禁待修复",
    READY_FOR_SEMANTIC_REVIEW: "全量入库与 S1 画像已完成",
  }[job?.structured_data?.status] ?? ({
    QUEUED: "等待处理",
    RUNNING: "正在处理",
    READY_FOR_REVIEW: (job?.warnings ?? []).length ? "等待处理异常" : "解析完成，等待复核提交",
    FAILED: "存在失败文件",
    CANCELLED: "已取消",
    TIMED_OUT: "已超时",
    COMMITTED: "已写入 S0",
  }[job?.status] ?? job?.status ?? "等待处理"));

  const formatRuntimeTime = (value) => {
    const parsed = Date.parse(String(value ?? ""));
    if (!Number.isFinite(parsed)) return null;
    return new Date(parsed).toLocaleString("zh-CN", { hour12: false });
  };

  const runtimeSummary = (job) => {
    const items = [];
    if (job.status === "QUEUED" && job.queue_position) {
      items.push(`队列第 ${job.queue_position} 位`);
      if (job.queue_concurrency) items.push(`并发上限 ${job.queue_concurrency}`);
    }
    const startedAt = formatRuntimeTime(job.started_at);
    const heartbeatAt = formatRuntimeTime(job.heartbeat_at);
    const deadlineAt = formatRuntimeTime(job.deadline_at);
    if (startedAt) items.push(`开始 ${startedAt}`);
    if (heartbeatAt && ["QUEUED", "RUNNING"].includes(job.status)) items.push(`心跳 ${heartbeatAt}`);
    if (deadlineAt && ["QUEUED", "RUNNING"].includes(job.status)) items.push(`截止 ${deadlineAt}`);
    if (job.worker_invocation_id) items.push(`执行编号 ${job.worker_invocation_id}`);
    return items.join(" · ");
  };

  const phaseLabels = {
    QUEUED: "等待处理",
    DISCOVERING: "正在发现文件",
    ROUTING: "正在判断处理路线",
    DIRECT_TEXT: "正在读取 PDF 文字层",
    RENDERING: "正在生成页面图像",
    STRUCTURE_RECOGNITION: "正在进行版面结构识别",
    OCR_FALLBACK: "正在使用基础文字识别回退",
    PAGE_COMPLETE: "本页结构化结果已生成",
    PAGE_FAILED: "本页处理失败",
    COMPLETE: "全部解析完成",
  };

  const intakeModeLabels = {
    DOCUMENT_ONLY: "纯资料",
    HYBRID: "混合",
    DATABASE_ONLY: "纯数据库",
  };

  const artifactUrl = (apiBase, jobId, path) => {
    if (!apiBase || !jobId || !path) return null;
    return `${apiBase}/jobs/${encodeURIComponent(jobId)}/artifact?${new URLSearchParams({ path })}`;
  };

  const parserUsage = (files) => {
    const usage = {
      directFiles: 0,
      directPages: 0,
      officeFiles: 0,
      structureFiles: new Set(),
      structurePages: 0,
      ocrFiles: new Set(),
      ocrUnits: 0,
    };
    (files ?? []).forEach((item, fileIndex) => {
      const processingMethod = String(item.processing_method ?? "");
      if (["PDF_TEXT_LAYER_DIRECT", "TEXT_OR_MARKUP_DIRECT", "DELIMITED_TABLE_DIRECT", "EMAIL_MIME_DIRECT"].includes(processingMethod)) usage.directFiles += 1;
      if (processingMethod.includes("OFFICE_OPEN_XML")) usage.officeFiles += 1;
      if (processingMethod.includes("PADDLEOCR_PP_STRUCTURE")) usage.structureFiles.add(fileIndex);
      if (["PADDLEOCR_OCR", "PADDLEOCR_OCR_FALLBACK"].includes(processingMethod)) {
        usage.ocrFiles.add(fileIndex);
        if (!(item.pages ?? []).length) usage.ocrUnits += Number(item.unit_count ?? 1);
      }
      (item.pages ?? []).forEach((page) => {
        const method = String(page.method ?? "");
        if (method === "DIRECT_TEXT") usage.directPages += 1;
        if (method.includes("PADDLEOCR_PP_STRUCTURE")) {
          usage.structureFiles.add(fileIndex);
          usage.structurePages += 1;
        }
        if (["PADDLEOCR_OCR", "PADDLEOCR_OCR_FALLBACK"].includes(method)) {
          usage.ocrFiles.add(fileIndex);
          usage.ocrUnits += 1;
        }
      });
    });
    return {
      ...usage,
      structureFiles: usage.structureFiles.size,
      ocrFiles: usage.ocrFiles.size,
    };
  };

  const normalizePage = (job, requestedPage) => {
    const totalPages = Math.max(1, Math.ceil((job?.files?.length ?? 0) / PAGE_SIZE));
    const page = Math.max(1, Math.min(totalPages, Number(requestedPage) || 1));
    return { page, totalPages };
  };

  const pageChips = (pages, label) => {
    const visible = (pages ?? []).slice(0, PAGE_CHIP_LIMIT);
    if (!visible.length) return "";
    const chips = visible.map((item) => `<span class="is-${escapeHtml(String(item.status ?? "pending").toLowerCase())}" title="第 ${escapeHtml(item.page)} 页：${escapeHtml(item.message ?? phaseLabels[item.phase] ?? "等待处理")}">${escapeHtml(item.page)}</span>`).join("");
    const remainder = (pages ?? []).length - visible.length;
    return `<div class="owa-document-file-pages" aria-label="${escapeHtml(label)} 每页处理状态">${chips}${remainder > 0 ? `<small>另有 ${escapeHtml(remainder)} 页，完整状态见复核报告</small>` : ""}</div>`;
  };

  const fileRow = (item, apiBase, jobId) => {
    const failed = item.status === "FAILED";
    const detail = failed
      ? item.error ?? item.route_reason ?? "处理失败，请按原路线重试。"
      : item.route_reason ?? item.error ?? "系统正在检查文件内容和最适合的解析方式。";
    const structuredResult = artifactUrl(apiBase, jobId, item.structured_markdown_path);
    const processingLabel = routeStrategyLabels[item.processing_method]
      ?? routeStrategyLabels[item.route_strategy]
      ?? item.processing_method
      ?? "正在判断处理路线";
    return `<article class="owa-document-file is-${escapeHtml(String(item.status ?? "running").toLowerCase())}">
      <div><strong title="${escapeHtml(item.source_name ?? "未命名资料")}">${escapeHtml(item.source_name ?? "未命名资料")}</strong><span>${escapeHtml(item.status === "SUCCEEDED" ? "处理成功" : failed ? "处理失败" : "处理中")}${item.page_count ? ` · ${escapeHtml(item.processed_pages ?? 0)}/${escapeHtml(item.page_count)} 页` : ""}</span></div>
      <p class="owa-document-file-method"><span>${escapeHtml(processingLabel)}</span>${structuredResult ? `<a href="${escapeHtml(structuredResult)}" target="_blank" rel="noopener">查看结构化结果</a>` : ""}</p>
      ${detail ? `<details class="owa-document-route-detail"><summary>查看路由依据</summary><small>${escapeHtml(detail)}</small></details>` : ""}
      ${pageChips(item.pages, item.source_name ?? "PDF")}
    </article>`;
  };

  const processingOverview = (job, usage) => {
    const mode = intakeModeLabels[job.intake_mode] ?? job.intake_mode ?? "资料";
    const nextStage = job.intake_mode === "HYBRID" ? "写入后继续 S1 数据理解" : "写入后进入 S2 语义识别";
    const directUsed = usage.directPages > 0 || usage.directFiles > 0;
    const structureUsed = usage.structurePages > 0 || usage.structureFiles > 0;
    const ocrUsed = usage.ocrUnits > 0 || usage.ocrFiles > 0;
    const directMeasure = usage.directPages > 0 ? `${usage.directPages} 页` : `${usage.directFiles} 份`;
    const structureMeasure = usage.structurePages > 0 ? `${usage.structurePages} 页` : `${usage.structureFiles} 份 / 图片`;
    const ocrMeasure = usage.ocrUnits > 0 ? `${usage.ocrUnits} 页 / 图片` : "0 页 / 图片";
    return `<section class="owa-document-product-flow" aria-label="资料接入与解析流程">
      <header><div><span>当前链路</span><strong>${escapeHtml(mode)}接入 → 智能路由 → 结果校验 → 写入 S0</strong></div><em>${escapeHtml(nextStage)}</em></header>
      <div class="owa-document-parser-grid">
        <article class="${directUsed ? "is-used" : "is-idle"}"><header><span>PDF 文字层</span><em>${directUsed ? "本批次已用" : "未触发"}</em></header><strong>${escapeHtml(directMeasure)}</strong><p>本地直接读取 · 不调用 OCR</p></article>
        <article class="${usage.officeFiles ? "is-used" : "is-idle"}"><header><span>Word / Excel</span><em>${usage.officeFiles ? "本批次已用" : "未触发"}</em></header><strong>${escapeHtml(usage.officeFiles)} 份</strong><p>读取 Office 原生结构 · 不调用 OCR</p></article>
        <article class="${structureUsed ? "is-used" : "is-idle"}"><header><span>复杂版面</span><em>${structureUsed ? "本批次已用" : "未触发"}</em></header><strong>${escapeHtml(structureMeasure)}</strong><p>PP-StructureV3 · MCP 10826</p></article>
        <article class="${ocrUsed ? "is-used" : "is-idle"}"><header><span>普通 OCR</span><em>${ocrUsed ? "本批次已用" : "本批次未触发"}</em></header><strong>${escapeHtml(ocrMeasure)}</strong><p>PaddleOCR OCR · MCP 10827</p></article>
      </div>
      <p class="owa-document-routing-rule">路由原则：有可靠文字层就直接读取；文字层缺失或版面复杂才调用 10826；图片直识别或复杂版面服务失败时才使用 10827。</p>
    </section>`;
  };

  const resultAndStorage = (job, apiBase) => {
    const ready = Boolean(job.review_report || (job.files ?? []).some((item) => item.structured_markdown_path));
    if (!ready) return "";
    const completeArtifacts = Boolean(job.review_report);
    const links = [
      ["查看全部结构化结果", job.review_report],
      ["查看证据索引", completeArtifacts ? "evidence-index.json" : null],
      ["查看质量报告", completeArtifacts ? "ingestion-quality-report.json" : null],
      ["查看工具调用轨迹", completeArtifacts ? "processing-trace.json" : null],
    ].map(([label, path]) => {
      const href = artifactUrl(apiBase, job.job_id, path);
      return href ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener">${escapeHtml(label)}</a>` : "";
    }).join("");
    const sourcePath = job.source_path ?? ".orion-s0-uploads/<UPLOAD-ID>";
    const jobPath = job.storage_paths?.job_directory ?? `.orion-s0-jobs/${job.job_id}`;
    const structuredPath = job.storage_paths?.structured_markdown_directory
      ?? `${jobPath}/structured-markdown`;
    const formalPath = `.orion-workflows/${job.project_id ?? "<PROJECT-ID>"}/00-document-evidence`;
    const committed = job.status === "COMMITTED";
    return `<section class="owa-document-result-handoff" aria-label="解析结果与存储位置">
      <article class="owa-document-result-access"><header><span>解析结果去哪看</span><strong>${escapeHtml(job.completed_files ?? 0)} 份结构化结果已生成</strong></header><div>${links}<button type="button" data-engineering-action="reveal-document-results">在 Finder 中打开</button><button type="button" data-engineering-action="copy-document-results-path" data-document-path="${escapeHtml(structuredPath)}">复制本地路径</button></div><p>逐文件列表显示原始中文文件名；内部文档编号只用于去重和审计。</p></article>
      <article class="owa-document-storage-map"><header><span>数据现在放在哪</span><strong>${committed ? "已进入正式工程" : "当前正在准备写入"}</strong></header>
        <dl><div><dt>上传原件</dt><dd><code>${escapeHtml(sourcePath)}</code><small>处理期位于本机受控目录；提交 S0 时以 SHA-256 校验后保存到 MinIO</small></dd></div><div><dt>S0 解析结果</dt><dd><code>${escapeHtml(structuredPath)}</code><small>按照原文件名生成 Markdown；证据、质量与工具轨迹保存在上一级任务目录</small></dd></div><div><dt>提交以后</dt><dd><code>${escapeHtml(formalPath)}</code><small>工程账本留在 PostgreSQL；选择“全量导入”时，表格全部行进入 orion_source_data，Ontop 只读查询；文档证据按发布版本进入 Fuseki</small></dd></div></dl>
        <p>${committed ? "本批次已完成 S0 持久化。" : "解析完成后需要人工确认：仅保留文档证据，或把表格全部行同时导入 S1。"}</p>
      </article>
    </section>`;
  };

  const pagination = (job, page, totalPages) => {
    const availableFiles = job?.files?.length ?? 0;
    const totalFiles = Math.max(availableFiles, Number(job?.total_files ?? 0));
    if (availableFiles <= PAGE_SIZE && totalFiles <= PAGE_SIZE) return "";
    return `<nav class="owa-document-list-pagination" aria-label="资料列表分页">
      <span>已载入 ${escapeHtml(availableFiles)} / ${escapeHtml(totalFiles)} 份资料 · 每页最多 ${PAGE_SIZE} 份</span>
      <div>
        <button type="button" data-engineering-action="document-page" data-document-page="${page - 1}" ${page <= 1 ? "disabled" : ""}>上一页</button>
        <strong>第 ${page} / ${totalPages} 页</strong>
        <button type="button" data-engineering-action="document-page" data-document-page="${page + 1}" ${page >= totalPages ? "disabled" : ""}>下一页</button>
      </div>
    </nav>`;
  };

  const build = ({ job, page = 1, busy = false, message = null, apiBase, s0ReportUrl }) => {
    if (!job) return "";
    const normalized = normalizePage(job, page);
    const files = job.files ?? [];
    const pageStart = (normalized.page - 1) * PAGE_SIZE;
    const visibleFiles = files.slice(pageStart, pageStart + PAGE_SIZE);
    const currentFile = files.find((item) => item.source_name === job.current_file)
      ?? files.find((item) => item.status === "RUNNING")
      ?? files.find((item) => (item.pages ?? []).length)
      ?? null;
    const progressPercent = Math.min(100, Math.round((Number(job.processed_pages ?? 0) / Math.max(1, Number(job.total_pages ?? 0))) * 100));
    const currentPageRows = pageChips(currentFile?.pages, currentFile?.source_name ?? "当前 PDF");
    const liveProgress = Number(job.total_pages ?? 0) > 0 ? `<section class="owa-document-live-progress">
      <div class="owa-document-live-copy"><span>${job.status === "RUNNING" ? "逐页实时进度" : "逐页处理结果"}</span><strong>${escapeHtml(job.current_file ?? currentFile?.source_name ?? "PDF 页面")}</strong><p>原批次第 ${escapeHtml(job.current_file_index ?? job.total_files ?? 0)}/${escapeHtml(job.total_files ?? 0)} 份${job.current_page ? ` · 当前第 ${escapeHtml(job.current_page)}/${escapeHtml(job.current_file_pages ?? 0)} 页` : ""} · ${escapeHtml(phaseLabels[job.progress_phase] ?? "正在处理")}</p></div>
      <div class="owa-document-page-meter"><div><i style="width:${progressPercent}%"></i></div><strong>${escapeHtml(job.processed_pages ?? 0)} / ${escapeHtml(job.total_pages ?? 0)} 页</strong></div>
      ${currentPageRows}
    </section>` : "";
    const warnings = (job.warnings ?? []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    const failureCount = Number(job.failed_files ?? files.filter((item) => item.status === "FAILED").length);
    const usage = parserUsage(files);
    const fileListOpen = ["READY_FOR_REVIEW", "FAILED", "CANCELLED", "TIMED_OUT", "COMMITTED"].includes(job.status);
    const failureActions = ["FAILED", "CANCELLED", "TIMED_OUT"].includes(job.status) ? `<section class="owa-document-retry-panel" aria-label="失败处理">
      <div><strong>${failureCount} 份资料需要继续处理</strong><p>留在当前页面即可重试。成功文件不会重复解析；失败文件仍按“直接解析 → 必要时调用 PaddleOCR MCP → 人工复核”的路线继续。</p></div>
      <div class="owa-review-actions"><button type="button" data-engineering-action="close-document-router">暂时关闭</button><button type="button" class="is-primary" data-engineering-action="retry-document-job" ${busy ? "disabled" : ""}>${busy ? "正在重试…" : `仅重试 ${failureCount} 个失败文件`}</button></div>
    </section>` : "";
    const cancelAction = ["QUEUED", "RUNNING"].includes(job.status) ? `<section class="owa-document-retry-panel" aria-label="取消任务"><div><strong>当前任务可以取消</strong><p>取消只停止真实后台资料任务；已完成文件和证据保留。同步阶段没有这个按钮。</p></div><div class="owa-review-actions"><button type="button" data-engineering-action="cancel-document-job" ${busy ? "disabled" : ""}>${busy ? "正在取消…" : "取消资料任务"}</button></div></section>` : "";
    const intakeMode = job.intake_mode ?? "DOCUMENT_ONLY";
    const sourcePreflight = job.source_preflight ?? {};
    const requiresStructuredImport = sourcePreflight.requires_structured_import === true;
    const routeExplanation = Array.isArray(sourcePreflight.reasons)
      ? sourcePreflight.reasons.join(" ")
      : "";
    const routeDecision = sourcePreflight.schema_version === 1
      ? `<section class="owa-document-ready-explanation"><strong>数据源预检：${requiresStructuredImport ? "HYBRID + IMPORT" : "DOCUMENT_ONLY"}</strong><p>${escapeHtml(routeExplanation)}</p><small>表格 ${escapeHtml(sourcePreflight.tabular_file_count ?? 0)} 份 · 叙述资料 ${escapeHtml(sourcePreflight.narrative_file_count ?? 0)} 份 · 估算表格行数 ${escapeHtml(sourcePreflight.total_tabular_rows_estimate ?? 0)} · 历史解析缓存${job.content_cache_enabled ? "已显式启用" : "未启用"}</small></section>`
      : "";
    const runtimeStatus = runtimeSummary(job);
    const hasDirectTabularFile = files.some((item) =>
      ["XLSX", "XLSM", "CSV", "TSV"].includes(String(item.source_type ?? "").toUpperCase()));
    const structuredDecision = intakeMode === "HYBRID"
      ? `<fieldset class="owa-structured-data-decision"><legend>表格数据进入哪条链路</legend><label class="owa-intake-confirm"><input type="radio" name="document-structured-data-action" value="IMPORT" ${requiresStructuredImport || hasDirectTabularFile ? "checked" : ""}><span><strong>全量导入结构化数据区${requiresStructuredImport ? "（预检要求）" : hasDirectTabularFile ? "（推荐）" : ""}</strong><small>Excel/CSV 将版本化写入独立 PostgreSQL 数据库；Word/PDF 仍留在文档证据链路。完成后形成 S1 数据画像、S2 候选和 S3 Mapping 待确认项。</small></span></label><label class="owa-intake-confirm"><input type="radio" name="document-structured-data-action" value="DOCUMENT_ONLY" ${requiresStructuredImport ? "disabled" : hasDirectTabularFile ? "" : "checked"}><span><strong>仅作为 S0 文档证据${requiresStructuredImport ? "（大表不可选）" : hasDirectTabularFile ? "" : "（推荐）"}</strong><small>${requiresStructuredImport ? "数据量已超过 S0 文档样本边界，后端也会拒绝绕过结构化导入。" : hasDirectTabularFile ? "不导入表格行；HYBRID 工程随后仍需连接其他数据库才能完成 S1。" : "当前批次没有直接识别到 Excel/CSV 数据文件；叙述型 Word/PDF 默认留在文档证据链路。"}</small></span></label></fieldset>`
      : `<input type="hidden" name="document-structured-data-action" value="DOCUMENT_ONLY"><div class="owa-document-ready-explanation"><strong>当前是纯资料工程</strong><p>文件只进入 S0/文档证据链路；S1 将正式留痕为不适用并进入 S2。</p></div>`;
    const commitForm = job.status === "READY_FOR_REVIEW" ? `<form class="owa-document-commit-form"><div class="owa-document-ready-explanation"><strong>${warnings ? "发现需要你决定的异常" : "解析与质量校验已完成"}</strong><p>提交前必须确认结构化表格的处理方式。该决定、复核说明和异常接受结果都会进入 S0 审计。</p></div>${structuredDecision}<label><span>复核结论</span><textarea name="document-review-rationale" rows="3" required aria-describedby="document-review-rationale-help document-review-rationale-error" placeholder="例如：已核对文件范围；表格需要全量问数，确认导入结构化数据区。"></textarea><small id="document-review-rationale-help">说明会进入 S0 质量报告和审计记录。</small><small id="document-review-rationale-error" class="owa-document-field-error" role="alert" hidden>请填写复核结论。</small></label>${warnings ? `<label class="owa-intake-confirm"><input type="checkbox" name="document-accept-warnings"><span><strong>接受上述截断或降级项并继续</strong><small>S0 Markdown 可能只保留样本；选择全量导入时，结构化导入器仍会重新读取原件全部数据行。</small></span></label>` : ""}<div class="owa-review-actions"><button type="button" data-engineering-action="close-document-router">暂不处理</button><button type="button" class="is-primary" data-engineering-action="commit-document-job" ${busy ? "disabled" : ""}>${busy ? "正在提交…" : "确认并写入 S0"}</button></div></form>` : "";

    return `<div class="owa-document-job-head is-${escapeHtml(String(job.status ?? "queued").toLowerCase())}">
        <div><span>任务状态</span><strong>${escapeHtml(statusLabel(job))}</strong><p>${escapeHtml(job.message ?? "")}</p><small>任务在后台运行，可以关闭此窗口，稍后回来继续查看。</small>${runtimeStatus ? `<small>${escapeHtml(runtimeStatus)}</small>` : ""}</div>
        <div class="owa-document-progress"><strong>${escapeHtml(job.completed_files ?? 0)} / ${escapeHtml(job.total_files ?? 0)}</strong><span>处理成功文件</span></div>
      </div>
      ${liveProgress}
      ${routeDecision}
      <div class="owa-document-compact-stats"><span>失败 <strong>${escapeHtml(job.failed_files ?? 0)}</strong></span><span>证据 <strong>${escapeHtml(job.evidence_count ?? 0)}</strong></span><span>待确认 <strong>${escapeHtml((job.warnings ?? []).length)}</strong></span></div>
      ${processingOverview(job, usage)}
      ${resultAndStorage(job, apiBase)}
      <details class="owa-document-file-list" ${fileListOpen ? "open" : ""}><summary><span>逐文件路由结果</span><small>已载入 ${escapeHtml(files.length)} / ${escapeHtml(job.total_files ?? files.length)} 份 · 第 ${normalized.page} 页</small></summary><div class="owa-document-file-list-body">${visibleFiles.map((item) => fileRow(item, apiBase, job.job_id)).join("") || '<p class="owa-review-muted">正在发现文件，请稍候。</p>'}${pagination(job, normalized.page, normalized.totalPages)}</div></details>
      ${warnings ? `<section class="owa-document-warnings"><h3>提交前必须确认</h3><ul>${warnings}</ul></section>` : ""}
      ${message ? `<p class="owa-review-message" role="status">${escapeHtml(message)}</p>` : ""}
      ${cancelAction}
      ${commitForm}
      ${failureActions}
      ${job.status === "COMMITTED" ? `<div class="owa-document-committed"><strong>${job.structured_data?.status === "READY_FOR_SEMANTIC_REVIEW" ? "结构化数据与 S1 画像已完成，等待 S2 语义识别" : job.structured_data?.status === "READY_FOR_MAPPING_REVIEW" ? "结构化数据已进入 S1，并推进到 S3 待确认" : intakeMode === "HYBRID" ? "混合工程已进入 S1" : "资料工程已进入 S2"}</strong><p>${escapeHtml(job.message ?? "")}当前项目编号：${escapeHtml(job.project_id ?? "—")}</p>${job.structured_data?.row_count != null ? `<p>已导入 ${escapeHtml(job.structured_data.dataset_count)} 个数据集、${escapeHtml(job.structured_data.sheet_count)} 个表、${escapeHtml(job.structured_data.row_count)} 行；运行时数据库访问为只读。</p>` : ""}<div class="owa-review-actions"><a href="${escapeHtml(s0ReportUrl ?? "#")}" target="_blank" rel="noopener">查看 S0 正式报告</a><button type="button" class="is-primary" data-engineering-action="close-document-router">返回工程页面</button></div></div>` : ""}`;
  };

  window.__ORION_DOCUMENT_JOB_VIEW__ = { PAGE_SIZE, build, normalizePage };
})();
