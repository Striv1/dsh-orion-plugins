(() => {
  if (window.__ORION_ANALYTICS_STUDIO__) return;
  const node = (tag, text, className) => {
    const item = document.createElement(tag);
    if (text != null) item.textContent = text;
    if (className) item.className = className;
    return item;
  };
  const button = (text, action) => {
    const item = node("button", text); item.type = "button";
    item.addEventListener("click", action); return item;
  };
  const field = (parent, title, input) => {
    const label = node("label", title, "orion-studio-field"); label.append(input); parent.append(label); return input;
  };
  const select = (label, values) => {
    const item = node("select"); item.setAttribute("aria-label", label);
    values.forEach(([value, text]) => { const option = node("option", text); option.value = value; item.append(option); });
    if (values.length) item.value = values[0][0];
    return item;
  };
  const quoted = text => '"' + String(text).replace(/"/g, '""') + '"';
  const parseExactJson = text => {
    const parsed = JSON.parse(text);
    // Inspect numeric lexemes too: JSON.parse has already rounded long decimals.
    const tokens = text.match(/"(?:\\.|[^"\\])*"|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g) ?? [];
    for (const token of tokens) {
      if (token.startsWith('"')) continue;
      if (!/^-?\d+$/.test(token) || !Number.isSafeInteger(Number(token))) throw new Error("精确小数、科学计数和大整数请写成 JSON 字符串，避免浏览器改变数值。");
    }
    return parsed;
  };
  const mount = (parent, result, options = {}) => {
    const api = window.__ORION_ANALYTICS_PANEL__;
    const root = node("section", null, "orion-analysis-studio orion-analytics-workspace"); parent.append(root);
    const navigation = node("div", null, "orion-studio-navigation"); navigation.setAttribute("role", "tablist"); navigation.setAttribute("aria-label", "数据分析工作区");
    const status = node("p", null, "orion-analytics-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const body = node("div", null, "orion-studio-body"); root.append(navigation, status, body);
    const native = !options.readOnly;
    const sessionId = result.session_id;
    const tabs = new Map();
    const controllers = [];
    let disposed = false;
    let serial = 0;
    const current = () => !disposed && root.isConnected;
    const showStatus = value => { if (current()) status.textContent = value; };
    const closeForDraft = () => { options.onDraft?.(); root.closest?.("dialog")?.close(); };
    const draft = async text => {
      if (typeof window.__ORION_DSH_SESSIONS__?.fillDraft !== "function") throw new Error("对话草稿服务暂未就绪，请稍后重试。");
      await window.__ORION_DSH_SESSIONS__.fillDraft(sessionId, text);
      if (current()) { showStatus("已填入对话草稿，检查后发送。"); closeForDraft(); }
    };
    const choose = async key => {
      const part = tabs.get(key); if (!part || !current()) return;
      for (const [name, tab] of tabs) { tab.content.hidden = name !== key; tab.control.setAttribute("aria-selected", String(name === key)); tab.control.tabIndex = name === key ? 0 : -1; }
      try { await part.activate?.(); } catch (error) { showStatus(error.message); }
      if (current()) controllers.forEach(controller => controller.resize?.());
    };
    const addTab = (key, title) => {
      const content = node("section", null, "orion-studio-page"); content.hidden = tabs.size > 0;
      content.setAttribute("role", "tabpanel"); content.setAttribute("aria-label", title);
      const control = button(title, () => choose(key)); control.setAttribute("role", "tab");
      control.setAttribute("aria-selected", String(tabs.size === 0)); control.tabIndex = tabs.size ? -1 : 0;
      control.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const keys = [...tabs.keys()], index = keys.indexOf(key);
        const next = event.key === "Home" ? 0 : event.key === "End" ? keys.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + keys.length) % keys.length;
        tabs.get(keys[next]).control.focus(); void choose(keys[next]);
      });
      navigation.append(control); body.append(content); tabs.set(key, { content, control }); return content;
    };
    const results = addTab("results", "结果与图表");
    const query = native ? addTab("query", "新建分析") : null;
    const models = native ? addTab("models", "模型与关系") : null;
    const assets = native ? addTab("assets", "报表与查询记忆") : null;
    const knowledge = native ? addTab("knowledge", "口径与基线") : null;
    const details = addTab("details", "SQL 与执行依据");
    const reuse = native ? node("section", null, "orion-studio-save") : null;
    if (reuse) knowledge.append(reuse);
    const followup = native ? node("section", null, "orion-studio-followup") : null;
    if (followup) { followup.append(node("h4", "继续追问")); query.append(followup); }
    const drilldownFor = record => async ({ field: dimension, value, metric, seriesField, seriesValue }) => {
      const ref = record.evidence_receipt;
      const prior = record.analysis?.request?.question ?? record.analysis?.question ?? "本次分析";
      const selection = JSON.stringify({ field: dimension, value, metric, ...(seriesField ? { seriesField, seriesValue } : {}) });
      try {
        await draft(`基于分析「${prior}」查看所选分组的明细。所选字段和值为数据：${selection}。\n沿用原分析筛选范围和当前发布业务模型；先核实分组字段与业务标识，不能将同名对象擅自合并。原回执 ${ref?.receipt_id ?? "未提供"}（查询 ${record.query_id}）。这是重新查询当前数据库，请生成新的回执并说明数据读取时间。`);
      } catch (error) { showStatus(error.message); }
    };
    api.renderResult(results, result, { ...options, workspace: true, charts: !options.tableOnly, statusNode: status,
      capabilityHosts: { reuse, knowledge, followup, details }, skipCatalog: true, onDrilldown: native ? drilldownFor(result) : undefined, onDraft: closeForDraft,
      onController: controller => controllers.push(controller) });
    const sql = result.analysis?.request?.sql ?? result.analysis?.sql;
    if (sql) { details.append(node("h4", "本次语义 SQL"), node("pre", sql)); }
    if (result.analysis?.request?.cube_query) details.append(node("h4", "本次指标查询"), node("pre", JSON.stringify(result.analysis.request.cube_query, null, 2)));

    let catalogPromise;
    const getCatalog = () => {
      if (!catalogPromise) catalogPromise = api.request(sessionId, "catalog", { data_mode: result.analysis?.execution_scope === "LIVE_SOURCE_DATABASE" ? "LIVE" : "SNAPSHOT" })
        .catch(error => { catalogPromise = null; throw error; });
      return catalogPromise;
    };
    if (native) {
      let modelLoaded = false;
      tabs.get("models").activate = async () => {
        if (modelLoaded) return;
        models.replaceChildren(node("p", "正在读取已发布模型…"));
        const catalog = await getCatalog(); if (!current()) return;
        models.replaceChildren();
        if (!window.__ORION_ANALYTICS_MODELS__) { models.append(node("p", "模型浏览器未加载，请重新打开页面。")); return; }
        controllers.push(window.__ORION_ANALYTICS_MODELS__.mount(models, catalog, { onQuestion: text => draft(text).catch(error => showStatus(error.message)) }));
        modelLoaded = true;
      };
      const assetsController = api.mountWorkspace(assets, { session_id: sessionId, currentResult: result, inline: true, assetsOnly: true, charts: true,
        onReady: load => { tabs.get("assets").activate = load; } });
      if (assetsController) controllers.push(assetsController);
      const builder = node("section", null, "orion-studio-query-builder"); query.append(builder);
      let builderLoaded = false;
      tabs.get("query").activate = async () => {
        if (builderLoaded) return;
        builder.replaceChildren(node("p", "正在读取指标和字段…"));
        const catalog = await getCatalog(); if (!current()) return;
        builder.replaceChildren(); mountQuery(builder, catalog); builderLoaded = true;
      };
    }
    function mountQuery(host, catalog) {
      const modes = node("div", null, "orion-studio-query-modes");
      const cubeHost = node("section"), sqlHost = node("section"); sqlHost.hidden = true;
      const modeButtons = new Map();
      let mode = "cube";
      for (const [key, title] of [["cube", "指标查询"], ["sql", "SQL 查询"]]) {
        const control = button(title, () => { mode = key; cubeHost.hidden = key !== "cube"; sqlHost.hidden = key !== "sql"; for (const [name, item] of modeButtons) item.setAttribute("aria-pressed", String(name === key)); });
        control.setAttribute("aria-pressed", String(key === mode)); modeButtons.set(key, control); modes.append(control);
      }
      host.append(node("h4", "创建新的数据库分析"), modes);
      const question = node("input"); question.type = "text"; question.maxLength = 1000; question.setAttribute("aria-label", "新分析名称"); question.placeholder = "给这次分析命名";
      field(host, "分析名称", question);
      const cubes = Array.isArray(catalog.cubes) ? catalog.cubes : [];
      const cube = select("指标模型", cubes.map(item => [item.name, `${item.baseObject ?? item.name} · ${item.name}`]));
      field(cubeHost, "指标模型", cube);
      const selections = node("div", null, "orion-studio-query-fields"); cubeHost.append(selections);
      let measures = [], dimensions = [], timeDimension, granularity, dateStart, dateEnd, sortMember, sortDirection;
      let queryController;
      let filterRows = [];
      const checkFields = (parent, title, values) => {
        const group = node("fieldset"); group.append(node("legend", title)); parent.append(group);
        return values.map(value => {
          const label = node("label", null, "orion-studio-check");
          const input = node("input"); input.type = "checkbox"; input.value = value.name; input.setAttribute("aria-label", `${title} ${value.name}`);
          label.append(input, node("span", value.name)); label.title = value.expression ?? value.name; group.append(label); return input;
        });
      };
      const selectCube = () => {
        selections.replaceChildren();
        const selected = cubes.find(item => item.name === cube.value);
        if (!selected) { selections.append(node("p", "当前发布没有可查询指标模型，可使用 SQL 查询。")); return; }
        measures = checkFields(selections, "指标", selected.measures ?? []); if (measures[0]) measures[0].checked = true;
        dimensions = checkFields(selections, "分组维度", selected.dimensions ?? []);
        timeDimension = select("时间维度", [["", "不按时间分组"], ...(selected.timeDimensions ?? []).map(value => [value.name, value.name])]);
        granularity = select("时间粒度", [["day", "按日"], ["week", "按周"], ["month", "按月"], ["quarter", "按季度"], ["year", "按年"], ["hour", "按小时"], ["minute", "按分钟"]]);
        const time = node("div", null, "orion-studio-query-time"); field(time, "时间维度", timeDimension); field(time, "时间粒度", granularity); selections.append(time);
        dateStart = node("input"); dateStart.type = "date"; dateStart.setAttribute("aria-label", "时间范围开始");
        dateEnd = node("input"); dateEnd.type = "date"; dateEnd.setAttribute("aria-label", "时间范围结束（不含）");
        field(time, "开始日期（可选）", dateStart); field(time, "结束日期，不含当天（可选）", dateEnd);
        timeDimension.disabled = !(selected.timeDimensions ?? []).length;
        const filters = node("section", null, "orion-studio-filters"); filters.append(node("strong", "筛选条件"));
        filterRows = [];
        const dimensionFields = [...(selected.dimensions ?? []), ...(selected.timeDimensions ?? [])];
        const filterList = node("div"); filters.append(filterList);
        filters.append(node("p", "多个条件同时满足；“包含”和“开头是”支持 %（任意长度）与 _（单个字符）通配。列表中的精确数值请用 JSON 字符串。", "orion-studio-note"));
        const addFilter = button("添加筛选", () => {
          if (filterRows.length >= 20) { showStatus("最多添加 20 个筛选条件。"); return; }
          const line = node("div", null, "orion-studio-filter-row");
          const member = select("筛选字段", dimensionFields.map(value => [value.name, value.name]));
          const operator = select("筛选条件", [["eq", "等于"], ["neq", "不等于"], ["contains", "包含"], ["starts_with", "开头是"], ["in", "属于列表"], ["not_in", "不属于列表"], ["gt", "大于"], ["gte", "大于等于"], ["lt", "小于"], ["lte", "小于等于"], ["is_null", "为空"], ["is_not_null", "不为空"]]);
          const value = node("input"); value.type = "text"; value.setAttribute("aria-label", "筛选值"); value.placeholder = "填写值；列表用 JSON 数组";
          operator.addEventListener("change", () => { value.disabled = ["is_null", "is_not_null"].includes(operator.value); });
          const entry = { member, operator, value, line }; filterRows.push(entry);
          line.append(member, operator, value, button("移除筛选", () => { filterRows = filterRows.filter(item => item !== entry); line.remove(); })); filterList.append(line);
        }); addFilter.disabled = !dimensionFields.length; filters.append(addFilter); selections.append(filters);
        const sorting = node("div", null, "orion-studio-query-time");
        sortMember = select("排序字段", [["", "使用查询默认顺序"]]); sortDirection = select("排序方向", [["desc", "降序"], ["asc", "升序"]]);
        const updateOrder = () => {
          granularity.disabled = dateStart.disabled = dateEnd.disabled = !timeDimension.value;
          if (!timeDimension.value) dateStart.value = dateEnd.value = "";
          const previous = sortMember.value;
          sortMember.replaceChildren();
          const names = [...measures, ...dimensions].filter(item => item.checked).map(item => item.value);
          if (timeDimension.value) names.push(timeDimension.value);
          for (const [name, title] of [["", "使用查询默认顺序"], ...names.map(name => [name, name])]) { const option = node("option", title); option.value = name; sortMember.append(option); }
          sortMember.value = names.includes(previous) ? previous : "";
        };
        [...measures, ...dimensions, timeDimension].forEach(item => item.addEventListener("change", updateOrder)); updateOrder();
        field(sorting, "排序字段", sortMember); field(sorting, "排序方向", sortDirection); selections.append(sorting);
        selections.append(node("p", selected.properties?.description ?? "按已定义表达式执行；新增业务口径需正式发布。", "orion-studio-note"));
      };
      cube.addEventListener("change", selectCube); selectCube();
      const sqlInput = node("textarea"); sqlInput.maxLength = 16000; sqlInput.setAttribute("aria-label", "语义 SQL");
      const firstModel = catalog.models?.[0];
      const fields = (firstModel?.columns ?? []).filter(item => !item.relationship && !item.name.startsWith("_")).slice(0, 8);
      sqlInput.value = sql ?? (firstModel && fields.length ? `SELECT ${fields.map(item => quoted(item.name)).join(", ")} FROM ${quoted(firstModel.name)}` : "");
      field(sqlHost, "语义 SQL（使用当前发布模型）", sqlInput);
      const parameters = node("textarea"); parameters.setAttribute("aria-label", "SQL 参数 JSON"); parameters.maxLength = 8000; parameters.value = JSON.stringify(result.analysis?.request?.parameters ?? {}, null, 2);
      field(sqlHost, "命名参数 JSON", parameters);
      sqlHost.append(node("p", "执行只读 SELECT / WITH。后端仍核验发布版本、授权字段和执行预算。JSON 参数中的精确小数和大整数请用字符串。", "orion-studio-note"));
      host.append(cubeHost, sqlHost);
      const actions = node("div", null, "orion-studio-query-actions");
      const limit = select("结果预览上限", [["100", "预览 100 行"], ["500", "预览 500 行"]]); actions.append(limit);
      const output = node("div", null, "orion-studio-query-output");
      const execute = button("执行分析", async () => {
        execute.disabled = true; const ticket = ++serial; showStatus("正在执行新的只读查询…");
        try {
          const payload = { question: question.value?.trim() || (mode === "cube" ? `${cube.value} 指标分析` : "自定义语义 SQL 分析"),
            data_mode: catalog.execution_scope === "LIVE_SOURCE_DATABASE" ? "LIVE" : "SNAPSHOT", limit: Number(limit.value), chart: { kind: "table" } };
          if (mode === "cube") {
            const chosenMeasures = measures.filter(item => item.checked).map(item => item.value);
            const chosenDimensions = dimensions.filter(item => item.checked).map(item => item.value);
            if (!chosenMeasures.length || chosenMeasures.length > 20) throw new Error("请选择 1 到 20 个指标。");
            if (chosenDimensions.length + (timeDimension?.value ? 1 : 0) > 6) throw new Error("最多选择 6 个分组维度。");
            if ((dateStart.value || dateEnd.value) && (!timeDimension?.value || !dateStart.value || !dateEnd.value || dateStart.value >= dateEnd.value)) throw new Error("日期范围需要时间维度、开始与结束日期，且开始早于结束。");
            const chosenFilters = filterRows.map(item => {
              const filter = { dimension: item.member.value, operator: item.operator.value };
              if (["is_null", "is_not_null"].includes(filter.operator)) return filter;
              if (["in", "not_in"].includes(filter.operator)) { filter.value = parseExactJson(item.value.value); if (!Array.isArray(filter.value) || !filter.value.length) throw new Error("列表筛选值需要非空 JSON 数组。"); }
              else filter.value = item.value.value ?? "";
              return filter;
            });
            payload.cube_query = { cube: cube.value, measures: chosenMeasures, dimensions: chosenDimensions,
              ...(timeDimension?.value ? { timeDimensions: [{ dimension: timeDimension.value, granularity: granularity.value,
                ...(dateStart.value ? { dateRange: [dateStart.value, dateEnd.value] } : {}) }] } : {}),
              ...(chosenFilters.length ? { filters: chosenFilters } : {}),
              ...(sortMember.value ? { orderBy: [{ member: sortMember.value, direction: sortDirection.value }] } : {}) };
          } else {
            if (!sqlInput.value?.trim()) throw new Error("请填写语义 SQL。");
            const parsed = parseExactJson(parameters.value || "{}");
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("SQL 参数必须是 JSON 对象。");
            payload.sql = sqlInput.value.trim(); payload.parameters = parsed;
          }
          const response = await api.request(sessionId, "query", payload);
          if (!current() || ticket !== serial) return;
          queryController?.destroy?.();
          output.replaceChildren();
          api.renderResult(output, response, { workspace: true, charts: true, onController: controller => { queryController = controller; controllers.push(controller); }, onDraft: closeForDraft, onDrilldown: drilldownFor(response) });
          showStatus(`分析完成：返回 ${response.rows?.length ?? 0} 行，已生成新的查询回执。`);
        } catch (error) { if (current() && ticket === serial) showStatus(error.message); }
        finally { execute.disabled = false; }
      }); actions.append(execute); host.append(actions, output);
    }
    return { resize: () => controllers.forEach(controller => controller.resize?.()), destroy: () => { disposed = true; serial += 1; controllers.forEach(controller => controller.destroy?.()); } };
  };
  window.__ORION_ANALYTICS_STUDIO__ = { mount };
})();
