import { createSessionBridge } from "./lib/session-bridge.js";
import { installNativeChat } from "./lib/native-chat.js";
import { installNativeEvidence } from "./lib/native-evidence.js";
import { installEngineeringStart } from "./lib/engineering-start.js";
import { createWorkbenchAssetLoader, mountWorkbench } from "./lib/workbench-page.js";
import packageMetadata from "./package.json" with { type: "json" };

// The packaged client owns its brand bytes. Hot enabling a bundle may mount
// client slots before the Host asset route is ready; branding must not race it.
const packagedBrandAssets = typeof __ORION_BRAND_ASSETS__ === "undefined" ? {} : __ORION_BRAND_ASSETS__;
export function createWorkbenchClientPlugin(require, hostWindow = globalThis.window, brandAssets = packagedBrandAssets) {
    const React = require("react");
    const PANEL = "orion-workbench";
    const SECTIONS = ["engineering", "manage", "templates"];
    const ownsPanel = (id) => id === PANEL;
    const NS = "orion-workbench";
    const dictionaries = {
      zh: { panel: "本体中心", title: "AHS 本体工作台", engineering: "本体工程", manage: "本体管理", templates: "行业模板",
        create: "发起工程", back: "返回会话", headline: "探索属于你的智能宇宙", preview: "预览版", loading: "正在加载本体工作台…", retry: "重新加载", dismiss: "关闭提示", openPage: "回到本体中心" },
      en: { panel: "Ontology Center", title: "AHS Ontology Workbench", engineering: "Engineering", manage: "Published Ontologies", templates: "Industry Templates",
        create: "Start a Project", back: "Back to Conversation", headline: "Explore Your Intelligent Universe", preview: "Preview", loading: "Loading the ontology workbench…", retry: "Reload", dismiss: "Dismiss", openPage: "Open Ontology Center" },
    };
    // Official hot enablement may rebuild the ModuleLoader factory. Keep the
    // already evaluated business scripts with the page, not that factory.
    const assetKey = Symbol.for("dsh-orion-workbench.assets");
    const assets = hostWindow[assetKey] ??= { version: packageMetadata.version, owners: new Set(),
      load: createWorkbenchAssetLoader({ window: hostWindow, fetch: (url, options) => hostWindow.fetch(url, options) }) };
    const loadAssets = () => assets.version === packageMetadata.version ? assets.load()
      : Promise.reject(new Error("工作台版本已更新，请刷新页面后继续。"));
    return {
      inject: ["slots", "layout", "locale", "theme", "sessions", "workspaces", "uiWorkspace", "remote", "remote.agentPresets"],
      apply(ctx) {
        let controller, chatPromise, releaseWebMcp, disposed = false, lastError = null, routeSubscriptionInstalled = false;
        const assetOwner = {};
        ctx.effect(() => {
          assets.owners.add(assetOwner);
          return () => { if (assets.owners.delete(assetOwner) && !assets.owners.size) assets.load.releaseStyles(); };
        });
        const errorListeners = new Set();
        const bridge = createSessionBridge(ctx);
        ctx.effect(() => ctx.locale.register(NS, dictionaries));
        const t = ctx.locale.bind(NS);
        const readError = () => lastError;
        const subscribeError = (listener) => { errorListeners.add(listener); return () => errorListeners.delete(listener); };
        const reportError = (error) => {
          if (disposed) return;
          lastError = { message: error instanceof Error ? error.message : "本体工作台操作未完成，请回到当前页面核对后重试。" };
          for (const listener of errorListeners) listener();
        };
        const dismissError = () => { lastError = null; for (const listener of errorListeners) listener(); };
        const activePanel = () => ctx.layout.panelInfo.getSnapshot().activePanelId;
        const syncPanel = (route) => {
          if (route.primary === "ontology") {
            if (activePanel() !== PANEL) ctx.layout.selectPanel(PANEL);
          } else if (ownsPanel(activePanel())) ctx.layout.selectPanel(null);
        };
        const bindRouteNavigation = () => {
          if (routeSubscriptionInstalled) return;
          const state = hostWindow.__ORION_SHELL_STATE__;
          if (typeof state?.get !== "function" || typeof state.subscribe !== "function") {
            throw new Error("本体工作台导航尚未完整加载，请重新打开工作台。");
          }
          // The first subscription value is existing state, not a user
          // navigation. Do not replace the sidebar seat the user just chose.
          let signature = JSON.stringify(state.get());
          ctx.effect(() => state.subscribe((route) => {
            const next = JSON.stringify(route);
            if (next === signature || disposed) return;
            signature = next;
            syncPanel(route);
          }));
          routeSubscriptionInstalled = true;
        };
        ctx.effect(() => {
          const previous = hostWindow.__ORION_DSH_SESSIONS__;
          hostWindow.__ORION_DSH_SESSIONS__ = bridge;
          return () => {
            if (hostWindow.__ORION_DSH_SESSIONS__ !== bridge) return;
            if (previous) hostWindow.__ORION_DSH_SESSIONS__ = previous;
            else delete hostWindow.__ORION_DSH_SESSIONS__;
          };
        });
        const chat = () => {
          if (disposed) return Promise.reject(new Error("本体工作台插件已停用，请重新启用后重试。"));
          // Install before the business scripts capture the compatibility face.
          // All shell navigation stays in the plugin-owned business state.
          controller ??= installNativeChat({ bridge,
            state: { navigate: (route) => {
              if (!hostWindow.__ORION_SHELL_STATE__) throw new Error("本体工作台尚未完成加载，请重新打开后重试。");
              hostWindow.__ORION_SHELL_STATE__.navigate(route);
            } }, fetch: hostWindow.fetch.bind(hostWindow), window: hostWindow, onError: reportError,
            onChange: (value) => { if (!value.error) dismissError(); } });
          if (!chatPromise) {
            const pending = loadAssets().then(() => {
              if (disposed) throw new Error("本体工作台插件已停用。");
              releaseWebMcp ??= hostWindow.__ORION_DOCUMENT_WEBMCP__?.acquire();
              bindRouteNavigation();
              return controller;
            });
            chatPromise = pending;
            pending.catch(() => { if (chatPromise === pending) chatPromise = null; });
          }
          return chatPromise;
        };
        installNativeEvidence({ ctx, React, bridge, controller: () => controller,
          fetch: hostWindow.fetch.bind(hostWindow), onError: reportError,
          analytics: () => hostWindow.__ORION_ANALYTICS_PANEL__, ensureReady: chat });
        installEngineeringStart({ ctx, React, bridge, ensureReady: chat, window: hostWindow });
        function useDarkBrand() {
          const [dark, setDark] = React.useState(() => ctx.theme.getTheme().active.colorScheme === "dark");
          React.useEffect(() => ctx.on("theme/change", (snapshot) => setDark(snapshot.active.colorScheme === "dark")), []);
          return dark;
        }
        function BrandMark({ size = 24, className }) {
          const dark = useDarkBrand();
          const file = `ahs-icon-${dark ? "white" : "black"}.svg`;
          return React.createElement("img", { src: brandAssets[file] ?? `/orion-workbench-assets/${file}`,
            alt: "", "aria-hidden": true, className, width: size, height: size,
            style: { display: "block", width: size, height: size, flexShrink: 0, objectFit: "contain" } });
        }
        function BrandName() {
          const dark = useDarkBrand();
          const file = `ahs-wordmark-${dark ? "white" : "black"}.svg`;
          return React.createElement("img", { src: brandAssets[file] ?? `/orion-workbench-assets/${file}`,
            alt: "AHS", width: 92, height: 24, style: { display: "block", objectFit: "contain" } });
        }
        function HeroBrand({ size = 34 }) {
          // RC2 exposes a brand slot but no headline slot. Keep the replacement
          // inside our slot and scope the presentation adapter to its own marker.
          // Unregistering the slot also removes this stylesheet and restores the
          // official headline; no native nodes or locale registries are changed.
          return React.createElement("span", { "data-orion-hero-brand": true, style: { display: "inline-flex", alignItems: "center", flexWrap: "wrap", justifyContent: "center", gap: "10px" } },
            React.createElement("style", null, "span:has(> [data-slot=\"conversation.hero.brand.mark\"] [data-orion-hero-brand]) + span { display: none !important; } [data-orion-hero-brand] .orion-hero-preview { font-size: 12px; line-height: 18px; padding: 1px 7px; border-radius: 999px; color: var(--dsw-alias-label-primary-bluish); background: var(--dsw-alias-state-business-tertiary); align-self: flex-start; margin-top: 2px; }"),
            React.createElement(BrandMark, { size }), React.createElement("span", null, t("headline")),
            React.createElement("span", { className: "orion-hero-preview" }, t("preview")));
        }
        function NavigationIcon({ kind = "center", size = 20, className }) {
          const paths = {
            center: ["M12 8v4m-7 3v-3h14v3", "M9 2h6v6H9zM2 15h6v6H2zM16 15h6v6h-6z"],
            engineering: ["M14 4l6 6M4 20l4-1 12-12a2.1 2.1 0 0 0-3-3L5 16z", "M13 20h7"],
            manage: ["M3 7h6l2-3h9a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z", "M8 13l3 3 5-5"],
            templates: ["M12 3l9 5-9 5-9-5z", "M3 12l9 5 9-5M3 16l9 5 9-5"],
            add: ["M12 5v14M5 12h14"], back: ["M19 12H5m6-6-6 6 6 6"],
          };
          return React.createElement("svg", { width: size, height: size, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
            strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true, className,
            style: { display: "block", flexShrink: 0 } },
            ...(paths[kind] ?? paths.center).map((d, index) => React.createElement("path", { d, key: index })));
        }
        function ErrorNotice({ readError, subscribeError, dismissError, showPage, t }) {
          const error = React.useSyncExternalStore(subscribeError, readError, readError);
          if (!error) return null;
          return React.createElement("aside", { role: "alert", style: { position: "fixed", insetInlineEnd: 20, bottom: 20,
            maxWidth: 440, padding: 16, borderRadius: 12, pointerEvents: "auto", zIndex: 1000,
            color: "var(--dsw-alias-label-primary)", background: "var(--dsw-alias-bg-layer-2)",
            border: "1px solid var(--dsw-alias-border-l2)", boxShadow: "0 8px 32px #0002" } },
            React.createElement("p", null, error.message),
            React.createElement("button", { type: "button", onClick: showPage }, t("openPage")),
            React.createElement("button", { type: "button", onClick: dismissError }, t("dismiss")));
        }
        function Page({ initialize, navigate, createSession, returnToChat, readError, subscribeError, dismissError, t }) {
          const body = React.useRef(null), navigation = React.useRef(null);
          const [section, setSection] = React.useState(() => hostWindow.__ORION_SHELL_STATE__?.get()?.section ?? "engineering");
          const [error, setError] = React.useState(null);
          const [ready, setReady] = React.useState(false);
          const [attempt, setAttempt] = React.useState(0);
          const operationError = React.useSyncExternalStore(subscribeError, readError, readError);
          React.useEffect(() => {
            let unmounted = false, release;
            setError(null);
            setReady(false);
            initialize(body.current, navigation.current, () => unmounted, (route) => setSection(route.section)).then((dispose) => {
              if (unmounted) { dispose?.(); return; }
              release = dispose;
              setReady(true);
            }).catch((error) => { if (!unmounted) setError(error.message); });
            return () => { unmounted = true; release?.(); };
          }, [attempt]);
          const create = () => createSession().catch(reportError);
          const message = error ?? operationError?.message;
          return React.createElement("section", { "data-orion-workbench-root": "true", "aria-label": "ORION 本体工作台", style: { display: "flex", flexDirection: "column", height: "100%", minHeight: 0 } },
            React.createElement("header", { className: "orion-workbench-navigation", style: { paddingInlineStart: "calc(var(--dsh-frame-leading-clearance, 0px) + 24px)" } },
              React.createElement("div", { className: "orion-workbench-heading" },
                React.createElement(NavigationIcon, { size: 22 }), React.createElement("strong", null, t("panel"))),
              React.createElement("div", { className: "orion-workbench-actions" },
                React.createElement("button", { type: "button", onClick: returnToChat }, React.createElement(NavigationIcon, { kind: "back", size: 16 }), t("back")),
                React.createElement("button", { type: "button", className: "is-primary", onClick: create, disabled: !ready }, React.createElement(NavigationIcon, { kind: "add", size: 16 }), t("create")))),
            React.createElement("nav", { className: "orion-workbench-sections", "aria-label": t("panel") },
              ...SECTIONS.map((key) => React.createElement("button", { key, type: "button", "aria-current": section === key ? "page" : undefined,
                disabled: !ready, onClick: () => navigate(key) }, React.createElement(NavigationIcon, { kind: key, size: 18 }), t(key)))),
            message ? React.createElement("p", { role: "alert" }, message,
              React.createElement("button", { type: "button", onClick: () => { dismissError(); setAttempt(value => value + 1); } }, t("retry"))) : null,
            !ready && !error ? React.createElement("p", { role: "status" }, t("loading")) : null,
            React.createElement("div", { style: { display: "flex", flex: 1, minHeight: 0, overflow: "hidden" } },
              React.createElement("nav", { ref: navigation, hidden: true, "aria-hidden": true, style: { display: "none" } }),
              React.createElement("div", { ref: body, "data-orion-workbench-content": "true", style: { flex: 1, minWidth: 0, overflow: "auto" } })));
        }
        const face = {
          initialize: async (root, navigation, cancelled, onRoute = () => {}) => {
            await chat();
            if (cancelled()) return null;
            const state = hostWindow.__ORION_SHELL_STATE__;
            if (!state || !hostWindow.__ORION_ONTOLOGY_CENTER__ || !hostWindow.__ORION_ENGINEERING_BRIDGE__) {
              throw new Error("本体工作台组件未完整加载，请重新打开或重新安装插件。");
            }
            const current = state.get();
            if (current.primary !== "ontology") {
              state.navigate({ primary: "ontology", section: "engineering", projectId: null, stage: null });
            }
            syncPanel(state.get());
            const pageState = { subscribe: (listener) => state.subscribe((route) => {
              onRoute(route);
              listener(route);
            }) };
            return mountWorkbench({ root, navigation, state: pageState, center: hostWindow.__ORION_ONTOLOGY_CENTER__, engineering: hostWindow.__ORION_ENGINEERING_BRIDGE__, layout: ctx.layout, onError: reportError });
          },
          navigate: (section) => {
            if (SECTIONS.includes(section)) hostWindow.__ORION_SHELL_STATE__?.navigate({ primary: "ontology", section, projectId: null, stage: null });
          },
          createSession: async () => (await chat()).openNewSession(),
          returnToChat: () => { hostWindow.__ORION_SHELL_STATE__?.navigate({ primary: "chat" }); ctx.layout.selectPanel(null); },
          readError, subscribeError, dismissError,
        };
        ctx.slots.inject("sidebar.brand.mark", () => ctx.slots.inject("sidebar.brand.name", function* () {
          yield ctx.slots.register({ name: "sidebar.brand.mark", priority: -10 }, BrandMark);
          yield ctx.slots.register({ name: "sidebar.brand.name", priority: -10 }, BrandName);
        }));
        ctx.slots.inject("conversation.hero.brand.mark", () => ctx.slots.register({ name: "conversation.hero.brand.mark", priority: -10, locale: NS }, HeroBrand));
        ctx.slots.inject("main", () => ctx.slots.register({ name: "main", key: PANEL, locale: NS, inject: () => face }, Page));
        ctx.slots.inject("sidebar.panellist", () => ctx.slots.register({ name: "sidebar.panellist", id: PANEL,
          order: 10, label: () => t("panel"), locale: NS }, NavigationIcon));
        ctx.slots.inject("shell.overlay", () => ctx.slots.register({ name: "shell.overlay", id: `${NS}.error`, locale: NS,
          inject: () => ({ readError, subscribeError, dismissError,
            showPage: () => ctx.layout.selectPanel(PANEL) }) }, ErrorNotice));
        ctx.effect(() => () => {
          if (disposed) return;
          disposed = true;
          controller?.dispose();
          releaseWebMcp?.();
          errorListeners.clear();
          if (ownsPanel(activePanel())) ctx.layout.selectPanel(null);
        });
      },
    };
}

if (globalThis.window?.__ModuleLoader__) {
  window.__ModuleLoader__.load({ id: "dsh-orion-workbench", factory: (require) => createWorkbenchClientPlugin(require, window) });
}
