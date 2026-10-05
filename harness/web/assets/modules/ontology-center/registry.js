(() => {
  if (window.__ORION_ONTOLOGY_REGISTRY__) return;

  const PAGE_SIZE = 12;
  let renderedAssets = [];
  let currentPage = 1;
  const selectedVersionProjects = new Map();

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  let closeSemantica = () => {};
  // Dedicated embedded explorer: never shared with the ontology graph viewer.
  const openSemantica = (asset, reportError = () => {}) => {
    closeSemantica();
    const returnFocus = document.activeElement;
    const panel = document.createElement("dialog");
    panel.className = "owa-semantica-explorer-dialog";
    panel.setAttribute("aria-label", "Semantica 探索");
    panel.style.cssText = "position:fixed;inset:0;margin:0;box-sizing:border-box;width:100vw;max-width:none;height:100dvh;max-height:none;padding:0;border:0;border-radius:0;background:#081321;color:#e4edf7;overflow:hidden;";
    panel.innerHTML = `<div style="height:100%;display:flex;flex-direction:column"><header style="display:flex;align-items:center;gap:12px;flex:0 0 44px;box-sizing:border-box;margin:0;padding:0 12px;border:0;border-bottom:1px solid #42566d;border-radius:0;background:#101d2c;color:#e4edf7;font-size:13px"><strong>Semantica 探索</strong><span data-scope style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span><a data-standalone hidden target="_blank" rel="noopener noreferrer" style="flex-shrink:0;color:#9cdce9;text-decoration:none">新窗口打开 ↗</a><button type="button" data-close aria-label="关闭 Semantica 探索" style="flex-shrink:0">关闭</button></header><section data-loading role="status" style="padding:24px"><h3 data-status>正在核验连接</h3><p data-detail>正在读取所选本体和发布版本的接入状态…</p><button type="button" data-retry hidden>重新连接</button></section><iframe title="当前本体的 Semantica 实例探索" hidden style="flex:1;width:100%;min-height:0;border:0;background:#081321"></iframe></div>`;
    panel.querySelector("[data-scope]").textContent = `${asset.name} · ${asset.version || "版本待核验"}`;
    const status = panel.querySelector("[data-status]"), detail = panel.querySelector("[data-detail]"), retry = panel.querySelector("[data-retry]");
    const loading = panel.querySelector("[data-loading]"), frame = panel.querySelector("iframe");
    let disposed = false, busy = false, timer;
    const close = () => {
      if (disposed) return;
      disposed = true;
      window.clearTimeout(timer);
      frame.onload = null;
      frame.src = "about:blank";
      frame.remove();
      panel.close();
      panel.remove();
      returnFocus?.focus?.();
    };
    closeSemantica = close;
    panel.querySelector("[data-close]").addEventListener("click", close);
    panel.addEventListener("cancel", event => { event.preventDefault(); close(); });
    panel.addEventListener("close", close);
    const connect = async () => {
      if (busy || disposed) return;
      busy = true;
      retry.hidden = true;
      status.textContent = "正在核验连接";
      detail.textContent = "正在读取所选本体和发布版本的接入状态…";
      try {
        const result = await window.__ORION_ONTOLOGY_API__.semanticaStatus(asset.sourceProjectId);
        if (disposed) return;
        if (result.available === false) throw new Error("Semantica 服务连接失败，请稍后重新连接。");
        if (!result.linked || !result.explorer_url) throw new Error(result.detail || "所选本体版本尚未接入 Semantica。");
        const url = new URL(result.explorer_url);
        if (!["http:", "https:"].includes(url.protocol) || url.searchParams.get("orionProjectId") !== asset.sourceProjectId || !url.searchParams.get("ontologyUri") || !url.searchParams.get("orionReleaseVersion") || !url.searchParams.get("orionSourceSha256") || (asset.version && url.searchParams.get("orionReleaseVersion") !== asset.version) || (asset.ontologyIri && url.searchParams.get("ontologyUri") !== asset.ontologyIri)) {
          throw new Error("接入地址缺少所选本体的版本绑定，暂不打开。请重新核验连接。");
        }
        url.searchParams.set("orionViewEpoch", String(Date.now()));
        const standalone = new URL(url.href);
        standalone.searchParams.delete("orionEmbedded");
        const standaloneLink = panel.querySelector("[data-standalone]");
        standaloneLink.href = standalone.href;
        standaloneLink.hidden = false;
        url.searchParams.set("orionEmbedded", "1");
        frame.onload = () => { if (!disposed) { window.clearTimeout(timer); loading.hidden = true; frame.hidden = false; } };
        status.textContent = "正在加载实例探索";
        timer = window.setTimeout(() => { if (!disposed) { status.textContent = "加载时间较长"; detail.textContent = "服务尚未完成响应，可以重新连接。"; retry.hidden = false; } }, 30000);
        frame.src = url.href;
      } catch (error) {
        if (disposed) return;
        status.textContent = "暂时无法打开 Semantica";
        detail.textContent = `${error instanceof Error ? error.message : "连接核验失败。"} 当前面板未加载数据，不代表本体没有实例。`;
        retry.hidden = false;
      } finally { busy = false; }
    };
    retry.addEventListener("click", () => { window.clearTimeout(timer); connect(); });
    document.body.appendChild(panel);
    panel.showModal();
    connect();
  };

  const dateLabel = (value) => {
    if (!value) return "发布时间待回读";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? String(value)
      : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium" }).format(date);
  };

  const ensureView = (root) => {
    let view = root.querySelector(":scope > .owa-ontology-registry-view");
    if (view) return view;
    view = document.createElement("section");
    view.className = "owa-ontology-registry-view";
    view.hidden = true;
    view.setAttribute("aria-label", "本体管理");
    root.appendChild(view);
    return view;
  };

  const announce = (view, message, tone = "info") => {
    let notice = view.querySelector(".owa-registry-notice");
    if (!notice) {
      notice = document.createElement("div");
      notice.className = "owa-registry-notice";
      notice.setAttribute("role", "status");
      notice.setAttribute("aria-live", "polite");
      view.appendChild(notice);
    }
    notice.dataset.tone = tone;
    notice.textContent = message;
    notice.hidden = false;
    window.clearTimeout(Number(notice.dataset.timer ?? 0));
    notice.dataset.timer = String(window.setTimeout(() => { notice.hidden = true; }, 3600));
  };

  const loadingMarkup = () => `
    <div class="owa-registry-heading">
      <div><span>ONTOLOGY REGISTRY</span><h1>本体管理</h1><p>只展示已完成 S7 正式发布、可以追溯来源工程的本体资产。</p></div>
    </div>
    <div class="owa-registry-state" role="status"><strong>正在回读已发布资产</strong><span>数据来自本机工作流状态接口。</span></div>`;

  const emptyMarkup = () => `
    <div class="owa-registry-state is-empty">
      <span class="owa-registry-state-icon" aria-hidden="true"></span>
      <strong>还没有正式发布的本体</strong>
      <p>工程完成 S7 发布后会自动出现在这里；未发布工程不会伪装成本体资产。</p>
      <button type="button" data-registry-action="open-engineering">进入工程详情</button>
    </div>`;

  const selectedRelease = (asset) => {
    const selectedProjectId = selectedVersionProjects.get(asset.ontologyKey);
    return (asset.versions ?? []).find((item) => item.sourceProjectId === selectedProjectId)
      ?? asset;
  };

  const releaseByProjectId = (projectId) => {
    for (const asset of renderedAssets) {
      const release = (asset.versions ?? []).find((item) => item.sourceProjectId === projectId);
      if (release) return release;
      if (asset.sourceProjectId === projectId) return asset;
    }
    return null;
  };

  const contractLabel = (release) => ({
    SEMANTIC_CONTRACT_RECORDED: "CQ 合同 v2",
    NEW_CONTRACT_RECORDED: `CQ 合同 ${String(release.releaseContract?.contract_version ?? "v1").replace("cq-answer-", "")}`,
    LEGACY_UNVERIFIED: "当前标准未验证",
    PACKAGE_INTEGRITY_FAILED: "发布包完整性异常",
  })[release.releaseContract?.status] ?? "验收信息待回读";

  // Names come from release bindings; never invent a Docker identity in the UI.
  const runtimeMarkup = (release, compact = false) => {
    const runtime = release.runtimeService ?? release.runtime_service ?? {};
    const kind = String(runtime.kind ?? "").toLowerCase();
    const project = runtime.compose_project ?? runtime.composeProject ?? "";
    const classes = `owa-registry-runtime${compact ? " is-compact" : ""}`;
    if (kind === "document" || kind === "document_runtime") {
      return `<div class="${classes}" data-runtime-kind="document"><span class="owa-registry-runtime-label">文档运行时</span><small>无独立 Docker 查询容器</small></div>`;
    }
    if (!project) {
      return `<div class="${classes}" data-runtime-kind="unknown"><span class="owa-registry-runtime-label">运行服务</span><small>尚未读取到 Docker 服务绑定</small></div>`;
    }
    return `<div class="${classes}" data-runtime-kind="docker_compose">
      <span class="owa-registry-runtime-label">Docker 服务${compact ? "" : ` · v${escapeHtml(release.version)}`}</span>
      <div class="owa-registry-runtime-identity"><code title="${escapeHtml(project)}">${escapeHtml(project)}</code><button type="button" data-registry-copy-runtime="${escapeHtml(project)}" aria-label="复制版本 ${escapeHtml(release.version)} 的 Docker 服务名">复制</button></div>
      ${compact ? "" : '<small>复制后，在 Docker Desktop 搜索同名项目分组</small>'}
    </div>`;
  };

  const assetMarkup = (asset) => {
    const release = selectedRelease(asset);
    const viewingCurrent = release.versionStatus === "CURRENT" || release.sourceProjectId === asset.sourceProjectId;
    const api = window.__ORION_ONTOLOGY_API__;
    const artifactLink = release.artifact?.path
      ? `<a href="${escapeHtml(api.artifactUrl(release.sourceProjectId, release.artifact.path))}" target="_blank" rel="noopener">发布报告</a>`
      : `<span class="is-muted">报告待生成</span>`;
    const statusLabel = (status) => ({
      CURRENT: "当前版本",
      HISTORICAL: "历史版本",
      REVOKED: "已撤回",
    })[status] ?? status;
    const versionHistory = (asset.versions ?? []).map((item) => {
      const selected = item.sourceProjectId === release.sourceProjectId;
      return `
      <li data-version-status="${escapeHtml(item.versionStatus)}"${selected ? " data-selected-version" : ""}>
        <button type="button" class="owa-registry-version-select" data-registry-version="${escapeHtml(item.sourceProjectId)}" data-registry-ontology="${escapeHtml(asset.ontologyKey)}" aria-pressed="${selected}" aria-label="查看版本 ${escapeHtml(item.version)}">
          <span><b>v${escapeHtml(item.version)}</b><em>${escapeHtml(statusLabel(item.versionStatus))}</em></span><small>${escapeHtml(contractLabel(item))}</small>
        </button>
        <button type="button" data-registry-project="${escapeHtml(item.sourceProjectId)}">来源工程</button>
        ${runtimeMarkup(item, true)}
      </li>`;
    }).join("");
    const versionBlock = `
      <details class="owa-registry-version-history"${viewingCurrent ? "" : " open"}${asset.versionCount > 1 ? "" : " data-single-version"}>
        <summary><span>版本记录 · 选择查看版本</span><b>${escapeHtml(asset.versionCount ?? 1)} 个版本</b></summary>
        <ul>${versionHistory}</ul>
      </details>`;
    const historicalNotice = viewingCurrent ? "" : `
      <aside class="owa-registry-version-context" data-contract-status="${escapeHtml(release.releaseContract?.status ?? "UNKNOWN")}" role="status">
        <div><strong>当前查看历史版本 v${escapeHtml(release.version)}</strong><span>最新发布版本为 v${escapeHtml(asset.version)}。当前操作只针对该历史发布资产，不会改变系统当前版本。</span><small>${escapeHtml(contractLabel(release))}</small></div>
        <button type="button" data-registry-version="${escapeHtml(asset.sourceProjectId)}" data-registry-ontology="${escapeHtml(asset.ontologyKey)}">返回最新版</button>
      </aside>`;
    return `
      <article class="owa-registry-card" data-viewing-version="${escapeHtml(release.versionStatus ?? "CURRENT")}">
        <header>
          <div class="owa-registry-card-kicker"><span>本体资产</span><b>${viewingCurrent ? "当前" : "正在查看"} v${escapeHtml(release.version)}</b></div>
          <em>${viewingCurrent ? "当前版本" : "历史版本"}</em>
        </header>
        <div class="owa-registry-card-summary">
          <h2>${escapeHtml(release.name)}</h2>
          <div class="owa-registry-card-identity"><code title="${escapeHtml(release.ontologyIri ?? release.ontologyId)}">${escapeHtml(release.ontologyIri ?? release.ontologyId)}</code><span>${viewingCurrent ? "当前版" : "该版"}发布于 ${escapeHtml(dateLabel(release.publishedAt))}</span></div>
          <div class="owa-registry-card-quality"><span>质量状态</span><strong>${escapeHtml(release.qualityStatus)}</strong></div>
        </div>
        ${versionBlock}
        ${historicalNotice}
        <dl aria-label="本体规模">
          <div><dt>类</dt><dd>${escapeHtml(release.classes)}</dd></div>
          <div><dt>对象属性</dt><dd>${escapeHtml(release.objectProperties)}</dd></div>
          <div><dt>数据属性</dt><dd>${escapeHtml(release.dataProperties)}</dd></div>
        </dl>
        <footer>
          <div class="owa-registry-primary-actions" role="group" aria-label="本体主要操作"><button type="button" class="is-graph" data-registry-graph="${escapeHtml(release.sourceProjectId)}">查看本体图谱</button><button type="button" class="is-semantica" data-registry-semantica="${escapeHtml(release.sourceProjectId)}">Semantica 探索</button><button type="button" class="is-protege" data-registry-protege="${escapeHtml(release.sourceProjectId)}">在 Protégé 中查看</button></div>
          <div class="owa-registry-secondary-actions" role="group" aria-label="本体辅助操作"><button type="button" data-registry-files="${escapeHtml(release.sourceProjectId)}">版本文件</button>${artifactLink}<button type="button" data-registry-question="${escapeHtml(release.sourceProjectId)}">发起本体问答</button><button type="button" data-registry-project="${escapeHtml(release.sourceProjectId)}">来源工程</button></div>
        </footer>
      </article>`;
  };

  const pageNumbers = (page, totalPages) => {
    if (totalPages <= 7) return Array.from({ length: totalPages }, (_, index) => index + 1);
    const pages = new Set([1, totalPages, page - 1, page, page + 1]);
    const ordered = [...pages].filter((item) => item >= 1 && item <= totalPages).sort((a, b) => a - b);
    return ordered.flatMap((item, index) => {
      if (index === 0 || item === ordered[index - 1] + 1) return [item];
      return ["ellipsis", item];
    });
  };

  const paginationMarkup = (totalAssets, page, totalPages, start, visibleCount) => {
    if (totalPages <= 1) return "";
    const numberedPages = pageNumbers(page, totalPages).map((item, index) => item === "ellipsis"
      ? `<span class="owa-registry-page-ellipsis" aria-hidden="true" data-page-gap="${index}">…</span>`
      : `<button type="button" class="owa-registry-page-number" data-registry-page="${item}"${item === page ? ' aria-current="page"' : ""} aria-label="第 ${item} 页">${item}</button>`).join("");
    return `
      <nav class="owa-registry-pagination" aria-label="本体列表分页">
        <span>显示 ${start + 1}–${start + visibleCount}，共 ${totalAssets} 个本体</span>
        <div class="owa-registry-page-controls">
          <button type="button" data-registry-page="${page - 1}"${page === 1 ? " disabled" : ""}>上一页</button>
          ${numberedPages}
          <button type="button" data-registry-page="${page + 1}"${page === totalPages ? " disabled" : ""}>下一页</button>
        </div>
      </nav>`;
  };

  const renderAssets = (view, assets) => {
    renderedAssets = assets;
    const totalPages = Math.max(1, Math.ceil(assets.length / PAGE_SIZE));
    currentPage = Math.min(totalPages, Math.max(1, currentPage));
    const start = (currentPage - 1) * PAGE_SIZE;
    const visibleAssets = assets.slice(start, start + PAGE_SIZE);
    view.innerHTML = `
      <div class="owa-registry-heading">
        <div class="owa-registry-heading-copy"><span>ONTOLOGY REGISTRY</span><div class="owa-registry-title-row"><h1>本体管理</h1><div class="owa-registry-summary" aria-label="${assets.length} 个正式本体资产"><strong>${assets.length}</strong><span>个正式本体资产</span></div></div><p>同一本体按 IRI 聚合为一张资产卡；默认使用当前版本，历史与撤回版本保留审计入口。</p></div>
      </div>
      ${assets.length ? `<div class="owa-registry-grid">${visibleAssets.map(assetMarkup).join("")}</div>${paginationMarkup(assets.length, currentPage, totalPages, start, visibleAssets.length)}` : emptyMarkup()}`;
  };

  const refresh = async (root, force = false) => {
    const view = ensureView(root);
    const requestId = String(Number(view.dataset.requestId ?? 0) + 1);
    view.dataset.requestId = requestId;
    view.setAttribute("aria-busy", "true");
    view.innerHTML = loadingMarkup();
    try {
      const assets = await window.__ORION_ONTOLOGY_API__.catalog(force);
      if (view.dataset.requestId !== requestId) return;
      renderAssets(view, assets);
    } catch (error) {
      if (view.dataset.requestId !== requestId) return;
      view.innerHTML = `<div class="owa-registry-error" role="alert"><strong>本体资产暂时无法读取</strong><span>${escapeHtml(error instanceof Error ? error.message : "未知错误")}</span><button type="button" data-registry-action="retry">重新读取</button></div>`;
    } finally {
      if (view.dataset.requestId === requestId) view.setAttribute("aria-busy", "false");
    }
  };

  const show = (root) => {
    const view = ensureView(root);
    view.hidden = false;
    root.classList.add("owa-ontology-registry-active");
    if (!view.dataset.bound) {
      view.dataset.bound = "true";
      view.addEventListener("click", (event) => {
        const copyButton = event.target.closest("[data-registry-copy-runtime]");
        if (copyButton) {
          const serviceName = copyButton.dataset.registryCopyRuntime;
          if (window.navigator?.clipboard?.writeText) {
            window.navigator.clipboard.writeText(serviceName)
              .then(() => announce(view, "已复制 Docker 服务名，请在 Docker Desktop 搜索框粘贴。", "success"))
              .catch(() => announce(view, `复制失败，请手动选中服务名复制：${serviceName}`, "error"));
          } else {
            announce(view, `当前浏览器不支持自动复制，请手动选中服务名：${serviceName}`, "error");
          }
          return;
        }
        const versionButton = event.target.closest("[data-registry-version]");
        if (versionButton) {
          const ontologyKey = versionButton.dataset.registryOntology;
          const sourceProjectId = versionButton.dataset.registryVersion;
          const asset = renderedAssets.find((item) => item.ontologyKey === ontologyKey);
          const release = asset?.versions?.find((item) => item.sourceProjectId === sourceProjectId);
          if (asset && release) {
            selectedVersionProjects.set(ontologyKey, sourceProjectId);
            renderAssets(view, renderedAssets);
            window.requestAnimationFrame(() => {
              view.querySelector(`[data-registry-version="${CSS.escape(sourceProjectId)}"]`)?.focus({ preventScroll: true });
            });
          }
          return;
        }
        const pageButton = event.target.closest("[data-registry-page]");
        if (pageButton && !pageButton.disabled) {
          const requestedPage = Number(pageButton.dataset.registryPage);
          if (Number.isInteger(requestedPage) && requestedPage !== currentPage) {
            currentPage = requestedPage;
            renderAssets(view, renderedAssets);
            const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
            view.scrollTo?.({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
            window.requestAnimationFrame(() => view.querySelector('[aria-current="page"]')?.focus({ preventScroll: true }));
          }
          return;
        }
        const graphProjectId = event.target.closest("[data-registry-graph]")?.dataset.registryGraph;
        if (graphProjectId) {
          const asset = releaseByProjectId(graphProjectId);
          if (asset) window.__ORION_ONTOLOGY_GRAPH_VIEWER__?.open(asset);
          return;
        }
        const semanticaProjectId = event.target.closest("[data-registry-semantica]")?.dataset.registrySemantica;
        if (semanticaProjectId) {
          const asset = releaseByProjectId(semanticaProjectId);
          if (asset) openSemantica(asset, message => announce(view, message, "error"));
          return;
        }
        const protegeButton = event.target.closest("[data-registry-protege]");
        const protegeProjectId = protegeButton?.dataset.registryProtege;
        if (protegeProjectId) {
          const originalLabel = protegeButton.innerHTML;
          protegeButton.disabled = true;
          protegeButton.classList.add("is-busy");
          protegeButton.textContent = "正在打开本体模型…";
          window.__ORION_ONTOLOGY_API__.openProtege(protegeProjectId)
            .then((result) => announce(view, result.detail ?? "正在建模工具中加载本体模型。", "success"))
            .catch((error) => announce(view, error instanceof Error ? error.message : "暂时无法打开本体模型。", "error"))
            .finally(() => {
              protegeButton.disabled = false;
              protegeButton.classList.remove("is-busy");
              protegeButton.innerHTML = originalLabel;
            });
          return;
        }
        const questionProjectId = event.target.closest("[data-registry-question]")?.dataset.registryQuestion;
        if (questionProjectId) {
          const asset = releaseByProjectId(questionProjectId);
          if (asset) window.__ORION_CHAT_MODES__?.openOntology?.(asset);
          return;
        }
        const filesProjectId = event.target.closest("[data-registry-files]")?.dataset.registryFiles;
        if (filesProjectId) {
          const asset = releaseByProjectId(filesProjectId);
          if (asset) window.__ORION_ONTOLOGY_VERSION_FILES__?.open(asset);
          return;
        }
        const projectId = event.target.closest("[data-registry-project]")?.dataset.registryProject;
        if (projectId) {
          window.__ORION_SHELL_STATE__.navigate({
            primary: "ontology",
            section: "engineering",
            projectId,
            stage: "S7",
          });
          return;
        }
        const action = event.target.closest("[data-registry-action]")?.dataset.registryAction;
        if (action === "open-engineering") {
          window.__ORION_SHELL_STATE__.navigate({ primary: "ontology", section: "engineering" });
        }
        if (action === "retry") {
          currentPage = 1;
          refresh(root, true);
        }
      });
    }
    currentPage = 1;
    refresh(root);
  };

  const hide = (root) => {
    closeSemantica();
    root?.classList.remove("owa-ontology-registry-active");
    const view = root?.querySelector(":scope > .owa-ontology-registry-view");
    if (view) view.hidden = true;
  };

  window.__ORION_ONTOLOGY_REGISTRY__ = { show, hide, refresh, openSemantica };
})();
