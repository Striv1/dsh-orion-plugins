import { readFile } from "node:fs/promises";
import { basename, dirname, join, resolve } from "node:path";

const json = async (path) => {
  try { return JSON.parse(await readFile(path, "utf8")); } catch { return null; }
};
const composeName = (text) => {
  // Generated Compose files use a literal top-level name. Do not resolve env
  // substitutions or guess a name from a deployment id or directory.
  const match = text.match(/^name:\s*([a-z0-9][a-z0-9_-]*|'[a-z0-9][a-z0-9_-]*'|"[a-z0-9][a-z0-9_-]*")\s*(?:#.*)?$/m);
  return match ? match[1].replace(/^['"]|['"]$/g, "") : null;
};
const registryPathFor = (workflowHome, environment = process.env) => resolve(
  environment.ORION_REALTIME_RUNTIME_REGISTRY
    || join(dirname(resolve(workflowHome)), ".orion-runtime", "realtime-runtime-registry.json"),
);

async function bindingSource(workflowHome, projectRoot, environment = process.env) {
  const registryPath = registryPathFor(workflowHome, environment);
  const registry = await json(registryPath);
  const entries = (Array.isArray(registry?.runtimes) ? registry.runtimes : [])
    .filter((item) => item.project_id === basename(projectRoot));
  // Ambiguous registrations must never select an arbitrary release.
  const entry = entries.length === 1 ? entries[0] : null;
  const bindingPath = typeof entry?.deployment_binding_path === "string"
    ? resolve(dirname(registryPath), entry.deployment_binding_path) : null;
  const binding = bindingPath ? await json(bindingPath) : null;
  const composePath = bindingPath && typeof binding?.compose_path === "string"
    ? resolve(dirname(bindingPath), binding.compose_path) : null;
  return { registryPath, entry, bindingPath, binding, composePath };
}

export async function runtimeServiceSourcePaths(workflowHome, projectRoot, environment = process.env) {
  const source = await bindingSource(workflowHome, projectRoot, environment);
  return [source.registryPath, source.bindingPath, source.composePath].filter(Boolean);
}

export async function loadRuntimeService({ workflowHome, projectRoot, publication, releaseRuntime, environment = process.env }) {
  const projectId = basename(projectRoot);
  const version = String(publication?.release_version ?? "");
  const base = { kind: "unknown", project_id: projectId, release_version: version || null,
    compose_project: null, status: "UNRESOLVED", status_source: "none" };
  if (!version) return base;
  const { entry, binding, composePath } = await bindingSource(workflowHome, projectRoot, environment);
  if (binding) {
    if (binding.project_id !== projectId || binding.release_version !== version) return base;
    const name = composePath ? composeName(await readFile(composePath, "utf8").catch(() => "")) : null;
    if (!name || (binding.compose_project && binding.compose_project !== name)) return base;
    let endpoint = null;
    let port = null;
    try {
      const url = new URL(binding.endpoint);
      if (["http:", "https:"].includes(url.protocol)) {
        endpoint = `${url.origin}${url.pathname}`;
        port = Number(url.port || (url.protocol === "https:" ? 443 : 80));
      }
    } catch { /* Invalid endpoint is omitted, never forwarded verbatim. */ }
    return { ...base, kind: "docker_compose", compose_project: name, endpoint, port,
      enabled: entry.enabled !== false, status: "BOUND", status_source: "deployment_binding" };
  }
  // Intake mode alone is insufficient evidence that a document runtime exists.
  if (releaseRuntime?.project_id === projectId && releaseRuntime?.release_version === version
      && releaseRuntime?.state === "DOCUMENT_RUNTIME_READY") {
    return { ...base, kind: "document", status: "DOCUMENT_RUNTIME_READY", status_source: "release_job" };
  }
  return base;
}
