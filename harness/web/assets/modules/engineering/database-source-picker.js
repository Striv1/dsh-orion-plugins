(() => {
  if (window.__ORION_DATABASE_SOURCE_PICKER__) return;
  const sourceKey = (item) => JSON.stringify([item.datasource_id, item.database, item.scope ?? "TABLE", item.schema, item.table]);
  const scopeText = (items) => items.map((item) => {
    const connection = `Chat2DB datasource_id=${JSON.stringify(item.datasource_id)}；数据源名称=${JSON.stringify(item.datasource_label)}；数据库=${JSON.stringify(item.database)}`;
    if (item.scope === "DATABASE") {
      if (!Array.isArray(item.frozen_tables) || !item.frozen_tables.length) throw new Error("整库当前表清单尚未完整核对，不能填入消息。");
      return `${connection}；范围=该库当前目录全部表（仅只读，不含未来新增表）；冻结表清单=${JSON.stringify(item.frozen_tables)}`;
    }
    return `${connection}；Schema=${JSON.stringify(item.schema)}；表=${JSON.stringify(item.table)}；范围=仅指定表（只读）`;
  }).join("\n");

  const create = (client, onChange = () => {}) => {
    const state = { active: false, rangeMode: "TABLE", sources: [], databases: [], schemas: [], tables: [],
      datasourceId: null, database: null, schema: null, sourceCatalogReady: false, loading: "", error: "", validating: false, selected: [] };
    let generation = 0;
    let verificationGeneration = 0;
    const cancelVerification = () => { verificationGeneration += 1; state.validating = false; };
    const snapshot = () => structuredClone(state);
    let directoryError = "";
    let selectionError = "";
    const emit = () => { state.error = [directoryError, selectionError].filter(Boolean).join(" "); onChange(snapshot()); };
    const rebuildSelectionError = () => {
      const pending = state.selected.filter((item) => item.verification !== "VERIFIED"
        && (item.verification_error || item.verification === "MISSING"));
      const details = pending.map((item) => item.verification_error).filter(Boolean);
      const emptySchema = details.some((message) => message.includes("空表目录"));
      selectionError = pending.length ? (emptySchema
        ? "某个 Schema 返回空表目录，无法确认整库范围完整性；已保留选择。请恢复目录后重试，或移除该整库范围后改用指定表。"
        : "部分已选范围待核验或未通过目录核验，请恢复连接后重试，或明确移除不可用项。") : "";
      if (details.length) selectionError += ` ${details.join("；")}`;
    };
    const reset = () => {
      state.sources = []; state.databases = []; state.schemas = []; state.tables = [];
      state.datasourceId = null; state.database = null; state.schema = null; state.sourceCatalogReady = false; directoryError = ""; selectionError = "";
    };
    const affected = (item, level, parameters) => level === "sources"
      || (item.datasource_id === parameters.datasource_id && (level === "databases"
      || (item.database === parameters.database_name && (level === "schemas" || item.scope === "DATABASE" || item.schema === parameters.schema_name))));
    const mark = (level, parameters, status) => {
      for (const item of state.selected) if (affected(item, level, parameters)) item.verification = status;
    };
    const load = async (level, parameters = {}) => {
      const request = ++generation;
      verificationGeneration += 1; state.validating = false;
      mark(level, parameters, "PENDING");
      state.loading = level; directoryError = ""; state[level] = []; emit();
      try {
        const response = level === "sources" ? await client.datasources() : await client.chat2dbCatalog(level, parameters);
        if (request !== generation || !state.active) return;
        if (level !== "sources" && response.freshness && response.freshness !== "LIVE") {
          throw new Error(response.warning || "目录提供方未证明刷新结果，不能核验当前范围。请恢复连接器的目录刷新能力后重试。");
        }
        const rows = level === "sources" ? response.datasources : response.items;
        if (!Array.isArray(rows)) throw new Error("目录回执格式不完整，请重新读取。");
        if (level === "sources") state.sourceCatalogReady = true;
        state[level] = rows.flatMap((row) => {
          if (!row || typeof row !== "object") return [];
          if (level === "sources") return row.id !== undefined && row.id !== null
            ? [{ id: String(row.id), label: String(row.label ?? row.name ?? row.id), type: String(row.type ?? "") }] : [];
          // The shared backend requires a nonempty schema for table browsing.
          // Empty results do not authorize inventing public/default or a schema-less query.
          if (typeof row.name !== "string" || !row.name) return [];
          if (/^no (?:schemas?|databases?|tables?) (?:were )?found[.!]?$/iu.test(row.name.trim())) return [];
          return [{ name: row.name, type: String(row.type ?? "") }];
        });
        for (const item of state.selected) {
          if (!affected(item, level, parameters)) continue;
          if (item.scope === "DATABASE" && ["schemas", "tables"].includes(level)) { item.verification = "PENDING"; continue; }
          const present = state[level].some((row) => level === "sources" ? row.id === item.datasource_id
            : row.name === item[({ databases: "database", schemas: "schema", tables: "table" })[level]]);
          item.verification = !present ? "MISSING" : level === "tables" ? "VERIFIED" : "PENDING";
          if (item.verification === "VERIFIED") item.verification_error = "";
        }
        if (!state[level].length) directoryError = `当前范围没有返回${({ sources: "数据源", databases: "数据库", schemas: "Schema", tables: "数据表" })[level]}；请检查连接或刷新。`;
      } catch (error) {
        if (request !== generation || !state.active) return;
        state[level] = [];
        mark(level, parameters, "PENDING");
        directoryError = error instanceof Error ? error.message : "目录读取失败，请重试。";
      } finally {
        if (request === generation && state.active) { state.loading = ""; rebuildSelectionError(); emit(); }
      }
    };
    const controller = {
      snapshot,
      setRangeMode(mode) {
        if (!["TABLE", "DATABASE"].includes(mode) || state.rangeMode === mode) return;
        cancelVerification(); state.rangeMode = mode; rebuildSelectionError(); emit();
      },
      chooseDatabase(name, checked) {
        if (!state.active || state.loading || state.rangeMode !== "DATABASE" || !state.databases.some((item) => item.name === name)) return;
        const source = state.sources.find((item) => item.id === state.datasourceId);
        if (!source) return;
        if (checked && state.selected.some((item) => item.datasource_id === source.id && item.database === name && item.scope !== "DATABASE")) {
          selectionError = "该库已有指定表范围；如需授权整个数据库，请先明确移除该库的表选择。"; emit(); return;
        }
        const selected = { datasource_id: source.id, datasource_label: source.label, database: name, scope: "DATABASE", verification: "PENDING" };
        const key = sourceKey(selected), exists = state.selected.some((item) => sourceKey(item) === key);
        if (checked !== exists) cancelVerification();
        if (checked && !exists) state.selected.push(selected);
        if (!checked) state.selected = state.selected.filter((item) => sourceKey(item) !== key);
        rebuildSelectionError(); emit();
      },
      chooseVisibleDatabases(checked) {
        if (typeof checked !== "boolean" || !state.active || state.loading || state.validating
          || !state.sourceCatalogReady || state.rangeMode !== "DATABASE" || !state.databases.length) return false;
        const source = state.sources.find((item) => item.id === state.datasourceId);
        if (!source) return false;
        const names = new Set(state.databases.map((item) => item.name));
        if (checked && state.selected.some((item) => item.datasource_id === source.id
          && names.has(item.database) && item.scope !== "DATABASE")) {
          selectionError = "本批数据库中已有指定表范围；请先明确移除冲突库的表选择，本次批量选择未作更改。";
          emit(); return false;
        }
        const existing = new Set(state.selected.map(sourceKey));
        const additions = checked ? [...names].map((name) => ({ datasource_id: source.id,
          datasource_label: source.label, database: name, scope: "DATABASE", verification: "PENDING" }))
          .filter((item) => !existing.has(sourceKey(item))) : [];
        const selected = checked ? [...state.selected, ...additions] : state.selected.filter((item) =>
          !(item.datasource_id === source.id && item.scope === "DATABASE" && names.has(item.database)));
        if (selected.length !== state.selected.length) cancelVerification();
        state.selected = selected; rebuildSelectionError(); emit(); return true;
      },
      chooseTables(names, checked) {
        if (typeof checked !== "boolean" || !Array.isArray(names) || !names.length
          || !state.active || state.loading || state.validating || !state.sourceCatalogReady
          || state.rangeMode !== "TABLE" || !state.tables.length || state.schema === null
          || !state.databases.some((item) => item.name === state.database)
          || !state.schemas.some((item) => item.name === state.schema)) return false;
        const source = state.sources.find((item) => item.id === state.datasourceId);
        if (!source || names.some((name) => typeof name !== "string"
          || !state.tables.some((item) => item.name === name))) return false;
        const visibleNames = new Set(names);
        if (checked && state.selected.some((item) => item.datasource_id === source.id
          && item.database === state.database && item.scope === "DATABASE")) {
          selectionError = "该库已选择整个数据库；请先明确移除整库范围，本次批量选择未作更改。";
          emit(); return false;
        }
        const existing = new Set(state.selected.map(sourceKey));
        const additions = checked ? [...visibleNames].map((name) => ({ datasource_id: source.id,
          datasource_label: source.label, database: state.database, schema: state.schema,
          table: name, scope: "TABLE", verification: "VERIFIED" }))
          .filter((item) => !existing.has(sourceKey(item))) : [];
        const selected = checked ? [...state.selected, ...additions] : state.selected.filter((item) =>
          !(item.datasource_id === source.id && item.database === state.database && item.schema === state.schema
            && item.scope !== "DATABASE" && visibleNames.has(item.table)));
        if (selected.length !== state.selected.length) cancelVerification();
        state.selected = selected; rebuildSelectionError(); emit(); return true;
      },
      async setActive(active) {
        if (state.active === active) return;
        generation += 1; verificationGeneration += 1; state.validating = false; state.active = active; reset(); selectionError = ""; state.loading = ""; emit();
        if (active) await load("sources");
      },
      async refresh() {
        if (!state.active) return;
        generation += 1; reset(); await load("sources");
      },
      async selectSource(id) {
        if (id === null) { generation += 1; cancelVerification(); state.datasourceId = null; state.database = null; state.schema = null; state.databases = []; state.schemas = []; state.tables = []; state.loading = ""; directoryError = ""; rebuildSelectionError(); emit(); return; }
        if (!state.active || !state.sources.some((item) => item.id === id)) return;
        generation += 1; state.datasourceId = id; state.database = null; state.schema = null;
        state.databases = []; state.schemas = []; state.tables = [];
        await load("databases", { datasource_id: id });
      },
      async selectDatabase(name) {
        if (name === null) { generation += 1; cancelVerification(); state.database = null; state.schema = null; state.schemas = []; state.tables = []; state.loading = ""; directoryError = ""; rebuildSelectionError(); emit(); return; }
        if (!state.active || state.datasourceId === null || !state.databases.some((item) => item.name === name)) return;
        generation += 1; state.database = name; state.schema = null; state.schemas = []; state.tables = [];
        await load("schemas", { datasource_id: state.datasourceId, database_name: name });
      },
      async selectSchema(name) {
        if (name === null) { generation += 1; cancelVerification(); state.schema = null; state.tables = []; state.loading = ""; directoryError = ""; rebuildSelectionError(); emit(); return; }
        if (!state.active || state.database === null || !state.schemas.some((item) => item.name === name)) return;
        generation += 1; state.schema = name; state.tables = [];
        await load("tables", { datasource_id: state.datasourceId, database_name: state.database, schema_name: name });
      },
      chooseTable(name, checked) {
        if (!state.active || state.loading || state.rangeMode !== "TABLE" || state.schema === null || !state.tables.some((item) => item.name === name)) return;
        const source = state.sources.find((item) => item.id === state.datasourceId);
        if (!source) return;
        if (checked && state.selected.some((item) => item.datasource_id === source.id && item.database === state.database && item.scope === "DATABASE")) {
          selectionError = "该库已选择整个数据库；如需限制为指定表，请先明确移除整库范围。"; emit(); return;
        }
        const selected = { datasource_id: source.id, datasource_label: source.label,
          scope: "TABLE", database: state.database, schema: state.schema, table: name, verification: "VERIFIED" };
        const key = sourceKey(selected);
        const exists = state.selected.some((item) => sourceKey(item) === key);
        if (checked !== exists) cancelVerification();
        if (checked && !exists) state.selected.push(selected);
        if (!checked) state.selected = state.selected.filter((item) => sourceKey(item) !== key);
        rebuildSelectionError(); emit();
      },
      remove(key) {
        if (!state.selected.some((item) => sourceKey(item) === key)) return;
        cancelVerification();
        state.selected = state.selected.filter((item) => sourceKey(item) !== key);
        rebuildSelectionError(); emit();
      },
      async verifySelected() {
        if (!state.active || state.loading || state.validating) return;
        const request = ++verificationGeneration;
        const browsing = generation;
        state.validating = true; selectionError = "";
        for (const item of state.selected) item.verification = "PENDING";
        emit();
        const cache = new Map();
        const catalog = (level, parameters) => {
          const key = JSON.stringify([level, parameters]);
          if (!cache.has(key)) cache.set(key, Promise.resolve().then(async () => {
            const response = level === "sources" ? await client.datasources() : await client.chat2dbCatalog(level, parameters);
            if (level !== "sources" && response.freshness && response.freshness !== "LIVE") {
              throw new Error(response.warning || "目录提供方未证明刷新结果，不能核验当前范围。");
            }
            return response;
          }));
          return cache.get(key);
        };
        const checked = new Map();
        const frozen = new Map();
        const verificationErrors = new Map();
        let verifiedSources = null;
        for (const item of [...state.selected]) {
          if (request !== verificationGeneration || browsing !== generation || !state.active) return;
          let location = `数据库 ${JSON.stringify(item.database)}`;
          try {
            const sources = await catalog("sources", {});
            if (!Array.isArray(sources.datasources)) throw new Error("数据源回执格式不完整");
            verifiedSources = sources.datasources;
            const actualSource = sources.datasources.find((row) => String(row.id) === item.datasource_id);
            let present = Boolean(actualSource);
            for (const [level, parameters, name] of [
              ["databases", { datasource_id: item.datasource_id }, item.database],
              ["schemas", { datasource_id: item.datasource_id, database_name: item.database }, item.schema],
              ["tables", { datasource_id: item.datasource_id, database_name: item.database, schema_name: item.schema }, item.table],
            ]) {
              if (!present || (item.scope === "DATABASE" && level !== "databases")) break;
              const response = await catalog(level, parameters);
              if (!Array.isArray(response.items)) throw new Error("目录回执格式不完整");
              present = response.items.some((row) => row.name === name && typeof row.name === "string" && row.name !== "");
            }
            if (present && item.scope === "DATABASE") {
              const schemas = await catalog("schemas", { datasource_id: item.datasource_id, database_name: item.database });
              const validName = (row) => row && typeof row.name === "string" && row.name.length > 0
                && !/^no (?:schemas?|databases?|tables?) (?:were )?found[.!]?$/iu.test(row.name.trim());
              if (!Array.isArray(schemas.items) || !schemas.items.length || !schemas.items.every(validName)) throw new Error("整库 Schema 目录不完整");
              const tables = [];
              for (const schema of new Set(schemas.items.map((row) => row.name))) {
                if (request !== verificationGeneration || browsing !== generation || !state.active) return;
                location = `数据库 ${JSON.stringify(item.database)} / Schema ${JSON.stringify(schema)}`;
                const response = await catalog("tables", { datasource_id: item.datasource_id, database_name: item.database, schema_name: schema });
                if (!Array.isArray(response.items) || !response.items.every(validName)) throw new Error("整库表目录不完整");
                if (!response.items.length) {
                  throw new Error("Schema 返回空表目录，无法确认整库范围完整性");
                }
                for (const table of new Set(response.items.map((row) => row.name))) tables.push({ schema, table });
              }
              if (!tables.length) throw new Error("该库没有可登记的表");
              frozen.set(sourceKey(item), tables);
            }
            checked.set(sourceKey(item), present ? "VERIFIED" : "MISSING");
          } catch (error) {
            checked.set(sourceKey(item), "PENDING");
            // Keep a bounded one-line diagnostic. Never echo connection credentials or URLs.
            const detail = String(error?.message || "目录回读失败")
              .replace(/(?:postgres(?:ql)?|mysql|jdbc|https?):\/\/[^\s]+/giu, "[连接地址已隐藏]")
              .replace(/\b(password|passwd|pwd|token|secret|authorization|api[_-]?key)\b["']?\s*[:=]\s*(?:Bearer\s+)?(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)/giu, "$1=[已隐藏]")
              .replace(/[\r\n\t]+/gu, " ").slice(0, 220);
            verificationErrors.set(sourceKey(item), `${location}：${detail}`);
          }
        }
        if (request !== verificationGeneration || browsing !== generation || !state.active) return;
        if (verifiedSources) {
          state.sources = verifiedSources.filter((row) => row && row.id !== undefined && row.id !== null)
            .map((row) => ({ id: String(row.id), label: String(row.label ?? row.name ?? row.id), type: String(row.type ?? "") }));
          state.sourceCatalogReady = true;
        }
        for (const item of state.selected) {
          item.verification = checked.get(sourceKey(item)) ?? "PENDING";
          item.verification_error = verificationErrors.get(sourceKey(item)) ?? "";
          if (item.scope === "DATABASE") item.frozen_tables = frozen.get(sourceKey(item)) ?? [];
        }
        state.validating = false;
        rebuildSelectionError();
        emit();
      },
      async prepareScope() {
        if (!state.active || state.loading || state.validating) throw new Error("来源目录正在读取或核验，请稍后再填入消息。");
        const selection = JSON.stringify(state.selected.map(sourceKey).sort());
        const browsing = generation;
        if (!state.sourceCatalogReady || state.selected.some((item) => item.verification !== "VERIFIED")) await controller.verifySelected();
        if (!state.active || generation !== browsing || selection !== JSON.stringify(state.selected.map(sourceKey).sort())) {
          throw new Error("选择范围或建设方式已变化，请核对当前选择后重新填入消息。");
        }
        return controller.scopeText();
      },
      scopeText: () => {
        if (!state.sourceCatalogReady) throw new Error("数据源目录尚未核对成功，请刷新后再填入消息；已选范围已保留。");
        if (state.selected.some((item) => !state.sources.some((source) => source.id === item.datasource_id))) {
          throw new Error("部分已选数据源已不可用，请恢复连接并刷新，或明确移除这些来源。");
        }
        if (state.validating || state.selected.some((item) => item.verification !== "VERIFIED")) {
          throw new Error(state.error || "部分已选库、Schema 或表待核验或已不可用，请点击核验已选范围，或明确移除。");
        }
        return scopeText(state.selected);
      },
    };
    return controller;
  };

  const mount = (root, client, onSelectionChange = () => {}) => {
    const escape = (value) => String(value).replace(/[&<>"']/gu, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
    let filter = "";
    let previousBrowseKey = "";
    let previousSelectionKey = "[]";
    // Keep the dialog and composer form nodes stable while catalog results update.
    root.innerHTML = `<div class="owa-start-database-summary"><span data-db-summary>尚未选择数据库范围</span><button type="button" data-db-open>选择数据库</button></div>
      <dialog class="owa-start-database-dialog" aria-label="选择数据库只读范围"><div class="owa-start-database-heading"><div><strong>选择数据库 · 可多选</strong><p class="owa-start-database-hint">选择数据源，再勾选需要使用的范围。</p></div></div><div data-db-body></div><div class="owa-start-database-footer"><span data-db-footer-summary role="status" aria-live="polite" aria-atomic="true"></span><button type="button" data-db-close aria-label="关闭数据库选择">完成选择</button></div></dialog>`;
    const dialog = root.querySelector("dialog");
    const body = root.querySelector("[data-db-body]");
    const render = (state) => {
      const selectionKey = JSON.stringify(state.selected.map(sourceKey).sort());
      const selectionChanged = selectionKey !== previousSelectionKey;
      previousSelectionKey = selectionKey;
      const browseKey = JSON.stringify([state.datasourceId, state.database, state.schema, state.rangeMode, filter]);
      const bodyScroll = body.scrollTop;
      const selectedScroll = body.querySelector(".owa-start-database-selected")?.scrollTop ?? 0;
      const tableScroll = [...body.querySelectorAll(".owa-start-database-tables")].map((element) => element.scrollTop);
      const removeButtons = [...body.querySelectorAll("[data-db-remove]")];
      const focusedRemoveIndex = removeButtons.indexOf(root.ownerDocument.activeElement);
      const focusedRemoveKey = focusedRemoveIndex < 0 ? null : removeButtons[focusedRemoveIndex].dataset.dbRemove;
      const options = (items, selected, field = "name") => items.map((item, index) => `<option value="${index}" ${item[field] === selected ? "selected" : ""}>${escape(field === "id" ? `${item.label} · ${item.id}${item.type ? ` · ${item.type}` : ""}` : item.name || "（无 Schema）")}</option>`).join("");
      const selectedKeys = new Set(state.selected.map(sourceKey));
      const current = { datasource_id: state.datasourceId, database: state.database, schema: state.schema };
      const databaseCount = state.selected.filter((item) => item.scope === "DATABASE").length;
      const tableCount = state.selected.length - databaseCount;
      const visibleTables = state.tables.filter((item) => item.name.toLowerCase().includes(filter.toLowerCase()));
      root.querySelector("[data-db-summary]").textContent = state.selected.length
        ? `已选 ${databaseCount} 个整库、${tableCount} 张指定表 · ${new Set(state.selected.map((item) => item.datasource_id)).size} 个数据源${state.selected.some((item) => item.verification !== "VERIFIED") ? " · 来源待核验" : ""}` : "尚未选择范围 · 支持多数据库";
      root.querySelector("[data-db-footer-summary]").textContent = state.selected.length ? `已选 ${databaseCount} 个整库 · ${tableCount} 张指定表` : "尚未选择范围";
      if (!state.active && dialog.open) dialog.close();
      body.innerHTML = `<div class="owa-start-database-browser">
        <div class="owa-start-database-heading"><strong>浏览真实目录</strong><button type="button" data-db-refresh>刷新目录</button></div>
        <p class="owa-start-database-hint">支持跨数据源、跨库选择，切换目录会保留已选项。</p>
        <fieldset class="owa-start-database-mode"><legend>使用范围</legend><label><input type="radio" name="db-range-mode" data-db-mode value="TABLE" ${state.rangeMode === "TABLE" ? "checked" : ""}>指定表</label><label><input type="radio" name="db-range-mode" data-db-mode value="DATABASE" ${state.rangeMode === "DATABASE" ? "checked" : ""}>该库当前目录全部表（只读）</label></fieldset>
        ${state.rangeMode === "DATABASE" ? "<p>包含该库可见 Schema 的表（可能含系统表）；精确范围请选择指定表。不包含未来新增表。</p>" : ""}
        <label>浏览数据源<select data-db-source ${state.loading === "sources" ? "disabled" : ""}><option value="">请选择数据源</option>${options(state.sources, state.datasourceId, "id")}</select></label>
        ${state.rangeMode === "DATABASE" ? `<div class="owa-start-database-bulk"><span>当前数据源 · ${state.databases.length} 个库</span><button type="button" data-db-select-all="DATABASE" ${state.loading || state.validating || !state.databases.length ? "disabled" : ""}>全选当前库（${state.databases.length}）</button><button type="button" data-db-clear-visible="DATABASE" ${state.loading || state.validating || !state.databases.length ? "disabled" : ""}>清空当前库</button></div><div class="owa-start-database-tables">${state.databases.map((item) => `<label><input type="checkbox" data-db-whole="${escape(item.name)}" ${selectedKeys.has(sourceKey({ datasource_id: state.datasourceId, database: item.name, scope: "DATABASE" })) ? "checked" : ""}><span>${escape(item.name)} · 当前全部表，只读</span></label>`).join("")}</div>` : `<label>浏览数据库<select data-db-database ${!state.databases.length ? "disabled" : ""}><option value="">请选择数据库</option>${options(state.databases, state.database)}</select></label>
        <label>数据库模式（Schema）<select data-db-schema ${!state.schemas.length ? "disabled" : ""}><option value="">请选择 Schema</option>${options(state.schemas, state.schema)}</select></label>`}
        <div role="status" aria-live="polite">${escape(state.loading ? "正在读取真实目录…" : state.error || (state.rangeMode === "TABLE" && state.schema !== null ? `当前 ${state.tables.length} 张表；勾选后可继续切换其他来源。` : "先浏览数据源，再勾选数据库或指定表；不默认全选。"))}</div>
        ${state.rangeMode === "TABLE" && state.tables.length ? `<label>筛选表名<input type="search" data-db-filter value="${escape(filter)}" placeholder="按表名筛选"></label>` : ""}
        ${state.rangeMode === "TABLE" && state.tables.length ? `<div class="owa-start-database-bulk"><span>当前列表 · ${visibleTables.length} 张表</span><button type="button" data-db-select-all="TABLE" ${state.loading || state.validating || !visibleTables.length ? "disabled" : ""}>全选当前表（${visibleTables.length}）</button><button type="button" data-db-clear-visible="TABLE" ${state.loading || state.validating || !visibleTables.length ? "disabled" : ""}>清空当前表</button></div>` : ""}
        <div class="owa-start-database-tables">${(state.rangeMode === "TABLE" ? visibleTables : []).map((item) => `<label><input type="checkbox" data-db-table="${escape(item.name)}" ${selectedKeys.has(sourceKey({ ...current, table: item.name })) ? "checked" : ""}><span>${escape(item.name)}</span></label>`).join("")}</div>
        <section class="owa-start-database-section"><div class="owa-start-database-heading"><strong>已选范围 · ${databaseCount} 个整库、${tableCount} 张表</strong><button type="button" data-db-verify ${state.validating || state.loading || !state.selected.length ? "disabled" : ""}>${state.validating ? "正在核验…" : "核验已选范围"}</button></div>
        <ul class="owa-start-database-selected">${state.selected.map((item) => `<li><span>${escape(`${item.datasource_label} [${item.datasource_id}] / ${item.database} / ${item.scope === "DATABASE" ? `该库当前目录全部表（只读）${item.verification === "VERIFIED" ? ` · 已冻结 ${item.frozen_tables?.length ?? 0} 张表` : ""}` : `${item.schema || "（无 Schema）"} / ${item.table}`}`)}${item.verification_error ? ` · ${escape(item.verification_error)}` : ""}${!state.sourceCatalogReady ? " · 目录待核对" : !state.sources.some((source) => source.id === item.datasource_id) ? " · 数据源不可用，请移除或刷新" : item.verification === "MISSING" ? " · 目录中已不存在，请移除或核验" : item.verification !== "VERIFIED" ? " · 待核验" : ""}</span><button type="button" data-db-remove="${escape(sourceKey(item))}" aria-label="移除 ${escape(item.scope === "DATABASE" ? item.database : item.table)}">移除</button></li>`).join("") || "<li>尚未选择范围。</li>"}</ul>
        </section><details class="owa-start-database-notes"><summary>范围说明</summary><p>整库使用当前可见目录的表，不含未来新增表；填稿前会核验清单。切换目录保留选择，同库的整库与指定表范围需明确替换。正式来源接入仍需核验。</p></details>
      </div>`;
      const selectedList = body.querySelector(".owa-start-database-selected");
      if (selectedList) selectedList.scrollTop = selectedScroll;
      if (browseKey === previousBrowseKey) {
        [...body.querySelectorAll(".owa-start-database-tables")].forEach((element, index) => { element.scrollTop = tableScroll[index] ?? 0; });
      }
      body.scrollTop = bodyScroll;
      previousBrowseKey = browseKey;
      if (focusedRemoveIndex >= 0 && dialog.open) {
        const nextButtons = [...body.querySelectorAll("[data-db-remove]")];
        const next = nextButtons.find((element) => element.dataset.dbRemove === focusedRemoveKey)
          ?? nextButtons[Math.min(focusedRemoveIndex, nextButtons.length - 1)]
          ?? root.querySelector("[data-db-close]");
        next?.focus({ preventScroll: true });
      }
      if (selectionChanged) onSelectionChange();
    };
    const picker = create(client, render);
    root.addEventListener("change", (event) => {
      const element = event.target;
      const state = picker.snapshot();
      if (element.matches("[data-db-mode]")) picker.setRangeMode(element.value);
      if (element.matches("[data-db-whole]")) picker.chooseDatabase(element.dataset.dbWhole, element.checked);
      if (element.matches("[data-db-source]")) { filter = ""; picker.selectSource(element.value === "" ? null : state.sources[Number(element.value)]?.id); }
      if (element.matches("[data-db-database]")) { filter = ""; picker.selectDatabase(element.value === "" ? null : state.databases[Number(element.value)]?.name); }
      if (element.matches("[data-db-schema]")) { filter = ""; picker.selectSchema(element.value === "" ? null : state.schemas[Number(element.value)]?.name); }
      if (element.matches("[data-db-table]")) picker.chooseTable(element.dataset.dbTable, element.checked);
    });
    root.addEventListener("input", (event) => {
      if (!event.target.matches("[data-db-filter]")) return;
      const start = event.target.selectionStart;
      filter = event.target.value; render(picker.snapshot());
      const field = root.querySelector("[data-db-filter]"); field?.focus(); field?.setSelectionRange(start, start);
    });
    root.addEventListener("click", (event) => {
      if (event.target.closest("[data-db-open]") && !dialog.open) dialog.showModal();
      if (event.target.closest("[data-db-close]") && dialog.open) dialog.close();
      const bulk = event.target.closest("[data-db-select-all], [data-db-clear-visible]");
      if (bulk) {
        const checked = bulk.hasAttribute("data-db-select-all");
        const kind = checked ? bulk.dataset.dbSelectAll : bulk.dataset.dbClearVisible;
        if (kind === "DATABASE") picker.chooseVisibleDatabases(checked);
        else picker.chooseTables(picker.snapshot().tables.filter((item) => item.name.toLowerCase().includes(filter.toLowerCase())).map((item) => item.name), checked);
      }
      if (event.target.closest("[data-db-refresh]")) { filter = ""; picker.refresh(); }
      if (event.target.closest("[data-db-verify]")) picker.verifySelected();
      const remove = event.target.closest("[data-db-remove]");
      if (remove) picker.remove(remove.dataset.dbRemove);
    });
    render(picker.snapshot());
    return picker;
  };
  window.__ORION_DATABASE_SOURCE_PICKER__ = Object.freeze({ create, mount, sourceKey, scopeText });
})();
