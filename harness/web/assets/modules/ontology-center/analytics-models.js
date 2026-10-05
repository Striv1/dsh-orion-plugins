(() => {
  if (window.__ORION_ANALYTICS_MODELS__) return;
  const array = value => Array.isArray(value) ? value.filter(item => item && typeof item === "object" && !Array.isArray(item)) : [];
  const valueText = value => value == null || value === "" ? "未提供" : typeof value === "object" ? JSON.stringify(value) : String(value);
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, action, className) => {
    const node = element("button", text, className);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  };
  const svgElement = (tag, attributes, text) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
    if (text != null) node.textContent = text;
    return node;
  };
  const label = item => item.properties?.label_zh ?? item.label_zh ?? item.name ?? "未命名";
  const description = item => item.properties?.description ?? item.description;
  const contains = (item, query) => !query || [item.name, label(item), description(item), item.type, item.expression,
    item.relationship, item.properties?.sourceColumn].some(value => typeof value === "string" && value.toLocaleLowerCase().includes(query));
  const cardinalities = joinType => ({ ONE_TO_ONE: ["1", "1"], ONE_TO_MANY: ["1", "N"], MANY_TO_ONE: ["N", "1"], MANY_TO_MANY: ["N", "N"] })[joinType] ?? ["?", "?"];
  const appendValue = (parent, name, value, code = false) => {
    const row = element("div", null, "orion-model-value");
    row.append(element("span", name), element(code ? "code" : "span", valueText(value)));
    parent.append(row);
  };
  const appendCode = (parent, title, code) => {
    parent.append(element("h5", title), element("pre", valueText(code)));
  };
  const appendDescription = (parent, item) => { if (description(item)) parent.append(element("p", valueText(description(item)), "orion-model-description")); };

  function mount(parent, input, options = {}) {
    const catalog = input && typeof input === "object" ? input : {};
    const models = array(catalog.models);
    const relationships = array(catalog.relationships);
    const cubes = array(catalog.cubes);
    const views = array(catalog.views);
    const sources = array(catalog.sources);
    const projections = array(catalog.model_projections);
    const omitted = array(catalog.omitted);
    const byName = new Map();
    for (const model of models) {
      if (typeof model.name !== "string" || !model.name) continue;
      if (!byName.has(model.name)) byName.set(model.name, []);
      byName.get(model.name).push(model);
    }
    const uniqueModel = name => byName.get(name)?.length === 1 ? byName.get(name)[0] : null;
    const projectionFor = model => {
      const matches = projections.filter(projection => projection.model === model.name);
      return matches.length === 1 ? matches[0] : null;
    };
    const sourceFor = projection => {
      const matches = projection && typeof projection.source_name === "string"
        ? sources.filter(source => source.name === projection.source_name) : [];
      return matches.length === 1 ? matches[0] : null;
    };
    const endpoints = relation => Array.isArray(relation.models) && relation.models.length === 2
      ? relation.models.map(uniqueModel) : [null, null];
    let destroyed = false;
    let activeTab = "models";
    let selected = models[0] ?? null;
    let selectedRelation = null;
    let query = "";
    let zoom = 1;
    let shownModels = models;
    let shownRelations = relationships;
    const nodeElements = new Map();
    const edgeElements = new Map();
    const listElements = new Map();
    const root = element("section", null, "orion-model-browser");
    root.setAttribute("aria-label", "分析模型浏览器");
    const toolbar = element("div", null, "orion-model-toolbar");
    const search = element("input");
    search.type = "search"; search.placeholder = "搜索模型、字段、指标或视图"; search.maxLength = 200;
    search.setAttribute("aria-label", "搜索模型与字段");
    const status = element("span", null, "orion-model-search-status");
    status.setAttribute("role", "status");
    toolbar.append(search, status);
    const nav = element("div", null, "orion-model-tabs");
    nav.setAttribute("role", "group"); nav.setAttribute("aria-label", "模型目录分类");
    const body = element("div", null, "orion-model-body");
    const tabButtons = new Map();
    const ask = (parentNode, text, draft, context) => {
      if (typeof options.onQuestion !== "function") return;
      const node = button(text, () => { if (!destroyed) options.onQuestion(draft, context); }, "orion-model-ask");
      node.setAttribute("aria-label", `${text}：${context.field ?? context.model ?? context.cube ?? context.view ?? ""}`);
      parentNode.append(node);
    };
    const heading = (parentNode, item, fallback) => {
      parentNode.append(element("h4", label(item) === "未命名" ? fallback : label(item)));
      if (item.name && item.name !== label(item)) parentNode.append(element("code", item.name));
      appendDescription(parentNode, item);
    };
    const updateSelection = () => {
      for (const [model, node] of nodeElements) {
        node.dataset.selected = String(model === selected);
        node.setAttribute("aria-pressed", String(model === selected));
      }
      for (const [model, node] of listElements) node.setAttribute("aria-pressed", String(model === selected));
      for (const [relation, node] of edgeElements) node.dataset.selected = String(relation === selectedRelation
        || (!selectedRelation && endpoints(relation).includes(selected)));
    };
    let detail;
    const showRelation = relation => {
      if (destroyed) return;
      selectedRelation = relation;
      updateSelection();
      detail.replaceChildren();
      heading(detail, relation, "未命名关系");
      const names = Array.isArray(relation.models) ? relation.models : [];
      appendValue(detail, "端点顺序", names.length ? names.join(" → ") : null);
      appendValue(detail, "关系基数", relation.joinType);
      const [left, right] = cardinalities(relation.joinType);
      appendValue(detail, "基数方向", left === "?" ? null : `${valueText(names[0])}（${left}）→ ${valueText(names[1])}（${right}）`);
      appendCode(detail, "连接条件", relation.condition);
      if (endpoints(relation).some(model => !model)) detail.append(element("p", "关系端点缺失或重名，未在图中生成连线。", "orion-model-warning"));
      const links = element("div", null, "orion-model-actions");
      for (const model of endpoints(relation).filter(Boolean)) links.append(button(`查看 ${label(model)}`, () => showModel(model)));
      detail.append(links);
      detail.append(element("h5", "使用此关系的字段"));
      let count = 0;
      for (const model of models) for (const field of array(model.columns)) {
        if (!relation.name || field.relationship !== relation.name) continue;
        count += 1;
        detail.append(button(`${model.name}.${valueText(field.name)}`, () => showModel(model), "orion-model-field-link"));
      }
      if (!count) detail.append(element("p", "目录未提供引用此关系的字段。"));
    };
    const renderFields = (parentNode, fields, model) => {
      if (!fields.length) { parentNode.append(element("p", "目录未提供字段。", "orion-model-empty")); return; }
      const wrapper = element("div", null, "orion-model-table-scroll");
      const table = element("table", null, "orion-model-fields");
      const head = element("thead"), tr = element("tr");
      for (const text of ["字段", "类型", "来源 / 关系 / 表达式", "操作"]) { const th = element("th", text); th.setAttribute("scope", "col"); tr.append(th); }
      head.append(tr); table.append(head);
      const rows = element("tbody");
      const mappedColumns = array(projectionFor(model)?.columns);
      for (const field of fields) {
        const row = element("tr"); row.dataset.match = String(Boolean(query) && contains(field, query));
        const name = element("td"); name.append(element("strong", valueText(field.name)));
        if (label(field) !== field.name && label(field) !== "未命名") name.append(element("span", label(field)));
        if (description(field)) name.append(element("small", valueText(description(field))));
        if (model.primaryKey === field.name || Array.isArray(model.primaryKey) && model.primaryKey.includes(field.name)) name.append(element("span", "主键", "orion-model-badge"));
        const definition = element("td");
        if (field.relationship) {
          const relations = relationships.filter(relation => relation.name === field.relationship);
          definition.append(element("span", "关系字段 "));
          if (relations.length === 1) definition.append(button(field.relationship, () => showRelation(relations[0]), "orion-model-field-link"));
          else definition.append(element("span", `${field.relationship}（关系定义缺失或重名）`));
        }
        const projected = mappedColumns.filter(column => column.name === field.name);
        const sourceColumn = projected.length === 1 ? projected[0].physical : field.properties?.sourceColumn;
        if (sourceColumn != null) appendValue(definition, "来源列", sourceColumn, true);
        if (field.expression != null || field.isCalculated) appendValue(definition, "表达式", field.expression, true);
        if (!field.relationship && sourceColumn == null && field.expression == null && !field.isCalculated) definition.append(element("span", "未提供映射或表达式"));
        const actions = element("td");
        ask(actions, "围绕此字段提问", `请围绕「${label(model)}」的「${label(field)}」字段分析：`, { kind: "field", model: model.name, field: field.name });
        row.append(name, element("td", valueText(field.type)), definition, actions); rows.append(row);
      }
      table.append(rows); wrapper.append(table); parentNode.append(wrapper);
    };
    const showModel = model => {
      if (destroyed) return;
      selected = model; selectedRelation = null; updateSelection();
      detail.replaceChildren(); heading(detail, model, "未命名模型");
      const metadata = element("div", null, "orion-model-metadata");
      appendValue(metadata, "主键", model.primaryKey);
      appendValue(metadata, "发布映射", model.properties?.mapping_id);
      const projection = projectionFor(model), source = sourceFor(projection);
      appendValue(metadata, "来源", source ? `${valueText(source.source_id)} · ${valueText(source.source_table)}` : null);
      detail.append(metadata);
      ask(detail, "围绕此模型提问", `请围绕「${label(model)}」模型分析：`, { kind: "model", model: model.name });
      detail.append(element("h5", `字段（${array(model.columns).length}）`));
      renderFields(detail, array(model.columns), model);
      const relations = relationships.filter(relation => endpoints(relation).includes(model));
      const relationList = element("div", null, "orion-model-related");
      relationList.append(element("h5", `关联模型（${relations.length} 条关系）`));
      for (const relation of relations) relationList.append(button(`${relation.models.join(" → ")} · ${valueText(relation.joinType)}`, () => showRelation(relation)));
      if (!relations.length) relationList.append(element("p", "目录未提供此模型的可解析关系。"));
      detail.append(relationList);
      const mapping = element("details", null, "orion-model-source-definition");
      mapping.append(element("summary", "查看来源映射与模型定义"));
      // Join only the catalog's explicit projection/source keys, never parse SQL or guess by a table name.
      const modelSource = model.source && typeof model.source === "object" ? model.source : {};
      const sourceId = source?.source_id ?? model.source_id ?? modelSource.source_id;
      appendValue(mapping, "来源 ID", sourceId);
      appendValue(mapping, "来源表", source?.source_table ?? model.source_table ?? modelSource.source_table);
      appendValue(mapping, "数据库", source?.database);
      appendValue(mapping, "业务身份列", projection?.identity_columns);
      if (projection && !source) mapping.append(element("p", "来源引用缺失或重名，不能唯一解析此模型的来源绑定。", "orion-model-warning"));
      else if (!sourceId) mapping.append(element("p", "目录未提供模型与来源 ID 的直接绑定；上方来源列和下方 SQL 保留目录中的原始映射。"));
      if (projection) {
        mapping.append(element("h5", "字段投影"));
        for (const column of array(projection.columns)) appendValue(mapping, valueText(column.name), `${valueText(column.physical)} → ${valueText(column.name)} · ${valueText(column.type)}`, true);
      }
      appendCode(mapping, "来源 SQL（refSql）", model.refSql);
      detail.append(mapping);
    };
    const renderGraph = parentNode => {
      nodeElements.clear(); edgeElements.clear();
      const validRelations = shownRelations.filter(relation => endpoints(relation).every(model => model && shownModels.includes(model)));
      const unresolved = shownRelations.filter(relation => endpoints(relation).some(model => !model));
      const graphHeader = element("div", null, "orion-model-graph-toolbar");
      graphHeader.append(element("strong", `关系图 · ${shownModels.length} 个模型 · ${validRelations.length} 条连线`));
      const zoomLabel = element("span"); zoomLabel.setAttribute("aria-live", "polite");
      const viewport = element("div", null, "orion-model-graph-viewport");
      viewport.tabIndex = 0; viewport.setAttribute("aria-label", "模型关系图，可滚动并点击模型或关系");
      const canvas = element("div", null, "orion-model-graph-canvas");
      const surface = element("div", null, "orion-model-graph-surface");
      const columns = shownModels.length > 6 ? 3 : 2;
      const width = columns * 280 + 48, height = Math.max(190, Math.ceil(shownModels.length / columns) * 132 + 48);
      const setZoom = next => {
        zoom = Math.max(.65, Math.min(1.4, next));
        canvas.style.width = `${width * zoom}px`; canvas.style.height = `${height * zoom}px`;
        surface.style.width = `${width}px`; surface.style.height = `${height}px`; surface.style.transform = `scale(${zoom})`;
        zoomLabel.textContent = `${Math.round(zoom * 100)}%`;
      };
      graphHeader.append(button("缩小", () => setZoom(zoom - .15)), zoomLabel, button("放大", () => setZoom(zoom + .15)), button("重置", () => setZoom(1)));
      const svg = svgElement("svg", { viewBox: `0 0 ${width} ${height}`, width, height, "aria-label": "目录中的模型关系连线" });
      const positions = new Map(shownModels.map((model, index) => [model, { x: 28 + index % columns * 280, y: 28 + Math.floor(index / columns) * 132 }]));
      validRelations.forEach((relation, index) => {
        const [left, right] = endpoints(relation), start = positions.get(left), end = positions.get(right);
        let x1 = start.x + 110, y1 = start.y + 36, x2 = end.x + 110, y2 = end.y + 36;
        let path;
        const offset = (index % 3 - 1) * 13;
        if (left === right) {
          x1 = start.x + 180; x2 = start.x + 220; y2 = start.y + 52;
          path = `M ${x1} ${start.y} C ${x1} ${start.y - 28}, ${x2 + 38} ${start.y - 28}, ${x2 + 38} ${y2} L ${x2} ${y2}`;
          y1 = start.y;
        } else if (start.y === end.y) {
          x1 += start.x < end.x ? 110 : -110; x2 += start.x < end.x ? -110 : 110;
          path = `M ${x1} ${y1 + offset} C ${(x1 + x2) / 2} ${y1 + offset}, ${(x1 + x2) / 2} ${y2 + offset}, ${x2} ${y2 + offset}`;
        } else {
          y1 += start.y < end.y ? 36 : -36; y2 += start.y < end.y ? -36 : 36;
          path = `M ${x1 + offset} ${y1} C ${x1 + offset} ${(y1 + y2) / 2}, ${x2 + offset} ${(y1 + y2) / 2}, ${x2 + offset} ${y2}`;
        }
        const group = svgElement("g", { class: "orion-model-edge", tabindex: 0, role: "button", "aria-label": `${valueText(relation.name)}：${relation.models.join(" → ")}，${valueText(relation.joinType)}` });
        group.dataset.relationship = relation.name ?? "";
        group.append(svgElement("title", {}, `${valueText(relation.name)} · ${valueText(relation.condition)}`));
        group.append(svgElement("path", { d: path, class: "orion-model-edge-hit" }), svgElement("path", { d: path, class: "orion-model-edge-line" }));
        const [a, b] = cardinalities(relation.joinType);
        group.append(svgElement("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 7, "text-anchor": "middle" }, `${a} → ${b}`));
        group.addEventListener("click", () => showRelation(relation));
        group.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); showRelation(relation); } });
        svg.append(group); edgeElements.set(relation, group);
      });
      surface.append(svg);
      for (const [model, position] of positions) {
        const node = button("", () => showModel(model), "orion-model-node");
        node.dataset.model = model.name ?? "";
        node.style.left = `${position.x}px`; node.style.top = `${position.y}px`;
        node.append(element("strong", label(model)), element("span", `${array(model.columns).length} 个字段 · ${valueText(model.name)}`));
        surface.append(node); nodeElements.set(model, node);
      }
      canvas.append(surface); viewport.append(canvas); setZoom(zoom);
      parentNode.append(graphHeader, viewport);
      if (!shownModels.length) parentNode.append(element("p", "没有匹配的模型或字段。", "orion-model-empty"));
      if (unresolved.length) parentNode.append(element("p", `${unresolved.length} 条关系的端点缺失或重名，保留在下方清单，未绘制连线。`, "orion-model-warning"));
      const list = element("details", null, "orion-model-relationship-list");
      list.append(element("summary", `关系清单（${validRelations.length + unresolved.length}）`));
      for (const relation of [...validRelations, ...unresolved]) {
        const row = button(`${valueText(relation.name)} · ${Array.isArray(relation.models) ? relation.models.join(" → ") : "端点未提供"} · ${valueText(relation.joinType)}`, () => showRelation(relation));
        row.dataset.relationship = relation.name ?? ""; list.append(row);
      }
      if (!relationships.length) list.append(element("p", "目录未提供模型关系；不会根据字段名猜测连线。"));
      if (query && relationships.length > validRelations.length + unresolved.length) list.append(element("p", "搜索期间仅绘制两个端点都在搜索结果中的关系。清空搜索可查看全部。"));
      parentNode.append(list);
    };
    const renderModels = () => {
      shownModels = models.filter(model => contains(model, query) || array(model.columns).some(field => contains(field, query)));
      shownRelations = relationships.filter(relation => !query || endpoints(relation).some(model => shownModels.includes(model)));
      status.textContent = `${shownModels.length} / ${models.length} 个模型`;
      const split = element("div", null, "orion-model-layout");
      const sidebar = element("div", null, "orion-model-list"); sidebar.setAttribute("aria-label", "模型清单");
      const main = element("div", null, "orion-model-main");
      listElements.clear();
      for (const model of shownModels) {
        const node = button("", () => showModel(model));
        node.dataset.model = model.name ?? "";
        node.append(element("strong", label(model)), element("span", valueText(model.name)));
        if (query) node.append(element("small", `${array(model.columns).filter(field => contains(field, query)).length} 个字段匹配`));
        sidebar.append(node); listElements.set(model, node);
      }
      renderGraph(main);
      detail = element("section", null, "orion-model-detail"); detail.setAttribute("aria-label", "选中模型或关系的详情");
      main.append(detail); split.append(sidebar, main); body.append(split);
      if (selectedRelation && shownRelations.includes(selectedRelation)) showRelation(selectedRelation);
      else if (shownModels.length) showModel(shownModels.includes(selected) ? selected : shownModels[0]);
    };
    const renderCubeFields = (parentNode, title, fields) => {
      parentNode.append(element("h5", `${title}（${fields.length}）`));
      if (!fields.length) { parentNode.append(element("p", `未提供${title}。`)); return; }
      const table = element("table", null, "orion-model-fields");
      const head = element("thead"), tr = element("tr");
      for (const title of ["名称", "类型", "表达式"]) { const th = element("th", title); th.setAttribute("scope", "col"); tr.append(th); }
      head.append(tr); table.append(head);
      const rows = element("tbody");
      for (const field of fields) {
        const row = element("tr"); row.dataset.match = String(Boolean(query) && contains(field, query));
        row.append(element("td", valueText(field.name)), element("td", valueText(field.type)), element("td", valueText(field.expression))); rows.append(row);
      }
      table.append(rows); const scroll = element("div", null, "orion-model-table-scroll"); scroll.append(table); parentNode.append(scroll);
    };
    const renderCubes = () => {
      const matches = cubes.filter(cube => contains(cube, query) || [cube.baseObject, ...array(cube.measures).map(valueText)].some(value => typeof value === "string" && value.toLocaleLowerCase().includes(query))
        || [...array(cube.dimensions), ...array(cube.timeDimensions)].some(item => contains(item, query)));
      status.textContent = `${matches.length} / ${cubes.length} 个 Cube`;
      for (const cube of matches) {
        const card = element("article", null, "orion-model-card"); heading(card, cube, "未命名 Cube");
        appendValue(card, "基础模型", cube.baseObject); appendValue(card, "业务口径审批", cube.properties?.approval_status);
        const base = uniqueModel(cube.baseObject);
        if (base) card.append(button(`查看模型 ${label(base)}`, () => { selected = base; selectedRelation = null; activate("models", true); }));
        ask(card, "围绕此 Cube 提问", `请使用「${label(cube)}」中已定义的指标和维度分析：`, { kind: "cube", cube: cube.name, model: cube.baseObject });
        renderCubeFields(card, "指标", array(cube.measures)); renderCubeFields(card, "维度", array(cube.dimensions)); renderCubeFields(card, "时间维度", array(cube.timeDimensions));
        body.append(card);
      }
      if (!matches.length) body.append(element("p", cubes.length ? "没有匹配的 Cube。" : "目录未提供 Cube。", "orion-model-empty"));
    };
    const renderViews = () => {
      const matches = views.filter(view => contains(view, query) || typeof view.statement === "string" && view.statement.toLocaleLowerCase().includes(query));
      status.textContent = `${matches.length} / ${views.length} 个视图`;
      for (const view of matches) {
        const card = element("article", null, "orion-model-card"); heading(card, view, "未命名视图");
        appendCode(card, "视图定义", view.statement);
        ask(card, "围绕此视图提问", `请基于「${label(view)}」视图分析：`, { kind: "view", view: view.name });
        body.append(card);
      }
      if (!matches.length) body.append(element("p", views.length ? "没有匹配的视图。" : "目录未提供视图。", "orion-model-empty"));
    };
    const renderSources = () => {
      const keys = [["source_id", "来源 ID"], ["database", "数据库"], ["source_table", "来源表"], ["dataset_id", "数据集"], ["registration_status", "登记状态"], ["expected_count", "目录记录数"]];
      const matches = sources.filter(source => !query || keys.some(([key]) => valueText(source[key]).toLocaleLowerCase().includes(query)));
      status.textContent = `${matches.length} / ${sources.length} 项来源`;
      for (const source of matches) {
        const card = element("article", null, "orion-model-card");
        card.append(element("h4", valueText(source.source_id)));
        for (const [key, title] of keys) appendValue(card, title, source[key]);
        body.append(card);
      }
      if (!matches.length) body.append(element("p", sources.length ? "没有匹配的来源。" : "目录未提供来源映射。", "orion-model-empty"));
    };
    const renderKnowledge = () => {
      status.textContent = "语义口径与未接入项";
      const knowledge = catalog.knowledge;
      if (knowledge && typeof knowledge === "object") {
        for (const [key, value] of Object.entries(knowledge)) {
          if (key === "omitted_semantics") continue;
          if (query && !valueText(value).toLocaleLowerCase().includes(query)) continue;
          const card = element("article", null, "orion-model-card");
          card.append(element("h4", ({ rules: "已登记口径", models: "模型说明", query_examples: "查询示例" })[key] ?? key));
          if (Array.isArray(value)) {
            if (!value.length) card.append(element("p", "未提供。"));
            for (const item of value) {
              if (item && typeof item === "object") {
                if (item.name) card.append(element("strong", valueText(item.name)));
                card.append(element("p", valueText(item.description ?? item.question ?? item)));
              } else card.append(element("p", valueText(item)));
            }
          } else card.append(element("p", valueText(value)));
          body.append(card);
        }
      } else body.append(element("p", knowledge ? valueText(knowledge) : "目录未提供语义说明。"));
      const missing = element("section", null, "orion-model-omitted"); missing.append(element("h4", `未接入的语义（${omitted.length}）`));
      for (const item of omitted.filter(item => !query || valueText(item).toLocaleLowerCase().includes(query))) {
        const card = element("article", null, "orion-model-card");
        appendValue(card, "业务对象", item.target); appendValue(card, "映射", item.mapping_id); appendValue(card, "原因", item.reason); missing.append(card);
      }
      if (!omitted.length) missing.append(element("p", "目录未列出未接入项。"));
      body.append(missing);
    };
    const render = () => {
      if (destroyed) return;
      body.replaceChildren();
      for (const [name, node] of tabButtons) node.setAttribute("aria-pressed", String(name === activeTab));
      ({ models: renderModels, cubes: renderCubes, views: renderViews, sources: renderSources, knowledge: renderKnowledge })[activeTab]();
    };
    const activate = (name, clearSearch = false) => {
      activeTab = name;
      if (clearSearch) { query = ""; search.value = ""; }
      render();
    };
    for (const [key, title] of [["models", `模型关系（${models.length}）`], ["cubes", `Cube（${cubes.length}）`], ["views", `视图（${views.length}）`], ["sources", `来源（${sources.length}）`], ["knowledge", `口径与缺口（${omitted.length}）`]]) {
      const node = button(title, () => activate(key)); node.dataset.category = key; nav.append(node); tabButtons.set(key, node);
    }
    search.addEventListener("input", () => { query = String(search.value ?? "").trim().toLocaleLowerCase(); render(); });
    root.append(toolbar, nav, body); parent.append(root); render();
    return Object.freeze({ destroy() { destroyed = true; nodeElements.clear(); edgeElements.clear(); listElements.clear(); root.remove(); } });
  }
  window.__ORION_ANALYTICS_MODELS__ = Object.freeze({ mount });
})();
