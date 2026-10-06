import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import vm from "node:vm";
import test from "node:test";

const source = await readFile(new URL("../../harness/plugins/dsh-client-ui-aqua/lib/client.js", import.meta.url), "utf8");

/** Track the browser GPU handles retained on one canvas across background switches. */
function harness({ failedShader = 0, failedProgram = 0, failedBuffer = 0, failedTexture = 0, failedFramebuffer = 0 } = {}) {
  const live = new Set(), deleted = [], frames = new Map(), mouseListeners = new Set(), counts = new Map();
  let nextFrame = 0, api;
  const allocate = (kind, failed = 0) => {
    const ordinal = (counts.get(kind) ?? 0) + 1;
    counts.set(kind, ordinal);
    if (ordinal === failed) return null;
    const resource = { kind, ordinal };
    live.add(resource);
    return resource;
  };
  const release = (resource) => {
    assert.ok(live.delete(resource), `GPU handle deleted more than once: ${resource?.kind}`);
    deleted.push(resource);
  };
  const gl = new Proxy({
    createShader: () => allocate("shader"), deleteShader: release,
    createProgram: () => allocate("program"), deleteProgram: release,
    createBuffer: () => allocate("buffer", failedBuffer), deleteBuffer: release,
    createTexture: () => allocate("texture", failedTexture), deleteTexture: release,
    createFramebuffer: () => allocate("framebuffer", failedFramebuffer), deleteFramebuffer: release,
    getShaderParameter: (shader) => shader.ordinal !== failedShader,
    getProgramParameter: (program) => program.ordinal !== failedProgram,
    getShaderInfoLog: () => "injected compile failure", getProgramInfoLog: () => "injected link failure",
    getUniformLocation: () => ({}), getAttribLocation: () => 0,
  }, { get: (target, name) => target[name] ?? (/^[A-Z_0-9]+$/.test(name) ? name : () => {}) });
  const canvas = { clientWidth: 320, clientHeight: 180, getContext: () => gl };
  const window = {
    devicePixelRatio: 1,
    matchMedia: () => ({ matches: false }),
    addEventListener: (name, listener) => { if (name === "mousemove") mouseListeners.add(listener); },
    removeEventListener: (name, listener) => { if (name === "mousemove") mouseListeners.delete(listener); },
    __ModuleLoader__: { load(plugin) { api = plugin.factory(() => ({})).testApi; } },
  };
  vm.runInNewContext(source.replace("return module.exports;", "exports.testApi = { attachFluidShader, SITE_FLUID_PARAMS }; return module.exports;"), {
    window, document: { querySelector: () => ({}) }, navigator: { userAgent: "Aqua test" },
    performance: { now: () => 100 }, console: { error() {} },
    requestAnimationFrame: (callback) => { frames.set(++nextFrame, callback); return nextFrame; },
    cancelAnimationFrame: (id) => frames.delete(id),
  });
  return { mount: () => api.attachFluidShader(canvas, api.SITE_FLUID_PARAMS), live, deleted, frames, mouseListeners };
}

test("repeated fluid/wallpaper switches release GPU handles on the reused canvas", () => {
  const h = harness();
  for (let i = 0; i < 12; i += 1) {
    const layer = h.mount();
    assert.ok(h.live.size > 0);
    assert.equal(h.frames.size, 1);
    const [frameId, render] = h.frames.entries().next().value;
    h.frames.delete(frameId);
    render(100);
    assert.equal(h.frames.size, 1);
    layer.dispose();
    assert.equal(h.live.size, 0, `switch ${i + 1} retained GPU resources`);
    assert.equal(h.frames.size, 0);
    assert.equal(h.mouseListeners.size, 0);
    const deleted = h.deleted.length;
    layer.dispose();
    assert.equal(h.deleted.length, deleted);
  }
});

for (const fault of [{ failedShader: 1 }, { failedProgram: 2 }]) {
  test(`shader initialization failure releases partial GPU allocations: ${JSON.stringify(fault)}`, () => {
    const h = harness(fault), layer = h.mount();
    assert.equal(h.live.size, 0);
    assert.equal(h.frames.size, 0);
    layer.dispose();
    assert.equal(h.live.size, 0);
  });
}

for (const fault of [{ failedBuffer: 1 }, { failedTexture: 2 }, { failedFramebuffer: 2 }]) {
  test(`target allocation failure releases earlier GPU allocations: ${JSON.stringify(fault)}`, () => {
    const h = harness(fault);
    assert.throws(() => h.mount(), /allocation failed/);
    assert.equal(h.live.size, 0);
    assert.equal(h.frames.size, 0);
    assert.equal(h.mouseListeners.size, 0);
  });
}
