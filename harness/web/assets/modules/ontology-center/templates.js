(() => {
  if (window.__ORION_ONTOLOGY_TEMPLATES__) return;

  const api = window.__ORION_ONTOLOGY_API__;
  const workflow = window.__ORION_ENGINEERING_WORKFLOW_CLIENT__;
  const FILTERS = [
    ["ALL", "全部"],
    ["ORION_TEMPLATE", "AHS 模板"],
    ["INDUSTRY_REFERENCE", "行业参考"],
  ];
  let templates = [];
  let activeFilter = "ALL";
  let search = "";
  let activeTemplate = null;
  let dataSources = [];
  let dataSourceState = "idle";
  let dataSourceMessage = "";
  let selectedDatasourceId = "";
  let selectedDatabase = "";
  let selectedSchema = "";
  let selectedTables = [];
  let databases = [];
  let schemas = [];
  let tables = [];
  let catalogLoading = "";
  let catalogMessage = "";
  let catalogRequest = 0;
  let sourceRequest = 0;
  let createInFlight = false;
  let createMessage = "";

  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");

  const ensureView = (root) => {
    let view = root.querySelector(":scope > .owa-ontology-templates-view");
    if (view) return view;
    view = document.createElement("section");
    view.className = "owa-ontology-templates-view";
    view.hidden = true;
    view.setAttribute("aria-label", "行业模板");
    root.appendChild(view);
    bind(view, root);
    return view;
  };

  const templateAsset = (template) => ({
    templateId: template.id,
    ontologyId: template.id,
    name: template.name,
    version: `v${template.version}`,
  });

  const kindLabel = (template) => template.kind === "ORION_TEMPLATE" ? "AHS 模板" : "行业参考";

  const filteredTemplates = () => templates.filter((template) => {
    if (activeFilter !== "ALL" && template.kind !== activeFilter) return false;
    if (!search) return true;
    const haystack = [template.name, template.short_name, template.domain, template.description, template.source_label]
      .join(" ").toLocaleLowerCase("zh-CN");
    return haystack.includes(search.toLocaleLowerCase("zh-CN"));
  });

  const shaLabel = (value) => {
    const digest = String(value ?? "").replace(/^sha256:/u, "");
    return digest ? `SHA-256 ${digest.slice(0, 10)}…${digest.slice(-6)}` : "SHA-256 待回读";
  };

  const cardMarkup = (template) => {
    const stats = template.stats ?? {};
    const isOrion = template.kind === "ORION_TEMPLATE";
    return `<article class="owa-template-card${isOrion ? " is-orion" : " is-reference"}">
      <div class="owa-template-card-copy">
        <div class="owa-template-card-meta"><span>${escapeHtml(kindLabel(template))} · ${escapeHtml(template.domain)}</span><em>${escapeHtml(template.status_label ?? kindLabel(template))}</em></div>
        <h2>${escapeHtml(template.name)}</h2>
        <b>v${escapeHtml(template.version)}</b>
        <p>${escapeHtml(template.description)}</p>
      </div>
      <dl aria-label="模板规模">
        <div><dt>类</dt><dd>${escapeHtml(stats.classes ?? "—")}</dd></div>
        <div><dt>关系</dt><dd>${escapeHtml(stats.object_properties ?? "—")}</dd></div>
        <div><dt>约束</dt><dd>${escapeHtml(stats.constraints ?? "—")}</dd></div>
      </dl>
      <div class="owa-template-provenance"><span>${escapeHtml(template.source_label)}</span><span>${escapeHtml(template.license)}</span><span title="${escapeHtml(template.sha256)}">${escapeHtml(shaLabel(template.sha256))}</span></div>
      <footer>
        ${isOrion ? `<button type="button" class="is-primary" data-template-create="${escapeHtml(template.id)}">基于模板新建工程</button>` : ""}
        <button type="button" data-template-graph="${escapeHtml(template.id)}">查看本体图谱</button>
        <button type="button" data-template-protege="${escapeHtml(template.id)}">在 Protégé 中查看</button>
        <a href="${escapeHtml(template.download_url)}" download>下载 OWL</a>
        ${template.source_url ? `<a class="is-source" href="${escapeHtml(template.source_url)}" target="_blank" rel="noopener">官方来源</a>` : ""}
      </footer>
    </article>`;
  };

  const mainMarkup = () => {
    const visible = filteredTemplates();
    const orionCount = templates.filter((item) => item.kind === "ORION_TEMPLATE").length;
    const referenceCount = templates.filter((item) => item.kind === "INDUSTRY_REFERENCE").length;
    return `<div class="owa-template-heading">
      <span>ONTOLOGY TEMPLATES</span>
      <div class="owa-template-title-row"><h1>行业模板</h1><div class="owa-template-summary"><strong>${orionCount}</strong><span>个 AHS 模板</span><i></i><strong>${referenceCount}</strong><span>个行业参考</span></div></div>
      <p>查看可复用的本体建设基线和固定版本的行业参考；模板进入工程后仍需结合客户资料与 Chat2DB 数据完成 S0–S7。</p>
    </div>
    <div class="owa-template-toolbar">
      <label class="owa-template-search"><input type="search" value="${escapeHtml(search)}" placeholder="搜索模板、行业或来源" aria-label="搜索行业模板"></label>
      <div class="owa-template-filters" role="tablist" aria-label="模板类型">${FILTERS.map(([value, label]) => `<button type="button" role="tab" data-template-filter="${value}" aria-selected="${activeFilter === value}" class="${activeFilter === value ? "is-active" : ""}">${label}</button>`).join("")}</div>
    </div>
    ${visible.length ? `<div class="owa-template-grid">${visible.map(cardMarkup).join("")}</div><p class="owa-template-count">共 ${visible.length} 项</p>` : '<div class="owa-template-empty"><strong>没有匹配的模板</strong><span>请调整搜索词或切换分类。</span></div>'}`;
  };

  const dataSourceOptionLabel = (item) => {
    const metadata = [
      item.type,
      item.database ? `数据库 ${item.database}` : "",
      item.schema_count ? `${item.schema_count} 个 Schema` : "",
      item.table_count ? `${item.table_count} 张表` : "",
      item.environment,
      item.status,
    ].filter(Boolean);
    return `${item.label}${metadata.length ? `（${metadata.join(" · ")}）` : ""}`;
  };

  const catalogOptionLabel = (level, item) => {
    const metadata = [
      level === "tables" ? item.type : "",
      item.columns ? `${item.columns} 个字段` : "",
      item.row_estimate ? `约 ${item.row_estimate} 行` : "",
      item.comment && item.comment !== item.name ? item.comment : "",
    ].filter(Boolean);
    return `${item.name}${metadata.length ? `（${metadata.join(" · ")}）` : ""}`;
  };

  const optionMarkup = (level, items, selected) => items.map((item) => {
    const isSelected = Array.isArray(selected) ? selected.includes(item.name) : selected === item.name;
    return `<option value="${escapeHtml(item.name)}" ${isSelected ? "selected" : ""}>${escapeHtml(catalogOptionLabel(level, item))}</option>`;
  }).join("");

  const dialogMarkup = () => {
    if (!activeTemplate) return "";
    const dataSourceBody = dataSourceState === "loading"
      ? '<p class="owa-template-source-state">正在从 Chat2DB 读取真实数据源与元数据…</p>'
      : dataSourceState === "error"
        ? `<p class="owa-template-source-state is-error">${escapeHtml(dataSourceMessage)}<button type="button" data-template-retry-datasources>重新读取</button></p>`
        : `<div class="owa-template-source-grid" data-template-database-fields>
            <label><span>Chat2DB 数据源</span><select name="datasource-id"><option value="">请选择已连接的数据源</option>${dataSources.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === selectedDatasourceId ? "selected" : ""}>${escapeHtml(dataSourceOptionLabel(item))}</option>`).join("")}</select><small>${escapeHtml(dataSourceMessage || "连接与凭据由 Chat2DB 管理；AHS 只保存引用和授权范围。")}</small></label>
            <label><span>数据库</span><select name="database-name" ${!selectedDatasourceId || catalogLoading === "databases" ? "disabled" : ""}><option value="">${catalogLoading === "databases" ? "正在读取数据库…" : "请选择数据库"}</option>${optionMarkup("databases", databases, selectedDatabase)}</select><small>仅展示所选数据源下的数据库。</small></label>
            <label><span>Schema（数据库模式）</span><select name="schema-name" ${!selectedDatabase || catalogLoading === "schemas" ? "disabled" : ""}><option value="">${catalogLoading === "schemas" ? "正在读取 Schema…" : "请选择 Schema"}</option>${optionMarkup("schemas", schemas, selectedSchema)}</select><small>仅展示所选数据库下的 Schema。</small></label>
            <label><span>需要使用的表</span><select name="table-scope" multiple size="5" ${!selectedSchema || catalogLoading === "tables" ? "disabled" : ""}>${optionMarkup("tables", tables, selectedTables)}</select><small>${escapeHtml(catalogLoading === "tables" ? "正在读取数据表…" : catalogMessage || "可多选；后续 S1 再回读字段、主外键、行数和关系证据。")}</small></label>
          </div>`;
    return `<div class="owa-template-dialog-backdrop" data-template-close></div>
      <section class="owa-template-dialog" role="dialog" aria-modal="true" aria-labelledby="owa-template-create-title">
        <header><div><span>基于模板新建工程</span><h2 id="owa-template-create-title">${escapeHtml(activeTemplate.name)}</h2><p>模板基线将按版本和 SHA-256 固化；客户资料与真实数据库仍是工程证据。</p></div><button type="button" data-template-close aria-label="关闭">关闭</button></header>
        <form data-template-create-form>
          <div class="owa-template-binding"><span>模板基线</span><strong>v${escapeHtml(activeTemplate.version)} · ${escapeHtml(shaLabel(activeTemplate.sha256))}</strong><small>${escapeHtml(activeTemplate.source_ref)}</small></div>
          <div class="owa-template-form-grid"><label><span>工程名称</span><input name="project-name" value="${escapeHtml(activeTemplate.short_name)}客户本体工程" required></label><label><span>业务领域</span><input name="domain" value="${escapeHtml(activeTemplate.domain)}" required></label></div>
          <fieldset><legend>客户将提供哪些输入</legend>
            <label><input type="radio" name="intake-mode" value="HYBRID" checked><span><strong>资料 + 数据库（推荐）</strong><small>资料走 S0，Chat2DB 数据走 S1，在 S2 汇合。</small></span></label>
            <label><input type="radio" name="intake-mode" value="DATABASE_ONLY"><span><strong>仅数据库</strong><small>绑定 Chat2DB 数据源，S0 留痕后进入 S1。</small></span></label>
            <label><input type="radio" name="intake-mode" value="DOCUMENT_ONLY"><span><strong>仅资料</strong><small>进入工作台后上传 PDF、Word、Excel 或图片。</small></span></label>
          </fieldset>
          <div class="owa-template-datasource">${dataSourceBody}</div>
          <label><span>建设目标</span><textarea name="rationale" rows="3">基于“${escapeHtml(activeTemplate.name)}”建立客户专属本体，并结合客户资料和业务数据完成验证与发布。</textarea></label>
          <p class="owa-template-create-message" role="status" ${createMessage ? "" : "hidden"}>${escapeHtml(createMessage)}</p>
          <footer><button type="button" data-template-close>取消</button><button type="submit" class="is-primary" ${createInFlight ? "disabled" : ""}>${createInFlight ? "正在创建并固化模板…" : "创建并进入本体工作台"}</button></footer>
        </form>
      </section>`;
  };

  const render = (view) => {
    // Keep the live form mounted: catalog responses must not reset edits,
    // radio selection, focus, or the dialog scroll position.
    const dialog = view.querySelector(".owa-template-dialog");
    if (activeTemplate && dialog) {
      const fragment = document.createElement("template");
      fragment.innerHTML = dialogMarkup();
      const current = dialog.querySelector(".owa-template-datasource");
      const next = fragment.content.querySelector(".owa-template-datasource");
      if (current && next) {
        const scrollTop = dialog.scrollTop;
        const formScrollTop = dialog.querySelector("form")?.scrollTop ?? 0;
        const focusedName = current.contains(document.activeElement) ? document.activeElement.name : "";
        current.replaceWith(next);
        if (focusedName) next.querySelector(`[name="${focusedName}"]`)?.focus({ preventScroll: true });
        dialog.scrollTop = scrollTop;
        dialog.querySelector("form").scrollTop = formScrollTop;
      }
    } else {
      view.innerHTML = `${mainMarkup()}${dialogMarkup()}`;
    }
    updateDatabaseFields(view);
  };

  const updateDatabaseFields = (view) => {
    const mode = view.querySelector('[name="intake-mode"]:checked')?.value ?? "HYBRID";
    view.querySelectorAll("[data-template-database-fields]").forEach((field) => {
      field.hidden = mode === "DOCUMENT_ONLY";
    });
    const region = view.querySelector(".owa-template-datasource");
    if (region) region.hidden = mode === "DOCUMENT_ONLY";
  };

  const setCreateStatus = (view, message, busy = false) => {
    createMessage = message;
    createInFlight = busy;
    const notice = view.querySelector(".owa-template-create-message");
    if (notice) {
      notice.textContent = message;
      notice.hidden = !message;
    }
    const submit = view.querySelector('[data-template-create-form] button[type="submit"]');
    if (submit) {
      submit.disabled = busy;
      submit.textContent = busy ? "正在创建并固化模板…" : "创建并进入本体工作台";
    }
  };

  const loadDataSources = async (view) => {
    const request = ++sourceRequest;
    dataSourceState = "loading";
    dataSourceMessage = "";
    render(view);
    try {
      const payload = await workflow.datasources();
      if (request !== sourceRequest || !activeTemplate) return;
      dataSources = payload.datasources ?? [];
      dataSourceState = "ready";
      dataSourceMessage = payload.detail ?? "";
    } catch (error) {
      if (request !== sourceRequest || !activeTemplate) return;
      dataSources = [];
      dataSourceState = "error";
      dataSourceMessage = error instanceof Error ? error.message : "Chat2DB 数据源读取失败";
    }
    render(view);
  };

  const loadCatalog = async (view, level) => {
    const request = ++catalogRequest;
    catalogLoading = level;
    catalogMessage = "";
    if (level === "databases") databases = [];
    if (level === "schemas") schemas = [];
    if (level === "tables") tables = [];
    render(view);
    try {
      const payload = await workflow.chat2dbCatalog(level, {
        datasource_id: selectedDatasourceId,
        ...(level === "schemas" || level === "tables" ? { database_name: selectedDatabase } : {}),
        ...(level === "tables" ? { schema_name: selectedSchema } : {}),
      });
      if (request !== catalogRequest || !activeTemplate) return;
      if (payload.freshness && payload.freshness !== "LIVE") {
        throw new Error(payload.warning || "目录提供方未证明刷新结果，不能使用缓存目录创建工程。");
      }
      const items = (payload.items ?? []).filter((item) => item.name && !/^no (?:schemas?|databases?|tables?) (?:were )?found[.!]?$/iu.test(item.name.trim()));
      if (level === "databases") databases = items;
      if (level === "schemas") schemas = items;
      if (level === "tables") tables = items;
      catalogMessage = items.length ? (payload.detail ?? "") : `当前范围未返回可用的${level === "schemas" ? " Schema，请检查所选数据库" : level === "tables" ? "数据表" : "数据库"}。`;
    } catch (error) {
      if (request !== catalogRequest || !activeTemplate) return;
      catalogMessage = error instanceof Error ? error.message : "Chat2DB 元数据读取失败";
    }
    catalogLoading = "";
    render(view);
  };

  const openCreate = (view, templateId) => {
    catalogRequest += 1;
    sourceRequest += 1;
    activeTemplate = null;
    render(view);
    activeTemplate = templates.find((item) => item.id === templateId && item.create_enabled) ?? null;
    createMessage = "";
    createInFlight = false;
    selectedDatasourceId = "";
    selectedDatabase = "";
    selectedSchema = "";
    selectedTables = [];
    databases = [];
    schemas = [];
    tables = [];
    catalogLoading = "";
    catalogMessage = "";
    render(view);
    if (activeTemplate) loadDataSources(view);
  };

  const submitCreate = async (view, form) => {
    const intakeMode = form.querySelector('[name="intake-mode"]:checked')?.value ?? "HYBRID";
    const sourceId = selectedDatasourceId || form.querySelector('[name="datasource-id"]')?.value || "";
    const source = dataSources.find((item) => item.id === sourceId);
    const tableScope = selectedTables.map((table) => `${selectedSchema}.${table}`);
    if (intakeMode !== "DOCUMENT_ONLY" && !source) {
      setCreateStatus(view, "请选择 Chat2DB 已连接的数据源；数据库连接仍在 Chat2DB 中维护。");
      return;
    }
    if (intakeMode !== "DOCUMENT_ONLY" && (!selectedDatabase || !selectedSchema || !tableScope.length)) {
      setCreateStatus(view, "请依次选择数据库、Schema 和至少一张需要使用的表。括号内会显示其来源元数据。");
      return;
    }
    const projectName = form.querySelector('[name="project-name"]').value.trim();
    const domain = form.querySelector('[name="domain"]').value.trim();
    const rationale = form.querySelector('[name="rationale"]').value.trim();
    if (!projectName || !domain || !rationale) {
      setCreateStatus(view, "请完整填写工程名称、业务领域和建设目标。");
      return;
    }
    setCreateStatus(view, "正在创建工程并校验模板基线…", true);
    try {
      const requestId = `template:${activeTemplate.id}:${crypto.randomUUID?.() ?? Date.now()}`;
      const payload = await workflow.create({
        project_name: projectName,
        domain,
        datasource_label: source ? `${source.id}|${dataSourceOptionLabel(source)}|数据库 ${selectedDatabase}` : null,
        table_scope: tableScope,
        intake_mode: intakeMode,
        intake_rationale: rationale,
        cq_mode: "USER_PLUS_AI",
        initial_competency_questions: activeTemplate.default_questions ?? [],
        template_id: activeTemplate.id,
        request_id: requestId,
      });
      const projectId = payload.workflow?.project_id ?? payload.dashboard?.project?.project_id;
      if (!projectId) throw new Error("工程已创建，但未返回工程编号");
      const template = activeTemplate;
      activeTemplate = null;
      createInFlight = false;
      createMessage = "";
      window.__ORION_ONTOLOGY_CENTER__?.refresh?.();
      window.__ORION_CHAT_MODES__?.openWorkbench?.(projectId, {
        projectName,
        stage: "S0",
        detail: `已固化模板 ${template.name} v${template.version}（${shaLabel(template.sha256)}）。请先确认客户资料与 Chat2DB 数据范围，再按 S0–S7 继续；模板基线不能被直接改写。`,
      });
    } catch (error) {
      setCreateStatus(view, error instanceof Error ? error.message : "模板工程创建失败");
    }
  };

  const bind = (view) => {
    view.addEventListener("input", (event) => {
      if (event.target.matches(".owa-template-search input")) {
        search = event.target.value.trim();
        const cursor = event.target.selectionStart ?? search.length;
        render(view);
        const field = view.querySelector(".owa-template-search input");
        field?.focus({ preventScroll: true });
        field?.setSelectionRange(cursor, cursor);
      }
    });
    view.addEventListener("change", (event) => {
      if (event.target.matches('[name="intake-mode"]')) updateDatabaseFields(view);
      if (event.target.matches('[name="datasource-id"]')) {
        catalogRequest += 1;
        catalogLoading = "";
        selectedDatasourceId = event.target.value;
        selectedDatabase = "";
        selectedSchema = "";
        selectedTables = [];
        databases = [];
        schemas = [];
        tables = [];
        if (selectedDatasourceId) loadCatalog(view, "databases");
        else render(view);
      }
      if (event.target.matches('[name="database-name"]')) {
        catalogRequest += 1;
        catalogLoading = "";
        selectedDatabase = event.target.value;
        selectedSchema = "";
        selectedTables = [];
        schemas = [];
        tables = [];
        if (selectedDatabase) loadCatalog(view, "schemas");
        else render(view);
      }
      if (event.target.matches('[name="schema-name"]')) {
        catalogRequest += 1;
        catalogLoading = "";
        selectedSchema = event.target.value;
        selectedTables = [];
        tables = [];
        if (selectedSchema) loadCatalog(view, "tables");
        else render(view);
      }
      if (event.target.matches('[name="table-scope"]')) {
        selectedTables = [...event.target.selectedOptions].map((option) => option.value);
      }
    });
    view.addEventListener("submit", (event) => {
      const form = event.target.closest("[data-template-create-form]");
      if (!form) return;
      event.preventDefault();
      submitCreate(view, form);
    });
    view.addEventListener("click", (event) => {
      const filter = event.target.closest("[data-template-filter]")?.dataset.templateFilter;
      if (filter) { activeFilter = filter; render(view); return; }
      if (event.target.closest("[data-template-close]")) { catalogRequest += 1; sourceRequest += 1; activeTemplate = null; createMessage = ""; render(view); return; }
      if (event.target.closest("[data-template-retry-datasources]")) { loadDataSources(view); return; }
      const createId = event.target.closest("[data-template-create]")?.dataset.templateCreate;
      if (createId) { openCreate(view, createId); return; }
      const graphId = event.target.closest("[data-template-graph]")?.dataset.templateGraph;
      if (graphId) {
        const template = templates.find((item) => item.id === graphId);
        if (template) window.__ORION_ONTOLOGY_GRAPH_VIEWER__?.open(templateAsset(template));
        return;
      }
      const protegeButton = event.target.closest("[data-template-protege]");
      const protegeId = protegeButton?.dataset.templateProtege;
      if (protegeId) {
        const label = protegeButton.textContent;
        protegeButton.disabled = true;
        protegeButton.textContent = "正在打开…";
        api.openTemplateProtege(protegeId)
          .then((payload) => { dataSourceMessage = payload.detail ?? "已交给 Protégé 打开。"; })
          .catch((error) => { dataSourceMessage = error instanceof Error ? error.message : "Protégé 打开失败"; })
          .finally(() => { protegeButton.disabled = false; protegeButton.textContent = label; });
      }
    });
  };

  const refresh = async (root) => {
    const view = ensureView(root);
    if (activeTemplate) return;
    view.innerHTML = '<div class="owa-template-loading"><strong>正在读取行业模板</strong><span>核对版本、来源和 SHA-256…</span></div>';
    try {
      const payload = await api.templates();
      if (activeTemplate) return;
      templates = payload.templates ?? [];
      render(view);
    } catch (error) {
      if (activeTemplate) return;
      view.innerHTML = `<div class="owa-template-empty"><strong>行业模板暂时无法读取</strong><span>${escapeHtml(error instanceof Error ? error.message : "未知错误")}</span></div>`;
    }
  };

  const show = (root) => {
    const view = ensureView(root);
    view.hidden = false;
    root.classList.add("owa-ontology-templates-active");
    refresh(root);
  };

  const hide = (root) => {
    root?.classList.remove("owa-ontology-templates-active");
    const view = root?.querySelector(":scope > .owa-ontology-templates-view");
    if (view) view.hidden = true;
    activeTemplate = null;
  };

  window.__ORION_ONTOLOGY_TEMPLATES__ = { show, hide, refresh };
})();
