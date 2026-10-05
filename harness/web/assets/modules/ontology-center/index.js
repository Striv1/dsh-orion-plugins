(() => {
  if (window.__ORION_ONTOLOGY_CENTER__) return;

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const displayOntologyName = (value) => String(value ?? "").replace(
    /(?:\s*(?:[·•]\s*)?(?:[（(]\s*)?(?:重建|修订)(?:\s*[）)])?)+\s*$/u,
    "",
  ).trim() || String(value ?? "").trim();

  const state = window.__ORION_SHELL_STATE__;
  const api = window.__ORION_ONTOLOGY_API__;
  const engineering = window.__ORION_ONTOLOGY_ENGINEERING__;
  const registry = window.__ORION_ONTOLOGY_REGISTRY__;
  const templates = window.__ORION_ONTOLOGY_TEMPLATES__;
  const projectLineage = window.__ORION_PROJECT_LINEAGE__;
  const COLLAPSED_PROJECT_LIMIT = 5;
  let context = null;
  let activeRoot = null;
  let recentProjectEntries = [];
  let projectsExpanded = false;
  let dashboardRefreshTimer = null;
  const releaseBootRouteGuard = () => window.requestAnimationFrame(() => {
    window.__ORION_BOOT_ROUTE_GUARD__?.release?.();
  });

  const sessionEngineeringRoot = () => {
    const tabList = [...document.querySelectorAll('[role="tablist"]')].find((candidate) => {
      const labels = [...candidate.querySelectorAll('[role="tab"]')].map((tab) => tab.textContent?.trim());
      return labels.includes("对话") && labels.includes("轨迹");
    });
    return tabList?.closest("header")?.parentElement?.parentElement ?? null;
  };

  const engineeringRoot = () => {
    if (window.__ORION_NATIVE_PLUGIN__ === true) return window.__ORION_ENGINEERING_BRIDGE__?.getRoot?.() ?? null;
    // The center column exists on both the welcome screen and an opened chat.
    // Mounting here keeps ontology pages independent from session-only tabs.
    const frame = document.querySelector("[data-dsh-frame]");
    return frame?.querySelector(':scope > [class*="centerCol"]')
      ?? sessionEngineeringRoot();
  };

  const projectStatusLabel = (value) => ({
    PUBLISHED: "已发布",
    PACKAGE_PUBLISHED: "发布包已生成",
    RUNTIME_VERIFYING: "运行时验证中",
    RUNTIME_FAILED: "运行时验证失败",
    PACKAGE_READY_RUNTIME_BLOCKED: "发布包就绪，运行时待恢复",
    RELEASE_REVOKED: "已撤回",
    ACTIVE: "进行中",
    IN_PROGRESS: "进行中",
    BLOCKED_HUMAN: "待确认",
    ARCHIVED: "已归档",
    DRAFT: "工程草稿",
  })[value] ?? "进行中";

  const collapsedProjects = (entries, activeProjectId) => {
    const visible = entries.slice(0, COLLAPSED_PROJECT_LIMIT);
    if (
      !activeProjectId
      || visible.some((entry) => entry.lineage_project_ids?.includes(activeProjectId))
    ) return visible;
    const activeEntry = entries.find((entry) => entry.lineage_project_ids?.includes(activeProjectId));
    if (!activeEntry) return visible;
    return [...visible.slice(0, COLLAPSED_PROJECT_LIMIT - 1), activeEntry];
  };

  const projectButtonMarkup = (entry, activeProjectId) => {
    const active = entry.lineage_project_ids?.includes(activeProjectId);
    const projectName = displayOntologyName(entry.project_name);
    const status = entry.current_stage
      ? `${entry.current_stage} · ${projectStatusLabel(entry.project_status)}`
      : projectStatusLabel(entry.project_status);
    const versionSummary = Number(entry.lineage_version_count ?? 1) > 1
      ? `${status}，${entry.lineage_version_count} 次建设`
      : status;
    return `
      <button type="button" class="owa-ontology-recent-item${active ? " is-active" : ""}" data-ontology-project="${escapeHtml(entry.project_id)}" title="查看“${escapeHtml(projectName)}”工程详情" aria-current="${active ? "page" : "false"}">
        <span>${escapeHtml(projectName)}</span>
        <small>${escapeHtml(versionSummary)}</small>
      </button>`;
  };

  const recentMarkup = (entries, activeProjectId = null) => {
    if (!entries.length) return '<p class="owa-ontology-recent-empty">暂无工程</p>';
    const visible = projectsExpanded ? entries : collapsedProjects(entries, activeProjectId);
    const remainingCount = Math.max(0, entries.length - visible.length);
    const toggle = entries.length > COLLAPSED_PROJECT_LIMIT ? `
      <button type="button" class="owa-ontology-project-toggle" data-ontology-recent-toggle aria-expanded="${projectsExpanded}" aria-controls="owa-ontology-project-list">
        ${projectsExpanded ? "收起工程列表" : `展开其余 ${remainingCount} 个工程`}
      </button>` : "";
    return `
      <div class="owa-ontology-project-list" id="owa-ontology-project-list" data-expanded="${projectsExpanded}">
        ${visible.map((entry) => projectButtonMarkup(entry, activeProjectId)).join("")}
      </div>
      ${toggle}`;
  };

  const activeProjectId = (route = state.get()) => route.section === "engineering" ? route.projectId : null;

  const renderDashboard = async (force = false) => {
    if (!context) return;
    const summary = context.querySelector("[data-ontology-summary]");
    const recent = context.querySelector("[data-ontology-recent]");
    try {
      const payload = await api.dashboard(force);
      const projects = payload.projects ?? [];
      const projectGroups = projectLineage?.groupProjects?.(projects) ?? projects.map((project) => ({
        current: project,
        displayName: displayOntologyName(project.project_name ?? project.project_id),
        projectIds: [project.project_id],
        versionCount: 1,
        allArchived: project.project_status === "ARCHIVED",
        published: project.project_status === "PUBLISHED",
      }));
      recentProjectEntries = projectGroups
        .filter((group) => !group.allArchived)
        .map((group) => ({
          ...group.current,
          project_name: group.displayName,
          lineage_project_ids: group.projectIds,
          lineage_version_count: group.versionCount,
        }));
      const active = recentProjectEntries.length;
      // Count released ontology assets by IRI, independently of active revisions.
      const published = (await api.catalog(force, payload)).length;
      summary.setAttribute("aria-label", `${active} 个工程，${published} 个正式本体`);
      summary.innerHTML = `<strong>${active}</strong><span>工程</span><i></i><strong>${published}</strong><span>正式本体</span>`;
      recent.innerHTML = recentMarkup(recentProjectEntries, activeProjectId());
    } catch (_error) {
      summary.removeAttribute("aria-label");
      summary.innerHTML = "<span>状态暂不可读</span>";
      recent.innerHTML = '<p class="owa-ontology-recent-empty">工程列表暂不可读</p>';
    }
  };

  const ensureContext = (shellNav) => {
    if (context?.isConnected) return context;
    context = document.createElement("section");
    context.className = "owa-ontology-context";
    context.hidden = true;
    context.innerHTML = `
      <div class="owa-ontology-context-heading"><span>本体中心</span><small data-ontology-summary aria-live="polite">读取中</small></div>
      <button type="button" class="owa-ontology-new-project" data-ontology-action="new-project" data-dsh-surface aria-label="新建工程"><span class="owa-shell-icon is-tools" aria-hidden="true"></span><strong>新建工程</strong></button>
      <nav aria-label="本体中心功能">
        <button type="button" data-ontology-section="engineering"><span class="owa-shell-icon is-tools" aria-hidden="true"></span><span><strong>工程详情</strong><small>建设进度与阶段成果</small></span></button>
        <button type="button" data-ontology-section="manage"><span class="owa-shell-icon is-database" aria-hidden="true"></span><span><strong>本体管理</strong><small>正式资产与版本</small></span></button>
        <button type="button" data-ontology-section="templates"><span class="owa-shell-icon is-network" aria-hidden="true"></span><span><strong>行业模板</strong><small>可复用基线与行业参考</small></span></button>
      </nav>
      <div class="owa-ontology-recent"><header><span>最近工程</span><small>点击查看工程详情</small></header><div data-ontology-recent><p class="owa-ontology-recent-empty">读取中</p></div></div>`;
    if (window.__ORION_NATIVE_PLUGIN__ === true) shellNav.append(context);
    else shellNav.insertAdjacentElement("afterend", context);
    context.addEventListener("click", (event) => {
      if (event.target.closest("[data-ontology-recent-toggle]")) {
        projectsExpanded = !projectsExpanded;
        const recent = context.querySelector("[data-ontology-recent]");
        recent.innerHTML = recentMarkup(recentProjectEntries, activeProjectId());
        return;
      }
      const projectId = event.target.closest("[data-ontology-project]")?.dataset.ontologyProject;
      if (projectId) {
        const project = recentProjectEntries.find((item) => item.project_id === projectId);
        window.__ORION_CHAT_MODES__?.rememberProjectSession?.(projectId, project?.project_name ?? "");
        state.navigate({
          primary: "ontology",
          section: "engineering",
          projectId,
          stage: project?.current_stage ?? null,
        });
        return;
      }
      const newProject = event.target.closest('[data-ontology-action="new-project"]');
      if (newProject) {
        window.__ORION_CHAT_MODES__?.openNewSession?.();
        return;
      }
      const section = event.target.closest("[data-ontology-section]")?.dataset.ontologySection;
      if (section) {
        state.navigate({ primary: "ontology", section, projectId: null, stage: null });
        return;
      }
    });
    return context;
  };

  const updateContext = (route) => {
    if (!context) return;
    for (const button of context.querySelectorAll("[data-ontology-section]")) {
      const selected = button.dataset.ontologySection === route.section;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-current", selected ? "page" : "false");
    }
    if (recentProjectEntries.length) {
      context.querySelector("[data-ontology-recent]").innerHTML = recentMarkup(recentProjectEntries, activeProjectId(route));
    }
  };

  const activate = (route, shellNav) => {
    ensureContext(shellNav).hidden = false;
    updateContext(route);
    activeRoot = engineeringRoot();
    if (!activeRoot) return false;
    renderDashboard();
    if (!dashboardRefreshTimer) {
      dashboardRefreshTimer = window.setInterval(() => {
        if (context && !context.hidden && document.visibilityState === "visible") {
          // Routine polling respects the API's 15-second TTL. Explicit
          // refresh after an action still bypasses it through refresh().
          renderDashboard();
        }
      }, 5000);
    }
    activeRoot.dataset.orionOntologyRoot = "true";
    if (route.section === "manage") {
      engineering.close();
      templates.hide(activeRoot);
      registry.show(activeRoot);
      releaseBootRouteGuard();
      return true;
    }
    if (route.section === "templates") {
      engineering.close();
      registry.hide(activeRoot);
      templates.show(activeRoot);
      releaseBootRouteGuard();
      return true;
    }
    registry.hide(activeRoot);
    templates.hide(activeRoot);
    engineering.open(route);
    releaseBootRouteGuard();
    return true;
  };

  const deactivate = () => {
    if (context) context.hidden = true;
    if (dashboardRefreshTimer) {
      window.clearInterval(dashboardRefreshTimer);
      dashboardRefreshTimer = null;
    }
    registry.hide(activeRoot);
    templates.hide(activeRoot);
    engineering.close();
    return true;
  };

  const dispose = () => {
    deactivate();
    context?.remove();
    context = null;
    activeRoot = null;
  };

  const isMounted = (route) => {
    const root = engineeringRoot();
    if (!root || root !== activeRoot || !activeRoot?.isConnected) return false;
    if (route.section === "manage") {
      return Boolean(root.querySelector(":scope > .owa-ontology-registry-view:not([hidden])"));
    }
    if (route.section === "templates") {
      return Boolean(root.querySelector(":scope > .owa-ontology-templates-view:not([hidden])"));
    }
    const view = root.querySelector(":scope > .owa-engineering-view");
    return Boolean(view && !view.hidden);
  };

  window.__ORION_ONTOLOGY_CENTER__ = {
    activate,
    deactivate,
    dispose,
    ensureContext,
    isMounted,
    refresh: () => renderDashboard(true),
  };
})();
