import { createHash } from "node:crypto";
import { lstat, mkdir, mkdtemp, readFile, readdir, rename, rm, writeFile } from "node:fs/promises";
import { basename, dirname, extname, isAbsolute, join, posix, relative, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { gzipSync } from "node:zlib";
import { tmpdir } from "node:os";
import { renderRc2PresetRows } from "../harness/web/persona-preset.mjs";
import { fingerprintRuntimeFiles, verifyRuntimeManifest } from "./orion_runtime_files.mjs";

const defaultProjectRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
export const ASSET_PREFIX = "/orion-workbench-assets/";
export const EXCLUDED_ASSETS = new Set([
  "boot-brand.js",
  "modules/app-shell.js",
  "modules/harness-adapter.js",
  "modules/ontology-center/ontology-chat.js",
]);
const ROOT_PACKAGE_FILES = new Set([
  "package.json", "index.js", "runtime.js", "client.js", "client.entry.js",
  "cordis.patch.yml", "README.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "glass.css", "icon.svg",
]);
const PACKAGE_DIRECTORIES = new Set(["lib", "locale", "styles"]);
const ASSET_EXTENSIONS = new Set([".js", ".css", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".woff", ".woff2", ".ttf", ".txt"]);
const PACKAGE_EXTENSIONS = new Set([".js", ".mjs", ".cjs", ".json", ".css", ".svg", ".png", ".md", ".txt"]);
const MAX_FILE_BYTES = 32 * 1024 * 1024;
const MAX_TOTAL_BYTES = 256 * 1024 * 1024;
const SECRET_FILE = /^(?:\.env(?:\..*)?|\.credentials(?:\..*)?|\.npmrc|\.netrc|id_rsa|id_ed25519|.*\.(?:pem|key|p12|pfx))$/i;
const PERSONAL_PATH = /(?:\/Users\/[^\s/"'`<>]+\/|\/home\/[^\s/"'`<>]+\/|[A-Za-z]:[\\/]Users[\\/][^\s\\/"'`<>]+[\\/])/;
const SECRET_VALUE = /(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:ghp_|github_pat_)[A-Za-z0-9_]{24,}|\bsk-[A-Za-z0-9_-]{24,})/;
const TEXT_EXTENSIONS = new Set([".js", ".mjs", ".cjs", ".json", ".css", ".svg", ".md", ".txt", ".yml", ".yaml"]);
const json = value => `${JSON.stringify(value, null, 2)}\n`;
const sha256 = value => createHash("sha256").update(value).digest("hex");
const exists = async path => lstat(path).then(() => true, error => {
  if (error.code === "ENOENT") return false;
  throw error;
});

function packagePath(value) {
  if (!value || isAbsolute(value) || value.includes("\\") || value.split("/").some(part => !part || part === "." || part === "..")) {
    throw new Error(`Unsafe package path: ${value}`);
  }
  return value;
}

function assertPublishableContent(path, body) {
  if (SECRET_FILE.test(basename(path))) throw new Error(`Secret file rejected: ${path}`);
  if (!TEXT_EXTENSIONS.has(extname(path))) return;
  const text = body.toString("utf8");
  if (PERSONAL_PATH.test(text)) throw new Error(`Machine-specific user path rejected in ${path}`);
  if (SECRET_VALUE.test(text)) throw new Error(`Credential-like content rejected in ${path}`);
}

async function readRegularFile(path, label = basename(path)) {
  const info = await lstat(path);
  if (info.isSymbolicLink() || !info.isFile()) throw new Error(`Only regular files are allowed: ${label}`);
  if (info.size > MAX_FILE_BYTES) throw new Error(`Input exceeds package file limit: ${label}`);
  return readFile(path);
}

async function walk(root, onFile, prefix = "") {
  const info = await lstat(root);
  if (info.isSymbolicLink() || !info.isDirectory()) throw new Error(`Directory symlink rejected: ${prefix || basename(root)}`);
  for (const entry of (await readdir(root, { withFileTypes: true })).sort((a, b) => a.name.localeCompare(b.name, "en"))) {
    const name = prefix ? `${prefix}/${entry.name}` : entry.name;
    if (SECRET_FILE.test(entry.name)) throw new Error(`Secret file rejected: ${name}`);
    if (entry.isSymbolicLink()) throw new Error(`Symlink rejected: ${name}`);
    if (entry.isDirectory()) await walk(join(root, entry.name), onFile, name);
    else if (entry.isFile()) await onFile(name, join(root, entry.name));
    else throw new Error(`Non-regular input rejected: ${name}`);
  }
}

// Only static resource roots are rewritten; business API URLs remain intact.
export function namespaceAssetUrls(text) {
  return text
    .replace(/(["'`(])\/(fonts|vendor|icons)\//g, `$1${ASSET_PREFIX}$2/`)
    .replace(/(["'`(])\/(ahs-(?:icon|wordmark)-(?:black|white)\.svg|ahs-logo-white\.png)(?=["'`)?])/g, `$1${ASSET_PREFIX}$2`);
}

export function assetOrder(frontendSource) {
  const collect = pattern => [...new Set([...frontendSource.matchAll(pattern)].map(match => {
    const path = match[1].split("?")[0].replace(/^\//, "");
    return packagePath(path);
  }))];
  return {
    scripts: collect(/<script src="([^"]+)"/g).filter(path => !EXCLUDED_ASSETS.has(path)),
    styles: collect(/<link rel="stylesheet" href="([^"]+)"/g),
  };
}

function dependencyManifest(pyproject) {
  const project = pyproject.match(/^\[project\]\s*\n([\s\S]*?)(?=^\[|(?![\s\S]))/m)?.[1];
  const name = project?.match(/^name\s*=\s*"([^"]+)"/m)?.[1];
  const version = project?.match(/^version\s*=\s*"([^"]+)"/m)?.[1];
  const requiresPython = project?.match(/^requires-python\s*=\s*"([^"]+)"/m)?.[1];
  if (!name || !version || !requiresPython) throw new Error("Missing explicit external Python package/version contract");
  return {
    kind: "external-services",
    python: { package: name, version, requiresPython, projectMetadataSha256: sha256(pyproject) },
    deployment: "Install and configure the matching ORION backend separately. Python source, wheels, databases, graph stores, MCP servers, business data and credentials are not included in this plugin.",
    requiredCapabilities: ["ontology_engineering", "structured_data", "document_ingestion", "release_registry", "realtime_qa", "ontology_analytics"],
    acceptanceRequired: true,
  };
}

export async function backendFingerprint(projectRoot) {
  const manifestPath = join(projectRoot, "contracts/runtime-source-manifest.json");
  if (await exists(manifestPath)) {
    const manifest = JSON.parse(await readRegularFile(manifestPath));
    return fingerprintRuntimeFiles(await verifyRuntimeManifest(projectRoot, manifest));
  }
  const records = [];
  const include = async (path, source) => {
    const body = await readRegularFile(source, `backend source ${path}`);
    records.push({ path, bytes: body.length, sha256: sha256(body) });
  };
  const scan = async (base, prefix, accept) => {
    if (!(await exists(base))) return;
    const info = await lstat(base);
    if (info.isSymbolicLink() || !info.isDirectory()) throw new Error(`Backend source directory symlink rejected: ${prefix}`);
    for (const entry of await readdir(base, { withFileTypes: true })) {
      if (entry.name.startsWith(".") || ["__pycache__", "node_modules", "data", "state"].includes(entry.name)) continue;
      const name = `${prefix}/${entry.name}`, source = join(base, entry.name);
      if (entry.isSymbolicLink()) throw new Error(`Backend source symlink rejected: ${name}`);
      if (entry.isDirectory()) await scan(source, name, accept);
      else if (entry.isFile() && accept(name)) await include(name, source);
    }
  };
  await include("pyproject.toml", join(projectRoot, "pyproject.toml"));
  await scan(join(projectRoot, "services"), "services", path => path.endsWith(".py") || /\/contracts\//.test(path));
  await scan(join(projectRoot, "harness"), "harness", path => path.endsWith(".py"));
  for (const prefix of ["contracts", "harness/contracts", "docs/contracts", "harness/skills"]) {
    await scan(join(projectRoot, prefix), prefix, path => [".py", ".json", ".yaml", ".yml", ".md", ".txt"].includes(extname(path)));
  }
  const distinct = [...new Map(records.map(record => [record.path, record])).values()].sort((a, b) => a.path.localeCompare(b.path, "en"));
  const digest = createHash("sha256");
  for (const record of distinct) digest.update(record.path).update("\0").update(record.sha256).update("\n");
  return {
    algorithm: "sha256-path-content-v1", sha256: digest.digest("hex"), fileCount: distinct.length,
    scope: ["pyproject.toml", "services/**/*.py", "harness/**/*.py", "services/**/contracts/*", "contracts", "harness/contracts", "docs/contracts", "harness/skills"],
    files: distinct,
  };
}

function moduleManifest(scripts) {
  const rows = [
    ["engineering", "本体工程 S0–S7", ["engineering/", "ontology-center/engineering.js", "ontology-center/index.js"]],
    ["documents", "资料接入与文件预览", ["document-", "file-", "version-files.js"]],
    ["registry", "正式资产与发布版本", ["registry.js", "version-files.js", "release-runtime.js"]],
    ["templates", "业务本体模板", ["templates.js"]],
    ["graph", "本体图谱与实例预览", ["ontology-graph-viewer.js"]],
    ["qa", "正式版本证据问答", ["api.js", "evidence-renderer.js"]],
    ["analytics", "查询、指标与分析", ["analytics-", "echarts.min.js"]],
  ];
  return rows.map(([id, title, patterns]) => ({
    id, title, backend: "external",
    scripts: scripts.filter(path => patterns.some(pattern => path.includes(pattern))).map(path => `${ASSET_PREFIX}${path}`),
  }));
}

export async function buildBrowserEntry(projectRoot, entry, { dependencyRoot } = {}) {
  dependencyRoot = resolve(dependencyRoot ?? (await exists(join(projectRoot, "node_modules/esbuild/lib/main.js")) ? join(projectRoot, "node_modules") : join(projectRoot, "harness/web/node_modules")));
  const module = await import(pathToFileURL(join(dependencyRoot, "esbuild/lib/main.js")).href);
  const brandAssets = {}, brandInputs = [];
  if (resolve(entry) === resolve(projectRoot, "harness/plugins/orion-workbench/client.entry.js")) {
    for (const name of ["ahs-icon-black.svg", "ahs-icon-white.svg", "ahs-wordmark-black.svg", "ahs-wordmark-white.svg"]) {
      const path = `harness/web/assets/${name}`;
      brandAssets[name] = `data:image/svg+xml;base64,${(await readRegularFile(join(projectRoot, path), path)).toString("base64")}`;
      brandInputs.push(path);
    }
  }
  const result = await module.build({
    absWorkingDir: projectRoot, entryPoints: [entry], bundle: true, write: false,
    format: "iife", platform: "browser", target: "es2022", sourcemap: false,
    legalComments: "inline", metafile: true, logLevel: "silent", nodePaths: [dependencyRoot],
    define: brandInputs.length ? { __ORION_BRAND_ASSETS__: JSON.stringify(brandAssets) } : {},
  });
  if (result.outputFiles.length !== 1) throw new Error("Browser entry must compile to one self-contained script");
  let output = Buffer.from(result.outputFiles[0].contents);
  const dependencyPath = relative(projectRoot, dependencyRoot).split("\\").join("/");
  if (dependencyPath.startsWith("../")) {
    const prefix = `// ${dependencyPath}/`;
    output = Buffer.from(output.toString("utf8").split("\n").map(line => line.trimStart().startsWith(prefix)
      ? `${line.slice(0, line.length - line.trimStart().length)}// npm/${line.trimStart().slice(prefix.length)}` : line).join("\n"));
  }
  return { body: output, inputs: [...brandInputs, ...Object.keys(result.metafile.inputs).map(path => {
    const name = isAbsolute(path) ? relative(projectRoot, path).split("\\").join("/") : path;
    if (name.startsWith("../")) {
      const packageInput = relative(dependencyRoot, resolve(projectRoot, path)).split("\\").join("/");
      if (packageInput.startsWith("../") || isAbsolute(packageInput)) throw new Error("Browser compiler input is outside this project or its explicit dependency root");
      return `npm/${packageInput}`;
    }
    return name;
  })].sort() };
}

async function readPresets(projectRoot, runtimeRoot) {
  const official = join(runtimeRoot, "node_modules/@deepseek-ai/dsh-web-app/presets/standard.patch.yml");
  const standard = (await readRegularFile(official, "official standard preset")).toString("utf8");
  const runtime = JSON.parse((await readRegularFile(join(runtimeRoot, "node_modules/@deepseek-ai/dsh/package.json"), "official runtime manifest")).toString("utf8"));
  const presets = [];
  for (const id of ["engineering", "ontology-qa"]) {
    const base = join(projectRoot, "harness/profile/agent-presets", id);
    const metadata = (await readRegularFile(join(base, "preset.yml"), `preset ${id} metadata`)).toString("utf8");
    const value = key => {
      const raw = metadata.match(new RegExp(`^${key}:\\s*(.+)$`, "m"))?.[1]?.trim();
      if (!raw) throw new Error(`Missing preset ${id} ${key}`);
      if (/^["']/.test(raw)) throw new Error(`Preset ${id} ${key} must be a plain scalar`);
      return raw;
    };
    const persona = (await readRegularFile(join(base, "persona.txt"), `preset ${id} persona`)).toString("utf8");
    presets.push({ id, name: value("name"), description: value("description"), order: Number(value("order")), persona: id === "ontology-qa" ? `${persona.trimEnd()}\n\nFor questions about a formally published ontology, first load the orion-ontology-qa skill. Its policy is method guidance; actual facts must still come from the release-bound tools.\n` : persona });
  }
  let license;
  for (const path of ["dsh-web-app/LICENSE", "dsh/LICENSE"]) {
    const source = join(runtimeRoot, "node_modules/@deepseek-ai", path);
    if (await exists(source)) { license = await readRegularFile(source, "DeepSeek license"); break; }
  }
  if (!license) throw new Error("Official preset license is required");
  return {
    rows: renderRc2PresetRows(standard, presets), license, version: runtime.version,
    provenance: { version: runtime.version, standardPresetSha256: sha256(standard), source: `https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v${runtime.version}/packages/bundle/web-app/presets/standard.patch.yml`, presets: presets.map(({ id, name, order }) => ({ id, name, order })) },
  };
}

async function readBundledSkills(projectRoot, runtimeRoot) {
  const files = new Map();
  const engineerRoot = "harness/skills/orion-ontology-engineer";
  await walk(join(projectRoot, engineerRoot), async (path, source) => {
    if (path !== "SKILL.md" && !/^references\/(?:[A-Za-z0-9._-]+\/)*[A-Za-z0-9._-]+\.md$/.test(path)) throw new Error(`Unapproved skill resource: ${path}`);
    files.set(`skills/orion-ontology-engineer/${path}`, await readRegularFile(source, `engineer skill ${path}`));
  });
  if (!files.has("skills/orion-ontology-engineer/SKILL.md")) throw new Error("Missing bundled engineering SKILL.md");
  const qaSource = "harness/profile/agent-presets/ontology-qa/persona.txt";
  const qaPersona = (await readRegularFile(join(projectRoot, qaSource), "QA skill source")).toString("utf8");
  // The identity line is rendered by the persona plugin, not the skill loader.
  const qaPolicy = qaPersona.trim().replace(/^You are [^\n]*\n\s*\n/u, "");
  files.set("skills/orion-ontology-qa/SKILL.md", Buffer.from(`---\nname: orion-ontology-qa\ndescription: 基于不可变正式发布本体和已核验来源回答业务问题；保持只读、证据范围、未知值及规则结论边界，不修改工程或代替业务批准。\n---\n\n# ORION 本体证据问答\n\n${qaPolicy}\n`));
  for (const id of ["orion-ontology-engineer", "orion-ontology-qa"]) {
    const frontmatter = files.get(`skills/${id}/SKILL.md`).toString("utf8").match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/u)?.[1];
    if (!frontmatter || frontmatter.match(/^name:\s*([^\r\n]+)$/mu)?.[1]?.trim() !== id || !frontmatter.match(/^description:\s*\S.+$/mu)) throw new Error(`Invalid bundled skill metadata: ${id}`);
  }
  // Referenced local resources must stay inside the owning skill and exist.
  for (const [path, body] of files) {
    const base = path.split("/").slice(0, 2).join("/");
    for (const link of body.toString("utf8").matchAll(/\[[^\]]*\]\(([^\s)]+)\)/g)) {
      const target = link[1].split(/[?#]/)[0];
      if (!target || /^[A-Za-z][A-Za-z0-9+.-]*:/.test(target)) continue;
      const resolved = posix.normalize(posix.join(posix.dirname(path), target));
      if (!resolved.startsWith(`${base}/`) || !files.has(resolved)) throw new Error(`Missing or escaping skill resource: ${path} -> ${target}`);
    }
  }
  const providerManifest = JSON.parse((await readRegularFile(join(runtimeRoot, "node_modules/@deepseek-ai/dsh-skill-filesystem/package.json"), "official skill provider manifest")).toString("utf8"));
  const provenance = {
    provider: { package: "@deepseek-ai/dsh-skill-filesystem", version: providerManifest.version, name: "orion-workbench-bundled", root: "skills", includeDefaultRoots: false, watch: false, source: `https://github.com/deepseek-ai/deepseek-harness/blob/dsh-v${providerManifest.version}/packages/skill/skill-filesystem/README.md` },
    skills: [
      { name: "orion-ontology-engineer", source: engineerRoot, transformation: "Copy original SKILL.md and all referenced Markdown resources without policy edits." },
      { name: "orion-ontology-qa", source: qaSource, sourceSha256: sha256(qaPersona), transformation: "Add required skill metadata and heading; reuse the complete QA policy verbatim, excluding the persona-only model/cwd identity line." },
    ],
    resources: [...files].map(([path, body]) => ({ path, sha256: sha256(body), bytes: body.length })).sort((a, b) => a.path.localeCompare(b.path, "en")),
    dependencies: "Methods only: workflow execution, release-bound facts, contracts and business receipts require the matching external ORION backend.",
  };
  return { files, provenance, version: providerManifest.version };
}

/** Use the installer's actual YAML dialect and composition, without evaluating
 * expressions, importing candidate entrypoints, starting plugins or installing. */
export async function validateOfficialBundle({ files, manifest, runtimeRoot, requireExactDshPeer = true }) {
  const bootRoot = join(runtimeRoot, "node_modules/@deepseek-ai/dsh-app-boot");
  const metadata = JSON.parse((await readRegularFile(join(bootRoot, "package.json"), "official bundle parser manifest")).toString("utf8"));
  const { loadOverlayPatches, composeEntries, evaluatePluginCompatibility, resolveBundleDir, readProfileManifest } = await import(pathToFileURL(join(bootRoot, "lib/index.js")).href);
  const declared = manifest.peerDependencies?.["@deepseek-ai/dsh"]?.split(/\s*\|\|\s*/).map(item => item.trim());
  if (requireExactDshPeer && !declared?.includes(metadata.version)) throw new Error("Official bundle parser must match an exact declared DSH peer version");
  const source = files.get("cordis.patch.yml");
  if (!source) throw new Error("No bundle patch to validate");
  const temporary = await mkdtemp(join(tmpdir(), "orion-official-bundle-preflight-"));
  try {
    const patchPath = join(temporary, "cordis.patch.yml");
    await writeFile(patchPath, source, { mode: 0o600 });
    const patches = loadOverlayPatches("orion-package-preflight", patchPath);
    const warnings = [], entries = composeEntries([patches], warning => warnings.push(String(warning)));
    if (warnings.length) throw new Error(`Bundle composition warnings: ${warnings.join("; ")}`);
    const ids = entries.map(row => row.id);
    if (ids.some(id => typeof id !== "string" || !id) || new Set(ids).size !== ids.length) throw new Error("Bundle contains missing or duplicate top-level entry ids");
    for (const row of entries) {
      if (typeof row.name !== "string" || !row.name) throw new Error("Bundle contains a missing plugin name");
      if (row.name !== manifest.name && !row.name.startsWith(`${manifest.name}/`)) continue;
      const key = row.name === manifest.name ? "." : `.${row.name.slice(manifest.name.length)}`;
      const exported = manifest.exports?.[key];
      const target = typeof exported === "string" ? exported : exported?.default ?? exported?.import;
      if (typeof target !== "string" || !target.startsWith("./") || !files.has(packagePath(target.slice(2)))) throw new Error(`Bundle reaches an unpublished package entry: ${key}`);
    }
    const issue = evaluatePluginCompatibility(manifest, {}, metadata.version);
    if (issue) throw new Error(`Official package compatibility rejected: ${JSON.stringify(issue)}`);
    // Match the installer's bundleComponentManifests traversal: only inserted
    // plugin rows (including groups), not the whole dependency closure. The
    // candidate itself is read from the staged manifest; runtime components
    // resolve through the official installation anchor, never a user's Home.
    const componentNames = new Set();
    const visit = rows => {
      for (const row of rows) {
        if (row.group && Array.isArray(row.config)) visit(row.config);
        if (typeof row.name !== "string" || /^[./]/.test(row.name) || row.name.includes(":")) continue;
        componentNames.add(row.name.split("/").slice(0, row.name.startsWith("@") ? 2 : 1).join("/"));
      }
    };
    visit(composeEntries([patches.filter(patch => patch.insert !== undefined)]));
    const componentAdmission = [];
    const anchor = resolve(runtimeRoot, "node_modules/@deepseek-ai/dsh/package.json");
    for (const name of componentNames) {
      const component = name === manifest.name ? manifest
        : readProfileManifest("orion-package-preflight", resolveBundleDir("orion-package-preflight", name, anchor, temporary));
      const componentIssue = evaluatePluginCompatibility(component, {}, metadata.version);
      if (componentIssue) throw new Error(`Official bundle component compatibility rejected: ${JSON.stringify(componentIssue)}`);
      componentAdmission.push({ name, version: component.version, source: name === manifest.name ? "candidate" : "runtime", compatible: true });
    }
    return { package: metadata.name, version: metadata.version, patchSha256: sha256(source), patchCount: patches.length,
      composedEntryIds: ids, componentAdmission, expressionEvaluation: false, candidateCodeImported: false, installationPerformed: false,
      checks: ["loadOverlayPatches", "composeEntries", "evaluatePluginCompatibility", "packaged-entry-exports", "bundle-component-compatibility"],
      scope: "Official parser, composition and manifest admission only; actual native installation and lifecycle require separate acceptance." };
  } finally { await rm(temporary, { recursive: true, force: true }); }
}

// USTAR with stable ownership, file modes and timestamps; no package lifecycle runs.
export function tarGzip(files, { prefix: archivePrefix = "package", executablePaths = new Set() } = {}) {
  packagePath(archivePrefix);
  const blocks = [];
  for (const [path, body] of [...files].sort(([a], [b]) => a.localeCompare(b, "en"))) {
    const full = `${archivePrefix}/${packagePath(path)}`;
    const header = Buffer.alloc(512);
    let name = full, prefix = "";
    if (Buffer.byteLength(name) > 100) {
      const cut = full.lastIndexOf("/");
      name = full.slice(cut + 1); prefix = full.slice(0, cut);
    }
    if (Buffer.byteLength(name) > 100 || Buffer.byteLength(prefix) > 155) throw new Error(`USTAR path is too long: ${path}`);
    const put = (value, offset, length) => header.write(value, offset, length, "utf8");
    const octal = (value, offset, length) => put(`${value.toString(8).padStart(length - 1, "0")}\0`, offset, length);
    put(name, 0, 100); octal(executablePaths.has(path) ? 0o755 : 0o644, 100, 8); octal(0, 108, 8); octal(0, 116, 8);
    octal(body.length, 124, 12); octal(0, 136, 12); header.fill(32, 148, 156);
    put("0", 156, 1); put("ustar\0", 257, 6); put("00", 263, 2); put(prefix, 345, 155);
    const sum = header.reduce((total, byte) => total + byte, 0);
    put(`${sum.toString(8).padStart(6, "0")}\0 `, 148, 8);
    blocks.push(header, body, Buffer.alloc((512 - body.length % 512) % 512));
  }
  blocks.push(Buffer.alloc(1024));
  return gzipSync(Buffer.concat(blocks), { level: 9, mtime: 0 });
}

async function deliverArchive(files, manifest, outputDir, facts) {
  const list = [...files].sort(([a], [b]) => a.localeCompare(b, "en")).map(([path, body]) => ({ path, bytes: body.length, sha256: sha256(body) }));
  const archive = tarGzip(files);
  const archiveName = `${manifest.name.replace(/^@/, "").replace("/", "-")}-${manifest.version}.tgz`;
  const digest = sha256(archive);
  const receipt = {
    schemaVersion: 1,
    package: { name: manifest.name, version: manifest.version, private: true, access: "restricted" },
    archive: { file: archiveName, sha256: digest, bytes: archive.length }, fileCount: list.length,
    unpackedBytes: [...files.values()].reduce((sum, body) => sum + body.length, 0), ...facts,
    actions: { publish: false, networkInstall: false, serviceStart: false, businessDataCopied: false, credentialsCopied: false, upstreamRuntimeCopied: false },
  };
  assertPublishableContent("receipt.json", Buffer.from(json(receipt)));
  await mkdir(outputDir, { recursive: true });
  const outputInfo = await lstat(outputDir);
  if (outputInfo.isSymbolicLink() || !outputInfo.isDirectory()) throw new Error("Output directory must be a regular owned directory");
  const temporary = await mkdtemp(join(outputDir, ".orion-pack-"));
  try {
    const outputs = new Map([
      [archiveName, archive], [`${archiveName}.sha256`, `${digest}  ${archiveName}\n`],
      [`${archiveName}.files.json`, json(list)], [`${archiveName}.receipt.json`, json(receipt)],
    ]);
    for (const [name, body] of outputs) await writeFile(join(temporary, name), body, { mode: 0o600 });
    for (const name of outputs.keys()) await rename(join(temporary, name), join(outputDir, name));
  } finally { await rm(temporary, { recursive: true, force: true }); }
  return { outputDir, archive: join(outputDir, archiveName), sha256: digest, fileCount: list.length, receipt };
}

export async function packageOrionPlugin({ projectRoot = defaultProjectRoot, pluginRoot, runtimeRoot, outputDir, dependencyRoot, backendSourceSnapshot, stageOnly = false, browserBuilder, bundleValidator = validateOfficialBundle } = {}) {
  projectRoot = resolve(projectRoot);
  pluginRoot = resolve(pluginRoot ?? join(projectRoot, "harness/plugins/orion-workbench"));
  runtimeRoot = resolve(runtimeRoot ?? join(projectRoot, ".orion-runtime/deepseek-harness/0.2.0-rc.2"));
  outputDir = resolve(outputDir ?? join(projectRoot, ".orion-runtime/plugin-packages/orion-workbench"));
  dependencyRoot = resolve(dependencyRoot ?? (await exists(join(projectRoot, "node_modules/esbuild/lib/main.js")) ? join(projectRoot, "node_modules") : join(projectRoot, "harness/web/node_modules")));
  browserBuilder ??= (root, entry) => buildBrowserEntry(root, entry, { dependencyRoot });
  const inputDirectories = [pluginRoot, join(projectRoot, "harness/web/assets"), join(projectRoot, "harness/plugins/branded-web-runtime"), runtimeRoot];
  for (const input of inputDirectories) {
    const info = await lstat(input);
    if (info.isSymbolicLink() || !info.isDirectory()) throw new Error("Input directory must not be a symlink");
    const distance = relative(input, outputDir);
    if (!distance || (!isAbsolute(distance) && !distance.startsWith(".."))) throw new Error("Package output must be outside every input directory");
  }
  const manifest = JSON.parse((await readRegularFile(join(pluginRoot, "package.json"))).toString("utf8"));
  if (!/^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*$/.test(manifest.name ?? "") || !/^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$/.test(manifest.version ?? "")) throw new Error("A valid package name and explicit version are required");
  if (manifest.private !== true || manifest.publishConfig?.access !== "restricted") throw new Error("Candidate packaging requires private:true and publishConfig.access:restricted");
  if (manifest.type !== "module" || manifest.exports?.["."] !== "./index.js" || manifest.exports?.["./client"] !== "./client.js" || manifest.dsh?.client?.platform !== "web" || manifest.dsh?.bundle?.patch !== "./cordis.patch.yml") throw new Error("Unexpected Host, Client or bundle entry contract");
  const files = new Map();
  let total = 0;
  const put = (path, body) => {
    path = packagePath(path); body = Buffer.isBuffer(body) ? body : Buffer.from(body);
    assertPublishableContent(path, body);
    if (body.length > MAX_FILE_BYTES) throw new Error(`Package file too large: ${path}`);
    total += body.length - (files.get(path)?.length ?? 0);
    if (total > MAX_TOTAL_BYTES || files.size > 4096) throw new Error("Package exceeds bounded candidate limits");
    files.set(path, body);
  };
  // A package may have local build inputs, but none of them can expand this allowlist.
  for (const entry of await readdir(pluginRoot, { withFileTypes: true })) {
    if (SECRET_FILE.test(entry.name)) throw new Error(`Secret file rejected: ${entry.name}`);
    if (entry.isSymbolicLink()) throw new Error(`Package symlink rejected: ${entry.name}`);
    if (ROOT_PACKAGE_FILES.has(entry.name)) {
      if (entry.name === "client.entry.js") continue;
      put(entry.name, await readRegularFile(join(pluginRoot, entry.name), entry.name));
    } else if (PACKAGE_DIRECTORIES.has(entry.name)) {
      await walk(join(pluginRoot, entry.name), async (path, source) => {
        if (!PACKAGE_EXTENSIONS.has(extname(path))) throw new Error(`Unapproved package file: ${entry.name}/${path}`);
        put(`${entry.name}/${path}`, await readRegularFile(source, `${entry.name}/${path}`));
      });
    }
  }
  for (const name of ["index.js", "runtime.js", "cordis.patch.yml", "lib/native-chat.js", "lib/skills.js"]) if (!files.has(name)) throw new Error(`Incomplete plugin package: ${name}`);
  if (manifest.exports?.["./skills"] !== "./lib/skills.js" || !files.get("cordis.patch.yml").toString("utf8").includes(`name: '${manifest.name}/skills'`)) throw new Error("Missing installable bundled skills entry");
  const clientEntry = join(pluginRoot, "client.entry.js");
  const compilations = [];
  if (await exists(clientEntry)) {
    await readRegularFile(clientEntry, "client.entry.js");
    const built = await browserBuilder(projectRoot, clientEntry);
    put("client.js", built.body); compilations.push({ entry: "harness/plugins/orion-workbench/client.entry.js", inputs: built.inputs });
  }
  if (!files.has("client.js")) throw new Error("Missing built Client entry");
  const shim = files.get("runtime.js").toString("utf8");
  if (!shim.includes('"../branded-web-runtime/index.js"') && !shim.includes("'../branded-web-runtime/index.js'")) throw new Error("Runtime shim must reference the existing ORION gateway");
  put("runtime.js", shim.replace(/(["'])\.\.\/branded-web-runtime\/index\.js\1/g, '"./runtime/index.js"'));
  const hostRoot = join(projectRoot, "harness/plugins/branded-web-runtime");
  put("runtime/index.js", await readRegularFile(join(hostRoot, "index.js"), "ORION host gateway"));
  await walk(join(hostRoot, "lib"), async (path, source) => {
    if (extname(path) !== ".js") throw new Error(`Unapproved host runtime file: ${path}`);
    put(`runtime/lib/${path}`, await readRegularFile(source, `runtime/lib/${path}`));
  });
  const assetRoot = join(projectRoot, "harness/web/assets");
  await walk(assetRoot, async (path, source) => {
    if (EXCLUDED_ASSETS.has(path)) return;
    if (!ASSET_EXTENSIONS.has(extname(path))) throw new Error(`Unapproved static asset: ${path}`);
    put(`assets/${path}`, await readRegularFile(source, `assets/${path}`));
  });
  const sourceOrder = assetOrder((await readRegularFile(join(projectRoot, "harness/web/build_frontend.mjs"), "frontend recipe")).toString("utf8"));
  const vendors = [
    [join(dependencyRoot, "three/build/three.module.min.js"), "three.module.min.js"],
    [join(dependencyRoot, "three/build/three.core.min.js"), "three.core.min.js"],
    [await exists(join(projectRoot, "vendor/echarts.min.js")) ? join(projectRoot, "vendor/echarts.min.js") : join(projectRoot, "services/ontology_engineering/report_assets/js/echarts.min.js"), "echarts.min.js"],
  ];
  for (const [source, name] of vendors) put(`assets/vendor/${name}`, await readRegularFile(source, `vendor/${name}`));
  for (const name of ["echarts-LICENSE.txt", "echarts-NOTICE.txt"]) {
    const source = join(projectRoot, "vendor", name);
    if (await exists(source)) put(`assets/licenses/${name}`, await readRegularFile(source, `ECharts ${name}`));
    else if (files.get("assets/vendor/echarts.min.js").toString("utf8").includes("Apache Software Foundation")) throw new Error(`Missing complete ECharts vendor notice: ${name}`);
  }
  for (const name of ["3d-force-graph", "force-graph", "three", "yaml"]) {
    for (const license of ["LICENSE", "LICENSE.txt", "LICENSE.md"]) {
      const source = join(dependencyRoot, name, license);
      if (await exists(source)) { put(`assets/licenses/${name}-${license}`, await readRegularFile(source, `license ${name}`)); break; }
    }
  }
  for (const entry of ["modules/ontology-graph-viewer.js", "modules/ontology-center/file-preview.js"]) {
    const built = await browserBuilder(projectRoot, join(assetRoot, entry));
    put(`assets/${entry}`, built.body); compilations.push({ entry: `harness/web/assets/${entry}`, inputs: built.inputs });
  }
  if (files.has("glass.css")) { put("assets/glass.css", files.get("glass.css")); sourceOrder.styles.push("glass.css"); }
  for (const [path, body] of files) {
    if (path.startsWith("assets/") && [".css", ".js"].includes(extname(path))) put(path, namespaceAssetUrls(body.toString("utf8")));
  }
  for (const path of [...sourceOrder.scripts, ...sourceOrder.styles]) if (!files.has(`assets/${path}`)) throw new Error(`Frontend manifest refers to an absent asset: ${path}`);
  const presets = await readPresets(projectRoot, runtimeRoot);
  const peers = manifest.peerDependencies?.["@deepseek-ai/dsh"]?.split(/\s*\|\|\s*/).map(item => item.trim());
  if (!peers?.includes(presets.version)) throw new Error("Preset source runtime must match an exact declared DSH peer version");
  const bundledSkills = await readBundledSkills(projectRoot, runtimeRoot);
  if (bundledSkills.version !== presets.version || manifest.peerDependencies?.["@deepseek-ai/dsh-skill-filesystem"] !== presets.version) throw new Error("Bundled skill provider must match the exact preset runtime peer");
  for (const [path, body] of bundledSkills.files) put(path, body);
  put("notices/skills-provenance.json", json(bundledSkills.provenance));
  put("cordis.patch.yml", `${files.get("cordis.patch.yml").toString("utf8").trimEnd()}\n\n${presets.rows}`);
  put("notices/DeepSeek-standard-preset-LICENSE.txt", presets.license);
  put("notices/preset-provenance.json", json(presets.provenance));
  const backend = dependencyManifest((await readRegularFile(join(projectRoot, "pyproject.toml"), "backend package metadata")).toString("utf8"));
  const backendSource = backendSourceSnapshot ?? await backendFingerprint(projectRoot);
  if (backendSourceSnapshot && await exists(join(projectRoot, "contracts/runtime-source-manifest.json"))) {
    const actual = await backendFingerprint(projectRoot);
    if (actual.sha256 !== backendSourceSnapshot.sha256) throw new Error("Installed runtime sources do not match the backend fingerprint snapshot");
  }
  if (backendSourceSnapshot) {
    if (backendSource.algorithm !== "sha256-path-content-v1" || !Array.isArray(backendSource.files) || backendSource.fileCount !== backendSource.files.length || !Array.isArray(backendSource.scope)) throw new Error("Malformed external backend source fingerprint snapshot");
    const digest = createHash("sha256"), seen = new Set();
    for (const record of backendSource.files) {
      packagePath(record.path);
      if (seen.has(record.path) || !Number.isSafeInteger(record.bytes) || record.bytes < 0 || !/^[a-f0-9]{64}$/.test(record.sha256)) throw new Error("Malformed external backend fingerprint file record");
      seen.add(record.path);
      digest.update(record.path).update("\0").update(record.sha256).update("\n");
    }
    if (digest.digest("hex") !== backendSource.sha256) throw new Error("External backend source fingerprint snapshot has changed");
  }
  backend.expectedBackendFingerprint = { algorithm: backendSource.algorithm, sha256: backendSource.sha256, fileCount: backendSource.fileCount, scope: backendSource.scope };
  const optionalFonts = [...new Set([...files].filter(([path]) => extname(path) === ".css").flatMap(([, body]) => [...body.toString("utf8").matchAll(/\/orion-workbench-assets\/(fonts\/[^"')\s]+)/g)].map(match => match[1])))].filter(path => !files.has(`assets/${path}`));
  const assets = {
    schemaVersion: 1, mode: "core", assetBase: ASSET_PREFIX,
    scripts: sourceOrder.scripts.map(path => `${ASSET_PREFIX}${path}`),
    styles: sourceOrder.styles.map(path => `${ASSET_PREFIX}${path}`),
    modules: moduleManifest(sourceOrder.scripts), backend,
    lifecycle: "Owned Host routes and Client slots manage loading, page mounting, cancellation and disposal. Static packaging is not business acceptance.",
    optionalFonts: { missing: optionalFonts, fallback: "system sans-serif" },
    exclusions: [...EXCLUDED_ASSETS].sort(),
    files: [...files].filter(([path]) => path.startsWith("assets/")).map(([path, body]) => ({ path: path.slice(7), url: `${ASSET_PREFIX}${path.slice(7)}`, sha256: sha256(body), bytes: body.length })).sort((a, b) => a.path.localeCompare(b.path, "en")),
  };
  put("workbench-assets.json", json(assets));
  // The Host can serve this file using the same prefix-to-assets-directory rule.
  put("assets/workbench-assets.json", json(assets));
  const packagedManifest = { ...manifest, private: true, publishConfig: { ...manifest.publishConfig, access: "restricted" }, files: ["index.js", "runtime.js", "runtime", "client.js", "lib", "skills", "assets", "workbench-assets.json", "cordis.patch.yml", "notices", ...["glass.css", "README.md", "LICENSE", "LICENSE.md", "LICENSE.txt", "icon.svg", "locale", "styles"].filter(path => files.has(path) || [...files.keys()].some(name => name.startsWith(`${path}/`)))], orion: { ...manifest.orion, candidate: true, externalBackend: backend, presetSource: presets.provenance, bundledSkills: bundledSkills.provenance.skills.map(({ name }) => name) } };
  delete packagedManifest.scripts;
  put("package.json", json(packagedManifest));
  const officialBundlePreflight = await bundleValidator({ files, manifest: packagedManifest, runtimeRoot });
  const facts = { assetManifest: "workbench-assets.json", backend, backendSource, presets: presets.provenance, skills: bundledSkills.provenance, officialBundlePreflight, compilation: compilations, exclusions: [...EXCLUDED_ASSETS].sort() };
  if (stageOnly) return { files, manifest: packagedManifest, outputDir, facts };
  return deliverArchive(files, manifest, outputDir, facts);
}

export async function packageAquaPlugin({ projectRoot = defaultProjectRoot, pluginRoot, outputDir, runtimeRoot, bundleValidator, stageOnly = false } = {}) {
  projectRoot = resolve(projectRoot);
  pluginRoot = resolve(pluginRoot ?? join(projectRoot, "harness/plugins/dsh-client-ui-aqua"));
  outputDir = resolve(outputDir ?? join(projectRoot, ".orion-runtime/plugin-packages/aqua"));
  const distance = relative(pluginRoot, outputDir);
  if (!distance || (!isAbsolute(distance) && !distance.startsWith(".."))) throw new Error("Aqua output must be outside its source");
  const info = await lstat(pluginRoot);
  if (info.isSymbolicLink() || !info.isDirectory()) throw new Error("Aqua source must be a regular directory");
  const manifest = JSON.parse((await readRegularFile(join(pluginRoot, "package.json"))).toString("utf8"));
  if (manifest.name !== "dsh-client-ui-aqua" || manifest.private !== true || manifest.license !== "MIT") throw new Error("Aqua candidate must retain its private legacy identity and MIT license");
  if (!/^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$/.test(manifest.version ?? "")) throw new Error("Invalid Aqua package version");
  if (manifest.dsh?.bundle?.patch !== "./cordis.patch.yml") throw new Error("Aqua candidate requires its installable bundle patch");
  const files = new Map();
  const put = (path, body) => {
    assertPublishableContent(path, body);
    files.set(packagePath(path), body);
  };
  for (const entry of await readdir(pluginRoot, { withFileTypes: true })) {
    if (SECRET_FILE.test(entry.name)) throw new Error(`Secret file rejected: ${entry.name}`);
    if (entry.isSymbolicLink()) throw new Error(`Aqua symlink rejected: ${entry.name}`);
  }
  for (const path of ["cordis.patch.yml", "README.md", "LICENSE"]) put(path, await readRegularFile(join(pluginRoot, path), path));
  await walk(join(pluginRoot, "lib"), async (path, source) => {
    if (!path.endsWith(".js") && !path.endsWith(".d.ts")) throw new Error(`Unapproved Aqua file: ${path}`);
    put(`lib/${path}`, await readRegularFile(source, `lib/${path}`));
  });
  const license = files.get("LICENSE").toString("utf8");
  if (!license.includes("MIT License") || !license.includes("Copyright")) throw new Error("Original Aqua license notice is required");
  const packaged = { ...manifest, private: true, publishConfig: { ...manifest.publishConfig, access: "restricted" }, files: ["lib", "cordis.patch.yml", "README.md", "LICENSE"], orion: { candidate: true, sourcePluginId: manifest.name, validation: "Candidate packaging does not replace independent runtime and UI acceptance." } };
  delete packaged.scripts;
  delete packaged.devDependencies;
  packaged.exports = { ...packaged.exports };
  delete packaged.exports["./src/*"];
  put("package.json", Buffer.from(json(packaged)));
  if ([...files.values()].reduce((sum, body) => sum + body.length, 0) > MAX_TOTAL_BYTES) throw new Error("Aqua package exceeds candidate limit");
  const facts = { kind: "aqua", retainedPluginId: manifest.name, license: { type: "MIT", file: "LICENSE", sha256: sha256(files.get("LICENSE")) } };
  if (bundleValidator) facts.officialBundlePreflight = await bundleValidator({ files, manifest: packaged, runtimeRoot });
  if (stageOnly) return { files, manifest: packaged, outputDir, facts };
  return deliverArchive(files, packaged, outputDir, facts);
}

function parseArgs(argv) {
  const options = {};
  const flags = new Map([["--project-root", "projectRoot"], ["--plugin-root", "pluginRoot"], ["--runtime-root", "runtimeRoot"], ["--output-dir", "outputDir"]]);
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === "--help") return null;
    if (argv[i] === "--aqua") { options.aqua = true; continue; }
    const key = flags.get(argv[i]);
    if (!key || !argv[i + 1] || argv[i + 1].startsWith("--")) throw new Error(`Unknown or incomplete packaging argument: ${argv[i]}`);
    options[key] = argv[++i];
  }
  return options;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const options = parseArgs(process.argv.slice(2));
    if (options === null) process.stdout.write("Usage: node scripts/package_orion_plugin.mjs [--aqua] [--project-root DIR] [--plugin-root DIR] [--runtime-root DIR] [--output-dir DIR]\nLocal private candidate only; no publish, installation, services or business-data access.\n");
    else {
      const result = options.aqua ? await packageAquaPlugin(options) : await packageOrionPlugin(options);
      process.stdout.write(json({ archive: result.archive, sha256: result.sha256, fileCount: result.fileCount, private: true, externalBackendRequired: Boolean(result.receipt.backend) }));
    }
  } catch (error) { process.stderr.write(`${error.message}\n`); process.exitCode = 1; }
}
