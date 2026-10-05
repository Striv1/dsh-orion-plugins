import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

const source = await readFile(new URL("../../harness/plugins/dsh-client-ui-aqua/lib/client.js", import.meta.url), "utf8");
const flush = () => new Promise((resolve) => setImmediate(resolve));

/** Run the shipped browser factory against controlled browser/storage faults.
 * The test hook stays inside this VM, outside the published plugin API. */
function harness({ abortWrite = false, delayRead = false, reducedMotion = false, rejectPlay = false } = {}) {
  const preferences = new Map(), blobs = new Map(), revoked = [], created = [], pendingReads = [];
  const policy = { reducedMotion }, stats = { closed: 0 };
  let failPreferenceWrite = false;
  class Element {
    constructor() { this.attributes = new Map(); this.dataset = {}; this.style = { setProperty() {}, removeProperty() {} }; this.hidden = false; this.paused = true; this.playCalls = []; this.pauseCount = 0; this.loadCount = 0; }
    getAttribute(name) { return this.attributes.get(name) ?? null; }
    setAttribute(name, value) { this.attributes.set(name, String(value)); }
    removeAttribute(name) { this.attributes.delete(name); }
    toggleAttribute(name, value) { if (value) this.setAttribute(name, ""); else this.removeAttribute(name); }
    hasAttribute(name) { return this.attributes.has(name); }
    set src(value) { this.setAttribute("src", value); }
    get src() { return this.getAttribute("src") ?? ""; }
    pause() { this.pauseCount += 1; this.paused = true; }
    load() { this.loadCount += 1; }
    play() {
      this.playCalls.push({ muted: this.muted, defaultMuted: this.defaultMuted, loop: this.loop, autoplay: this.autoplay, playsInline: this.playsInline });
      this.paused = false;
      return rejectPlay ? Promise.reject(new Error("injected playback failure")) : Promise.resolve();
    }
    remove() {}
  }
  const html = new Element(), body = new Element(), ambient = new Element(), wallpaperLayer = new Element(), image = new Element(), video = new Element();
  const nodes = new Map([
    ["[data-dsh-aqua-ambient]", ambient], ["[data-dsh-aqua-wallpaper-layer]", wallpaperLayer],
    ["[data-dsh-aqua-wallpaper-img]", image], ["[data-dsh-aqua-wallpaper-video]", video],
  ]);
  const document = {
    documentElement: html, body, hidden: false,
    querySelector: (selector) => selector.startsWith("style[") ? new Element() : nodes.get(selector) ?? null,
    querySelectorAll: () => [], addEventListener() {}, removeEventListener() {},
  };
  const mediaQuery = { get matches() { return policy.reducedMotion; }, addEventListener() {}, removeEventListener() {} };
  const window = { addEventListener() {}, removeEventListener() {}, matchMedia: () => mediaQuery, setTimeout, clearTimeout };
  const indexedDB = { open() {
    const request = {};
    const db = {
      objectStoreNames: { contains: () => true }, close: () => { stats.closed += 1; },
      transaction() {
        const transaction = { objectStore: () => store };
        const commit = (action, request) => {
          queueMicrotask(() => {
            request.onsuccess?.();
            queueMicrotask(() => {
              if (abortWrite) { transaction.error = new Error("injected transaction abort"); transaction.onabort?.(); }
              else { action(); transaction.oncomplete?.(); }
            });
          });
        };
        const store = {
          put(blob, key) { const request = {}; commit(() => blobs.set(key, blob), request); return request; },
          delete(key) { const request = {}; commit(() => blobs.delete(key), request); return request; },
          get(key) {
            const request = {};
            const finish = () => { request.result = blobs.get(key); request.onsuccess?.(); };
            if (delayRead) pendingReads.push(finish); else queueMicrotask(finish);
            return request;
          },
        };
        return transaction;
      },
    };
    queueMicrotask(() => { request.result = db; request.onsuccess?.(); });
    return request;
  } };
  let api;
  window.__ModuleLoader__ = { load(plugin) {
    const exported = plugin.factory(() => ({}));
    api = exported.testApi;
  } };
  vm.runInNewContext(source.replace("return module.exports;", "exports.testApi = { AquaLayer, saveVideoBlob, loadVideoBlob, validateWallpaperFile }; return module.exports;"), {
    window, document, indexedDB, HTMLElement: Element, console,
    localStorage: {
      getItem: (key) => preferences.get(key) ?? null,
      setItem: (key, value) => { if (failPreferenceWrite) throw new Error("injected quota failure"); preferences.set(key, String(value)); },
      removeItem: (key) => preferences.delete(key),
    },
    URL: { createObjectURL: (blob) => { const url = `blob:test-${created.length + 1}`; created.push({ url, blob }); return url; }, revokeObjectURL: (url) => revoked.push(url) },
    setTimeout, clearTimeout,
  });
  const context = { effect: (effect) => effect(), on: () => () => {}, theme: { getTheme: () => ({ active: { colorScheme: "light" } }) } };
  const newLayer = () => new api.AquaLayer(context);
  const activeVideoLayer = (marker = "idb:saved") => {
    preferences.set("dsh.ui-aqua.wallpaper", marker);
    preferences.set("dsh.ui-aqua.background", "wallpaper");
    const layer = newLayer();
    layer.enabled = true;
    layer.applySettings();
    return layer;
  };
  return { api, newLayer, activeVideoLayer, preferences, blobs, html, document, ambient, wallpaperLayer, image, video, policy, stats, created, revoked, pendingReads, failWrites: () => { failPreferenceWrite = true; } };
}

