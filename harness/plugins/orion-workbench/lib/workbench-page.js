const PREFIX = "/orion-workbench-assets/";

export function validateAssetManifest(manifest) {
  if (!manifest || manifest.schemaVersion !== 1 || manifest.mode !== "core"
    || !Array.isArray(manifest.scripts) || !manifest.scripts.length || !Array.isArray(manifest.styles)
    || (manifest.assetBase != null && manifest.assetBase !== PREFIX)) {
    throw new Error("工作台资源清单不受支持，请重新安装匹配的插件版本。");
  }
  const paths = new Set();
  for (const [entries, extension] of [[manifest.scripts, "js"], [manifest.styles, "css"]]) {
    const pattern = new RegExp(`^${PREFIX}[A-Za-z0-9_./-]+\\.${extension}(?:\\?[A-Za-z0-9_=&.-]+)?$`, "u");
    for (const path of entries) {
      if (typeof path !== "string" || !pattern.test(path) || path.includes("..") || paths.has(path)) {
        throw new Error("工作台资源地址或文件类型不合法，请重新安装插件。");
      }
      paths.add(path);
    }
  }
  return manifest;
}

/** Own only the elements this loader appended; failed loads may be retried. */
export function createWorkbenchAssetLoader({ window: hostWindow = globalThis.window,
  document: hostDocument = hostWindow?.document, fetch: fetcher = globalThis.fetch, timeoutMs = 20_000 } = {}) {
  let assetsPromise, scriptManifest, activeAttempt, epoch = 0;
  const ownedStyles = new Set();
  const load = function load() {
    if (assetsPromise) return assetsPromise;
    const version = epoch;
    const abort = new AbortController();
    const attempt = { abort };
    activeAttempt = attempt;
    const deadline = setTimeout(() => abort.abort(new Error("工作台资源加载超时，请检查本地服务后重试。")), timeoutMs);
    // Bound the whole attempt, including response bodies and stalled browser
    // resource events. Late responses must never mount into a newer attempt.
    const wait = (promise) => new Promise((resolve, reject) => {
      const cancel = () => reject(abort.signal.reason);
      if (abort.signal.aborted) cancel();
      else abort.signal.addEventListener("abort", cancel, { once: true });
      Promise.resolve(promise).then(resolve, reject)
        .finally(() => abort.signal.removeEventListener("abort", cancel));
    });
    const pending = (async () => {
      if (!hostWindow || !hostDocument?.head || typeof fetcher !== "function") {
        throw new Error("工作台资源加载环境尚未就绪，请重新打开工作台。");
      }
      let manifest = scriptManifest;
      if (!manifest) {
        const response = await wait(fetcher(`${PREFIX}workbench-assets.json`, {
          credentials: "same-origin", headers: { Accept: "application/json" }, signal: abort.signal,
        }));
        if (!response.ok) throw new Error(`工作台资源读取失败（${response.status}），请重新打开或重新安装插件。`);
        manifest = validateAssetManifest(await wait(response.json()));
      }
      abort.signal.throwIfAborted();
      if (version !== epoch) throw new Error("工作台插件已停用，已取消资源加载。");
      const previousNative = hostWindow.__ORION_NATIVE_PLUGIN__;
      const previousMode = hostWindow.__OWA_PRODUCT_MODE__;
      hostWindow.__ORION_NATIVE_PLUGIN__ = true;
      hostWindow.__OWA_PRODUCT_MODE__ = "core";
      const added = [];
      const append = (tag, path) => new Promise((resolve, reject) => {
        abort.signal.throwIfAborted();
        const node = hostDocument.createElement(tag);
        if (tag === "link") { node.rel = "stylesheet"; node.href = path; }
        else { node.src = path; node.async = false; }
        node.dataset.orionWorkbenchAsset = path;
        let settled = false;
        const finish = (error) => {
          if (settled) return;
          settled = true;
          node.onload = null;
          node.onerror = null;
          abort.signal.removeEventListener("abort", cancel);
          if (error) reject(error);
          else resolve();
        };
        const cancel = () => finish(abort.signal.reason);
        abort.signal.addEventListener("abort", cancel, { once: true });
        node.onload = () => finish();
        node.onerror = () => finish(new Error(`工作台资源未加载：${path}。请重新打开工作台重试。`));
        added.push(node);
        if (tag === "link") ownedStyles.add(node);
        hostDocument.head.append(node);
      });
      try {
        await Promise.all(manifest.styles.map((path) => append("link", path)));
        if (!scriptManifest) for (const path of manifest.scripts) await append("script", path);
        if (version !== epoch) throw new Error("工作台插件已停用，已取消资源加载。");
        scriptManifest = manifest;
        return manifest;
      } catch (error) {
        abort.abort(error);
        for (const node of added) { node.onload = null; node.onerror = null; ownedStyles.delete(node); node.remove(); }
        if (version === epoch) {
          if (previousNative === undefined) delete hostWindow.__ORION_NATIVE_PLUGIN__;
          else hostWindow.__ORION_NATIVE_PLUGIN__ = previousNative;
          if (previousMode === undefined) delete hostWindow.__OWA_PRODUCT_MODE__;
          else hostWindow.__OWA_PRODUCT_MODE__ = previousMode;
        }
        throw error;
      }
    })();
    assetsPromise = pending;
    pending.catch(() => { if (assetsPromise === pending) assetsPromise = null; });
    pending.finally(() => {
      clearTimeout(deadline);
      if (activeAttempt === attempt) activeAttempt = null;
    }).catch(() => {});
    return pending;
  };
  // A complete script set is safe to retain for a same-page re-enable. Styles
  // belong to this plugin's live lifetime and must not alter upstream fallback.
  load.releaseStyles = () => {
    epoch++;
    activeAttempt?.abort.abort(new Error("工作台插件已停用，已取消资源加载。"));
    for (const node of ownedStyles) node.remove();
    ownedStyles.clear();
    assetsPromise = null;
  };
  return load;
}

let defaultLoader;
export function loadWorkbenchAssets() {
  defaultLoader ??= createWorkbenchAssetLoader();
  return defaultLoader();
}

/** Mount business renderers only inside the React-owned main-slot subtree. */
export function mountWorkbench({ root, navigation, state, center, engineering, layout, onError = () => {} }) {
  if (!root || !navigation || typeof state?.subscribe !== "function" || typeof center?.activate !== "function"
    || typeof engineering?.mount !== "function" || typeof layout?.selectPanel !== "function") {
    throw new Error("工作台页面组件尚未就绪，请重新打开或重新安装匹配版本。");
  }
  const releaseRoot = engineering.mount(root);
  let unsubscribe, disposed = false, initialError = null, initializing = true;
  const report = (error) => {
    if (disposed) return;
    if (initializing) initialError = error;
    onError(error);
  };
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    try { unsubscribe?.(); }
    finally { try { center.dispose?.(); } finally { releaseRoot?.(); } }
  };
  try {
    unsubscribe = state.subscribe((route) => {
      if (disposed) return;
      try {
        if (route.primary === "chat") { layout.selectPanel(null); return; }
        const activated = center.activate(route, navigation);
        if (activated === false) throw new Error("本体工作台未能挂载到自己的页面，请重新打开工作台。");
        if (activated?.then) activated.catch(report);
      } catch (error) { report(error); }
    });
    initializing = false;
    if (initialError) throw initialError;
    return dispose;
  } catch (error) { dispose(); throw error; }
}
