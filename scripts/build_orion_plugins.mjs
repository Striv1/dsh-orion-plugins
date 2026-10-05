import { lstat, mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { packageAquaPlugin, packageOrionPlugin, validateOfficialBundle } from "./package_orion_plugin.mjs";
import { packageRuntimeSource } from "./package_orion_runtime.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const exists = path => lstat(path).then(() => true, error => { if (error.code === "ENOENT") return false; throw error; });
const json = value => `${JSON.stringify(value, null, 2)}\n`;

async function writeStage(result, outputDir) {
  await mkdir(outputDir, { recursive: true });
  if ((await lstat(outputDir)).isSymbolicLink()) throw new Error("Build output must not be a symlink");
  const target = join(outputDir, result.manifest.name), marker = ".orion-build-owned.json";
  if (await exists(target)) {
    const owner = JSON.parse(await readFile(join(target, marker), "utf8"));
    if (owner.producer !== "dsh-orion-plugin-build" || owner.package !== result.manifest.name || (await lstat(target)).isSymbolicLink()) throw new Error("Refusing to replace a build directory this tool does not own");
  }
  const temporary = await mkdtemp(join(outputDir, ".orion-stage-"));
  try {
    for (const [path, body] of result.files) {
      await mkdir(dirname(join(temporary, path)), { recursive: true });
      await writeFile(join(temporary, path), body);
    }
    await writeFile(join(temporary, marker), json({ producer: "dsh-orion-plugin-build", package: result.manifest.name }));
    await rm(target, { recursive: true, force: true });
    await rename(temporary, target);
    await writeFile(join(outputDir, `${result.manifest.name}.build-receipt.json`), json({ schemaVersion: 1,
      package: { name: result.manifest.name, version: result.manifest.version, private: true }, fileCount: result.files.size,
      ...result.facts, actions: { archiveCreated: false, publish: false, networkInstall: false, serviceStart: false, businessDataCopied: false } }));
  } finally { await rm(temporary, { recursive: true, force: true }); }
  return target;
}

export async function buildPlugins(options = {}) {
  const projectRoot = resolve(options.projectRoot ?? root);
  const runtimeRoot = resolve(options.runtimeRoot ?? (await exists(join(projectRoot, "node_modules/@deepseek-ai/dsh-app-boot/package.json")) ? projectRoot : join(projectRoot, ".orion-runtime/deepseek-harness/0.2.0-rc.2")));
  const outputDir = resolve(options.outputDir ?? join(projectRoot, "dist"));
  const snapshotFile = join(projectRoot, "contracts/backend-source-fingerprint.json");
  const backendSourceSnapshot = await exists(snapshotFile) ? JSON.parse(await readFile(snapshotFile, "utf8")) : undefined;
  const hasRuntimeSource = await exists(join(projectRoot, "contracts/runtime-source-manifest.json"));
  // Validate every source/resource before writing any deliverable.
  if (hasRuntimeSource) await packageRuntimeSource({ projectRoot, outputDir, stageOnly: true });
  const config = { projectRoot, runtimeRoot, outputDir, dependencyRoot: options.dependencyRoot, backendSourceSnapshot, stageOnly: !options.pack };
  const workbench = await packageOrionPlugin(config);
  const aqua = await packageAquaPlugin({ ...config, bundleValidator: context => validateOfficialBundle({ ...context, requireExactDshPeer: false }) });
  const results = [];
  for (const result of [workbench, aqua]) {
    if (options.pack) results.push({ name: result.receipt.package.name, version: result.receipt.package.version, archive: result.archive, sha256: result.sha256, fileCount: result.fileCount });
    else results.push({ name: result.manifest.name, version: result.manifest.version, fileCount: result.files.size,
      stagedDirectory: options.checkOnly ? null : await writeStage(result, outputDir),
      officialPreflight: result.facts.officialBundlePreflight ?? null, archiveCreated: false });
  }
  if (hasRuntimeSource) {
    const runtime = await packageRuntimeSource({ projectRoot, outputDir, stageOnly: !options.pack });
    results.push(options.pack ? { name: runtime.name, version: runtime.version, archive: runtime.archive,
      sha256: runtime.sha256, fileCount: runtime.fileCount, sourceFingerprint: runtime.receipt.sourceFingerprint }
      : { name: "dsh-orion-runtime", version: runtime.manifest.runtimeVersion, fileCount: runtime.fileCount,
        sourceFingerprint: runtime.fingerprint, archiveCreated: false, resourcesVerified: true });
  }
  return results;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const options = {}, flags = new Map([["--runtime-root", "runtimeRoot"], ["--dependency-root", "dependencyRoot"], ["--output-dir", "outputDir"], ["--project-root", "projectRoot"]]);
    for (let i = 2; i < process.argv.length; i++) {
      const argument = process.argv[i];
      if (argument === "--pack") { options.pack = true; continue; }
      if (argument === "--check-only") { options.checkOnly = true; continue; }
      const key = flags.get(argument);
      if (!key || !process.argv[i + 1] || process.argv[i + 1].startsWith("--")) throw new Error(`Unknown or incomplete build argument: ${argument}`);
      options[key] = process.argv[++i];
    }
    if (options.pack && options.checkOnly) throw new Error("Choose archive creation or preparation checking");
    process.stdout.write(json(await buildPlugins(options)));
  } catch (error) { process.stderr.write(`${error.message}\n`); process.exitCode = 1; }
}
