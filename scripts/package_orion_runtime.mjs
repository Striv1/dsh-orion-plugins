import { lstat, mkdir, mkdtemp, readFile, rename, rm, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { tarGzip } from "./package_orion_plugin.mjs";
import { fingerprintRuntimeFiles, runtimePath, runtimeRegular, runtimeSha256, verifyRuntimeManifest } from "./orion_runtime_files.mjs";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const json = value => `${JSON.stringify(value, null, 2)}\n`;

// Only canonical stable, alpha.N, beta.N and rc.N releases are supported.
// Do not guess how arbitrary SemVer prereleases, build metadata or PEP 440
// local/dev/post releases map to one another.
function pythonReleaseVersion(version) {
  const match = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(alpha|beta|rc)\.(0|[1-9]\d*))?$/.exec(version);
  if (!match) throw new Error(`Unsupported runtime release version: ${version}`);
  return `${match[1]}.${match[2]}.${match[3]}${match[4] ? { alpha: "a", beta: "b", rc: "rc" }[match[4]] + match[5] : ""}`;
}

async function verifyPythonReleaseMetadata(files, runtimeVersion) {
  const expected = pythonReleaseVersion(runtimeVersion);
  for (const path of ["pyproject.toml", "uv.lock"]) {
    if (!files.has(path)) throw new Error(`Required Python release metadata missing: ${path}`);
  }
  // Use the runtime's required Python 3.11+ standard TOML parser, not regexes
  // that can confuse dependency tables, comments or multiline strings. Parse
  // the verified in-memory snapshot so a later disk edit cannot change it.
  const script = `
import json, sys, tomllib
try:
    sources = json.load(sys.stdin)
    project = tomllib.loads(sources["pyproject.toml"]).get("project", {})
    packages = tomllib.loads(sources["uv.lock"]).get("package", [])
    if not isinstance(project, dict) or project.get("name") != "dsh-orion-runtime":
        raise ValueError("pyproject.toml must declare project dsh-orion-runtime")
    if not isinstance(packages, list) or any(not isinstance(p, dict) for p in packages):
        raise ValueError("uv.lock must contain package tables")
    matches = [p for p in packages if p.get("name") == "dsh-orion-runtime"]
    if len(matches) != 1:
        raise ValueError("uv.lock must contain exactly one dsh-orion-runtime package")
    versions = {"pyproject.toml": project.get("version"), "uv.lock": matches[0].get("version")}
    if any(not isinstance(v, str) for v in versions.values()):
        raise ValueError("Python runtime versions must be explicit strings")
    print(json.dumps(versions))
except (ValueError, TypeError) as error:
    print(str(error), file=sys.stderr)
    sys.exit(1)
`;
  const metadata = await new Promise((resolve, reject) => {
    const child = execFile("python3", ["-I", "-c", script], { timeout: 10000, maxBuffer: 64 * 1024 }, (error, stdout, stderr) => {
      if (error) reject(new Error(`Cannot validate Python runtime metadata (python3 3.11+ required): ${stderr.trim() || error.message}`));
      else {
        try { resolve(JSON.parse(stdout)); } catch { reject(new Error("Invalid Python runtime metadata validation result")); }
      }
    });
    child.stdin.on("error", () => {}); // execFile reports startup/early-exit failures.
    child.stdin.end(json(Object.fromEntries(["pyproject.toml", "uv.lock"].map(path => [path, files.get(path).toString("utf8")]))));
  });
  for (const [path, version] of Object.entries(metadata)) {
    if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(?:a|b|rc)(?:0|[1-9]\d*))?$/.test(version)) throw new Error(`Unsupported Python runtime version in ${path}: ${version}`);
    if (version !== expected) throw new Error(`Python runtime version mismatch in ${path}: expected ${expected} for ${runtimeVersion}, got ${version}`);
  }
}

export async function packageRuntimeSource({ projectRoot = root, outputDir, stageOnly = false } = {}) {
  projectRoot = resolve(projectRoot); outputDir = resolve(outputDir ?? join(projectRoot, "dist"));
  const manifestPath = "contracts/runtime-source-manifest.json";
  const manifestBody = await runtimeRegular(projectRoot, manifestPath), manifest = JSON.parse(manifestBody);
  const files = await verifyRuntimeManifest(projectRoot, manifest);
  const project = JSON.parse(await runtimeRegular(projectRoot, "package.json"));
  if (project.name !== "dsh-orion-plugins" || project.private !== true || project.version !== manifest.runtimeVersion) throw new Error("Runtime release must match the private monorepo version");
  await verifyPythonReleaseMetadata(files, manifest.runtimeVersion);
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
