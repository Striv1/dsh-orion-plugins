(() => {
  if (window.__ORION_ANALYTICS_PANEL__) return;
  const API = "/orion-ontology-qa-api/analytics";
  const MAX_ROWS = 500;
  const labels = { table: "结果表", bar: "柱状图", line: "趋势图", kpi: "指标值" };
  const textValue = value => value == null ? "未知/缺失" : typeof value === "object" ? JSON.stringify(value) : String(value);
  const displayValue = (value, type) => {
    const text = textValue(value);
    if (typeof value !== "string" || typeof type !== "string"
      || !/^(?:decimal(?:128|256)?|numeric|number|double|float(?:16|32|64)?|u?int(?:8|16|32|64)?)(?:\b|\()/i.test(type.trim())) return text;
    const decimal = /^([+-]?\d+)\.(\d+)$/.exec(value);
    if (!decimal) return text;
    const fractional = decimal[2].replace(/0+$/, "");
    return decimal[1] + (fractional ? "." + fractional : "");
  };
  const localTime = value => {
    if (typeof value !== "string" || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return value ?? "未提供";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return value;
    try {
      const formatter = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23", timeZoneName: "longOffset" });
      const parts = Object.fromEntries(formatter.formatToParts(date).map(part => [part.type, part.value]));
      return `${parts.year}年${parts.month}月${parts.day}日 ${parts.hour}:${parts.minute}:${parts.second}（${formatter.resolvedOptions().timeZone}，${parts.timeZoneName}）`;
    } catch { return value; }
  };
  const appendReadingTime = (parent, started, finished, suffix, label = "读取数据时间") => {
    const node = element("p", `${label}：${localTime(started)}${finished ? ` 至 ${localTime(finished)}` : ""}${suffix}`);
    node.title = [started, finished].filter(Boolean).join(" → "); parent.append(node);
  };
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (text, handler) => {
    const node = element("button", text);
    node.type = "button";
    node.addEventListener("click", handler);
    return node;
  };
  const numeric = value => {
    if (value == null || typeof value === "boolean") return null;
    if (typeof value === "string" && !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value)) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  };
  const variablesOf = result => {
    const names = Array.isArray(result.variables) && result.variables.length
      ? result.variables : [...new Set((result.rows ?? []).flatMap(row => Object.keys(row)))];
    return names.filter(name => typeof name === "string").slice(0, 100);
  };
  const csvCell = value => {
    let text = value == null ? "" : typeof value === "object" ? JSON.stringify(value) : String(value);
    // Spreadsheet formulas can start after whitespace/control characters.
    if (typeof value !== "number" && (/^[\s\u0000-\u001f]*[=+\-@]/u.test(text) || /^[\t\r\n]/u.test(text))) text = "'" + text;
    return `"${text.replace(/"/g, '""')}"`;
  };
  const toCsv = (rows, variables) => {
    if (!Array.isArray(rows) || rows.length > MAX_ROWS) throw new Error("导出范围超过本次 500 行结果上限。");
    return "\uFEFF" + [variables.map(csvCell).join(","), ...rows.map(row => variables.map(name => csvCell(row[name])).join(","))].join("\r\n");
  };
  const request = async (sessionId, route, payload = {}) => {
    if (!/^session-[a-f0-9-]{36}$/.test(sessionId ?? "")) throw new Error("当前分析缺少有效会话身份。");
    const response = await fetch(`${API}/${route}`, {
      method: "POST", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ ...payload, session_id: sessionId }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "分析操作未完成，请稍后重试。");
    if (result.session_id !== sessionId) throw new Error("分析响应与当前会话不一致。");
    return result;
  };
  const receiptOf = result => {
    const ref = result.evidence_receipt;
    if (!ref || !/^EVD-[a-f0-9]{32}$/.test(ref.receipt_id ?? "") || !/^sha256:[a-f0-9]{64}$/.test(ref.sha256 ?? "")) return null;
    return { receipt_id: ref.receipt_id, sha256: ref.sha256, query_id: ref.query_id ?? result.query_id };
  };
  const fixedBasis = basis => ["FIXED_ACCEPTANCE_DATASET", "PUBLISHED_SNAPSHOT"].includes(basis?.kind)
    && typeof basis.dataset_id === "string" && typeof basis.version === "string" && /^sha256:[a-f0-9]{64}$/.test(basis.sha256 ?? "");
  const renderSources = (parent, sources, { catalog = false } = {}) => {
    if (!Array.isArray(sources) || !sources.length) { parent.append(element("p", "数据来源：回执未提供来源清单。")); return; }
    const list = element("ul");
    for (const source of sources.slice(0, 50)) {
      const parts = [source.label ?? source.display_name ?? source.source_id ?? "未命名来源"];
      if (source.database) parts.push(`数据库 ${source.database}`);
      const tables = Array.isArray(source.tables) ? source.tables.join("、") : source.source_table;
      if (tables) parts.push(`表 ${tables}`);
      if (source.dataset_id) parts.push(`数据集 ${source.dataset_id}`);
      if (catalog) parts.push(`登记状态 ${source.registration_status ?? source.status ?? "未提供"}`);
      list.append(element("li", parts.join(" · ")));
    }
    parent.append(list);
  };
  const renderScope = (parent, result, { compact = false } = {}) => {
    const analysis = result.analysis ?? {};
    const live = analysis.execution_scope === "LIVE_SOURCE_DATABASE";
    const snapshot = analysis.execution_scope === "RELEASE_SNAPSHOT_DATABASE";
    const freshness = analysis.data_freshness && typeof analysis.data_freshness === "object" ? analysis.data_freshness : {};
    const release = result.expected_release ?? result.evidence_receipt ?? {};
    if (compact) {
      if (!live && !snapshot) {
        parent.append(element("p", "数据范围：已核验回执的结果视图；这里只展示已返回结果，未重查源数据库，也未重新聚合。"));
        const scope = result.fact_source ?? result.answer_scope_zh ?? result.scope_warning_zh;
        if (scope) parent.append(element("p", `结果依据：${textValue(scope)}`));
        const mode = result.query_mode ?? analysis.query_mode;
        if (mode) parent.append(element("p", `回执查询模式：${mode}。图表不改变原查询的数据边界。`));
        if (result.upstream_freshness) parent.append(element("p", `上游时效声明：${textValue(result.upstream_freshness)}`));
        const observed = result.observed_at ?? analysis.queried_at;
        if (observed) appendReadingTime(parent, observed, null, "；不据此推断上游记录最新。", "回执观测时间");
        if (result.truncated) parent.append(element("p", "此回执标明结果截断；预览 CSV 只包含本次已返回的行。", "orion-analytics-warning"));
        return;
      }
      const model = analysis.model_version ?? release.release_version ?? result.release_version ?? "未提供";
      const started = analysis.query_started_at ?? freshness.queried_at ?? analysis.queried_at ?? result.queried_at ?? "未提供";
      parent.append(element("p", `业务定义依据：本体模型 ${model}，按已发布实体、字段和关系约束统计。`));
      appendReadingTime(parent, started, analysis.query_finished_at, ` · ${live ? "在线源，只读已提交数据" : snapshot ? "已发布固定快照" : "来源范围未确认"}`);
      const names = [...new Set((analysis.sources ?? analysis.source_tables ?? []).map(source => source.label ?? source.display_name ?? source.source_id).filter(Boolean))];
      parent.append(element("p", `数据来源：${names.join("、") || "未提供"}。${freshness.status === "LIVE_QUERY" && freshness.upstream_requeried === true ? "本次已重查在线源；查询时间不等于源记录更新时间。" : snapshot ? "快照数据不代表上游当前状态。" : "未取得本次在线源时效证明。"}`));
      if (result.truncated) parent.append(element("p", `结果预览已截断至 ${analysis.result_limit ?? MAX_ROWS} 行；筛选和聚合仍覆盖完整授权范围。可用顶部导出重新查询完整结果。`, "orion-analytics-warning"));
      return;
    }
    parent.append(element("p", `业务定义依据：已发布本体的业务模型版本 ${analysis.model_version ?? release.release_version ?? result.release_version ?? "未提供"}。按该版本的实体、字段和关系映射查询与统计；未因此改变本体定义或取得新的业务审批。`));
    parent.append(element("p", `数据读取方式：${live ? "在线数据源查询（只读已提交数据）" : snapshot ? "已发布快照查询（固定数据范围）" : "本次已核验证据"}。`));
    const started = analysis.query_started_at ?? freshness.queried_at ?? analysis.queried_at ?? result.queried_at;
    appendReadingTime(parent, started, analysis.query_finished_at, "。这表示本次查询时间，不表示业务记录最后更新时间。");
    renderSources(parent, analysis.sources ?? analysis.source_tables);
    const states = { LIVE_QUERY: "本次已查询在线源", SNAPSHOT: "发布快照", STALE: "数据已过期", UNKNOWN: "时效未知" };
    const freshnessState = states[freshness.status] ?? freshness.status ?? "时效未知";
    const freshnessNote = element("p", `数据时效：${freshnessState}。${freshness.upstream_requeried === true ? "本次已向源数据库查询。" : "未提供本次重查上游的证明。"}${freshness.observed_at ? `本次可见数据观测时间 ${localTime(freshness.observed_at)}；不等于源记录最后更新时间。` : "源数据自身更新时间未确认。"}${snapshot ? "快照查询成功不代表上游数据最新。" : ""}${freshness.note_zh ?? ""}`);
    if (freshness.observed_at) freshnessNote.title = freshness.observed_at;
    parent.append(freshnessNote);
    if (result.truncated) parent.append(element("p", `当前展示为结果预览，已达到 ${analysis.result_limit ?? MAX_ROWS} 行上限；筛选和聚合仍在完整授权数据范围执行，预览 CSV 不等于全量导出。`));
    if (result.answer_scope_zh || result.scope_warning_zh) parent.append(element("p", result.answer_scope_zh ?? result.scope_warning_zh));
  };
  const fillDraft = async (sessionId, text) => {
    const bridge = window.__ORION_DSH_SESSIONS__;
    if (typeof bridge?.fillDraft !== "function") throw new Error("对话草稿服务暂未就绪。");
    await bridge.fillDraft(sessionId, text);
  };
  const mountExport = (parent, sessionId, reference, { inline = false } = {}) => {
    const section = element(inline ? "section" : "details", null, "orion-analytics-export-task");
    section.append(element(inline ? "strong" : "summary", "重新查询并导出完整结果"));
    section.append(element("p", "导出会按原分析问题重新查询授权数据源，在独立事务中执行；在线数据可能已经变化，不是接着下载原预览的剩余行。完整导出以新任务记录的执行时间为准。"));
    section.append(element("p", "默认最多 100000 行，可调整到 1000000 行；任务还受 100 MiB 和 130 秒执行预算约束。超过预算或执行失败时，不提供部分文件。"));
    const maximum = element("input"); maximum.type = "number"; maximum.min = "1"; maximum.max = "1000000"; maximum.step = "1"; maximum.value = "100000";
    maximum.setAttribute("aria-label", "完整导出最大行数");
    const status = element("p"); status.setAttribute("role", "status");
    const statuses = { QUEUED: "已排队", RUNNING: "正在重新查询与导出", CANCELLING: "正在取消", COMPLETED: "已完成", FAILED: "失败，未发布部分文件", CANCELLED: "已取消", INTERRUPTED: "已中断，未发布部分文件" };
    const active = new Set(["QUEUED", "RUNNING", "CANCELLING"]);
    let job = null;
    let busy = false;
    const draw = () => {
      create.disabled = busy || Boolean(job && active.has(job.status));
      refresh.disabled = busy || !job;
      cancel.disabled = busy || !job || !["QUEUED", "RUNNING"].includes(job.status);
      download.disabled = busy || job?.status !== "COMPLETED";
      maximum.disabled = busy || Boolean(job && active.has(job.status));
      if (!job) return;
      const information = [`任务 ${job.job_id}`, statuses[job.status] ?? "状态未确认"];
      if ((job.row_count ?? job.execution?.row_count) != null) information.push(`${job.row_count ?? job.execution.row_count} 行`);
      if (job.byte_count != null) information.push(`${job.byte_count} 字节`);
      if (job.started_at) information.push(`新查询开始 ${localTime(job.started_at)}`);
      if (job.completed_at ?? job.finished_at) information.push(`完成 ${localTime(job.completed_at ?? job.finished_at)}`);
      if (job.detail) information.push(String(job.detail));
      status.textContent = information.join(" · ");
      status.title = [job.started_at, job.completed_at ?? job.finished_at].filter(Boolean).join(" → ");
    };
    const action = async (name, payload = {}) => {
      if (busy) return;
      busy = true; draw();
      try {
        const response = await request(sessionId, "export", { action: name, ...payload });
        if (!section.isConnected) return;
        const returned = response.job;
        if (!returned || typeof returned.job_id !== "string" || returned.job_id.length > 100
          || (name !== "create" && returned.job_id !== job?.job_id)) throw new Error("导出任务响应与当前任务不一致。");
        job = returned;
        busy = false; draw();
      } catch (error) { if (section.isConnected) { busy = false; draw(); status.textContent = error.message; } }
      finally { busy = false; }
    };
    const create = button("重新查询并导出", async () => {
      const maxRows = Number(maximum.value);
      if (!Number.isInteger(maxRows) || maxRows < 1 || maxRows > 1000000) { status.textContent = "导出最大行数须为 1 到 1000000 的整数。"; return; }
      await action("create", { receipt: reference, max_rows: maxRows });
    });
    const refresh = button("刷新导出状态", async () => { if (job) await action("status", { job_id: job.job_id }); });
    const cancel = button("取消导出任务", async () => { if (job) await action("cancel", { job_id: job.job_id }); });
    const download = button("下载已完成的完整 CSV", async () => {
      if (busy || job?.status !== "COMPLETED") return;
      busy = true; draw();
      try {
        const response = await fetch(`${API}/export`, { method: "POST", credentials: "same-origin", cache: "no-store",
          headers: { "Content-Type": "application/json", Accept: "text/csv" },
          body: JSON.stringify({ session_id: sessionId, action: "download", job_id: job.job_id }) });
        if (!response.ok) {
          const error = await response.json().catch(() => ({}));
          throw new Error(typeof error.detail === "string" ? error.detail : "完整导出文件暂时无法下载。");
        }
        if (!response.headers.get("content-type")?.toLowerCase().startsWith("text/csv")) throw new Error("导出响应不是已验证的 CSV 文件，未保存。");
        const data = await response.blob();
        if (!section.isConnected) return;
        if (data.size > 100 * 1024 * 1024) throw new Error("导出文件超出 100 MiB 下载预算，未保存。");
        if (Number.isInteger(job.byte_count) && data.size !== job.byte_count) throw new Error("导出文件字节数与已完成任务不一致，未保存。");
        const url = URL.createObjectURL(data);
        const link = element("a"); link.href = url; link.download = "analysis-full-export.csv";
        section.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
        busy = false; draw(); status.textContent += " · 完整文件已下载。";
      } catch (error) { if (section.isConnected) { busy = false; draw(); status.textContent = error.message; } }
      finally { busy = false; }
    });
    section.append(maximum, create, refresh, cancel, download, status);
    section.append(element("p", "状态由手动刷新读取；离开页面不会持续轮询。取消后以任务返回的最终状态为准。"));
    parent.append(section); draw();
    return section;
  };
  const drawTable = (host, rows, variables, types) => {
    let page = 0;
    const table = element("table");
    const count = element("span");
    const previous = button("上一页", () => { if (page > 0) { page -= 1; paint(); } });
    const next = button("下一页", () => { if ((page + 1) * 50 < rows.length) { page += 1; paint(); } });
    const paint = () => {
      const head = element("thead");
      const header = element("tr");
      variables.forEach(name => { const column = element("th", name); column.setAttribute("scope", "col"); header.append(column); });
      head.append(header);
      const body = element("tbody");
      rows.slice(page * 50, (page + 1) * 50).forEach(row => {
        const line = element("tr");
        variables.forEach(name => { const cell = element("td", displayValue(row[name], types[name])); cell.title = textValue(row[name]); line.append(cell); });
        body.append(line);
      });
      table.replaceChildren(head, body);
      count.textContent = rows.length ? `显示 ${page * 50 + 1}–${Math.min((page + 1) * 50, rows.length)} / ${rows.length} 条结果` : "本次查询结果为 0 条";
      previous.disabled = page === 0;
      next.disabled = (page + 1) * 50 >= rows.length;
    };
    const scroll = element("div", null, "orion-analytics-table-scroll");
    scroll.style.overflowX = "auto";
    scroll.append(table);
    const pagination = element("div", null, "orion-analytics-pagination");
    if (rows.length > 50) pagination.append(previous, count, next); else pagination.append(count);
    host.append(scroll, pagination);
    paint();
  };
  const drawBar = (host, rows, spec, types) => {
    if (!spec.y.length) { host.append(element("p", "没有可展示的指标字段，请查看结果表。")); return; }
    const visible = rows.slice(0, 40);
    if (rows.length > 40) host.append(element("p", "图表展示前 40 条；其余结果可在结果表与 CSV 中查看。"));
    for (const metric of spec.y) {
      host.append(element("strong", metric));
      const values = visible.map(row => numeric(row[metric])).filter(value => value !== null);
      const minimum = Math.min(0, ...values);
      const maximum = Math.max(1, ...values);
      for (const row of visible) {
        const label = `${spec.x ? displayValue(row[spec.x], types[spec.x]) + " · " : ""}${displayValue(row[metric], types[metric])}`;
        const original = `${spec.x ? textValue(row[spec.x]) + " · " : ""}${textValue(row[metric])}`;
        const line = element("div", null, "orion-analysis-bar");
        const valueLabel = element("span", label); valueLabel.title = original; line.append(valueLabel);
        const value = numeric(row[metric]);
        if (value !== null) {
          const meter = element("meter");
          meter.min = minimum; meter.max = maximum; meter.value = value; meter.title = original;
          line.append(meter);
        }
        host.append(line);
      }
    }
  };
  const svgElement = (tag, attributes = {}) => {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.entries(attributes).forEach(([name, value]) => node.setAttribute(name, String(value)));
    return node;
  };
  const drawLine = (host, rows, spec) => {
    if (!spec.x || !spec.y.length) { host.append(element("p", "趋势图需要横轴和指标字段，请查看结果表。")); return; }
    const visible = rows.slice(0, 100);
    host.append(element("p", "趋势按查询结果顺序展示；未知值保留断点。时间顺序由查询确定。"));
    if (rows.length > 100) host.append(element("p", "趋势图展示前 100 条；全部本次结果见结果表。"));
    for (const metric of spec.y) {
      const values = visible.map(row => numeric(row[metric]));
      const finite = values.filter(value => value !== null);
      host.append(element("strong", metric));
      if (!finite.length) { host.append(element("p", "没有有效数值")); continue; }
      const minimum = Math.min(...finite), maximum = Math.max(...finite);
      const svg = svgElement("svg", { viewBox: "0 0 620 220", role: "img", "aria-label": `${metric}趋势图` });
      svg.style.width = "100%";
      svg.style.maxWidth = "620px";
      const scale = Math.max(Math.abs(minimum), Math.abs(maximum), 1);
      const low = minimum / scale, high = maximum / scale;
      const yAt = value => 185 - (high === low ? 0.5 : (value / scale - low) / (high - low)) * 160;
      const xAt = index => 35 + index / Math.max(1, visible.length - 1) * 550;
      let previous = null;
      visible.forEach((row, index) => {
        const value = values[index];
        if (value === null) { previous = null; return; }
        const x = xAt(index), y = yAt(value);
        if (previous) svg.append(svgElement("line", { x1: previous.x, y1: previous.y, x2: x, y2: y, stroke: "currentColor", "stroke-width": 2 }));
        const point = svgElement("circle", { cx: x, cy: y, r: 3, fill: "currentColor" });
        const title = svgElement("title");
        title.textContent = `${textValue(row[spec.x])} · ${textValue(row[metric])}`;
        point.append(title); svg.append(point);
        previous = { x, y };
      });
      for (const index of [...new Set([0, visible.length - 1])]) {
        if (!visible[index]) continue;
        const label = svgElement("text", { x: xAt(index), y: 211, "text-anchor": index === 0 ? "start" : "end", "font-size": 12, fill: "currentColor" });
        label.textContent = textValue(visible[index][spec.x]);
        svg.append(label);
      }
      host.append(svg);
    }
  };
  const drawKpi = (host, rows, spec, types) => {
    if (rows.length !== 1) { host.append(element("p", "指标卡要求恰好一条汇总结果，请查看结果表或分组图。")); return; }
    for (const metric of spec.y) { const value = element("p", `${metric}：${displayValue(rows[0][metric], types[metric])}`); value.title = textValue(rows[0][metric]); host.append(value); }
  };
  const renderResult = (parent, result, options = {}) => {
    const rows = Array.isArray(result.rows) ? result.rows : [];
    const workspace = options.workspace === true;
    const panel = element("section", null, "orion-analysis-chart" + (workspace ? " orion-analytics-workspace" : ""));
    panel.dataset.orionAnalytics = "true";
    parent.append(panel);
    if (rows.length > MAX_ROWS) { panel.append(element("p", "结果超过页面允许的 500 行，未将超范围数据渲染或导出。")); return panel; }
    const analysis = result.analysis ?? {};
    const types = analysis.row_types ?? result.row_types ?? {};
    const variables = variablesOf(result);
    const chart = analysis.chart ?? {};
    const metricFields = Array.isArray(options.metricFields)
      ? new Set(options.metricFields.filter(name => typeof name === "string" && variables.includes(name))) : null;
    const spec = { kind: labels[chart.kind] ? chart.kind : "table", x: variables.includes(chart.x) ? chart.x : null,
      y: (Array.isArray(chart.y) ? chart.y : []).filter(name => variables.includes(name) && (!metricFields || metricFields.has(name))).slice(0, 4) };
    if (!spec.y.length) spec.y = variables.filter(name => (!metricFields || metricFields.has(name)) && rows.some(row => numeric(row[name]) !== null)).slice(0, 4);
    if (options.tableOnly === true) spec.y = [];
    spec.y = spec.y.filter(name => rows.some(row => numeric(row[name]) !== null));
    if (!spec.x && workspace) spec.x = variables.find(name => !spec.y.includes(name)) ?? null;
    const hasMetric = spec.y.some(name => rows.some(row => numeric(row[name]) !== null));
    const timeValues = spec.x ? rows.map(row => row[spec.x]).filter(value => value != null) : [];
    const temporal = timeValues.length > 0 && timeValues.every(value => typeof value === "string" && /^\d{4}-\d{2}(?:-\d{2}(?:[T ][\d:.+Z-]+)?)?$/.test(value));
    const available = ["table", ...(spec.x && hasMetric ? ["bar"] : []), ...(spec.x && hasMetric && temporal && rows.length > 1 ? ["line"] : []), ...(hasMetric && rows.length === 1 ? ["kpi"] : [])];
    if (!available.includes(spec.kind)) spec.kind = "table";
    const header = element("div", null, "orion-analytics-header");
    header.append(element("strong", analysis.question ?? analysis.request?.question ?? (analysis.engine ? "本体数据分析" : "查询结果")));
    header.append(element("span", `${rows.length} 条${result.truncated ? " · 预览截断" : "结果"}`, "orion-analytics-count"));
    panel.append(header);
    if (!workspace) {
      panel.append(element("p", `本次返回 ${rows.length} 条${result.truncated ? "（结果存在截断）" : ""} · 返回行数受结果上限控制`));
      renderScope(panel, result);
      panel.append(element("p", "分析结果可供核查；查询记忆、报表及回归测试不代表业务口径已通过正式审批。"));
    }
    const controls = element("div", null, "orion-analytics-toolbar");
    const views = element("div", null, "orion-analytics-views");
    views.setAttribute("role", "group"); views.setAttribute("aria-label", "分析结果视图");
    const tools = element("div", null, "orion-analytics-tools");
    const chartHost = element("div", null, "orion-analytics-canvas");
    const viewButtons = new Map();
    const richCharts = options.charts === true && window.__ORION_ANALYTICS_CHARTS__;
    let chartController = null;
    let chartConfig = options.initialChart;
    let selectedMetric = spec.y[0];
    const paint = kind => {
      if (!richCharts && !available.includes(kind)) return;
      if (chartController) chartConfig = chartController.getConfig();
      chartController?.destroy(); chartController = null;
      spec.kind = kind;
      for (const [name, node] of viewButtons) node.setAttribute("aria-pressed", String(name === kind || (richCharts && name === "chart" && kind !== "table")));
      chartHost.replaceChildren();
      if (richCharts && kind !== "table") {
        chartController = richCharts.mount(chartHost, { rows, variables, types,
          ...(metricFields ? { metricFields: [...metricFields] } : {}),
          initialChart: chartConfig ?? { ...spec, y: selectedMetric ? [selectedMetric] : [], kind: kind === "chart" ? "bar" : kind },
          onDrilldown: options.onDrilldown });
        return;
      }
      const plotted = workspace && (kind === "bar" || kind === "line") ? { ...spec, y: [selectedMetric] } : spec;
      if (workspace && (kind === "bar" || kind === "line") && spec.y.length > 1) {
        const selection = element("div", null, "orion-analytics-metric-picker");
        selection.append(element("span", "展示指标"));
        const picker = element("select"); picker.setAttribute("aria-label", "图表指标");
        for (const metric of spec.y) { const item = element("option", metric); item.value = metric; picker.append(item); }
        picker.value = selectedMetric;
        picker.addEventListener("change", () => { if (spec.y.includes(picker.value)) { selectedMetric = picker.value; paint(spec.kind); } });
        selection.append(picker); chartHost.append(selection);
      }
      if (kind === "table") drawTable(chartHost, rows, variables, types);
      if (kind === "bar") drawBar(chartHost, rows, plotted, types);
      if (kind === "line") drawLine(chartHost, rows, plotted);
      if (kind === "kpi") drawKpi(chartHost, rows, spec, types);
      if (kind === "bar" || kind === "line") chartHost.append(element("p", "图形位置按近似数值绘制；显示值和导出值保留原始精度，空值不记为零。"));
    };
    for (const kind of richCharts ? ["table", "chart"] : available) {
      const node = button(kind === "chart" ? "图表" : labels[kind], () => paint(kind));
      viewButtons.set(kind, node); views.append(node);
    }
    const status = options.statusNode ?? element("p", null, "orion-analytics-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const perform = async (node, action) => {
      node.disabled = true;
      status.textContent = "正在核验并处理…";
      try { await action(); } catch (error) { if (panel.isConnected) status.textContent = error instanceof Error ? error.message : "分析操作未完成。"; }
      finally { node.disabled = false; }
    };
    const exportButton = button("导出当前预览 CSV", () => {
      try {
        const content = toCsv(rows, variables);
        const url = URL.createObjectURL(new Blob([content], { type: "text/csv;charset=utf-8" }));
        const link = element("a"); link.href = url; link.download = "analysis-preview.csv";
        panel.append(link); link.click(); link.remove(); URL.revokeObjectURL(url);
        status.textContent = `已导出当前预览 ${rows.length} 行；这是本次已返回的结果，${result.truncated ? "不包含截断范围外的数据；" : ""}空值为空单元格，文本公式已转为普通文本。`;
      } catch (error) { status.textContent = error.message; }
    });
    const sessionId = result.session_id ?? options.session_id;
    const reference = receiptOf(result);
    if (workspace) {
      const exports = element("details", null, "orion-analytics-export-menu");
      exports.append(element("summary", "导出"));
      const exportOptions = element("div", null, "orion-analytics-export-options");
      exportOptions.append(exportButton);
      if (!options.readOnly && reference && sessionId) mountExport(exportOptions, sessionId, reference, { inline: true });
      exports.append(exportOptions); tools.append(exports);
    } else tools.append(exportButton);
    controls.append(views, tools);
    panel.append(controls, chartHost);
    if (!options.statusNode) panel.append(status);
    paint(richCharts && options.initialChart?.kind ? options.initialChart.kind : spec.kind);
    options.onController?.({ resize: () => chartController?.resize(), destroy: () => chartController?.destroy(),
      getView: () => ({ view: spec.kind, visualization: spec.kind !== "table" ? chartController?.getConfig() ?? chartConfig : undefined }) });
    if (workspace) {
      const context = element("div", null, "orion-analytics-context");
      renderScope(context, result, { compact: true }); panel.append(context);
    }
    const externalCapabilities = options.capabilityHosts;
    const more = workspace ? element("details", null, "orion-analytics-more") : panel;
    const capabilityNav = element("div", null, "orion-analytics-capabilities");
    const panes = new Map();
    let activeCapability = null;
    if (workspace && !externalCapabilities) {
      more.open = options.expandedCapabilities === true && !options.readOnly;
      more.append(element("summary", "更多分析能力 · 记忆、基线、口径、目录与追问"));
      capabilityNav.setAttribute("role", "group"); capabilityNav.setAttribute("aria-label", "更多分析能力");
      more.append(capabilityNav); panel.append(more);
    }
    const capability = (key, label) => {
      if (externalCapabilities?.[key]) return externalCapabilities[key];
      if (!workspace) return panel;
      if (panes.has(key)) return panes.get(key).host;
      const host = element("section", null, "orion-analytics-capability-pane"); host.dataset.analysisCapability = key;
      const choose = button(label, async () => {
        activeCapability = key;
        for (const [name, part] of panes) { part.host.hidden = name !== key; part.button.setAttribute("aria-pressed", String(name === key)); }
        await panes.get(key).activate?.();
      });
      if (!activeCapability) activeCapability = key;
      host.hidden = activeCapability !== key; choose.setAttribute("aria-pressed", String(activeCapability === key));
      panes.set(key, { host, button: choose }); capabilityNav.append(choose); more.append(host);
      return host;
    };
    const reuseHost = !options.readOnly && reference && sessionId ? capability("reuse", "查询记忆与基线") : null;
    const detailsHost = capability("details", "原始结果与技术详情");
    const raw = element("details");
    raw.append(element("summary", "本次返回的原始结果明细"));
    let rawLoaded = false;
    raw.addEventListener("toggle", () => {
      if (!raw.open || rawLoaded) return;
      raw.append(element("p", `以下保留本次返回的 ${rows.length} 行及其 JSON 值；不是源数据库的全部明细。`));
      raw.append(element("pre", JSON.stringify({ variables, rows, row_types: types,
        row_terms: result.row_terms ?? analysis.row_terms ?? null }, null, 2)));
      rawLoaded = true;
    });
    detailsHost.append(raw);
    const technical = element("details");
    technical.append(element("summary", "分析技术与回执详情"));
    technical.append(element("p", `${analysis.engine ? `分析执行器 ${analysis.engine} ${analysis.engine_version ?? ""}` : "回执未提供分析引擎声明"} · 查询 ${result.query_id ?? reference?.query_id ?? "未提供"}`));
    if (reference) technical.append(element("p", `证据回执 ${reference.receipt_id} · ${reference.sha256}`));
    if (analysis.mdl_sha256) technical.append(element("p", `分析模型指纹 ${analysis.mdl_sha256}`));
    if (analysis.snapshot_set_id) technical.append(element("p", `快照集 ${analysis.snapshot_set_id}`));
    if (workspace) {
      renderSources(technical, analysis.sources ?? analysis.source_tables);
      if (result.answer_scope_zh || result.scope_warning_zh) technical.append(element("p", result.answer_scope_zh ?? result.scope_warning_zh));
      if (analysis.data_freshness) technical.append(element("pre", JSON.stringify(analysis.data_freshness, null, 2)));
    }
    detailsHost.append(technical);
    if (options.readOnly || !reference || !sessionId) return panel;
    if (!workspace) mountExport(panel, sessionId, reference);
    const titleInput = element("input");
    titleInput.type = "text"; titleInput.maxLength = 200;
    titleInput.value = (analysis.request?.question ?? analysis.question ?? "分析报告").slice(0, 200);
    titleInput.setAttribute("aria-label", "保存的分析名称");
    const actions = element("div", null, "orion-analytics-asset-actions");
    actions.append(element("label", "报告与资产名称"), titleInput);
    if (workspace) actions.append(element("p", "查询记忆、口径说明和观察基线由你确认保存；不会修改正式本体或替代业务审批。"));
    const save = (action, title, success, confirmed = false) => {
      const node = button(title, () => perform(node, async () => {
        const saved = await request(sessionId, "assets", { action, title: titleInput.value, question: analysis.request?.question ?? analysis.question ?? "",
          receipts: [reference], confirmed, ...(action === "report" ? { layout: { blocks: [{ receipt_id: reference.receipt_id, view: spec.kind,
            ...(workspace && ["bar", "line"].includes(spec.kind) ? { metric: selectedMetric } : {}),
            ...(chartController ? { visualization: chartController.getConfig() } : {}) }] } } : {}) });
        if (panel.isConnected) status.textContent = `${success} · ${saved.asset?.asset_id ?? ""}。与当前项目、发布版本和会话绑定。`;
      }));
      (workspace && action === "report" ? tools : actions).append(node);
    };
    save("report", "保存报告", "报告已保存");
    if (analysis.request && result.truncated !== true) {
      save("remember", "确认记住这次查询", "用户确认的查询记忆已保存；未改变正式业务口径", true);
      if (fixedBasis(analysis.evaluation_basis)) save("evaluation", "确认设为回归基线", "当前固定数据结果已保存为回归基线", true);
      else {
        save("evaluation", "确认保存本次观察基线", "本次结果已作为观察基线保存；数据变化不能直接认定为模型回归失败", true);
        actions.append(element("p", "正式回归验收需要固定数据集和版本指纹。在线数据会变化，当前结果只可用于观察比较。"));
      }
    } else if (result.truncated === true) actions.append(element("p", "当前是截断预览，可保存报告；完整查询记忆和结果基线需先取得未截断结果。"));
    (reuseHost ?? panel).append(actions);
    const knowledge = element(workspace ? "section" : "details");
    knowledge.append(element(workspace ? "strong" : "summary", "保存本次分析的口径说明"));
    const definition = element("textarea"); definition.maxLength = 8000;
    definition.setAttribute("aria-label", "口径说明"); definition.placeholder = "记录已核对的含义、计算范围、未知值处理和使用限制。";
    const saveKnowledge = button("确认保存口径说明", () => perform(saveKnowledge, async () => {
      if (!definition.value?.trim()) throw new Error("请填写要保存的口径说明。");
      const saved = await request(sessionId, "assets", { action: "knowledge", title: titleInput.value,
        definition: definition.value.trim(), receipts: [reference], confirmed: true });
      if (panel.isConnected) status.textContent = `口径说明已保存 · ${saved.asset.asset_id}。这是用户确认的说明，未修改正式业务定义。`;
    }));
    knowledge.append(definition, saveKnowledge);
    const followup = element("div");
    const question = element("textarea"); question.maxLength = 1000;
    question.setAttribute("aria-label", "继续追问"); question.placeholder = "继续追问，例如：按月份比较，或查看某个分组的明细。";
    const ask = button("将追问填入对话草稿", () => perform(ask, async () => {
      if (!question.value?.trim()) throw new Error("请填写要继续追问的问题。");
      const prior = analysis.request?.question ?? analysis.question ?? "本次分析";
      await fillDraft(sessionId, `基于上一条分析「${prior}」，${question.value.trim()}\n请沿用当前发布的业务模型和授权数据源，并关联分析回执 ${reference.receipt_id}（查询 ${reference.query_id}）。`);
      if (panel.isConnected) status.textContent = "追问已填入现有对话草稿，可检查后发送。";
      options.onDraft?.();
    }));
    followup.append(question, ask);
    capability("knowledge", "口径说明").append(knowledge);
    const followupHost = capability("followup", "继续追问"); followupHost.append(followup);
    if (!options.skipCatalog) mountWorkspace(capability("catalog", "分析目录与资产"), { session_id: sessionId, currentResult: result,
      inline: workspace, onReady: workspace ? load => { panes.get("catalog").activate = load; } : undefined });
    return panel;
  };
  const mountWorkspace = (parent, options) => {
    const section = element(options.inline ? "section" : "details");
    const heading = element(options.inline ? "strong" : "summary", "分析目录与已保存资产");
    let headerRow = null;
    if (options.inline) {
      headerRow = element("div", null, "orion-analytics-catalog-header");
      headerRow.append(heading); section.append(headerRow);
      section.dataset.catalogHeader = "true";
    } else section.append(heading);
    const body = element("div"); section.append(body); parent.append(section);
    const sessionId = options.session_id;
    let busy = false;
    let loaded = false;
    const selectedReports = new Map();
    const viewControllers = new Set();
    section.destroy = () => { viewControllers.forEach(controller => controller.destroy?.()); viewControllers.clear(); };
    let assetKind = "all";
    const paintAssets = async () => {
      const response = await request(sessionId, "assets", { action: "list" });
      if (!section.isConnected) return;
      const list = element("div");
      list.append(element("strong", "当前会话与发布版本的分析资产"));
      const assets = Array.isArray(response.assets) ? response.assets : [];
      const filters = element("div", null, "orion-analytics-asset-filter");
      const kindSelect = element("select"); kindSelect.setAttribute("aria-label", "资产类型");
      for (const [value, title] of [["all", "全部资产"], ["report", "报表看板"], ["memory", "问题与 SQL 经验"], ["knowledge", "口径说明"], ["evaluation", "观察与回归基线"]]) {
        const option = element("option", title); option.value = value; kindSelect.append(option);
      }
      kindSelect.value = assetKind;
      const items = [];
      kindSelect.addEventListener("change", () => { assetKind = kindSelect.value; items.forEach(item => { item.hidden = assetKind !== "all" && item.dataset.assetKind !== assetKind; }); });
      filters.append(kindSelect); list.append(filters);
      const boardName = element("input"); boardName.type = "text"; boardName.maxLength = 200;
      boardName.setAttribute("aria-label", "组合看板名称"); boardName.placeholder = "选中下方报告，组成多图看板";
      const boardStatus = element("p"); boardStatus.setAttribute("role", "status");
      const compose = button("保存组合看板", async () => {
        compose.disabled = true;
        try {
          const refs = new Map(), blocks = [];
          for (const asset of selectedReports.values()) {
            for (const ref of asset.payload?.receipt_refs ?? []) refs.set(ref.receipt_id, { receipt_id: ref.receipt_id, sha256: ref.sha256, query_id: ref.query_id });
            for (const block of asset.payload?.layout?.blocks ?? []) blocks.push({ ...block });
          }
          if (!refs.size) throw new Error("请先选择至少一份真实报告。");
          if (refs.size > 50) throw new Error("看板最多关联 50 份查询结果。");
          if (blocks.length > 50) throw new Error("看板最多放置 50 个结果卡片。");
          if (!boardName.value?.trim()) throw new Error("请填写组合看板名称。");
          const saved = await request(sessionId, "assets", { action: "report", title: boardName.value.trim(), receipts: [...refs.values()], layout: { kind: "grid", blocks } });
          if (section.isConnected) boardStatus.textContent = `组合看板已保存 · ${saved.asset.asset_id}，刷新列表后可查看。`;
        } catch (error) { if (section.isConnected) boardStatus.textContent = error.message; }
        finally { compose.disabled = false; }
      });
      if (options.inline) { filters.append(boardName, compose); list.append(boardStatus); }
      if (!assets.length) list.append(element("p", "暂无已保存资产。"));
      for (const asset of assets.slice(0, 100)) {
        const item = element("section");
        item.className = "orion-analytics-saved-asset";
        item.dataset.assetKind = asset.kind;
        item.hidden = assetKind !== "all" && asset.kind !== assetKind; items.push(item);
        const payload = asset.payload ?? {};
        const kind = { report: "报告", evaluation: fixedBasis(payload.evaluation_basis) ? "回归基线" : "观察基线", memory: "查询记忆", knowledge: "口径说明" }[asset.kind] ?? asset.kind;
        item.append(element("strong", `${kind} · ${payload.title ?? payload.name ?? payload.question ?? asset.asset_id}`));
        if (options.inline && asset.kind === "report") {
          const choice = element("input"); choice.type = "checkbox"; choice.checked = selectedReports.has(asset.asset_id);
          choice.setAttribute("aria-label", `加入看板 ${payload.title ?? asset.asset_id}`);
          choice.addEventListener("change", () => { if (choice.checked) selectedReports.set(asset.asset_id, asset); else selectedReports.delete(asset.asset_id); });
          const label = element("label", "加入组合看板"); label.prepend?.(choice);
          if (!label.prepend) label.append(choice);
          item.append(label);
        }
        const output = element("div");
        let itemControllers = [];
        const read = button("查看", async () => {
          read.disabled = true;
          try {
            const response = await request(sessionId, "assets", { action: "get", asset_id: asset.asset_id });
            if (!section.isConnected) return;
            itemControllers.forEach(controller => { controller.destroy?.(); viewControllers.delete(controller); }); itemControllers = [];
            output.replaceChildren();
            if (asset.kind === "report") {
              const records = response.results ?? [];
              if (!records.length) output.append(element("p", "报告没有可回读的已验证结果。"));
              const board = element("div", null, "orion-analytics-report-board"); output.append(board);
              const blocks = [];
              const savedBlocks = response.asset?.payload?.layout?.blocks;
              const configured = Array.isArray(savedBlocks) && savedBlocks.length
                ? savedBlocks.map(block => ({ block, record: records.find(record => record.evidence_receipt?.receipt_id === block.receipt_id) }))
                : records.map(record => ({ record, block: null }));
              if (configured.some(part => !part.record)) output.append(element("p", "部分布局没有对应的已核验回执，未展示这些卡片。"));
              for (const { record, block } of configured.filter(part => part.record)) {
                const metric = variablesOf(record).includes(block?.metric) ? block.metric : null;
                const card = element("section", null, "orion-analytics-report-card"); board.append(card);
                const entry = { card, record, layout: { ...block, receipt_id: record.evidence_receipt?.receipt_id, width: block?.width === "full" ? "full" : "half" } };
                if (block?.width !== "full" && block?.width !== "half" && configured.filter(part => part.record).length === 1) entry.layout.width = "full";
                blocks.push(entry); card.dataset.width = entry.layout.width;
                const controls = element("div", null, "orion-analytics-report-layout"); card.append(controls);
                const reorder = direction => {
                  const index = blocks.indexOf(entry), next = index + direction;
                  if (next < 0 || next >= blocks.length) return;
                  [blocks[index], blocks[next]] = [blocks[next], blocks[index]];
                  blocks.forEach(part => board.append(part.card));
                  blocks.forEach(part => part.controller?.resize?.());
                };
                controls.append(button("前移", () => reorder(-1)), button("后移", () => reorder(1)),
                  button("切换卡片宽度", () => {
                    entry.layout.width = entry.layout.width === "full" ? "half" : "full";
                    card.dataset.width = entry.layout.width; entry.controller?.resize?.();
                  }),
                  button("从当前布局移除", () => { blocks.splice(blocks.indexOf(entry), 1); entry.controller?.destroy?.(); if (entry.controller) viewControllers.delete(entry.controller); card.remove(); if (blocks.length === 1) { blocks[0].layout.width = "full"; blocks[0].card.dataset.width = "full"; blocks[0].controller?.resize?.(); } }));
                renderResult(card, { ...record, analysis: { ...record.analysis, chart: { ...record.analysis?.chart,
                  ...(labels[block?.view] ? { kind: block.view } : {}), ...(metric ? { y: [metric] } : {}) } }, session_id: sessionId }, { readOnly: true, workspace: options.inline, charts: options.charts,
                  onController: controller => { entry.controller = controller; itemControllers.push(controller); viewControllers.add(controller); },
                  ...(block?.visualization ? { initialChart: block.visualization } : {}) });
              }
              if (records.length) {
                const boardFeedback = element("p"); boardFeedback.setAttribute("role", "status");
                const saveLayout = button("保存布局为新报告", async () => {
                  saveLayout.disabled = true;
                  try {
                    if (!blocks.length) throw new Error("至少保留一个结果卡片。");
                    const references = blocks.map(part => receiptOf(part.record));
                    if (references.some(ref => !ref)) throw new Error("布局中存在无法核验的结果回执。");
                    const receipts = [...new Map(references.map(ref => [ref.receipt_id, ref])).values()];
                    const saved = await request(sessionId, "assets", { action: "report", title: `${payload.title ?? "报告"} · 新布局`.slice(0, 200), receipts,
                      layout: { kind: "grid", blocks: blocks.map(part => ({ ...part.layout, ...part.controller?.getView() })) } });
                    if (section.isConnected) boardFeedback.textContent = `新布局已保存 · ${saved.asset.asset_id}。原报告保留。`;
                  } catch (error) { if (section.isConnected) boardFeedback.textContent = error.message; }
                  finally { saveLayout.disabled = false; }
                }); output.append(saveLayout, boardFeedback);
              }
            } else {
              output.append(element("p", response.asset?.payload?.definition ?? response.asset?.payload?.question ?? ""));
              if (asset.kind === "evaluation") {
                output.append(element("p", `保存的结果 ${response.asset?.payload?.expected_row_count} 条 · ${response.asset?.payload?.expected_result_sha256}`));
                output.append(element("p", fixedBasis(response.asset?.payload?.evaluation_basis) ? "具有固定验收数据身份。" : "尚未建立相同固定数据的验收条件；动态数据差异不能直接视为模型回归失败。"));
              }
              for (const record of response.results ?? []) renderResult(output, { ...record, session_id: sessionId }, { readOnly: true });
              output.append(element("p", "此资产不等于正式业务审批。"));
            }
          } catch (error) { if (section.isConnected) output.textContent = error.message; }
          finally { read.disabled = false; }
        });
        item.append(read);
        if (asset.kind === "evaluation") {
          const evaluate = button("重新查询数据库并对比基线", async () => {
            evaluate.disabled = true; output.textContent = "正在读取当前发布数据并重新执行…";
            try {
              const response = await request(sessionId, "assets", { action: "evaluate", asset_id: asset.asset_id });
              if (!section.isConnected) return;
              const evaluation = response.evaluation;
              const statuses = { PASSED: "固定数据回归通过", FAILED: "固定数据结果存在差异", OBSERVATION_MATCH: "两次观察结果一致", DATA_CHANGED: "在线数据观察结果发生变化", INCOMPARABLE: "数据条件不可比较" };
              output.replaceChildren(element("p", `${statuses[evaluation.status] ?? "比较状态未确认"} · 实际 ${evaluation.actual_row_count} 条 / 期望 ${evaluation.expected_row_count} 条 · 结果哈希${evaluation.hash_matches ? "一致" : "不一致"}`));
              output.append(element("p", evaluation.interpretation_zh ?? "结果哈希差异本身不能证明模型回归失败；正式回归需要相同固定验收数据。"));
              if (response.execution) renderResult(output, response.execution, { readOnly: true });
            } catch (error) { if (section.isConnected) output.textContent = error.message; }
            finally { evaluate.disabled = false; }
          });
          item.append(evaluate);
        }
        if (asset.kind === "memory" && payload.question) {
          item.append(button("将问题填入对话草稿", async () => {
            try {
              await fillDraft(sessionId, payload.question);
              output.textContent = "问题已填入草稿，可检查后发送。再次执行时会重新校验版本和权限。";
            } catch (error) { output.textContent = error.message; }
          }));
        }
        item.append(output); list.append(item);
      }
      if (assets.length > 100) list.append(element("p", "页面先展示最近 100 项资产。"));
      const old = body.querySelector?.('[data-analysis-asset-list="true"]');
      old?.remove(); list.dataset.analysisAssetList = "true"; body.append(list);
    };
    const load = async () => {
      if ((!options.inline && !section.open) || busy || loaded) return;
      busy = true; body.textContent = "正在读取当前发布版本的分析目录…";
      try {
        const catalog = options.assetsOnly ? null : await request(sessionId, "catalog");
        if (!section.isConnected) return;
        body.replaceChildren();
        if (catalog) {
        body.append(element("strong", "工程已登记的数据来源"));
        renderSources(body, catalog.sources, { catalog: true });
        body.append(element("p", "数据库连接沿用 Chat2DB 中的配置，通过工程现有的“选择数据库”入口绑定范围。分析页面无需重复配置账号或密码。"));
        if (catalog.scope_warning_zh) body.append(element("p", catalog.scope_warning_zh));
        for (const [key, title] of [["models", "可查询模型"], ["cubes", "指标立方体"], ["relationships", "模型关系"], ["views", "已定义视图"], ["knowledge", "语义说明"], ["omitted", "当前未接入或需补充的语义"]]) {
          const values = Array.isArray(catalog[key]) ? catalog[key] : catalog[key] && typeof catalog[key] === "object" ? Object.entries(catalog[key]).map(([name, value]) => ({ name, value })) : [];
          const part = element("details"); part.append(element("summary", `${title}（${values.length}）`));
          for (const value of values.slice(0, 100)) {
            if (typeof value === "string") { part.append(element("p", value)); continue; }
            const name = value.name ?? value.target ?? value.mapping_id ?? "";
            const description = value.properties?.description ?? value.description ?? value.reason ?? "";
            part.append(element("strong", name));
            if (description) part.append(element("p", description));
            if (value.columns) part.append(element("p", `字段：${value.columns.map(column => `${column.name}（${column.type}）`).join("、")}`));
            if (value.measures) part.append(element("p", `指标：${value.measures.map(metric => metric.name).join("、")}`));
            if (value.dimensions) part.append(element("p", `维度：${value.dimensions.map(dimension => dimension.name).join("、")}`));
            if (value.models) part.append(element("p", `关联：${value.models.join(" ↔ ")}`));
            if (value.value != null) part.append(element("p", typeof value.value === "string" ? value.value : JSON.stringify(value.value)));
          }
          body.append(part);
        }
        }
        const searchBox = element("input"); searchBox.type = "search"; searchBox.maxLength = 1000;
        searchBox.setAttribute("aria-label", "检索查询记忆和口径说明");
        searchBox.placeholder = "输入业务问题，查找已确认的查询与口径说明";
        const matches = element("div");
        const search = button("检索已确认的分析经验", async () => {
          search.disabled = true;
          try {
            if (!searchBox.value?.trim()) throw new Error("请输入要检索的业务问题。");
            const response = await request(sessionId, "assets", { action: "search", question: searchBox.value.trim() });
            if (!section.isConnected) return;
            matches.replaceChildren(element("p", "采用中文词组与英文词面匹配；匹配依据会列出，不代表语义或业务口径相同。"));
            const found = [...(response.memories ?? []), ...(response.knowledge ?? [])];
            if (!found.length) matches.append(element("p", "当前会话和发布版本没有匹配的已确认经验。"));
            for (const match of found.slice(0, 50)) {
              const value = match.asset?.payload ?? {};
              matches.append(element("strong", value.title ?? value.question ?? "分析经验"));
              if (value.definition) matches.append(element("p", value.definition));
              matches.append(element("p", `匹配词：${(match.matched_terms ?? []).join("、") || "未提供"}。已确认经验仍需按当前版本重新核验。`));
              if (value.question) {
                const use = button("将此问题填入对话草稿", async () => {
                  try { await fillDraft(sessionId, value.question); use.textContent = "已填入草稿，可检查后发送"; }
                  catch (error) { use.textContent = error.message; }
                });
                matches.append(use);
              }
            }
          } catch (error) { if (section.isConnected) matches.textContent = error.message; }
          finally { search.disabled = false; }
        });
        const refresh = button("刷新已保存资产", () => paintAssets().catch(error => { refresh.textContent = error.message; }));
        const searchRow = element("div", null, "orion-analytics-memory-search");
        searchRow.append(searchBox, search, refresh);
        matches.className = "orion-analytics-memory-matches";
        body.append(searchRow, matches);
        await paintAssets(); loaded = true;
      } catch (error) { if (section.isConnected) body.textContent = error.message; }
      finally { busy = false; }
    };
    if (options.inline) headerRow.append(button("重新加载目录与资产", async () => { loaded = false; await load(); }));
    else section.addEventListener("toggle", load);
    options.onReady?.(load);
    return section;
  };
  window.__ORION_ANALYTICS_PANEL__ = { renderResult, mountWorkspace, toCsv, request };
})();