test("image choice and background survive a fresh layer, while failed writes retain the old choice", () => {
  const h = harness(), layer = h.newLayer();
  layer.setWallpaper("data:image/jpeg;base64,saved");
  layer.setBackground("wallpaper");
  const restored = h.newLayer();
  assert.equal(restored.getSettings().wallpaper, "data:image/jpeg;base64,saved");
  assert.equal(restored.getSettings().background, "wallpaper");
  h.failWrites();
  assert.throws(() => layer.setWallpaper("data:image/jpeg;base64,new"), /WALLPAPER_STORAGE_FAILED/);
  assert.equal(layer.getSettings().wallpaper, "data:image/jpeg;base64,saved");
  assert.equal(h.preferences.get("dsh.ui-aqua.wallpaper"), "data:image/jpeg;base64,saved");
});

test("video persistence waits for transaction commit rather than a successful put request", async () => {
  const h = harness({ abortWrite: true });
  assert.equal(await h.api.saveVideoBlob({ size: 123 }), "");
  assert.equal(h.blobs.size, 0);
  assert.equal(h.stats.closed, 1);
});

test("a committed video marker restores the stored blob through a new layer", async () => {
  const h = harness(), blob = { size: 123, type: "video/mp4" };
  const marker = await h.api.saveVideoBlob(blob);
  assert.match(marker, /^idb:/);
  h.activeVideoLayer(marker);
  await flush();
  assert.equal(h.created[0].blob, blob);
  assert.equal(h.video.src, h.created[0].url);
  assert.equal(h.video.playCalls.length, 1);
  assert.ok(h.video.playCalls[0].muted && h.video.playCalls[0].defaultMuted && h.video.playCalls[0].loop && h.video.playCalls[0].playsInline);
});

test("switching from video to fluid and back releases the old URL and restores playback", async () => {
  const h = harness(); h.blobs.set("saved", { type: "video/mp4" });
  const layer = h.activeVideoLayer(); await flush();
  const firstUrl = h.video.src;
  layer.setBackground("fluid");
  assert.equal(h.video.src, ""); assert.equal(h.video.paused, true);
  assert.deepEqual(h.revoked, [firstUrl]);
  layer.setBackground("wallpaper"); await flush();
  assert.notEqual(h.video.src, ""); assert.notEqual(h.video.src, firstUrl);
  assert.equal(h.video.playCalls.length, 2);
});

test("failed background persistence preserves the active video and the previous durable source", async () => {
  const h = harness(); h.blobs.set("saved", { type: "video/mp4" });
  const layer = h.activeVideoLayer(); await flush();
  const src = h.video.src; h.failWrites();
  assert.throws(() => layer.setBackground("fluid"), /WALLPAPER_STORAGE_FAILED/);
  assert.equal(layer.getSettings().background, "wallpaper");
  assert.equal(h.preferences.get("dsh.ui-aqua.background"), "wallpaper");
  assert.equal(h.video.src, src); assert.equal(h.revoked.length, 0);
});

test("missing stored video restores fluid and produces a visible error state", async () => {
  const h = harness(), layer = h.activeVideoLayer("idb:missing"); await flush();
  assert.equal(layer.mediaError, "aqua.mediaUnavailable");
  assert.equal(h.ambient.dataset.background, "fluid");
  assert.equal(h.wallpaperLayer.hidden, true);
  assert.equal(h.html.hasAttribute("data-dsh-aqua-wallpaper"), false);
  assert.equal(h.video.playCalls.length, 0);
});

