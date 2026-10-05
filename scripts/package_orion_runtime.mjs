import { lstat, mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { tarGzip } from "./package_orion_plugin.mjs";
import { fingerprintRuntimeFiles, runtimePath, runtimeRegular, runtimeSha256, verifyRuntimeManifest } from "./orion_runtime_files.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const json = value => `${JSON.stringify(value, null, 2)}\n`;

export async function packageRuntimeSource({ projectRoot = root, outputDir, stageOnly = false } = {}) {
  projectRoot = resolve(projectRoot); outputDir = resolve(outputDir ?? join(projectRoot, "dist"));
  const manifestPath = "contracts/runtime-source-manifest.json";
  const manifestBody = await runtimeRegular(projectRoot, manifestPath), manifest = JSON.parse(manifestBody);
  const files = await verifyRuntimeManifest(projectRoot, manifest);
  const project = JSON.parse(await runtimeRegular(projectRoot, "package.json"));
  if (project.name !== "dsh-orion-plugins" || project.private !== true || project.version !== manifest.runtimeVersion) throw new Error("Runtime release must match the private monorepo version");
  files.set(manifestPath, manifestBody);
  files.set("contracts/backend-source-fingerprint.json", await runtimeRegular(projectRoot, "contracts/backend-source-fingerprint.json"));
  const snapshot = JSON.parse(files.get("contracts/backend-source-fingerprint.json"));
  if (snapshot.sha256 !== manifest.fingerprint || snapshot.fileCount !== manifest.files.length || fingerprintRuntimeFiles(new Map([...files].filter(([path]) => manifest.files.some(record => record.path === path)))).sha256 !== snapshot.sha256) throw new Error("Runtime source and backend fingerprint disagree");
  // Test source is useful for maintaining the runtime, but does not change its compatibility fingerprint.
  for (const path of manifest.tests ?? []) files.set(runtimePath(path), await runtimeRegular(projectRoot, path));
  const bytes = [...files.values()].reduce((sum, body) => sum + body.length, 0);
  if (bytes > 64 * 1024 * 1024) throw new Error("Runtime source archive exceeds bounded release size");
  if (stageOnly) return { files, manifest, fileCount: files.size, fingerprint: manifest.fingerprint, archiveCreated: false };
  const prefix = `dsh-orion-runtime-${manifest.runtimeVersion}`;
  const executablePaths = new Set(manifest.files.filter(record => record.path.endsWith(".sh")).map(record => record.path));
  const archive = tarGzip(files, { prefix, executablePaths }), archiveName = `${prefix}.tar.gz`, digest = runtimeSha256(archive);
  const fileList = [...files].map(([path, body]) => ({ path, bytes: body.length, sha256: runtimeSha256(body), mode: executablePaths.has(path) ? "0755" : "0644" })).sort((a, b) => a.path.localeCompare(b.path, "en"));
  const receipt = { schemaVersion: 1, runtimeVersion: manifest.runtimeVersion, private: true,
    archive: { file: archiveName, sha256: digest, bytes: archive.length }, sourceFingerprint: manifest.fingerprint,
    fileCount: files.size, unpackedBytes: bytes, required: manifest.required,
    distribution: "Complete private runtime source archive; wheel equivalence has not been verified.",
    actions: { publish: false, networkInstall: false, serviceStart: false, businessDataCopied: false, credentialsCopied: false, virtualEnvironmentCopied: false, thirdPartyServicesCopied: false } };
  await mkdir(outputDir, { recursive: true });
  if ((await lstat(outputDir)).isSymbolicLink()) throw new Error("Runtime output directory must not be a symlink");
  const temporary = await mkdtemp(join(outputDir, ".orion-runtime-pack-"));
  try {
    const outputs = new Map([[archiveName, archive], [`${archiveName}.sha256`, `${digest}  ${archiveName}\n`],
      [`${archiveName}.files.json`, json(fileList)], [`${archiveName}.receipt.json`, json(receipt)]]);
    for (const [path, body] of outputs) await writeFile(join(temporary, path), body, { mode: 0o600 });
    for (const path of outputs.keys()) await rename(join(temporary, path), join(outputDir, path));
  } finally { await rm(temporary, { recursive: true, force: true }); }
  return { name: "dsh-orion-runtime", version: manifest.runtimeVersion, archive: join(outputDir, archiveName), sha256: digest, fileCount: files.size, receipt };
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const options = {};
    for (let i = 2; i < process.argv.length; i++) {
      const arg = process.argv[i];
      if (arg === "--check-only") options.stageOnly = true;
      else if (["--project-root", "--output-dir"].includes(arg) && process.argv[i + 1] && !process.argv[i + 1].startsWith("--")) options[arg === "--project-root" ? "projectRoot" : "outputDir"] = process.argv[++i];
      else throw new Error("Unknown runtime archive argument");
    }
    const result = await packageRuntimeSource(options);
    process.stdout.write(json(options.stageOnly ? { fileCount: result.fileCount, fingerprint: result.fingerprint, archiveCreated: false } : result));
  } catch (error) { process.stderr.write(`${error.message}\n`); process.exitCode = 1; }
}
