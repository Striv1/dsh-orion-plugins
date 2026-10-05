import { existsSync, createReadStream } from "node:fs";
import { readFile, realpath, stat } from "node:fs/promises";
import { dirname, extname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { homedir } from "node:os";
import { pipeline } from "node:stream/promises";
import { apply as applyGateway, inject as gatewayInject } from "./runtime.js";

const packageRoot = dirname(fileURLToPath(import.meta.url));
export const inject = [...gatewayInject, "orionRuntime"];
export const ASSET_ROUTE_PREFIX = "/orion-workbench-assets";
const PREFIX = ASSET_ROUTE_PREFIX + "/";
const MIME = { ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".woff2": "font/woff2" };

export function runtimeConfig(config = {}, { home = process.env.DSH_HOME || join(homedir(), ".deepseek-harness"), root = packageRoot, runtime } = {}) {
  const runtimeSettings = runtime?.settings?.() || {};
  const managed = runtime?.status?.().mode === "managed";
  config = { ...config, ...runtimeSettings };
  const backendRoot = config.backendRoot ? resolve(config.backendRoot) : null;
  const backendConfigured = Boolean((!managed || runtimeSettings.backendValidated === true) && backendRoot && existsSync(join(backendRoot, "harness/orion_workflow_mcp.py")) && existsSync(join(backendRoot, "services/ontology_engineering")));
  const workflowHome = resolve(config.workflowHome || join(home, "orion-workflows"));
  return {
    ...config,
    nativePlugin: true,
    readOnly: config.readOnly !== false,
    distIndex: join(root, "assets/index.html"),
    productName: "AHS",
    businessApiEnabled: false,
    documentIngestionApiEnabled: backendConfigured && config.documentIngestionApiEnabled === true,
    documentIngestionRoot: resolve(config.documentIngestionRoot || join(home, "orion-document-inputs")),
    workflowApiEnabled: backendConfigured,
    workflowHome,
    workflowCwd: backendRoot,
    workflowPython: config.python || "python3",
    ontologyGraphBuilder: backendRoot ? join(backendRoot, "scripts/build_ontology_graph.py") : null,
    ontologyGraphPython: config.python || "python3",
    workflowActionContract: backendRoot ? join(backendRoot, "harness/contracts/workflow-ui-actions.json") : null,
    workflowActor: config.actor || null,
    mcpControlEnabled: config.mcpControlEnabled === true,
    realtimeQaApiUrl: config.realtimeQaApiUrl || "http://127.0.0.1:8091",
    printUrl: false,
    backendConfigured,
  };
}

export async function assetPath(pathname, root = join(packageRoot, "assets")) {
  if (!pathname.startsWith(PREFIX)) throw new Error("Unknown asset path");
  const part = decodeURIComponent(pathname.slice(PREFIX.length));
  const target = resolve(root, part);
  const rel = relative(root, target);
  if (!part || isAbsolute(part) || rel === ".." || rel.startsWith("../") || rel.startsWith("..\\")) throw new Error("Asset path escapes package");
  const actual = await realpath(target);
  const resolvedRelative = relative(await realpath(root), actual);
  if (isAbsolute(resolvedRelative) || resolvedRelative === ".." || resolvedRelative.startsWith("../") || resolvedRelative.startsWith("..\\")) throw new Error("Asset link escapes package");
  return actual;
}

export async function apply(ctx, config = {}) {
  const runtime = ctx.orionRuntime;
  await runtime.ready;
  const settings = runtimeConfig(config, { runtime });
  applyGateway(ctx, settings);
  ctx.inject(["connection"], (connected) => {
    const authorize = (request, response) => {
      const rejected = connected.connection.requestRejection(request);
      if (rejected === undefined) return true;
      response.writeHead(rejected, { "cache-control": "no-store" });
      response.end();
      return false;
    };
    connected.effect(() => connected.webServer.register({
      kind: "prefix", path: ASSET_ROUTE_PREFIX,
      async handler(request, response) {
        if (!authorize(request, response)) return;
        if (!["GET", "HEAD"].includes(request.method)) { response.writeHead(405); response.end(); return; }
        try {
          const pathname = new URL(request.url, "http://local").pathname;
          const target = pathname === PREFIX + "workbench-assets.json"
            ? join(packageRoot, "workbench-assets.json") : await assetPath(pathname);
          if (!(await stat(target)).isFile()) { response.writeHead(404); response.end(); return; }
          response.writeHead(200, { "content-type": MIME[extname(target)] || "application/octet-stream", "cache-control": "no-cache", "x-content-type-options": "nosniff" });
          if (request.method === "HEAD") response.end();
          else await pipeline(createReadStream(target), response);
        } catch (error) {
          if (!response.headersSent) response.writeHead(error.code === "ENOENT" ? 404 : 403, { "x-content-type-options": "nosniff" });
          response.end();
        }
      },
    }));
    connected.effect(() => connected.webServer.register({
      kind: "exact", path: "/orion-workbench-api/status",
      async handler(request, response) {
        if (!authorize(request, response)) return;
        if (!["GET", "HEAD"].includes(request.method)) { response.writeHead(405); response.end(); return; }
        const manifest = JSON.parse(await readFile(join(packageRoot, "package.json"), "utf8"));
        response.writeHead(200, { "content-type": MIME[".json"], "cache-control": "no-store" });
        response.end(request.method === "HEAD" ? undefined : JSON.stringify({
          plugin: manifest.name, version: manifest.version, backendConfigured: settings.backendConfigured,
          readOnly: settings.readOnly,
          runtime: await runtime.refreshStatus(),
          modules: ["engineering", "documents", "registry", "templates", "graph", "qa", "analytics"],
          detail: settings.backendConfigured ? "已连接本地 ORION 后端；业务服务按实际状态读取。" : "请配置匹配版本的 ORION 后端与业务数据目录。",
        }));
      },
    }));
  });
}
