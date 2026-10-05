import ForceGraph3D from "3d-force-graph";
import ForceGraph from "force-graph";
import { perspectiveGraphFrame } from "./graph-framing.js";
import { layeredGraphPositions } from "./graph-layered-layout.js";
import { projectGraph, graphNeighborhood } from "./graph-view-projection.js";
import { graphNodeInteraction, graphEdgeInteraction, tintToWhite } from "./graph-interaction-style.js";
import { readGraphCanvasTheme } from "./graph-theme.js";
import { instancePreviewNotice } from "./graph-instance-preview.js";
import { Group, Sprite, SpriteMaterial, CanvasTexture, AdditiveBlending } from "three";

(() => {
  if (window.__ORION_ONTOLOGY_GRAPH_VIEWER__) return;

  const TYPE_META = {
    class: ["Class", "类", "#4f8cff"],
    objectProperty: ["Object Property", "对象属性", "#b78cff"],
    dataProperty: ["Data Property", "数据属性", "#3ddc97"],
    individual: ["Individual", "实例", "#ffb454"],
  };
  const EDGE_META = {
    subclass: ["subClassOf", "继承", "#85818c", .12],
    schema: ["Object relation", "对象关系", "#b7a4d7", .28],
    domain: ["domain", "定义域", "#3ddc97", .28],
    range: ["range", "值域", "#3ddc97", .28],
    inverse: ["inverseOf", "逆属性", "#ff6b81", .5],
    relationship: ["Assertion", "实例关系", "#929292", .18],
    instance: ["rdf:type", "实例归属", "#a8a8a8", .18],
  };
  const VIEW_LABELS = { model: "本体", instances: "实例预览", professional: "专业视图" };
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
  const endpointId = (value) => typeof value === "object" ? value?.id : value;
  const reducedMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;

  let dialog = null;
  let state = null;
  const pendingInitialFit = new WeakSet();
  const firstFrameFit = new WeakSet();
  let canvasObserver = null;
  let openRevision = 0;
  let canvasTheme = readGraphCanvasTheme(null);
  let themeDisposer = null;
  const graphDrawers = new WeakMap();

  const syncCanvasTheme = () => {
    if (!dialog || dialog.hidden) return;
    const next = readGraphCanvasTheme(dialog);
    if (JSON.stringify(next) === JSON.stringify(canvasTheme)) return;
    canvasTheme = next;
    for (const [mode, graph] of Object.entries(state?.graphs ?? {})) {
      if (!graph) continue;
      graph.linkColor(link => linkColor(link));
      if (mode === "3d") graph.nodeThreeObject(node => nodeAccent(node));
      else {
        const draw = graphDrawers.get(graph);
        if (draw) graph.nodeCanvasObject((...args) => draw(...args));
      }
      graph.refresh?.();
    }
  };
  const watchCanvasTheme = () => {
    themeDisposer?.();
    syncCanvasTheme();
    const observer = new MutationObserver(syncCanvasTheme);
    observer.observe(document.body, { attributes: true, attributeFilter: ["data-ds-dark-theme", "style", "class"] });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-dsh-aqua", "data-dsh-float", "data-dsh-compat", "style"] });
    const queries = ["(forced-colors: active)", "(prefers-contrast: more)", "(prefers-reduced-transparency: reduce)"].map(query => window.matchMedia(query));
    queries.forEach(query => query.addEventListener("change", syncCanvasTheme));
    themeDisposer = () => { observer.disconnect(); queries.forEach(query => query.removeEventListener("change", syncCanvasTheme)); };
  };

  // Pull disconnected components into one view without adding semantic edges.
  const compactForce = () => {
    let nodes = [];
    const force = (alpha) => {
      for (const node of nodes) for (const axis of ["x", "y", "z"]) {
        if (node[`f${axis}`] == null && Number.isFinite(node[axis]))
          node[`v${axis}`] = (node[`v${axis}`] || 0) - node[axis] * alpha * .032;
      }
    };
    force.initialize = (items) => { nodes = items; };
    return force;
  };

  const nodeAccent = (node) => {
    if (node.kind !== "class") return undefined;
    const group = new Group();
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 64;
    const context = canvas.getContext("2d");
    const glow = context.createRadialGradient(32, 32, 2, 32, 32, 32);
    glow.addColorStop(0, "rgba(90,175,255,.8)");
    glow.addColorStop(.3, "rgba(60,135,255,.22)");
    glow.addColorStop(1, "rgba(45,110,255,0)");
    context.fillStyle = glow; context.fillRect(0, 0, 64, 64);
    const halo = new Sprite(new SpriteMaterial({ map: new CanvasTexture(canvas), transparent: true, depthWrite: false, blending: AdditiveBlending }));
    halo.scale.set(24, 24, 1); group.add(halo);
    if (state.labelIds.has(node.id)) {
      const labelCanvas = document.createElement("canvas");
      const labelContext = labelCanvas.getContext("2d");
      labelContext.font = "500 26px sans-serif";
      labelCanvas.width = Math.ceil(labelContext.measureText(node.label).width) + 28;
      labelCanvas.height = 48;
      labelContext.font = "500 26px sans-serif";
      labelContext.fillStyle = canvasTheme.labelColor; labelContext.textBaseline = "middle";
      labelContext.fillText(node.label, 14, 24);
      const label = new Sprite(new SpriteMaterial({ map: new CanvasTexture(labelCanvas), transparent: true, depthWrite: false }));
      label.scale.set(labelCanvas.width / 2.8, 17, 1); label.position.y = 17;
      group.add(label);
    }
    return group;
  };

  const ensureDialog = () => {
    if (dialog) return dialog;
    dialog = document.createElement("div");
    dialog.className = "owa-graph-dialog";
    dialog.hidden = true;
    dialog.innerHTML = `
      <div class="owa-graph-shell" role="dialog" aria-modal="true" aria-labelledby="owa-graph-title">
        <header class="owa-graph-header">
          <div class="owa-graph-title"><img class="owa-graph-brand" src="/ahs-logo-white.png" alt="AHS" /><span class="owa-graph-divider">/</span><div><h2 id="owa-graph-title">业务本体</h2><p data-graph-subtitle></p></div></div>
          <div class="owa-graph-toolbar"><span class="owa-graph-readonly">已发布 · 只读</span><button type="button" class="owa-graph-close" data-graph-close aria-label="返回本体管理">返回本体管理</button></div>
        </header>
        <nav class="owa-graph-views" aria-label="本体工作区视图"><button type="button" data-graph-view="model" aria-pressed="true"><svg class="owa-graph-tab-icon" xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M8.3 10a.7.7 0 0 1-.626-1.079L11.4 3a.7.7 0 0 1 1.198-.043L16.3 8.9a.7.7 0 0 1-.572 1.1Z"></path><rect x="3" y="14" width="7" height="7" rx="1"></rect><circle cx="17.5" cy="17.5" r="3.5"></circle></svg>本体</button><button type="button" data-graph-view="instances" aria-pressed="false"><svg class="owa-graph-tab-icon" xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m10.586 5.414-5.172 5.172"></path><path d="m18.586 13.414-5.172 5.172"></path><path d="M6 12h12"></path><circle cx="12" cy="20" r="2"></circle><circle cx="12" cy="4" r="2"></circle><circle cx="20" cy="12" r="2"></circle><circle cx="4" cy="12" r="2"></circle></svg>实例预览</button>
          <details class="owa-graph-display"><summary>显示设置</summary><div class="owa-graph-display-menu"><div class="owa-graph-mode" role="group" aria-label="图谱维度"><button type="button" data-graph-mode="2d" class="is-active">2D</button><button type="button" data-graph-mode="3d">3D</button><button type="button" data-graph-layer aria-pressed="false">同心分层</button></div><label class="owa-graph-hierarchy" data-graph-hierarchy-control><input type="checkbox" data-graph-hierarchy />显示分类层级</label><button type="button" data-graph-view="professional" aria-pressed="false">专业视图 · OWL</button></div></details>
        </nav>
        <div class="owa-graph-body">
          <aside class="owa-graph-left">
            <input class="owa-graph-list-search" type="search" data-graph-list-search aria-label="筛选对象列表" placeholder="筛选…" />
            <button type="button" class="owa-graph-schema-home" data-graph-reset><span class="owa-graph-nav-icon" aria-hidden="true"></span> <span data-graph-list-title>模式图</span></button>
            <div class="owa-graph-categories" data-graph-categories role="group" aria-label="本体对象类型"><button type="button" data-graph-category="class" aria-pressed="true">类</button><button type="button" data-graph-category="properties" aria-pressed="false">属性</button></div>
            <p class="owa-graph-view-description" data-graph-view-description hidden></p>
            <div class="owa-graph-instance-controls" data-graph-instance-controls hidden><label>所属类<select data-graph-instance-class aria-label="实例所属类"></select></label><label>具体实例<select data-graph-instance-choice aria-label="具体实例"></select></label></div>
            <label class="owa-graph-filter"><span class="sr-only">关系类型</span><select data-graph-filter aria-label="关系类型"><option value="all">全部关系</option>${Object.entries(EDGE_META).map(([kind, meta]) => `<option value="${kind}">${meta[1]} (${meta[0]})</option>`).join("")}</select></label>
            <details class="owa-graph-preview-scope" data-graph-preview-scope hidden><summary>实例预览范围</summary><p data-graph-instance-preview role="status"></p></details>
            <p class="owa-graph-search-status" data-graph-search-status role="status"></p>
            <div class="owa-graph-map-head"><span class="owa-graph-copy"><b>节点类型</b><small>Node Types</small></span><span data-graph-nav-count></span></div>
            <div class="owa-graph-hops" hidden role="group" aria-label="多跳关系范围"><button type="button" data-graph-hops="1" class="is-active" aria-pressed="true"><b>1-hop</b><small>一跳</small></button><button type="button" data-graph-hops="2" aria-pressed="false"><b>2-hop</b><small>二跳</small></button><button type="button" data-graph-hops="3" aria-pressed="false"><b>3-hop</b><small>三跳</small></button></div>
            <div class="owa-graph-legend" data-graph-legend></div>
            <div class="owa-graph-node-list" data-graph-node-list aria-label="当前视图节点"></div>
            <p class="owa-graph-hint">拖拽旋转 · 滚轮缩放 · 悬停查看详情 · 点击节点固定</p>
          </aside>
          <main class="owa-graph-stage"><div class="owa-graph-canvas-tools"><label class="owa-graph-search"><input type="search" aria-label="搜索当前视图" placeholder="搜索本体…" /></label><button class="owa-graph-filter-toggle" type="button" data-graph-filter-toggle aria-expanded="false" hidden>对象筛选</button><div class="owa-graph-edge-key" data-graph-edge-key><span>— 继承</span><span>— 关系</span></div></div><div class="owa-graph-canvas" data-graph-canvas="3d"></div><div class="owa-graph-canvas" data-graph-canvas="2d" hidden></div><div class="owa-graph-filter-info" data-graph-filter-info></div><div class="owa-graph-loading" data-graph-loading><i></i><strong>正在加载本体图谱</strong><span>读取正式发布版本中的节点与关系…</span></div><div class="owa-graph-empty" data-graph-empty hidden></div><span class="owa-graph-count" data-graph-visible></span><div class="owa-graph-badge"><button type="button" data-graph-zoom="out" aria-label="缩小图谱" title="缩小">−</button><button type="button" data-graph-zoom="in" aria-label="放大图谱" title="放大">+</button><button type="button" data-graph-reset aria-label="适应视图" title="适应视图">⛶</button></div><div class="owa-graph-tip" data-graph-tip hidden></div></main>
          <aside class="owa-graph-detail" data-graph-detail><div class="owa-graph-detail-empty"><span aria-hidden="true"></span><strong>选择一个节点</strong><p>查看对象定义、属性值与关联关系。</p></div></aside>
        </div>
        <footer class="owa-graph-footer"><div data-graph-status>正式发布资产 · 模型关系 · 2D</div><div data-graph-runtime><span class="owa-graph-live"></span>真实本体数据</div></footer>
      </div>`;
    document.body.appendChild(dialog);
    dialog.addEventListener("click", (event) => {
      if (event.target === dialog || event.target.closest("[data-graph-close]")) close();
      if (event.target.closest("[data-graph-filter-toggle]") && state) {
        const expanded = dialog.querySelector(".owa-graph-shell").classList.toggle("show-object-filter");
        dialog.querySelector("[data-graph-filter-toggle]").setAttribute("aria-expanded", String(expanded));
      }
      const category = event.target.closest("[data-graph-category]")?.dataset.graphCategory;
      if (category && state) { state.listCategory = category; renderNodeList(); }
      const view = event.target.closest("[data-graph-view]")?.dataset.graphView;
      if (view && state) setView(view);
      const instanceClass = event.target.closest("[data-graph-browse-instances]")?.dataset.graphBrowseInstances;
      if (instanceClass && state) { state.instanceClassId = instanceClass; state.instanceId = null; state.allClassInstances = false; setView("instances"); dialog.querySelector(".owa-graph-shell").classList.add("show-object-filter"); dialog.querySelector("[data-graph-filter-toggle]").setAttribute("aria-expanded", "true"); }
      const focusId = event.target.closest("[data-graph-focus]")?.dataset.graphFocus;
      if (focusId && state) selectNode(state.byId.get(focusId));
      const edgeId = event.target.closest("[data-graph-edge]")?.dataset.graphEdge;
      if (edgeId && state) selectEdge(state.visibleData.links.find(edge => edge.id === edgeId));
      const mode = event.target.closest("[data-graph-mode]")?.dataset.graphMode;
      if (mode && state) setMode(mode);
      const tab = event.target.closest("[data-graph-detail-tab]")?.dataset.graphDetailTab;
      if (tab && state) setDetailTab(tab);
      const hops = Number(event.target.closest("[data-graph-hops]")?.dataset.graphHops);
      if (hops && state) setHops(hops);
      if (event.target.closest("[data-graph-detail-close]") && state) selectNode(null, false);
      if (event.target.closest("[data-graph-layer]") && state) toggleLayered();
      if (event.target.closest("[data-graph-reset]") && state) { state.viewScale = 1; resetCamera(true); }
      const zoom = event.target.closest("[data-graph-zoom]")?.dataset.graphZoom;
      if (zoom && state) zoomGraph(zoom === "in" ? 1.2 : 1 / 1.2);
    });
    dialog.querySelector("[data-graph-list-search]").addEventListener("input", () => { if (state) renderNodeList(); });
    dialog.querySelector("[data-graph-filter]").addEventListener("change", (event) => setFilter(event.target.value));
    dialog.querySelector("[data-graph-hierarchy]").addEventListener("change", (event) => { state.showHierarchy = event.target.checked; refreshView(); });
    dialog.querySelector("[data-graph-instance-class]").addEventListener("change", (event) => { state.instanceClassId = event.target.value || null; state.instanceId = null; state.allClassInstances = false; refreshView(); });
    dialog.querySelector("[data-graph-instance-choice]").addEventListener("change", (event) => { state.allClassInstances = event.target.selectedOptions[0]?.dataset.scope === "all"; state.instanceId = state.allClassInstances ? null : event.target.value || null; refreshView(); if (state.instanceId) selectNode(state.byId.get(state.instanceId)); });
    dialog.querySelector(".owa-graph-search input").addEventListener("input", event => { if (state && !event.target.value.trim()) locateNode(""); });
    dialog.querySelector(".owa-graph-search input").addEventListener("keydown", (event) => {
      if (event.key === "Enter") locateNode(event.target.value);
    });
    const list = dialog.querySelector("[data-graph-node-list]");
    const previewListNode = event => {
      const button = event.target.closest("[data-graph-focus]");
      if (!state || !button) return;
      const node = state.visibleData.nodes.find(node => node.id === button.dataset.graphFocus);
      if (node) showTip(node);
    };
    list.addEventListener("pointerover", previewListNode);
    list.addEventListener("focusin", previewListNode);
    list.addEventListener("pointerleave", () => showTip(null));
    list.addEventListener("focusout", event => { if (!list.contains(event.relatedTarget)) showTip(null); });
    const stage = dialog.querySelector(".owa-graph-stage");
    stage.addEventListener("pointerleave", () => { if (state) { state.hoveredEdge = null; showTip(null); refreshHighlight(); } });
    // A user's camera gesture takes precedence over the initial layout fit.
    stage.addEventListener("pointerdown", () => {
      if (state?.graph) pendingInitialFit.delete(state.graph);
    });
    stage.addEventListener("pointermove", (event) => {
      const tip = dialog.querySelector("[data-graph-tip]");
      if (!tip || tip.hidden) return;
      const rect = stage.getBoundingClientRect();
      tip.style.left = `${Math.min(rect.width - 280, Math.max(12, event.clientX - rect.left + 16))}px`;
      tip.style.top = `${Math.min(rect.height - 80, Math.max(12, event.clientY - rect.top - 10))}px`;
    });
    window.addEventListener("keydown", (event) => { if (event.key === "Escape" && !dialog.hidden) close(); });
    return dialog;
  };

  const neighborhoodCache = new WeakMap();
  const neighborhoodFor = (nodeId, hops = 1) => {
    const data = state?.visibleData;
    if (!data) return { distances: new Map(), edges: [] };
    if (!neighborhoodCache.has(data)) neighborhoodCache.set(data, new Map());
    const cache = neighborhoodCache.get(data), key = JSON.stringify([nodeId, hops]);
    if (!cache.has(key)) { const result = graphNeighborhood(data, nodeId, hops); result.edgeIds = new Set(result.edges.map(edge => edge.id)); cache.set(key, result); }
    return cache.get(key);
  };

  const emptyState = (title, detail) => `<div class="owa-graph-detail-state"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(detail)}</p></div>`;
  const detailsMarkup = (node) => {
    if (!node) return `<div class="owa-graph-detail-empty"><span aria-hidden="true"></span><strong>选择一个节点</strong><p>查看它的中文定义、属性与关联关系。</p></div>`;
    const neighborhood = neighborhoodFor(node.id, state.hops);
    const connections = neighborhood.edges.slice(0, 36);
    const relationRows = connections.map((edge) => {
      const sourceId = endpointId(edge.source), targetId = endpointId(edge.target);
      const sourceDistance = neighborhood.distances.get(sourceId) ?? 0;
      const targetDistance = neighborhood.distances.get(targetId) ?? 0;
      const otherId = sourceDistance >= targetDistance ? sourceId : targetId;
      const other = state.byId.get(otherId), source = state.byId.get(sourceId), target = state.byId.get(targetId);
      const hop = Math.max(sourceDistance, targetDistance);
      const cardinality = edge.cardinality && edge.cardinality !== "未声明" ? `<b>${escapeHtml(edge.cardinality)}</b><em>${escapeHtml(edge.cardinalityBasis ?? "")}</em>` : "";
      return `<button type="button" data-graph-edge="${escapeHtml(edge.id)}"><span>${escapeHtml(edge.label)} <i>${hop} 跳</i></span><strong>${escapeHtml(other?.label ?? otherId)}</strong><small>${escapeHtml(source?.label ?? sourceId)} <i aria-hidden="true">→</i> ${escapeHtml(target?.label ?? targetId)} ${cardinality}</small></button>`;
    }).join("");
    const schemaAttrs = (node.schemaAttributes ?? []).slice(0, 20).map((item) => `<li><span>${escapeHtml(item.property)}</span><strong>${escapeHtml(item.value)}</strong></li>`).join("");
    const attrs = (node.attributes ?? []).slice(0, 20).map((item) => `<li><span>${escapeHtml(item.property)}</span><strong>${escapeHtml(item.value)}</strong></li>`).join("");
    const memberships = state.payload.edges.filter(edge => edge.kind === "instance" && endpointId(edge.source) === node.id).map(edge => state.byId.get(endpointId(edge.target))?.label ?? endpointId(edge.target));
    const membership = memberships.length ? `<section><h4>所属类</h4><p class="owa-graph-summary">${memberships.map(escapeHtml).join("、")}</p></section>` : "";
    const browse = node.kind === "class" ? `<button type="button" class="owa-graph-action" data-graph-browse-instances="${escapeHtml(node.id)}">预览该类实例</button>` : "";
    const parents = state.payload.edges.filter(edge => edge.kind === "subclass" && endpointId(edge.source) === node.id).map(edge => state.byId.get(endpointId(edge.target))?.label ?? endpointId(edge.target));
    const classDefinition = node.kind === "class" ? `<section><h4>形状与颜色</h4><div class="owa-graph-node-appearance"><i style="background:${nodePalette(node)}"></i><span>方形</span></div></section><section><h4>父类</h4><p class="owa-graph-summary">${parents.length ? parents.map(escapeHtml).join("、") : "当前模型未声明父类"}</p></section>` : "";
    const definition = `<section><h4>标签</h4><p class="owa-graph-summary">${escapeHtml(node.label)}</p></section>${classDefinition}${membership}<section><h4>描述</h4><p class="owa-graph-summary">${escapeHtml(node.summary || "当前发布版本未提供描述。")}</p></section><details class="owa-graph-identifier"><summary>标识与来源</summary><code>${escapeHtml(node.id)}</code><p>来自当前选定的发布版本 ${escapeHtml(state.asset.version)}</p></details>`;
    const properties = `${schemaAttrs ? `<section><h4>数据属性</h4><ul>${schemaAttrs}</ul></section>` : ""}${attrs ? `<section><h4>属性值</h4><ul>${attrs}</ul></section>` : ""}<section><h4>关系 <span>${neighborhood.edges.length} 条</span></h4><div class="owa-graph-relations">${relationRows || "<p>当前视图暂无显式关联</p>"}</div></section>`;
    const members = node.kind === "class" ? state.payload.edges.filter(edge => edge.kind === "instance" && endpointId(edge.target) === node.id).length : 0;
    const instancePanel = `<section><h4>已加载实例 <span>${members}</span></h4><p class="owa-graph-summary">${members ? "在实例预览中选择具体对象，查看属性值与关系。" : "当前加载范围没有该类实例，不代表完整业务数据中不存在。"}</p>${browse}<p class="owa-graph-summary">${escapeHtml(instancePreviewNotice(state.payload.instance_preview) || "仅统计当前发布版本已加载的数据。")}</p></section>`;
    const panel = state.detailTab === "relations" ? properties : state.detailTab === "instances" ? instancePanel : definition;
    const tabs = [["overview", node.kind === "individual" ? "概览" : "定义"], ["relations", node.kind === "individual" ? "属性与关系" : "属性"], ...(node.kind === "class" ? [["instances", "实例预览"]] : [])];
    return `<div class="owa-graph-detail-head"><i style="background:${nodePalette(node)}"></i><h3>${escapeHtml(node.label)}</h3><button type="button" data-graph-detail-close aria-label="关闭详情">×</button></div><div class="owa-graph-detail-tabs" role="tablist" aria-label="节点详情">${tabs.map(([key, label]) => `<button type="button" role="tab" data-graph-detail-tab="${key}" aria-selected="${state.detailTab === key}" class="${state.detailTab === key ? "is-active" : ""}">${label}</button>`).join("")}</div><div class="owa-graph-detail-panel" role="tabpanel">${panel}</div>`;

  };

  const edgeDetailsMarkup = (edge) => {
    const source = state.byId.get(endpointId(edge.source)), target = state.byId.get(endpointId(edge.target));
    const rows = [["起点", source?.label ?? endpointId(edge.source)], ["终点", target?.label ?? endpointId(edge.target)], ["关系类型", EDGE_META[edge.kind]?.[1] ?? edge.kind]];
    if (edge.cardinality && edge.cardinality !== "未声明") rows.push(["关系基数", edge.cardinality], ["基数依据", edge.cardinalityBasis || "未提供"]);
    const basisNote = edge.cardinalityBasis === "数据观察" ? "此基数仅反映已加载数据，不是模型约束。" : edge.cardinalityBasis === "OWL 约束" ? "功能性约束表示最多关联一个对象，不代表该关系必须存在。" : "";
    return `<div class="owa-graph-detail-head"><button type="button" data-graph-detail-close>收起</button><em>关系定义</em><h3>${escapeHtml(edge.label)}</h3><code>${escapeHtml(edge.predicate)}</code></div><section><ul>${rows.map(([label,value]) => `<li><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong></li>`).join("")}</ul></section>${edge.summary ? `<p class="owa-graph-summary">${escapeHtml(edge.summary)}</p>` : ""}${basisNote ? `<p class="owa-graph-summary">${basisNote}</p>` : ""}<section class="owa-graph-endpoints"><button class="owa-graph-action" data-graph-focus="${escapeHtml(endpointId(edge.source))}">查看起点</button><button class="owa-graph-action" data-graph-focus="${escapeHtml(endpointId(edge.target))}">查看终点</button></section>`;
  };
  const selectEdge = (edge) => {
    if (!edge) return;
    state.selected = null; state.hovered = null; state.hoveredEdge = null; state.selectedEdge = edge;
    dialog.querySelector(".owa-graph-shell").classList.add("has-selection");
    renderDetails(); refreshHighlight();
  };
  const renderDetails = () => {
    dialog.querySelector("[data-graph-detail]").innerHTML = state.selectedEdge ? edgeDetailsMarkup(state.selectedEdge) : detailsMarkup(state.selected);
    dialog.querySelector(".owa-graph-hops").hidden = !state.selected || state.view === "model";
  };

  const activeNeighborhood = () => {
    const edge = state.hoveredEdge ?? state.selectedEdge;
    if (edge) return new Map([endpointId(edge.source), endpointId(edge.target)].map(id => [id, 0]));
    return neighborhoodFor(state.selected?.id ?? state.hovered?.id, state.selected ? state.hops : 1).distances;
  };
  const nodeInteraction = node => graphNodeInteraction({
    selected: state.selected?.id === node.id, hovered: state.hovered?.id === node.id,
    hasSelection: Boolean(state.selected || state.selectedEdge), hasHover: Boolean(state.hovered || state.hoveredEdge),
    neighbor: activeNeighborhood().has(node.id),
    palette: canvasTheme,
  });
  const rgba = (color, opacity) => {
    const value = color.replace("#", "");
    const integer = Number.parseInt(value, 16);
    return `rgba(${integer >> 16},${integer >> 8 & 255},${integer & 255},${opacity})`;
  };
  const nodePalette = (node) => {
    const palette = ["#ff9d76", "#80c7ad", "#a9a3ce", "#88b7d5", "#d6c69d", "#d894ae"];
    let hash = 0; for (const c of String(node.id)) hash = (hash * 31 + c.charCodeAt(0)) >>> 0;
    return palette[hash % palette.length];
  };
  const nodeColor = (node) => {
    const base = nodePalette(node);
    return rgba(base, nodeInteraction(node).opacity);
  };
  const highlightedLink = link => {
    if (state.hoveredEdge?.id === link.id || state.selectedEdge?.id === link.id) return true;
    return [state.hovered, state.selected].some(node => node && (endpointId(link.source) === node.id || endpointId(link.target) === node.id));
  };
  const linkStyle = link => graphEdgeInteraction({
    model: state.view !== "instances", kind: link.kind, highlighted: highlightedLink(link),
    selected: Boolean(state.selected || state.selectedEdge), hovered: Boolean(state.hovered || state.hoveredEdge),
    zoom: state.mode === "2d" ? Math.max(.05, state.graph?.zoom() ?? 1) : 1,
    palette: canvasTheme,
  });
  const linkColor = link => linkStyle(link).color;
  const linkWidth = link => linkStyle(link).width;
  const refreshHighlight = () => {
    if (!state?.graph) return;
    state.graph.nodeColor(nodeColor).linkColor(linkColor).linkWidth(linkWidth).refresh?.();
    dialog.querySelectorAll("[data-graph-node-list] [data-graph-focus]").forEach(button => button.classList.toggle("is-hovered", button.dataset.graphFocus === state.hovered?.id));
  };

  const selectNode = (node, move = true) => {
    state.selectedEdge = null;
    state.hoveredEdge = null;
    state.hovered = null;
    dialog.querySelector("[data-graph-tip]").hidden = true;
    state.selected = node;
    if (node?.kind !== "class" && state.detailTab === "instances") state.detailTab = "overview";
    dialog.querySelectorAll("[data-graph-focus]").forEach(button => button.classList.toggle("is-selected", button.dataset.graphFocus === node?.id));
    dialog.querySelector(".owa-graph-shell")?.classList.toggle("has-selection", Boolean(node));
    if (!node) state.detailTab = "overview";
    renderDetails(); refreshHighlight();
    if (move && node) focusNode(node);
  };
  const showTip = (node) => {
    if (!state || state.hovered?.id === node?.id) return;
    state.hovered = node;
    state.hoveredEdge = null;
    const tip = dialog.querySelector("[data-graph-tip]");
    if (!node) { tip.hidden = true; refreshHighlight(); return; }
    const relationships = state.visibleData.links.filter((edge) => endpointId(edge.source) === node.id || endpointId(edge.target) === node.id).length;
    tip.innerHTML = `<div><strong>${escapeHtml(node.label)}</strong><span>${escapeHtml(TYPE_META[node.kind]?.[1] ?? node.kind)}</span></div><small>${relationships} 条关系</small>`;
    tip.hidden = state.mode === "2d"; refreshHighlight();
  };

  const filteredGraphData = () => state.view === "instances" && state.instanceClassId && (!state.instanceId && !state.allClassInstances)
    ? { nodes: [], links: [] }
    : projectGraph(state.payload, state);
  const viewKey = () => JSON.stringify([state.view, state.showHierarchy, state.filter, state.instanceClassId, state.instanceId, state.allClassInstances, state.view === "instances" ? state.hops : 1]);
  const instanceSeeds = () => new Set(state.payload.edges.filter(edge => edge.kind === "instance" && endpointId(edge.target) === state.instanceClassId).map(edge => endpointId(edge.source)));
  const renderViewControls = () => {
    dialog.querySelector(".owa-graph-shell").dataset.view = state.view;
    dialog.querySelector("[data-graph-filter-toggle]").hidden = state.view !== "instances";
    dialog.querySelector("[data-graph-categories]").hidden = state.view !== "model";
    dialog.querySelector("[data-graph-list-title]").textContent = state.view === "model" ? "模式图" : state.view === "instances" ? "业务对象" : "专业视图";
    dialog.querySelector(".owa-graph-search input").placeholder = state.view === "instances" ? "搜索实体…" : "搜索本体…";
    dialog.querySelector("[data-graph-edge-key]").hidden = state.view !== "model";
    dialog.querySelectorAll("[data-graph-view]").forEach(button => { const active = button.dataset.graphView === state.view; button.classList.toggle("is-active", active); button.setAttribute("aria-pressed", String(active)); });
    dialog.querySelector("[data-graph-view-description]").textContent = { model: "先看业务对象及其关系。点击节点查看属性，点击连线查看定义。", instances: "从所属类选择具体对象，查看实例关系及关联对象。当前数据为预览范围。", professional: "查看类、属性、实例及 OWL 显式关系，用于工程检查。" }[state.view];
    dialog.querySelector("[data-graph-hierarchy-control]").hidden = state.view !== "model";
    dialog.querySelector("[data-graph-hierarchy]").checked = state.showHierarchy;
    dialog.querySelector("[data-graph-instance-controls]").hidden = state.view !== "instances";
    const classSelect = dialog.querySelector("[data-graph-instance-class]");
    classSelect.innerHTML = '<option value="">请选择所属类</option>' + state.payload.nodes.filter(node => node.kind === "class").map(node => `<option value="${escapeHtml(node.id)}">${escapeHtml(node.label)}</option>`).join("");
    classSelect.value = state.instanceClassId ?? "";
    const seeds = instanceSeeds();
    const instanceSelect = dialog.querySelector("[data-graph-instance-choice]");
    instanceSelect.innerHTML = '<option value="">请选择具体实例</option><option value="__all_previews__" data-scope="all">展开该类全部预览实例</option>' + state.payload.nodes.filter(node => seeds.has(node.id) && node.kind === "individual").map(node => `<option value="${escapeHtml(node.id)}">${escapeHtml(node.label)}</option>`).join("");
    instanceSelect.value = state.allClassInstances ? "__all_previews__" : state.instanceId ?? "";
    instanceSelect.disabled = !state.instanceClassId || !seeds.size;
    const filter = dialog.querySelector("[data-graph-filter]");
    filter.closest("label").hidden = state.view !== "professional";
    filter.value = state.filter;
    dialog.querySelector("[data-graph-preview-scope]").hidden = state.view === "model" || !dialog.querySelector("[data-graph-instance-preview]").textContent;
    dialog.querySelector("[data-graph-status]").textContent = `${state.asset.templateId ? "行业模板" : "正式发布资产"} · ${VIEW_LABELS[state.view]} · ${state.mode.toUpperCase()}`;
    dialog.querySelector(".owa-graph-hint").textContent = state.mode === "2d" ? "拖动画布 · 滚轮缩放 · 点击节点或连线查看详情" : "拖拽旋转 · 滚轮缩放 · 点击节点或连线查看详情";
  };
  const refreshView = () => {
    state.selected = null; state.selectedEdge = null; state.hovered = null; state.hoveredEdge = null;
    dialog.querySelector(".owa-graph-shell").classList.remove("has-selection");
    dialog.querySelector("[data-graph-tip]").hidden = true;
    dialog.querySelector("[data-graph-search-status]").textContent = "";
    dialog.querySelector("[data-graph-filter-info]").textContent = state.filter === "all" ? "" : `过滤：${EDGE_META[state.filter]?.[1] ?? ""}`;
    state.detailTab = "overview";
    renderViewControls(); buildGraph(); renderDetails();
  };
  const setView = (view) => {
    if (!VIEW_LABELS[view]) return;
    state.view = view; dialog.querySelector(".owa-graph-shell").classList.remove("show-object-filter"); dialog.querySelector("[data-graph-filter-toggle]").setAttribute("aria-expanded", "false"); dialog.querySelector(".owa-graph-display").open = false; state.filter = "all"; state.hops = 1;
    dialog.querySelector(".owa-graph-search input").value = "";
    setHops(1); refreshView();
  };
  const cameraPreset = (nodeCount, layered = state?.layered) => {
    const distance = layered ? 520 : Math.max(170, Math.min(400, 27 * Math.sqrt(Math.max(1, nodeCount))));
    return { x: distance * .66, y: distance * .36, z: distance * .66 };
  };

  const buildGraph = () => {
    const mode = state.mode;
    dialog.querySelectorAll("[data-graph-canvas]").forEach((canvas) => { canvas.hidden = canvas.dataset.graphCanvas !== mode; });
    const container = dialog.querySelector(`[data-graph-canvas="${mode}"]`);
    const data = filteredGraphData();
    state.visibleData = data;
    const visibleIds = new Set(data.nodes.map(node => node.id));
    if (state.selected && !visibleIds.has(state.selected.id)) state.selected = null;
    if (state.hovered && !visibleIds.has(state.hovered.id)) { state.hovered = null; dialog.querySelector("[data-graph-tip]").hidden = true; }
    if (state.selectedEdge && !data.links.some(edge => edge.id === state.selectedEdge.id)) state.selectedEdge = null;
    dialog.querySelector(".owa-graph-shell").classList.toggle("has-selection", Boolean(state.selected || state.selectedEdge));
    const is3d = mode === "3d";
    let graph = state.graphs[mode];
    state.graph = graph;
    if (graph) {
      if (state.graphFilters[mode] !== viewKey()) {
        pendingInitialFit.add(graph);
        firstFrameFit.delete(graph);
        graph.graphData(data);
        state.graphFilters[mode] = viewKey();
      }
      applyLayered(state.layered);
      resize();
      graph.resumeAnimation?.();
      graph.d3ReheatSimulation?.();
      resetCamera(false);
      updateVisibleCount(graph.graphData());
      refreshHighlight();
      return;
    }
    graph = (is3d ? ForceGraph3D({ controlType: "trackball" }) : ForceGraph())(container);
    state.graph = graph;
    state.graphs[mode] = graph;
    state.graphFilters[mode] = viewKey();
    pendingInitialFit.add(graph);
    graph
      .graphData(data)
      .nodeId("id")
      .nodeLabel(() => "")
      .nodeColor(nodeColor)
      .nodeVal((node) => {
        if (is3d) return node.kind === "class" ? 12 : 2.6;
        // Match the custom canvas node boundary so arrowheads meet the target.
        const zoom = Math.max(.05, graph.zoom());
        const radius = (state.view === "model" ? 7 : 5) / Math.sqrt(zoom);
        return ((radius + 2 / zoom) / 4) ** 2;
      })
      .linkColor(linkColor)
      .linkWidth(linkWidth)
      .linkCurvature(() => 0)
      .linkDirectionalArrowLength(link => is3d ? 3.5 : 0)
      .linkDirectionalArrowRelPos(1)
      .linkDirectionalArrowColor(linkColor)
      .linkDirectionalParticles(() => reducedMotion() || state.mode === "2d" ? 0 : 3)
      .linkDirectionalParticleSpeed(.004)
      .linkDirectionalParticleWidth(1.5)
      .linkDirectionalParticleColor((link) => EDGE_META[link.kind]?.[2] ?? "#667799")
      .backgroundColor("rgba(0,0,0,0)")
      .d3AlphaDecay(.02)
      .warmupTicks(110)
      .cooldownTicks(is3d ? 200 : 0)
      .onEngineTick(() => {
        if (state?.graph === graph && pendingInitialFit.has(graph) && !firstFrameFit.has(graph)) {
          firstFrameFit.add(graph); resetCamera(false);
        }
      })
      .onEngineStop(() => {
        if (state?.graph !== graph || !pendingInitialFit.has(graph)) return;
        pendingInitialFit.delete(graph);
        resetCamera(false);
      })
      .onLinkHover(edge => {
        if (!state || state.graph !== graph) return;
        state.hoveredEdge = edge; refreshHighlight();
        container.style.cursor = edge ? "pointer" : "grab";
      })
      .onLinkClick(selectEdge)
      .onNodeHover(node => { showTip(node); container.style.cursor = node ? "pointer" : "grab"; })
      .onNodeClick((node) => {
        if (node.fx == null) { node.fx = node.x; node.fy = node.y; if (is3d) node.fz = node.z; }
        else { node.fx = null; node.fy = null; if (is3d) node.fz = null; }
        selectNode(state.byId.get(node.id) ?? node, false);
      })
      .onBackgroundClick(() => selectNode(null, false));
    graph.d3Force("compact", compactForce());
    graph.d3Force("charge")?.strength(-100);
    graph.d3Force("link")?.distance(100);
    if (is3d) {
      graph.nodeThreeObjectExtend(true).nodeThreeObject(nodeAccent);
      graph
        .linkOpacity(.7)
        .linkCurveRotation((link) => ["schema", "domain", "range"].includes(link.kind) ? .45 : 0);
      graph.cameraPosition(cameraPreset(data.nodes.length), { x: 0, y: 0, z: 0 }, 0);
    } else {
      graph.linkCanvasObjectMode(() => "replace").linkCanvasObject((link, context, scale) => {
        const { source: start, target: end } = link;
        if (![start?.x, start?.y, end?.x, end?.y].every(Number.isFinite)) return;
        const style = linkStyle(link);
        const dx0 = end.x - start.x, dy0 = end.y - start.y;
        const length = Math.hypot(dx0, dy0);
        if (!length) return;
        // Sigma rc5 triangle: length/thickness 2.5, width/thickness 2.
        // Draw the shaft and head as one fill: translucent colors must not overlap.
        const ux = dx0 / length, uy = dy0 / length;
        const radius = (state.view === "model" ? 7 : 5) / Math.sqrt(scale) + 2 / scale;
        const width = style.width / scale, headLength = width * 2.5, halfHead = width;
        const tipX = end.x - ux * radius, tipY = end.y - uy * radius;
        const tailX = tipX - ux * headLength, tailY = tipY - uy * headLength;
        const sx = start.x + ux * radius, sy = start.y + uy * radius;
        context.save(); context.fillStyle = style.color;
        context.beginPath();
        context.moveTo(sx - uy * width / 2, sy + ux * width / 2);
        context.lineTo(tailX - uy * width / 2, tailY + ux * width / 2);
        context.lineTo(tailX - uy * halfHead, tailY + ux * halfHead);
        context.lineTo(tipX, tipY);
        context.lineTo(tailX + uy * halfHead, tailY - ux * halfHead);
        context.lineTo(tailX + uy * width / 2, tailY - ux * width / 2);
        context.lineTo(sx + uy * width / 2, sy - ux * width / 2);
        context.closePath(); context.fill(); context.restore();
        if (state.view === "professional" && state.selectedEdge?.id !== link.id) return;
        const { source, target } = link;
        if (![source?.x, source?.y, target?.x, target?.y].every(Number.isFinite)) return;
        const focus = state.selected;
        if (state.view === "model" && link.kind === "subclass") return;
        if (focus && !highlightedLink(link) && !neighborhoodFor(focus.id, state.hops).edgeIds.has(link.id)) return;
        if (state.visibleData.links.length > 35 && !focus && state.selectedEdge?.id !== link.id && scale < 1.8) return;
        const curvature = 0;
        const dx = target.x - source.x, dy = target.y - source.y;
        const x = (source.x + target.x) / 2 + dy * curvature / 2;
        const y = (source.y + target.y) / 2 - dx * curvature / 2;
        context.save(); context.translate(x, y);
        let angle = Math.atan2(dy, dx); if (angle > Math.PI / 2 || angle < -Math.PI / 2) angle += Math.PI;
        context.rotate(angle); context.font = `${10 / scale}px "Geist", "Inter", sans-serif`;
        context.textAlign = "center"; context.textBaseline = "middle";
        const labelWidth = context.measureText(link.label).width + 8 / scale;
        context.fillStyle = canvasTheme.labelOutline; context.fillRect(-labelWidth / 2, -8 / scale, labelWidth, 16 / scale);
        context.fillStyle = state.view === "model" ? canvasTheme.relationLabelColor : canvasTheme.inheritanceLabelColor;
        context.fillText(link.label, 0, 0); context.restore();
      });
      graph.onRenderFramePre((context, scale) => {
        // World-anchored grid, matching the running 1516 rc5 drawWorldGrid.
        // Draw in CSS pixels so strokes stay crisp while the camera pans/zooms.
        const origin = graph.graph2ScreenCoords(0, 0);
        const width = graph.width(), height = graph.height();
        const pixelRatio = context.canvas.width / width;
        if (width > 0 && height > 0 && Number.isFinite(scale) && scale > 0) {
          context.save();
          context.setTransform(pixelRatio, 0, 0, pixelRatio, 0, 0);
          let step = 24;
          while (step * scale < 13) step *= 4;
          while (step * scale >= 52) step /= 4;
          for (let spacing = step * scale; spacing < 480; spacing *= 4) {
            const fade = Math.min(1, (spacing - 13) / (52 - 13));
            if (fade <= 0) continue;
            context.globalAlpha = fade;
            context.strokeStyle = canvasTheme.gridColor;
            context.lineWidth = 1; context.beginPath();
            for (let x = ((origin.x % spacing) + spacing) % spacing; x <= width; x += spacing) {
              const pixel = Math.round(x) + .5;
              context.moveTo(pixel, 0); context.lineTo(pixel, height);
            }
            for (let y = ((origin.y % spacing) + spacing) % spacing; y <= height; y += spacing) {
              const pixel = Math.round(y) + .5;
              context.moveTo(0, pixel); context.lineTo(width, pixel);
            }
            context.stroke();
          }
          context.restore();
        }
        if (!state.layered) return;
        const radii = [...new Set(graph.graphData().nodes.map(node => Math.round(Math.hypot(node.fx || 0, node.fy || 0))))];
        context.save(); context.strokeStyle = canvasTheme.layerGridColor; context.lineWidth = 1 / scale;
        context.setLineDash([4 / scale, 6 / scale]);
        radii.forEach(radius => { context.beginPath(); context.arc(0, 0, radius, 0, Math.PI * 2); context.stroke(); });
        context.restore();
      });
      graph.zoom(1.5, 0).nodeCanvasObjectMode(() => "replace").nodeCanvasObject((node, context, scale) => {
        const focus = state.selected ?? state.hovered;
        const visual = nodeInteraction(node);
        const emphasized = visual.ring !== null;
        const active = visual.opacity > .2;
        const color = nodePalette(node);
        context.save();
        context.globalAlpha = visual.opacity;
        const radius = (state.view === "model" ? 7 : 5) / Math.sqrt(scale);
        const square = state.view === "model" && node.kind === "class";
        const shape = r => { context.beginPath(); if (square) context.rect(node.x-r,node.y-r,r*2,r*2); else context.arc(node.x,node.y,r,0,Math.PI*2); };
        if (emphasized) {
          const outer = radius + 32 / scale;
          const glow = context.createRadialGradient(node.x,node.y,radius*.5,node.x,node.y,outer);
          glow.addColorStop(0,rgba(color,visual.halo)); glow.addColorStop(.5,rgba(color,visual.halo*.45)); glow.addColorStop(1,rgba(color,0));
          context.fillStyle = glow; context.fillRect(node.x-outer,node.y-outer,outer*2,outer*2);
        }
        shape(radius); context.fillStyle = color; context.fill();
        context.strokeStyle = canvasTheme.nodeStroke; context.lineWidth = 3 / scale; context.stroke();
        if (emphasized) { shape(radius+3/scale); context.strokeStyle=tintToWhite(color,visual.ring); context.lineWidth=1.5/scale; context.stroke(); }
        const showLabel = active && (emphasized || state.visibleData.nodes.length <= 35 || scale >= 1.8 || state.labelIds.has(node.id)
          || (focus && neighborhoodFor(focus.id, 1).distances.has(node.id)));
        if (showLabel) {
          const size = 12 / scale;
          context.font = `${visual.bold ? 600 : 500} ${size}px sans-serif`; context.textAlign = "center"; context.textBaseline = "bottom";
          context.lineWidth = 3 / scale; context.strokeStyle = canvasTheme.labelOutline;
          const label = String(node.label ?? node.id);
          const displayLabel = label.length > 26 ? `${label.slice(0, 23)}…` : label;
          if (visual.labelBackground) {
            const width = context.measureText(displayLabel).width + 12 / scale;
            context.fillStyle = visual.labelBackground;
            context.beginPath(); context.roundRect(node.x-width/2,node.y-radius-20/scale,width,20/scale,4/scale); context.fill();
          }
          context.fillStyle = visual.labelColor;
          if (!visual.labelBackground) context.strokeText(displayLabel, node.x, node.y - radius - 4 / scale);
          context.fillText(displayLabel, node.x, node.y - radius - 4 / scale);
        }
        context.restore();
      });
      graphDrawers.set(graph, graph.nodeCanvasObject());
    }
    if (state.layered) applyLayered(true);
    resize(); resetCamera(false); updateVisibleCount(data); refreshHighlight();
  };

  const applyLayered = (on) => {
    if (!state?.graph) return;
    const data = state.graph.graphData();
    const positions = on ? layeredGraphPositions(data.nodes, { dimensions: state.mode === "2d" ? 2 : 3 }) : null;
    data.nodes.forEach((node) => {
      if (!on) { node.fx = null; node.fy = null; node.fz = null; return; }
      const position = positions.get(node.id);
      node.x = node.fx = position.x;
      node.y = node.fy = position.y;
      node.vx = node.vy = 0;
      if (state.mode === "3d") { node.z = node.fz = position.z; node.vz = 0; }
      else node.fz = null;
    });
    state.graph.d3ReheatSimulation?.();
  };
  const toggleLayered = () => {
    state.layered = !state.layered;
    const button = dialog.querySelector("[data-graph-layer]");
    button.classList.toggle("is-active", state.layered);
    button.setAttribute("aria-pressed", String(state.layered));
    applyLayered(state.layered);
    resetCamera(true);
  };
  const setFilter = (kind) => { state.filter = EDGE_META[kind] ? kind : "all"; refreshView(); };
  const setMode = (mode) => {
    if (!state || state.mode === mode) return;
    state.graph?.pauseAnimation?.();
    state.mode = mode;
    dialog.querySelectorAll("[data-graph-mode]").forEach((button) => { button.classList.toggle("is-active", button.dataset.graphMode === mode); button.setAttribute("aria-pressed", String(button.dataset.graphMode === mode)); });
    renderViewControls();
    buildGraph();
  };
  const focusNode = (node) => {
    const rendered = state.graph.graphData().nodes.find((item) => item.id === node.id);
    if (!rendered || !Number.isFinite(rendered.x)) return;
    if (state.mode === "3d") {
      const distance = 70;
      const length = Math.hypot(rendered.x, rendered.y, rendered.z) || 1;
      const ratio = 1 + distance / length;
      state.graph.cameraPosition({ x: rendered.x * ratio, y: rendered.y * ratio, z: rendered.z * ratio }, rendered, reducedMotion() ? 0 : 1200);
    } else {
      state.graph.centerAt(rendered.x, rendered.y, reducedMotion() ? 0 : 1200);
      state.graph.zoom(3, reducedMotion() ? 0 : 1200);
    }
  };
  const locateNode = (value) => {
    const query = value.trim().toLowerCase();
    if (!query) { renderNodeList(); dialog.querySelector("[data-graph-search-status]").textContent = ""; return; }
    const matches = state.visibleData.nodes.filter(item => `${item.label} ${item.id}`.toLowerCase().includes(query));
    const node = matches.find(item => item.id.toLowerCase() === query || item.label.toLowerCase() === query) ?? matches[0];
    dialog.querySelector("[data-graph-search-status]").textContent = matches.length ? `当前视图找到 ${matches.length} 个节点` : "当前视图没有匹配节点，可切换视图或所属类后搜索。";
    renderNodeList(matches);
    if (!node) return;
    selectNode(node, true);
  };
  const resetCamera = (animate = true) => {
    if (!state?.graph) return;
    const rect = dialog.querySelector(`[data-graph-canvas="${state.mode}"]`).getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) return;
    // Fit the actual node bounds to the current viewport, including narrow
    // windows. A node-count-only camera distance clips sparse published graphs.
    const duration = animate && !reducedMotion() ? 800 : 0;
    if (state.mode === "3d") {
      const camera = state.graph.camera();
      const frame = perspectiveGraphFrame(state.graph.graphData().nodes, camera.position, camera.fov, rect.width / rect.height);
      if (frame) {
        const position = Object.fromEntries(["x", "y", "z"].map(axis => [axis, frame.center[axis] + (frame.position[axis] - frame.center[axis]) / state.viewScale]));
        state.graph.cameraPosition(position, frame.center, duration);
      }
    } else {
      const points = state.graph.graphData().nodes.filter(node => Number.isFinite(node.x) && Number.isFinite(node.y));
      if (!points.length) return;
      const xs = points.map(node => node.x), ys = points.map(node => node.y);
      const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
      const scale = Math.min((rect.width - 150) / (maxX - minX + 45), (rect.height - 160) / (maxY - minY + 45));
      state.graph.centerAt((minX + maxX) / 2, (minY + maxY) / 2, duration);
      state.graph.zoom(Math.min(8, Math.max(.05, scale * state.viewScale)), duration);
    }
  };
  const zoomGraph = (factor) => {
    if (!state?.graph) return;
    pendingInitialFit.delete(state.graph);
    const previousScale = state.viewScale;
    state.viewScale = Math.min(3, Math.max(.4, previousScale * factor));
    factor = state.viewScale / previousScale;
    const duration = reducedMotion() ? 0 : 300;
    if (state.mode === "2d") state.graph.zoom(state.graph.zoom() * factor, duration);
    else {
      const target = state.graph.controls().target;
      const position = state.graph.camera().position;
      state.graph.cameraPosition(Object.fromEntries(["x", "y", "z"].map(axis => [axis, target[axis] + (position[axis] - target[axis]) / factor])), target, duration);
    }
  };
  const resize = () => {
    if (!state?.graph) return;
    const rect = dialog.querySelector(`[data-graph-canvas="${state.mode}"]`).getBoundingClientRect();
    if (rect.width > 0 && rect.height > 0 && (state.graph.width() !== rect.width || state.graph.height() !== rect.height)) {
      state.graph.width(rect.width).height(rect.height);
      resetCamera(false);
    }
  };
  const renderNodeList = (nodes = state.visibleData.nodes) => {
    if (state.view === "model") nodes = state.payload.nodes.filter(node => state.listCategory === "properties" ? ["objectProperty", "dataProperty"].includes(node.kind) : node.kind === "class");
    const query = dialog.querySelector("[data-graph-list-search]").value.trim().toLocaleLowerCase();
    if (query) nodes = nodes.filter(node => `${node.label} ${node.id}`.toLocaleLowerCase().includes(query));
    dialog.querySelectorAll("[data-graph-category]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.graphCategory === state.listCategory)));
    const items = nodes.slice(0, 100);
    dialog.querySelector("[data-graph-node-list]").innerHTML = `${items.map(node => `<button type="button" class="${state.selected?.id === node.id ? "is-selected" : ""}" data-graph-focus="${escapeHtml(node.id)}" style="--node-accent:${nodePalette(node)}" title="${escapeHtml(node.label)}"><i style="background:${nodePalette(node)}" class="is-${escapeHtml(node.kind)}"></i><span>${escapeHtml(node.label)}</span></button>`).join("")}${!nodes.length ? "<p>没有匹配的对象</p>" : `<p class="owa-graph-list-count">${items.length} / 共 ${nodes.length}</p>`}${nodes.length > items.length ? "<p>可通过筛选查找其余对象。</p>" : ""}`;
  };
  const updateVisibleCount = (data = state.graph.graphData()) => {
    dialog.querySelector("[data-graph-visible]").textContent = `${state.view === "instances" ? "预览 · " : ""}${data.nodes.length} 节点 · ${data.links.length} 关系`;
    dialog.querySelector("[data-graph-nav-count]").textContent = `${data.nodes.length} 节点`;
    const nodeCounts = data.nodes.reduce((counts, node) => ({ ...counts, [node.kind]: (counts[node.kind] ?? 0) + 1 }), {});
    const edgeCounts = data.links.reduce((counts, edge) => ({ ...counts, [edge.kind]: (counts[edge.kind] ?? 0) + 1 }), {});
    dialog.querySelector("[data-graph-legend]").innerHTML = `${Object.entries(TYPE_META).filter(([kind]) => nodeCounts[kind]).map(([kind, meta]) => `<div><i class="is-${kind}"></i><span><b>${meta[1]}</b></span><strong>${nodeCounts[kind]}</strong></div>`).join("")}<h4>关系类型</h4>${Object.entries(EDGE_META).filter(([kind]) => edgeCounts[kind]).map(([kind, meta]) => `<div><i class="is-edge-${kind}"></i><span><b>${meta[1]}</b></span><strong>${edgeCounts[kind]}</strong></div>`).join("")}`;
    renderNodeList();
    const empty = dialog.querySelector("[data-graph-empty]");
    empty.hidden = data.nodes.length > 0;
    if (!empty.hidden) {
      const needsClass = !state.instanceClassId;
      const hasSeeds = instanceSeeds().size > 0;
      const needsInstance = !state.instanceId && !state.allClassInstances;
      empty.innerHTML = state.view === "instances"
        ? `<strong>${needsClass ? "当前版本没有可展示的实例" : hasSeeds && needsInstance ? "选择一个具体实例" : "该类暂无可展示实例"}</strong><span>${needsClass ? "当前加载范围未包含实例数据；本体定义仍可在「本体」中查看。" : hasSeeds && needsInstance ? "选择具体对象查看关联，也可主动展开该类全部预览实例。" : "当前预览范围没有该类实例；这不代表完整业务数据中不存在。"}</span>`
        : `<strong>当前视图没有可展示内容</strong><span>可调整关系筛选，或切换专业视图检查模型定义。</span>`;
    }
  };

  const setHops = (hops) => {
    state.hops = Math.max(1, Math.min(3, hops));
    dialog.querySelectorAll("[data-graph-hops]").forEach((button) => {
      const active = Number(button.dataset.graphHops) === state.hops;
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    if (state.view === "instances" && state.instanceClassId) buildGraph();
    renderDetails(); refreshHighlight();
  };
  const setDetailTab = (tab) => { state.detailTab = tab; renderDetails(); };
  const open = async (asset) => {
    ensureDialog();
    if (state) close(false);
    const revision = ++openRevision;
    const shell = dialog.querySelector(".owa-graph-shell");
    shell.classList.remove("has-selection", "show-object-filter");
    shell.classList.add("is-loading");
    shell.dataset.view = "model";
    shell.setAttribute("aria-busy", "true");
    dialog.querySelector("[data-graph-visible]").textContent = "";
    dialog.querySelector("[data-graph-filter-info]").textContent = "";
    dialog.querySelector("[data-graph-node-list]").innerHTML = "";
    dialog.querySelector("[data-graph-tip]").hidden = true;
    dialog.querySelectorAll("[data-graph-view]").forEach(button => {
      const active = button.dataset.graphView === "model";
      button.classList.toggle("is-active", active); button.setAttribute("aria-pressed", String(active));
    });
    const isTemplate = Boolean(asset.templateId);
    dialog.querySelector(".owa-graph-readonly").textContent = isTemplate ? "行业模板 · 只读" : "已发布 · 只读";
    const returnLabel = isTemplate ? "返回行业模板" : "返回本体管理";
    const closeButton = dialog.querySelector("[data-graph-close]");
    closeButton.textContent = returnLabel;
    closeButton.setAttribute("aria-label", returnLabel);
    dialog.querySelector(".owa-graph-search input").value = "";
    dialog.querySelector("[data-graph-list-search]").value = "";
    const previousFocus = document.activeElement;
    dialog.hidden = false; document.body.classList.add("owa-graph-open");
    watchCanvasTheme();
    dialog.querySelector("#owa-graph-title").textContent = asset.name;
    dialog.querySelector("[data-graph-subtitle]").textContent = `版本 ${asset.version} · ${asset.templateId ? "行业模板" : "正式本体"}`;
    dialog.querySelector("[data-graph-preview-scope]").hidden = true;
    dialog.querySelector("[data-graph-preview-scope]").open = false;
    dialog.querySelector("[data-graph-loading]").hidden = false;
    dialog.querySelector("[data-graph-loading] strong").textContent = "正在加载本体图谱";
    dialog.querySelector("[data-graph-loading] span").textContent = "读取正式发布版本中的节点与关系…";
    dialog.querySelector("[data-graph-empty]").hidden = true;
    try {
      const rawPayload = await (isTemplate
        ? window.__ORION_ONTOLOGY_API__.templateGraph(asset.templateId)
        : window.__ORION_ONTOLOGY_API__.graph(asset.sourceProjectId));
      if (revision !== openRevision || dialog.hidden) return;
      const payload = { ...rawPayload, nodes: rawPayload.nodes.map((node) => ({ ...node })), edges: rawPayload.edges.map((edge) => ({ ...edge })) };
      const previewNotice = dialog.querySelector("[data-graph-instance-preview]");
      previewNotice.textContent = instancePreviewNotice(payload.instance_preview);

      state = { asset, payload, previousFocus, mode: "2d", view: "model", listCategory: "class", showHierarchy: true, instanceClassId: null, instanceId: null, allClassInstances: false, selectedEdge: null, visibleData: { nodes: [], links: [] }, filter: "all", viewScale: 1, layered: false, hops: 1, detailTab: "overview", selected: null, hovered: null, graph: null, graphs: { "3d": null, "2d": null }, graphFilters: { "3d": null, "2d": null }, byId: new Map(payload.nodes.map((node) => [node.id, node])) };
      const degree = new Map();
      payload.edges.forEach((edge) => [endpointId(edge.source), endpointId(edge.target)].forEach((id) => degree.set(id, (degree.get(id) || 0) + 1)));
      state.labelIds = new Set(payload.nodes.filter((node) => node.kind === "class").sort((a, b) => (degree.get(b.id) || 0) - (degree.get(a.id) || 0)).slice(0, 10).map((node) => node.id));
      dialog.querySelector("[data-graph-filter]").value = "all";
      dialog.querySelector("[data-graph-layer]").classList.remove("is-active");
      dialog.querySelector("[data-graph-layer]").setAttribute("aria-pressed", "false");
      dialog.querySelectorAll("[data-graph-mode]").forEach((button) => { button.classList.toggle("is-active", button.dataset.graphMode === "2d"); button.setAttribute("aria-pressed", String(button.dataset.graphMode === "2d")); });
      setHops(1);
      dialog.querySelector("[data-graph-loading] strong").textContent = "正在准备视图";
      dialog.querySelector("[data-graph-loading] span").textContent = "整理节点与关系…";
      refreshView(); window.addEventListener("resize", resize);
      // Let the warmed layout and initial camera fit paint before revealing it.
      requestAnimationFrame(() => requestAnimationFrame(() => {
        if (revision !== openRevision || dialog.hidden) return;
        dialog.querySelector("[data-graph-loading]").hidden = true;
        shell.classList.remove("is-loading");
        shell.setAttribute("aria-busy", "false");
      }));
      canvasObserver = new ResizeObserver(() => { resize(); });
      canvasObserver.observe(dialog.querySelector(".owa-graph-stage"));
      dialog.querySelector("[data-graph-close]").focus();
    } catch (error) {
      if (revision !== openRevision || dialog.hidden) return;
      shell.classList.remove("is-loading");
      shell.setAttribute("aria-busy", "false");
      dialog.querySelector("[data-graph-loading]").hidden = true;
      const empty = dialog.querySelector("[data-graph-empty]");
      empty.hidden = false; empty.innerHTML = `<strong>图谱暂时无法生成</strong><span>${escapeHtml(error instanceof Error ? error.message : "未知错误")}</span>`;
    }
  };
  const close = (restoreFocus = true) => {
    if (!dialog) return;
    openRevision += 1;
    themeDisposer?.(); themeDisposer = null;
    const previousFocus = state?.previousFocus;
    Object.values(state?.graphs ?? {}).forEach((graph) => graph?.pauseAnimation?.());
    dialog.querySelectorAll("[data-graph-canvas]").forEach((canvas) => { canvas.innerHTML = ""; });
    window.removeEventListener("resize", resize);
    canvasObserver?.disconnect(); canvasObserver = null;
    dialog.hidden = true; document.body.classList.remove("owa-graph-open"); state = null;
    if (restoreFocus) previousFocus?.focus?.();
  };

  window.__ORION_ONTOLOGY_GRAPH_VIEWER__ = { open, close };
})();
