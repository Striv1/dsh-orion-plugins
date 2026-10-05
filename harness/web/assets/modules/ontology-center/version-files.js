(() => {
  if (window.__ORION_ONTOLOGY_VERSION_FILES__) return;

  const GROUPS = [
    { id: "07-release", title: "S7 发布与版本", detail: "正式交付包、本体模型、发布说明与版本清单", prefixes: ["07-release/"] },
    { id: "06-quality-validation", title: "S6 质量验证", detail: "逻辑、约束、Mapping、业务问题与运行验证", prefixes: ["06-quality-validation/"] },
    { id: "05-ontology-build", title: "S5 本体构建", detail: "OWL、TTL、SHACL 与 Protégé 构建证据", prefixes: ["05-ontology-build/"] },
    { id: "04-ontology-design", title: "S4 本体设计", detail: "本体施工图与可验证业务问题", prefixes: ["04-ontology-design/"] },
    { id: "03-mapping-review", title: "S3 映射评审", detail: "正式 Mapping、评审结论与人工确认", prefixes: ["03-mapping-review/"] },
    { id: "02-semantic-recognition", title: "S2 业务语义", detail: "业务对象、关系、属性与规则候选", prefixes: ["02-semantic-recognition/"] },
    { id: "01-data-understanding", title: "S1 数据理解", detail: "数据范围、画像与结构证据", prefixes: ["01-data-understanding/"] },
    { id: "00-document-evidence", title: "S0 资料与证据", detail: "原始资料登记、结构化结果与追溯证据", prefixes: ["00-document-evidence/"] },
    { id: "audit", title: "审计与修订", detail: "执行轨迹、历史调整和版本差异", prefixes: ["events/", "revisions/"] },
    { id: "project", title: "工程元数据", detail: "工程定义、工作流状态与存储回执", prefixes: [] },
  ];

  const V2_STAGES = {
    S0: { name: "目标与来源登记", purpose: "业务目标、资料与单库、多库、混合来源的登记范围" },
    S1: { name: "资料与数据理解", purpose: "资料理解、数据画像、来源身份与完整性证据" },
    S2: { name: "业务语义草案", purpose: "业务对象、关系、属性、公理依据与规则候选" },
    S3: { name: "语义与映射评审", purpose: "业务口径、候选映射可行性与评审决定" },
    S4: { name: "联合设计定稿", purpose: "本体、正式映射、规则与业务验收问题的整体批准基线" },
    S5: { name: "构建与装配", purpose: "本体、约束、映射、规则、查询资产与装配回执" },
    S6: { name: "质量与业务验收", purpose: "逻辑、约束、完整来源、问数和规则推理的真实验收" },
    S7: { name: "交付与发布", purpose: "工程包、运行发布资产、版本清单与接入回执" },
  };

  const groupsFor = (asset) => {
    if (asset?.stage_contract_version !== "s0-s7-stage-contract-v2") return GROUPS;
    const catalog = asset.stage_contracts?.contract_version === asset.stage_contract_version
      && Array.isArray(asset.stage_contracts.stages) ? asset.stage_contracts.stages : [];
    return GROUPS.map((group) => {
      const stage = group.title.slice(0, 2);
      const fallback = V2_STAGES[stage];
      if (!fallback) return group;
      const contract = catalog.find((item) => item.stage === stage);
      return { ...group, title: `${stage} ${contract?.name || fallback.name}`, detail: contract?.purpose || fallback.purpose };
    });
  };

  const DIRECTORY_LABELS = {
    "": "阶段根目录",
    assets: "报告资源",
    "structured-markdown": "结构化资料",
    "01-本体模型": "本体模型",
    "02-工程定义": "工程定义",
    "03-质量结论": "质量结论",
    "04-发布信息": "发布信息",
    "05-运行时": "运行资产",
    "06-工程追溯": "阶段产物与工程追溯",
    "rule-review": "正式规则评审",
    "formal-rules": "正式规则合同",
    "规则目录": "业务规则与执行说明",
    events: "审计事件",
    revisions: "历史修订",
  };

  const CORE_FILE_RULES = [
    /^00-document-evidence\/(?:document-register|evidence-index)\.json$/,
    /^00-document-evidence\/source-scope\.json$/,
    /^01-data-understanding\/data-understanding-report\.html$/,
    /^01-data-understanding\/source-understanding\.json$/,
    /^02-semantic-recognition\/ontology-candidates\.yaml$/,
    /^02-semantic-recognition\/(?:business-rule-candidates|capability-plan)\.json$/,
    /^03-mapping-review\/mapping\.yaml$/,
    /^04-ontology-design\/ontology-design\.yaml$/,
    /^04-ontology-design\/(?:joint-design-baseline|competency-question-review)\.json$/,
    /^05-ontology-build\/(?:ontology\.owl|ontology\.ttl|shapes\.ttl)$/,
    /^05-ontology-build\/runtime-assembly\.json$/,
    /^05-ontology-build\/rule-review\/(?:rule-catalog\.json|业务规则目录\.md|规则执行与Protégé复核说明\.md|formal-rules\/[^/]+\.json)$/,
    /^06-quality-validation\/(?:quality-summary\.json|quality-validation-report\.html)$/,
    /^07-release\/release-report\.html$/,
    /^07-release\/ontology-(?:engineering-package|model-delivery)-[^/]+\/01-本体模型\/(?:ontology\.owl|ontology\.ttl|shapes\.ttl)$/,
    /^07-release\/ontology-(?:engineering-package|model-delivery)-[^/]+\/04-发布信息\/publication\.json$/,
    /^07-release\/ontology-(?:engineering-package|model-delivery)-[^/]+\/manifest\.json$/,
    /^07-release\/ontology-(?:engineering-package|model-delivery)-[^/]+\/(?:package-contract\.json|工程包与发布包说明\.md)$/,
    /^07-release\/ontology-(?:engineering-package|model-delivery)-[^/]+\/02-工程定义\/规则目录\/(?:rule-catalog\.json|业务规则目录\.md|规则执行与Protégé复核说明\.md|formal-rules\/[^/]+\.json)$/,
    /^07-release\/ontology-(?:engineering-package|model-delivery)-[^/]+\/06-工程追溯\/阶段产物索引\.json$/,
    /^07-release\/ontology-(?:engineering-package|model-delivery)-[^/]+\/06-工程追溯\/(?:00-document-evidence\/source-scope|01-data-understanding\/source-understanding|04-ontology-design\/joint-design-baseline)\.json$/,
    /^06-quality-validation\/materialized\.ttl$/,
    /^(?:03-mapping-review|05-ontology-build)\/runtime\/(?:mapping\.obda|runtime-source\.json|document-facts\/[^/]+\.json|rules\/[^/]+\.json)$/,
    /^events\/agent-trace\.jsonl$/,
    /^(?:project|workflow-state)\.json$/,
  ];

  let view = "stage";
  const guideFor = file => window.__ORION_FILE_GUIDE__?.describe(file) ?? { category: "audit", role: "工程资料", purpose: file.purpose || "工程文件", inputs: "请查看文件正文", outputs: "请查看文件正文", impact: "需结合实际执行记录确认", known: false };
  const displayGroups = () => view === "purpose" ? window.__ORION_FILE_GUIDE__.groups : groupsFor(activeAsset);
  let activeAsset = null;
  let activeGroupId = "07-release";
  let scope = "core";
  let query = "";
  let previousFocus = null;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const groupFor = (path) => GROUPS.find((group) => (
    group.id === "project"
      ? !path.includes("/")
      : group.prefixes.some((prefix) => path.startsWith(prefix))
  ));

  const isCoreFile = (file) => CORE_FILE_RULES.some((rule) => rule.test(file.path));
  const allFilesFor = (group) => (activeAsset?.artifacts ?? []).filter((file) => view === "purpose" ? guideFor(file).category === group.id : groupFor(file.path)?.id === group.id);
  const filesFor = (group) => allFilesFor(group).filter((file) => scope === "all" || isCoreFile(file));
  const fileName = (path) => String(path ?? "").split("/").at(-1) ?? path;
  const fileExtension = (path) => {
    const name = fileName(path);
    const extension = name.includes(".") ? name.split(".").at(-1) : "FILE";
    return String(extension ?? "FILE").toUpperCase();
  };
  const directoryName = (path, group) => {
    const parts = String(path ?? "").split("/");
    parts.pop();
    if (group.id !== "project" && parts[0] === group.id) parts.shift();
    const relative = parts.join("/");
    if (/^ontology-(?:engineering-package|model-delivery)-/.test(parts[0] ?? "")) {
      const packageLabel = parts[0].replace(/^ontology-(?:engineering-package|model-delivery)-/, "正式交付包 v");
      const subsection = parts.length > 1 ? (DIRECTORY_LABELS[parts.at(-1)] ?? parts.slice(1).join("/")) : "";
      return subsection ? `${packageLabel} · ${subsection}` : packageLabel;
    }
    const last = parts.at(-1) ?? "";
    return DIRECTORY_LABELS[last] ?? (relative || DIRECTORY_LABELS[""]);
  };

  const ensureDialog = () => {
    let overlay = document.querySelector(".owa-version-files-overlay");
    if (overlay) return overlay;
    overlay = document.createElement("div");
    overlay.className = "owa-version-files-overlay";
    overlay.hidden = true;
    overlay.innerHTML = `
      <section class="owa-version-files-dialog" role="dialog" aria-modal="true" aria-labelledby="owa-version-files-title">
        <header class="owa-version-files-header">
          <div class="owa-version-files-title"><strong>AHS</strong><h2 id="owa-version-files-title">版本文件</h2></div>
          <button type="button" data-version-files-close aria-label="关闭版本文件"><img src="/icons/xmark.svg" alt="" /></button>
        </header>
        <section class="owa-version-files-identity">
          <div><h3 data-version-files-name></h3><span data-version-files-version></span><p><b>本体资产</b><i aria-hidden="true">·</i><code data-version-files-project></code></p></div>
          <label><span>搜索文件</span><input type="search" data-version-files-search placeholder="搜索文件名、用途或类型" autocomplete="off" /></label>
        </section>
        <div class="owa-version-files-toolbar">
          <div class="owa-version-files-scope"><span>显示范围</span><div role="group" aria-label="版本文件显示范围"><button type="button" data-version-files-scope="core">核心文件</button><button type="button" data-version-files-scope="all">全部文件</button></div><p data-version-files-summary></p></div>
          <div class="owa-version-file-actions"><label class="owa-vf-view-label">组织方式 <select data-version-files-view aria-label="文件组织方式"><option value="stage">按阶段查看</option><option value="purpose">按用途查看</option></select></label><span data-version-package-download></span></div>
        </div>
        <div class="owa-version-files-layout">
          <nav class="owa-version-files-groups" aria-label="工程文件夹" data-version-files-groups></nav>
          <main class="owa-version-files-main">
            <header data-version-files-folder-heading></header>
            <div class="owa-version-files-columns" aria-hidden="true"><span>文件名</span><span>用途 / 描述</span><span>操作</span></div>
            <div class="owa-version-files-list" data-version-files-list></div>
          </main>
        </div>
        <footer class="owa-version-files-footer"><button type="button" data-version-files-close>关闭</button></footer>
        <div class="owa-version-files-notice" role="status" aria-live="polite" hidden></div>
      </section>`;
    document.body.appendChild(overlay);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay || event.target.closest("[data-version-files-close]")) {
        close();
        return;
      }
      const previewButton = event.target.closest("[data-version-files-preview]");
      if (previewButton && activeAsset) {
        const file = activeAsset.artifacts.find(item => item.path === previewButton.dataset.versionFilesPreview);
        if (file) window.__ORION_FILE_PREVIEW__.open(activeAsset, file, guideFor(file));
        return;
      }
      const groupButton = event.target.closest("[data-version-files-group]");
      if (groupButton) {
        activeGroupId = groupButton.dataset.versionFilesGroup;
        query = "";
        const search = overlay.querySelector("[data-version-files-search]");
        if (search) search.value = "";
        render(overlay);
        overlay.querySelector(`[data-version-files-group="${CSS.escape(activeGroupId)}"]`)?.focus({ preventScroll: true });
        return;
      }
      const scopeButton = event.target.closest("[data-version-files-scope]");
      if (scopeButton) {
        scope = scopeButton.dataset.versionFilesScope === "all" ? "all" : "core";
        query = "";
        const search = overlay.querySelector("[data-version-files-search]");
        if (search) search.value = "";
        const groups = displayGroups();
        if (!filesFor(groups.find((group) => group.id === activeGroupId) ?? groups[0]).length) {
          activeGroupId = groups.find((group) => filesFor(group).length)?.id ?? "project";
        }
        render(overlay);
        return;
      }
      const revealButton = event.target.closest("[data-version-files-reveal]");
      if (revealButton && activeAsset) {
        revealButton.disabled = true;
        revealButton.setAttribute("aria-busy", "true");
        const originalLabel = revealButton.textContent;
        revealButton.textContent = "正在定位…";
        window.__ORION_ONTOLOGY_API__.revealArtifact(activeAsset.sourceProjectId, revealButton.dataset.versionFilesReveal)
          .then((payload) => notify(overlay, payload.detail ?? "已在 Finder 中定位文件。", "success"))
          .catch((error) => notify(overlay, error instanceof Error ? error.message : "暂时无法定位文件。", "error"))
          .finally(() => {
            revealButton.disabled = false;
            revealButton.removeAttribute("aria-busy");
            revealButton.textContent = originalLabel;
          });
      }
    });
    overlay.querySelector("[data-version-files-view]")?.addEventListener("change", event => {
      view = event.target.value === "purpose" ? "purpose" : "stage";
      query = "";
      overlay.querySelector("[data-version-files-search]").value = "";
      activeGroupId = displayGroups().find(group => filesFor(group).length)?.id ?? "project";
      render(overlay);
    });
    overlay.querySelector("[data-version-files-search]")?.addEventListener("input", (event) => {
      query = event.target.value.trim().toLocaleLowerCase("zh-CN");
      renderList(overlay);
    });
    return overlay;
  };

  const notify = (overlay, message, tone = "info") => {
    const notice = overlay.querySelector(".owa-version-files-notice");
    if (!notice) return;
    notice.textContent = message;
    notice.dataset.tone = tone;
    notice.hidden = false;
    window.clearTimeout(Number(notice.dataset.timer ?? 0));
    notice.dataset.timer = String(window.setTimeout(() => { notice.hidden = true; }, 4200));
  };

  const groupMarkup = (group) => {
    const count = filesFor(group).length;
    if (!count) return "";
    return `<button type="button" data-version-files-group="${escapeHtml(group.id)}"${group.id === activeGroupId ? ' aria-current="page"' : ""}>
      <strong>${escapeHtml(group.title)}</strong><b>${count}</b>
    </button>`;
  };

  const fileMarkup = (file) => {
    const api = window.__ORION_ONTOLOGY_API__;
    const href = api.artifactUrl(activeAsset.sourceProjectId, file.path);
    const guide = guideFor(file);
    const stageGroup = groupsFor(activeAsset).find(group => group.id === groupFor(file.path)?.id);
    const location = file.path.startsWith("07-release/ontology-")
      ? directoryName(file.path, stageGroup)
      : `${stageGroup?.title || "工程文件"} · ${fileName(file.path)}`;
    const lifecycle = file.lifecycle_status && file.lifecycle_status !== "CURRENT"
      ? `<em>${escapeHtml(file.lifecycle_status === "HISTORICAL" ? "历史" : file.lifecycle_status)}</em>`
      : "";
    return `<article class="owa-version-file-row">
      <div class="owa-version-file-copy">
        <img src="/icons/page.svg" alt="" />
        <div><div><strong>${escapeHtml(file.display_name ?? fileName(file.path))}</strong>${lifecycle}<span>${escapeHtml(fileExtension(file.path))}</span></div><code title="${escapeHtml(file.path)}">${escapeHtml(scope === "all" ? file.path : location)}</code></div>
      </div>
      <div class="owa-version-file-purpose"><span class="owa-vf-role">${escapeHtml(guide.role)}</span><p>${escapeHtml(guide.purpose)}</p><details class="owa-vf-guide"><summary>来源、后续用途与修改影响</summary><dl><dt>输入 / 生成依据</dt><dd>${escapeHtml(guide.inputs)}</dd><dt>输出 / 后续使用</dt><dd>${escapeHtml(guide.outputs)}</dd><dt>修改影响</dt><dd>${escapeHtml(guide.impact)}</dd><dt>文件状态</dt><dd>${escapeHtml(file.lifecycle_status || "账本未提供状态")}</dd><dt>所属位置</dt><dd>${escapeHtml(file.path)}</dd></dl><small>以上为文件用途说明；实际依赖、校验结果以本版本执行记录为准。</small></details></div>
      <div class="owa-version-file-actions">
        <button type="button" class="owa-vf-preview-button" data-version-files-preview="${escapeHtml(file.path)}">预览</button><a href="${escapeHtml(href)}" target="_blank" rel="noopener">原文件</a>
        <a href="${escapeHtml(href)}" download="${escapeHtml(fileName(file.path))}">下载</a>
        <button type="button" data-version-files-reveal="${escapeHtml(file.path)}">Finder 定位</button>
      </div>
    </article>`;
  };

  const renderList = (overlay) => {
    const groups = displayGroups();
    const group = groups.find((item) => item.id === activeGroupId) ?? groups[0];
    const sourceFiles = filesFor(group);
    const files = query
      ? sourceFiles.filter((file) => [file.path, file.display_name, file.purpose, file.artifact_type, guideFor(file).purpose, guideFor(file).role]
        .some((value) => String(value ?? "").toLocaleLowerCase("zh-CN").includes(query)))
      : sourceFiles;
    const heading = overlay.querySelector("[data-version-files-folder-heading]");
    heading.innerHTML = `<div><span>${escapeHtml(group.id === "project" ? "PROJECT" : group.id.toUpperCase())}</span><h3>${escapeHtml(group.title)}</h3><p>${escapeHtml(group.detail)}</p></div><b>${files.length} 个${scope === "core" ? "核心" : ""}文件</b>`;
    const folders = new Map();
    for (const file of files) {
      const directory = directoryName(file.path, group);
      if (!folders.has(directory)) folders.set(directory, []);
      folders.get(directory).push(file);
    }
    const list = overlay.querySelector("[data-version-files-list]");
    const folderEntries = scope === "core" ? [["", files]] : [...folders.entries()];
    list.innerHTML = files.length
      ? folderEntries.map(([directory, items]) => `<section class="owa-version-file-folder">${directory ? `<header><strong>${escapeHtml(directory)}</strong><span>${items.length} 个文件</span></header>` : ""}${items.map(fileMarkup).join("")}</section>`).join("")
      : `<div class="owa-version-files-empty"><strong>没有找到匹配文件</strong><span>调整关键词，或选择左侧其他工程阶段。</span></div>`;
  };

  const render = (overlay) => {
    overlay.querySelector("[data-version-files-name]").textContent = activeAsset.name;
    overlay.querySelector("[data-version-files-version]").textContent = `v${activeAsset.version}`;
    overlay.querySelector("[data-version-files-project]").textContent = activeAsset.sourceProjectId;
    const artifacts = activeAsset.artifacts ?? [];
    const manifest = `07-release/ontology-engineering-package-${activeAsset.version}/manifest.json`;
    const canDownload = (activeAsset.status ?? activeAsset.releaseStatus) === "PUBLISHED"
      && artifacts.some((file) => file.path === manifest);
    const download = overlay.querySelector("[data-version-package-download]");
    if (download) {
      const url = `/orion-workflow-api/package-download?project_id=${encodeURIComponent(activeAsset.sourceProjectId)}&release_version=${encodeURIComponent(activeAsset.version)}`;
      download.innerHTML = canDownload
        ? `<a href="${escapeHtml(url)}" data-version-package-download-link target="_blank" rel="noopener">下载完整工程包</a>`
        : "";
    }
    const coreCount = artifacts.filter(isCoreFile).length;
    overlay.querySelectorAll("[data-version-files-scope]").forEach((button) => {
      const selected = button.dataset.versionFilesScope === scope;
      button.setAttribute("aria-pressed", String(selected));
      button.textContent = button.dataset.versionFilesScope === "all" ? `全部文件 ${artifacts.length}` : `核心文件 ${coreCount}`;
    });
    overlay.querySelector("[data-version-files-summary]").textContent = scope === "core"
      ? "仅展示交付、复用和核验所需的关键产物。"
      : "显示工程账本登记的全部过程文件与证据。";
    overlay.querySelector("[data-version-files-groups]").innerHTML = displayGroups().map(groupMarkup).join("");
    renderList(overlay);
    const list = overlay.querySelector("[data-version-files-list]");
    list.scrollTop = 0;
    if (!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      list.getAnimations?.().forEach(animation => animation.cancel());
      list.animate?.([{ opacity: 0.45, transform: "translateY(4px)" }, { opacity: 1, transform: "translateY(0)" }], { duration: 160, easing: "ease-out" });
    }
  };

  const open = (asset) => {
    if (!asset) return;
    activeAsset = asset;
    scope = "core";
    view = "stage";
    const groups = displayGroups();
    activeGroupId = filesFor(groups[0]).length ? groups[0].id : groups.find((group) => filesFor(group).length)?.id ?? "project";
    query = "";
    previousFocus = document.activeElement;
    const overlay = ensureDialog();
    overlay.hidden = false;
    overlay.querySelector(".owa-version-files-notice").hidden = true;
    overlay.querySelector("[data-version-files-view]").value = view;
    document.body.classList.add("owa-version-files-open");
    const search = overlay.querySelector("[data-version-files-search]");
    if (search) search.value = "";
    render(overlay);
    window.requestAnimationFrame(() => overlay.querySelector("[data-version-files-close]")?.focus());
  };

  function close() {
    window.__ORION_FILE_PREVIEW__?.close();
    const overlay = document.querySelector(".owa-version-files-overlay");
    if (!overlay || overlay.hidden) return;
    overlay.hidden = true;
    document.body.classList.remove("owa-version-files-open");
    previousFocus?.focus?.();
    activeAsset = null;
  }

  document.addEventListener("keydown", (event) => {
    const overlay = document.querySelector(".owa-version-files-overlay");
    if (!overlay || overlay.hidden) return;
    // A file preview owns keyboard focus while it is above this dialog.
    if (document.querySelector(".owa-file-preview[open]")) return;
    if (event.key === "Escape") { event.preventDefault(); close(); }
    if (event.key === "Tab") {
      const targets = [...overlay.querySelectorAll('button:not(:disabled), a[href], input, select, summary')]
        .filter(element => element.getClientRects().length > 0);
      const first = targets[0], last = targets.at(-1);
      if (event.shiftKey && (document.activeElement === first || !overlay.contains(document.activeElement))) {
        event.preventDefault(); last?.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !overlay.contains(document.activeElement))) {
        event.preventDefault(); first?.focus();
      }
    }
  });

  window.__ORION_ONTOLOGY_VERSION_FILES__ = { open, close };
})();
