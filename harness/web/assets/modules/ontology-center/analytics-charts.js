(() => {
  if (window.__ORION_ANALYTICS_CHARTS__) return;
  const KINDS = [
    ["bar", "柱状图"], ["horizontal_bar", "条形图"], ["grouped_bar", "分组柱状"],
    ["stacked_bar", "堆叠柱状"], ["line", "折线 / 趋势"], ["multiline", "多线趋势"], ["area", "面积图"],
    ["pie", "饼图"], ["donut", "环形图"], ["scatter", "散点图"], ["kpi", "指标卡"],
  ];
  const ALIASES = { column: "bar", horizontal: "horizontal_bar", grouped: "grouped_bar", stacked: "stacked_bar",
    "grouped-bar": "grouped_bar", "stacked-bar": "stacked_bar", "multi-line": "multiline",
    big_number: "kpi", number: "kpi", doughnut: "donut", trend: "line" };
  const PALETTE = ["#3277a8", "#29968c", "#a37742", "#7267a8", "#ba697c", "#6c9360", "#657f94", "#b58730"];
  const numericType = type => typeof type === "string" && /^(?:decimal(?:128|256)?|numeric|number|double|float(?:16|32|64)?|u?int(?:8|16|32|64)?)(?:\b|\()/i.test(type.trim());
  const identityName = name => /(?:^id$|(?:^|[_\s])(?:id|uuid|key)$|(?:Id|ID|Uuid|UUID)$|编号|编码|标识)/.test(name);
  const plain = value => value == null ? "未知 / 缺失" : typeof value === "object" ? JSON.stringify(value) : String(value);
  const number = value => {
    if (typeof value !== "number" && (typeof value !== "string" || !/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$/i.test(value))) return null;
    const converted = Number(value);
    return Number.isFinite(converted) ? converted : null;
  };
  const exact = (value, type) => {
    const original = plain(value);
    if (!numericType(type) || typeof value !== "string") return original;
    const match = /^([+-]?\d+)\.(\d+)$/.exec(value);
    if (!match) return original;
    const fraction = match[2].replace(/0+$/, "");
    return match[1] + (fraction ? "." + fraction : "");
  };
  const key = value => JSON.stringify(value == null ? ["null", null] : [typeof value, value]);
  const clip = (value, length = 48) => { const text = plain(value); return text.length > length ? text.slice(0, length) + "…" : text; };
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text != null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const button = (label, handler) => {
    const node = element("button", label); node.type = "button";
    node.addEventListener("click", handler); return node;
  };
  const isTime = (field, rows, types) => {
    if (typeof types[field] === "string" && /^(?:date|timestamp|datetime)/i.test(types[field])) return true;
    const values = rows.map(row => row[field]).filter(value => value != null);
    return values.length > 0 && values.every(value => typeof value === "string"
      && /^\d{4}-\d{2}(?:-\d{2}(?:[T ][\d:.+Z-]+)?)?$/.test(value) && Number.isFinite(Date.parse(value)));
  };
  const mount = (parent, input = {}) => {
    const root = element("section", null, "orion-charts"); parent.append(root);
    const error = element("p", null, "orion-charts-notice"); error.setAttribute("role", "status"); error.setAttribute("aria-live", "polite");
    if (!Array.isArray(input.rows) || input.rows.length > 500 || input.rows.some(row => !row || typeof row !== "object" || Array.isArray(row))) {
      error.textContent = "图表需要至多 500 行已经返回的记录；未拉取额外来源数据。"; root.append(error);
      return { getConfig: () => null, getState: () => ({ status: "INVALID_INPUT", reason: error.textContent }), resize() {}, destroy: () => root.remove() };
    }
    // Copies protect the receipt's row objects from display adapters. No rows are aggregated.
    const rows = input.rows.map(row => ({ ...row }));
    const fields = [...new Set(Array.isArray(input.variables) ? input.variables : rows.flatMap(row => Object.keys(row)))].filter(name => typeof name === "string").slice(0, 100);
    const types = input.types && typeof input.types === "object" ? { ...input.types } : {};
    const declared = Array.isArray(input.metricFields) ? new Set(input.metricFields) : null;
    const metrics = fields.filter(field => declared ? declared.has(field) : numericType(types[field]));
    const dimensions = fields.filter(field => rows.every(row => row[field] == null || typeof row[field] !== "object"));
    const initial = input.initialChart ?? {};
    const requestedKind = ALIASES[initial.kind] ?? initial.kind;
    const initialY = Array.isArray(initial.y) ? initial.y : [];
    let config = {
      kind: KINDS.some(([kind]) => kind === requestedKind) ? requestedKind : "bar",
      x: dimensions.includes(initial.x) ? initial.x : dimensions.find(field => !metrics.includes(field)) ?? dimensions[0] ?? null,
      y: initialY.length ? [...new Set(initialY)].filter(field => metrics.includes(field)) : metrics.filter(field => !identityName(field)).slice(0, 1),
      series: dimensions.includes(initial.series) ? initial.series : null,
      stackConfirmed: initial.stackConfirmed === true,
    };
    let chart = null, observer = null, disposed = false, state = null;
    const gallery = element("div", null, "orion-charts-gallery"); gallery.setAttribute("role", "group"); gallery.setAttribute("aria-label", "图表类型");
    const kindButtons = new Map();
    for (const [kind, label] of KINDS) {
      const node = button(label, () => setConfig({ kind }));
      kindButtons.set(kind, node); gallery.append(node);
    }
    const controls = element("div", null, "orion-charts-fields");
    const dimensionLabel = element("label", "维度 / 横轴");
    const dimension = element("select"); dimension.setAttribute("aria-label", "图表维度或横轴");
    const seriesLabel = element("label", "分组系列");
    const series = element("select"); series.setAttribute("aria-label", "图表分组系列");
    const addOption = (select, value, label) => { const node = element("option", label); node.value = value; select.append(node); };
    addOption(dimension, "", "选择字段"); addOption(series, "", "不分组，逐行展示");
    for (const field of dimensions) {
      const type = types[field] ? ` · ${types[field]}` : "";
      addOption(dimension, field, field + type); addOption(series, field, field + type);
    }
    dimension.addEventListener("change", () => setConfig({ x: dimension.value || null }));
    series.addEventListener("change", () => setConfig({ series: series.value || null }));
    dimensionLabel.append(dimension); seriesLabel.append(series);
    const metricPicker = element("details", null, "orion-charts-metric-picker");
    const metricSummary = element("summary", "选择指标");
    const metricList = element("div", null, "orion-charts-metric-list");
    const metricBoxes = new Map();
    if (!metrics.length) metricList.append(element("p", "没有得到类型或白名单确认的数值字段。数字标识与未声明类型的文本不会自动作为指标。"));
    for (const field of metrics) {
      const label = element("label"); const box = element("input"); box.type = "checkbox";
      box.setAttribute("aria-label", `指标 ${field}`);
      box.addEventListener("change", () => setConfig({ y: [...metricBoxes].filter(([, input]) => input.checked).map(([name]) => name) }));
      label.append(box, element("span", `${field}${types[field] ? ` · ${types[field]}` : " · 已授权数值字段"}${identityName(field) ? "（标识字段，需明确选择）" : ""}`));
      metricBoxes.set(field, box); metricList.append(label);
    }
    metricPicker.append(metricSummary, metricList);
    const stackLabel = element("label", null, "orion-charts-stack-confirm");
    const stackConfirmation = element("input"); stackConfirmation.type = "checkbox";
    stackConfirmation.setAttribute("aria-label", "确认所选指标或分组可相加");
    stackConfirmation.addEventListener("change", () => setConfig({ stackConfirmed: stackConfirmation.checked }));
    stackLabel.append(stackConfirmation, element("span", "我确认所选指标或分组的口径、单位允许相加"));
    controls.append(dimensionLabel, metricPicker, seriesLabel, stackLabel);
    const toolbar = element("div", null, "orion-charts-tools");
    const svgUrl = () => {
      if (!chart || state?.status !== "READY") throw new Error("请先完成一个可展示的图表。");
      const url = chart.getDataURL({ type: "svg", pixelRatio: 1, excludeComponents: ["toolbox"] });
      if (typeof url !== "string" || !url.startsWith("data:image/svg+xml")) throw new Error("本地图表引擎没有返回 SVG 图像。");
      return url;
    };
    const downloadUrl = (url, name) => {
      const separator = url.indexOf(","), metadata = url.slice(5, separator), content = url.slice(separator + 1);
      const mime = metadata.split(";")[0].toLowerCase();
      if (!url.startsWith("data:") || separator < 0 || !["image/svg+xml", "image/png"].includes(mime)) throw new Error("图像格式无效，未发起下载。");
      // Decode bytes before creating the Blob: base64 is binary, while an SVG URL
      // can mix percent-encoded UTF-8 bytes with literal Unicode characters.
      let parts;
      if (/;base64$/i.test(metadata)) parts = [Uint8Array.from(atob(content), character => character.charCodeAt(0))];
      else {
        if (/%(?![\da-f]{2})/i.test(content)) throw new Error("图像编码无效，未发起下载。");
        parts = content.split(/(%[\da-f]{2})/gi).map(part => /^%[\da-f]{2}$/i.test(part)
          ? new Uint8Array([parseInt(part.slice(1), 16)]) : new TextEncoder().encode(part));
      }
      const objectUrl = URL.createObjectURL(new Blob(parts, { type: metadata.replace(/;base64$/i, "") }));
      const link = element("a"); link.href = objectUrl; link.download = name; root.append(link);
      try { link.click(); }
      finally {
        link.remove();
        // Keep the URL alive across dialog destruction while the browser takes
        // ownership. This bounded cleanup does not retain chart/DOM state.
        setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      }
    };
    const download = button("下载 SVG 图像", () => {
      try {
        downloadUrl(svgUrl(), "analysis-chart.svg");
        error.textContent = "已发起下载当前图表视窗的 SVG；数据来源与回执未改变。";
      } catch (failure) { error.textContent = failure.message ?? "图像导出失败，可重试。"; }
    });
    const png = button("下载 PNG 图像", async () => {
      png.disabled = true;
      try {
        const url = svgUrl();
        if (typeof Image !== "function") throw new Error("当前环境不支持本地图像转换，可下载 SVG。");
        const rendered = await new Promise((resolve, reject) => { const image = new Image(); image.onload = () => resolve(image); image.onerror = () => reject(new Error("PNG 图像转换失败，可重试或下载 SVG。")); image.src = url; });
        if (disposed) return;
        const bitmap = document.createElement("canvas"); bitmap.width = Math.ceil(chart.getWidth() * 2); bitmap.height = Math.ceil(chart.getHeight() * 2);
        const context = bitmap.getContext("2d"); if (!context) throw new Error("当前环境不能生成 PNG，可下载 SVG。");
        context.drawImage(rendered, 0, 0, bitmap.width, bitmap.height);
        const result = bitmap.toDataURL("image/png");
        if (!result.startsWith("data:image/png")) throw new Error("图像转换未返回 PNG，可下载 SVG。");
        downloadUrl(result, "analysis-chart.png"); error.textContent = "已发起下载当前图表视窗的 PNG；数据来源与回执未改变。";
      } catch (failure) { if (!disposed) error.textContent = failure.message ?? "PNG 导出失败，可重试。"; }
      finally { if (!disposed) png.disabled = state?.status !== "READY"; }
    });
    toolbar.append(element("span", "展示转换 · 不重新查询或改变业务指标"), download, png);
    const canvas = element("div", null, "orion-charts-canvas"); canvas.setAttribute("role", "img");
    const note = element("p", null, "orion-charts-footnote");
    const selection = element("details", null, "orion-charts-selection"); selection.append(element("summary", "选中数据"));
    const selectionBody = element("div"); selectionBody.append(element("p", "点选柱、线上的数据点、扇区或散点，查看其原始行；可交由对话草稿继续追问。")); selection.append(selectionBody);
    root.append(gallery, controls, toolbar, error, canvas, note, selection);

    const validate = (candidate) => {
      const notices = [];
      if (!rows.length) return { reason: "本次返回 0 行，当前没有可绘制的数据。", notices };
      if (!candidate.y.length) return { reason: "请选择已确认类型的数值指标；数字 ID 不会默认作为统计指标。", notices };
      if (candidate.y.length > 8) return { reason: "一次最多对比 8 个指标，请减少所选指标；原始结果不变。", notices };
      if (candidate.y.some(field => !metrics.includes(field))) return { reason: "所选指标不在允许的数值字段范围内。", notices };
      if (candidate.y.some(field => rows.some(row => row[field] != null && number(row[field]) == null))) return { reason: "指标含非数值或超过图形数值范围的内容；不能将其记为零。请查看原始结果。", notices };
      if (candidate.kind === "kpi") {
        return { reason: rows.length === 1 ? null : "指标卡需要恰好一条已返回汇总结果；展示层不会自动求和。", notices };
      }
      if (!dimensions.includes(candidate.x)) return { reason: "请选择一个可作为维度或横轴的简单字段。", notices };
      if (candidate.series === candidate.x) return { reason: "分组系列需与维度使用不同字段。", notices };
      if (candidate.series && candidate.y.length !== 1) return { reason: "选择分组系列时请只选一个指标，避免混合字段含义。", notices };
      if (["pie", "donut", "scatter"].includes(candidate.kind) && candidate.y.length !== 1) return { reason: "该图需要恰好一个纵轴或数值指标，请调整指标选择。", notices };
      if (["pie", "donut", "scatter"].includes(candidate.kind) && candidate.series) return { reason: "该图不使用分组系列，请先选择“不分组”。", notices };
      if (candidate.kind === "scatter") {
        if (!metrics.includes(candidate.x)) return { reason: "散点图横轴也需要一个允许的数值字段；不会把标识文本转换为数值。", notices };
        if (rows.some(row => row[candidate.x] != null && number(row[candidate.x]) == null)) return { reason: "横轴含不可绘制的数值，请核对原始结果。", notices };
        const valid = rows.filter(row => row[candidate.x] != null && row[candidate.y[0]] != null).length;
        if (!valid) return { reason: "散点图没有同时具备横轴与纵轴数值的行。", notices };
        if (valid < rows.length) notices.push(`${rows.length - valid} 行有缺失坐标，未画成零点；原始行仍保留。`);
        return { reason: null, notices };
      }
      const duplicates = new Set(rows.map(row => key(row[candidate.x]))).size < rows.length;
      if (candidate.series) {
        const pairs = rows.map(row => key([row[candidate.x], row[candidate.series]]));
        if (new Set(pairs).size !== pairs.length) return { reason: "同一维度与分组出现多行，不能静默合并或求和。请使用逐行展示，或在原查询中明确聚合口径。", notices };
        const groups = new Set(rows.map(row => key(row[candidate.series]))).size;
        if (groups > 20) return { reason: "系列超过 20 组，请明确筛选范围；展示层没有合并其他组。", notices };
      } else if (duplicates) notices.push("维度存在重复值；按返回行分别绘制并标明行号，没有求和或合并。");
      if (["line", "multiline", "area"].includes(candidate.kind)) {
        if (!isTime(candidate.x, rows, types)) return { reason: "折线与面积趋势需要日期或时间维度；当前字段不是已确认的时间字段。", notices };
        notices.push("时间点沿用原查询返回顺序，缺失值保留断点；未自动排序或补齐日期。");
      }
      if (["pie", "donut"].includes(candidate.kind)) {
        if (duplicates) return { reason: "饼图和环形图需要唯一的分类标签；重复维度不会自动合并。", notices };
        const values = rows.map(row => row[candidate.y[0]]).filter(value => value != null);
        if (values.some(value => number(value) < 0 || (typeof value === "string" && /^-/.test(value) && /[1-9]/.test(value.split(/[eE]/)[0])))) return { reason: "饼图和环形图不能表示负数，请选择柱状或条形图。", notices };
        if (!values.some(value => number(value) > 0)) return { reason: "饼图和环形图需要至少一个可绘制的正值；不会给零值或缺失值虚构占比。", notices };
        if (values.length < rows.length) notices.push(`${rows.length - values.length} 行指标缺失，没有计为零或参与扇区。`);
      }
      const groupCount = candidate.series ? new Set(rows.map(row => key(row[candidate.series]))).size : candidate.y.length;
      if (["grouped_bar", "stacked_bar", "multiline"].includes(candidate.kind) && groupCount < 2) return { reason: "请选择至少两个指标，或一个指标加具有两个值的分组字段。", notices };
      if (candidate.kind === "stacked_bar") {
        if (!candidate.series && duplicates) return { reason: "堆叠图需要唯一的维度行；重复记录不会先求和。", notices };
        if (!candidate.stackConfirmed) return { reason: "堆叠会在图上相加，请先确认所选指标或分组的口径与单位允许相加。", notices };
        notices.push("堆叠仅用于已确认可相加的返回值；不写回新业务指标。");
      }
      if (!candidate.y.some(field => rows.some(row => number(row[field]) != null))) return { reason: "所选指标全部缺失，没有可绘制的数值。", notices };
      return { reason: null, notices };
    };
    const chooseData = data => {
      if (disposed || !data || !Number.isInteger(data.sourceIndex) || !rows[data.sourceIndex]) return;
      const row = rows[data.sourceIndex];
      selection.open = true;
      selectionBody.replaceChildren(element("strong", `返回结果第 ${data.sourceIndex + 1} 行`), element("pre", JSON.stringify(row, null, 2)));
      if (typeof input.onDrilldown === "function") {
        try {
          const pending = input.onDrilldown({ field: config.x, value: row[config.x], metric: data.metric,
            seriesField: config.series, seriesValue: config.series ? row[config.series] : null, row: { ...row } });
          if (pending && typeof pending.catch === "function") pending.catch(() => { if (!disposed) error.textContent = "数据已选中，但追问草稿回填失败，可重试。"; });
        } catch { error.textContent = "数据已选中，但追问草稿回填失败，可重试。"; }
      }
    };
    const buildOption = () => {
      const tooltip = params => (Array.isArray(params) ? params : [params]).filter(item => item?.data && Number.isInteger(item.data.sourceIndex)).map(item => {
        const row = rows[item.data.sourceIndex];
        return `${config.x ?? "结果"}：${config.x ? plain(row[config.x]) : "第 1 行"}${config.series ? `\n${config.series}：${plain(row[config.series])}` : ""}\n${item.data.metric}：${exact(row[item.data.metric], types[item.data.metric])}\n返回行号：${item.data.sourceIndex + 1}`;
      }).join("\n\n");
      const base = { animation: false, color: PALETTE, backgroundColor: "transparent", textStyle: { color: "#244657", fontFamily: "ORION Noto Sans SC, sans-serif" },
        aria: { enabled: true }, tooltip: { trigger: "item", renderMode: "richText", confine: true, formatter: tooltip,
          backgroundColor: "rgba(224,242,243,0.98)", borderColor: "#8cb5c0", textStyle: { color: "#183d50" } },
        legend: { type: "scroll", top: 0, textStyle: { color: "#294e62" } }, grid: { left: 64, right: 30, top: 52, bottom: 68, containLabel: true } };
      if (config.kind === "kpi") {
        const width = Math.max(320, canvas.clientWidth || 640);
        return { ...base, legend: { show: false }, graphic: config.y.flatMap((metric, index) => {
          const columns = width >= 700 ? 3 : 2;
          const left = 20 + (index % columns) * (width - 40) / columns;
          const top = 24 + Math.floor(index / columns) * 112;
          return [
            { type: "text", left, top, style: { text: clip(metric, 28), fill: "#496b7b", font: "14px sans-serif" }, silent: true },
            { type: "text", left, top: top + 34, style: { text: exact(rows[0][metric], types[metric]), fill: PALETTE[index % PALETTE.length], font: "24px sans-serif", width: (width - 56) / columns, overflow: "breakAll" },
              onclick: () => chooseData({ sourceIndex: 0, metric }) },
          ];
        }) };
      }
      const point = (index, metric, value) => ({ value, sourceIndex: index, metric });
      if (["pie", "donut"].includes(config.kind)) return { ...base, legend: { type: "scroll", bottom: 0, top: null }, series: [{
        type: "pie", radius: config.kind === "donut" ? ["42%", "68%"] : "68%", center: ["50%", "45%"],
        label: { color: "#294e62", formatter: data => clip(data.name, 28) },
        data: rows.flatMap((row, index) => row[config.y[0]] == null ? [] : [{ ...point(index, config.y[0], number(row[config.y[0]])), name: plain(row[config.x]) }]),
      }] };
      const valueAxis = { type: "value", scale: false, nameTextStyle: { color: "#294e62" }, axisLine: { show: true, onZero: true, lineStyle: { color: "#668c9d" } },
        axisLabel: { color: "#416779" }, splitLine: { lineStyle: { color: "rgba(91,140,157,.2)" } } };
      if (config.kind === "scatter") return { ...base, xAxis: { ...valueAxis, name: config.x }, yAxis: { ...valueAxis, name: config.y[0] }, series: [{
        type: "scatter", name: config.y[0], symbolSize: 10,
        data: rows.flatMap((row, index) => row[config.x] == null || row[config.y[0]] == null ? [] : [point(index, config.y[0], [number(row[config.x]), number(row[config.y[0]])])]),
      }] };
      const duplicate = new Set(rows.map(row => key(row[config.x]))).size < rows.length;
      const categories = config.series ? [...new Map(rows.map(row => [key(row[config.x]), row[config.x]])).values()]
        : rows.map((row, index) => duplicate ? `${plain(row[config.x])}（第 ${index + 1} 行）` : plain(row[config.x]));
      const categoryAxis = { type: "category", data: categories.map(plain), axisLabel: { color: "#416779", formatter: value => clip(value, 22), hideOverlap: true },
        axisLine: { onZero: true, lineStyle: { color: "#668c9d" } }, axisTick: { alignWithLabel: true } };
      const horizontal = config.kind === "horizontal_bar";
      const line = ["line", "multiline", "area"].includes(config.kind);
      const seriesModel = (name, data) => ({ name, type: line ? "line" : "bar", data,
        ...(line ? { connectNulls: false, showSymbol: true, symbolSize: 7, smooth: false } : { barMaxWidth: 48 }),
        ...(config.kind === "area" ? { areaStyle: { opacity: .22 } } : {}),
        ...(config.kind === "stacked_bar" ? { stack: "user-confirmed-display-stack" } : {}),
        emphasis: { focus: "series" },
        markLine: { silent: true, symbol: "none", label: { show: false }, lineStyle: { color: "#456d80", width: 1 }, data: [{ [horizontal ? "xAxis" : "yAxis"]: 0 }] },
      });
      let seriesData;
      if (config.series) {
        const groups = [...new Map(rows.map(row => [key(row[config.series]), row[config.series]])).values()];
        const lookup = new Map(rows.map((row, index) => [key([row[config.x], row[config.series]]), index]));
        seriesData = groups.map(group => seriesModel(plain(group), categories.map(category => {
          const index = lookup.get(key([category, group]));
          return index == null ? { value: null } : point(index, config.y[0], number(rows[index][config.y[0]]));
        })));
      } else seriesData = config.y.map(metric => seriesModel(metric, rows.map((row, index) => point(index, metric, number(row[metric])))));
      return { ...base, xAxis: horizontal ? valueAxis : categoryAxis, yAxis: horizontal ? categoryAxis : valueAxis,
        series: seriesData, ...(categories.length > 24 ? { dataZoom: [{ type: "slider", ...(horizontal ? { yAxisIndex: 0, right: 4 } : { xAxisIndex: 0, bottom: 10 }), start: 0, end: Math.min(100, 24 / categories.length * 100) }] } : {}) };
    };
    const render = () => {
      if (disposed) return;
      const eligibility = validate(config);
      const availability = Object.fromEntries(KINDS.map(([kind]) => [kind, validate({ ...config, kind }).reason]));
      state = { status: eligibility.reason ? "UNAVAILABLE" : "READY", reason: eligibility.reason, notices: eligibility.notices,
        rowCount: rows.length, metricFields: [...metrics], availableKinds: availability, config: getConfig() };
      dimension.value = config.x ?? ""; series.value = config.series ?? "";
      dimension.disabled = config.kind === "kpi";
      series.disabled = ["pie", "donut", "scatter", "kpi"].includes(config.kind);
      for (const [field, box] of metricBoxes) box.checked = config.y.includes(field);
      metricSummary.textContent = config.y.length ? `指标：${config.y.map(name => clip(name, 16)).join("、")}` : "选择指标";
      stackLabel.hidden = config.kind !== "stacked_bar"; stackConfirmation.checked = config.stackConfirmed;
      for (const [kind, node] of kindButtons) {
        node.setAttribute("aria-pressed", String(config.kind === kind));
        node.title = availability[kind] ?? "当前字段适合此图";
        node.dataset.chartAvailable = String(!availability[kind]);
      }
      error.textContent = eligibility.reason ?? "";
      note.textContent = [...eligibility.notices, "图形按近似数值定位；提示与选中行保留原始精度，NULL 不记为零。仅使用本次返回记录。"].join(" ");
      canvas.setAttribute("aria-label", `${KINDS.find(([kind]) => kind === config.kind)?.[1]}，${rows.length} 行返回结果`);
      download.disabled = Boolean(eligibility.reason);
      png.disabled = Boolean(eligibility.reason);
      if (eligibility.reason) { chart?.clear(); return; }
      if (!window.echarts?.init) { state.status = "ENGINE_UNAVAILABLE"; state.reason = "本地图表引擎尚未加载，请重新打开分析窗口。"; error.textContent = state.reason; download.disabled = png.disabled = true; return; }
      try {
        if (!chart) {
          chart = window.echarts.init(canvas, null, { renderer: "svg", width: Math.max(320, canvas.clientWidth || 640), height: 380 });
          chart.on("click", params => chooseData(params.data));
        }
        chart.setOption(buildOption(), { notMerge: true, lazyUpdate: false });
        resize();
      } catch { state.status = "RENDER_ERROR"; state.reason = "图表暂时未能渲染，可调整字段或重新选择图表；原始结果不受影响。"; error.textContent = state.reason; download.disabled = png.disabled = true; }
    };
    function getConfig() { return { ...config, y: [...config.y] }; }
    function setConfig(patch = {}) {
      if (disposed || !patch || typeof patch !== "object") return getConfig();
      const changed = { ...config };
      if (typeof patch.kind === "string") { const kind = ALIASES[patch.kind] ?? patch.kind; if (KINDS.some(([name]) => name === kind)) changed.kind = kind; }
      if (Object.hasOwn(patch, "x")) changed.x = dimensions.includes(patch.x) ? patch.x : null;
      if (Object.hasOwn(patch, "series")) changed.series = dimensions.includes(patch.series) ? patch.series : null;
      if (Array.isArray(patch.y)) changed.y = [...new Set(patch.y)].filter(field => metrics.includes(field));
      if (Object.hasOwn(patch, "stackConfirmed")) changed.stackConfirmed = patch.stackConfirmed === true;
      if (changed.x !== config.x || changed.series !== config.series || JSON.stringify(changed.y) !== JSON.stringify(config.y)) changed.stackConfirmed = patch.stackConfirmed === true;
      config = changed; render(); return getConfig();
    }
    const resize = () => {
      if (disposed || !chart) return;
      const height = config.kind === "kpi" ? Math.max(260, Math.ceil(config.y.length / ((canvas.clientWidth || 640) >= 700 ? 3 : 2)) * 112 + 40) : 380;
      canvas.style.height = `${height}px`;
      chart.resize({ width: Math.max(320, canvas.clientWidth || 640), height });
      if (config.kind === "kpi" && state?.status === "READY") chart.setOption(buildOption(), { notMerge: true, lazyUpdate: false });
    };
    if (typeof ResizeObserver === "function") { observer = new ResizeObserver(resize); observer.observe(canvas); }
    render();
    return { getConfig, getState: () => ({ ...state, config: getConfig(), notices: [...(state?.notices ?? [])], metricFields: [...(state?.metricFields ?? [])], availableKinds: { ...state?.availableKinds } }), setConfig, resize,
      destroy() { if (disposed) return; disposed = true; observer?.disconnect(); chart?.dispose(); chart = null; root.remove(); } };
  };
  window.__ORION_ANALYTICS_CHARTS__ = { mount, kinds: KINDS.map(([kind, label]) => ({ kind, label })) };
})();