test("a pending video read cannot create an object URL after the layer is disabled", async () => {
  const h = harness({ delayRead: true }); h.blobs.set("saved", { type: "video/mp4" });
  const layer = h.activeVideoLayer(); await flush();
  assert.equal(h.pendingReads.length, 1);
  layer.setEnabled(false);
  h.pendingReads[0](); await flush();
  assert.equal(h.created.length, 0); assert.equal(h.video.playCalls.length, 0);
  assert.equal(h.video.src, "");
});

test("repeated knob applications share one pending read, and a background change invalidates it", async () => {
  const h = harness({ delayRead: true }); h.blobs.set("saved", { type: "video/mp4" });
  const layer = h.activeVideoLayer(); layer.applySettings(); layer.setVideoBlur(9); await flush();
  assert.equal(h.pendingReads.length, 1);
  layer.setBackground("fluid"); h.pendingReads[0](); await flush();
  assert.equal(layer.enabled, true); assert.equal(h.created.length, 0);
  assert.equal(h.ambient.dataset.background, "fluid");
});

test("an image that stops decoding restores fluid and notifies the appearance store", () => {
  const h = harness(), layer = h.newLayer(); let notifications = 0;
  layer.setWallpaper("data:image/jpeg;base64,broken"); layer.setBackground("wallpaper");
  layer.enabled = true; layer.onMediaChange = () => { notifications += 1; }; layer.applySettings();
  h.image.onerror();
  assert.equal(layer.mediaError, "aqua.mediaUnavailable"); assert.equal(notifications, 1);
  assert.equal(h.image.hidden, true); assert.equal(h.image.src, ""); assert.equal(h.ambient.dataset.background, "fluid");
});

test("reduced motion and hidden documents pause video without deleting its preference", async () => {
  const h = harness({ reducedMotion: true }); h.blobs.set("saved", { type: "video/mp4" });
  const layer = h.activeVideoLayer(); await flush();
  assert.notEqual(h.video.src, ""); assert.equal(h.video.autoplay, false); assert.equal(h.video.playCalls.length, 0);
  h.policy.reducedMotion = false;
  layer.configureWallpaperVideo(h.video);
  assert.equal(h.video.playCalls.length, 1);
  h.document.hidden = true;
  layer.configureWallpaperVideo(h.video);
  assert.equal(h.video.paused, true); assert.equal(h.video.autoplay, false);
  assert.equal(layer.getSettings().wallpaper, "idb:saved");
});

test("an intentional pause does not turn an interrupted autoplay attempt into a media failure", async () => {
  const h = harness(); h.blobs.set("saved", { type: "video/mp4" });
  let reject;
  h.video.play = () => { h.video.paused = false; return new Promise((_, fail) => { reject = fail; }); };
  const layer = h.activeVideoLayer(); await flush();
  h.document.hidden = true;
  layer.configureWallpaperVideo(h.video);
  reject(new Error("injected pause interruption")); await flush();
  assert.equal(layer.mediaError, ""); assert.equal(h.wallpaperLayer.hidden, false);
});

test("playback rejection restores fluid, pauses decoding, and revokes the object URL", async () => {
  const h = harness({ rejectPlay: true }); h.blobs.set("saved", { type: "video/mp4" });
  const layer = h.activeVideoLayer(); await flush();
  assert.equal(layer.mediaError, "aqua.videoPlaybackFailed");
  assert.equal(h.ambient.dataset.background, "fluid");
  assert.equal(h.video.paused, true); assert.equal(h.video.src, "");
  assert.deepEqual(h.revoked, [h.created[0].url]);
});

test("clearing a video removes its durable marker, blob, decoder source and object URL", async () => {
  const h = harness(); h.blobs.set("saved", { type: "video/mp4" });
  const layer = h.activeVideoLayer(); await flush();
  layer.setWallpaper(""); await flush();
  assert.equal(h.preferences.get("dsh.ui-aqua.wallpaper"), "");
  assert.equal(h.blobs.has("saved"), false);
  assert.equal(h.video.src, ""); assert.equal(h.video.paused, true);
  assert.equal(h.revoked.length, 1); assert.equal(h.ambient.dataset.background, "fluid");
});

test("a legacy video without read permission falls back without requesting permission automatically", async () => {
  const h = harness(); let fileReads = 0;
  h.blobs.set("videoHandle", { queryPermission: async () => "denied", getFile: async () => { fileReads += 1; return {}; } });
  const layer = h.activeVideoLayer("fsa:legacy"); await flush();
  assert.equal(layer.mediaError, "aqua.videoPermissionRequired"); assert.equal(fileReads, 0);
  assert.equal(h.ambient.dataset.background, "fluid"); assert.equal(h.created.length, 0);
});
