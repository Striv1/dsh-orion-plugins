import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import { gunzipSync } from "node:zlib";
import test from "node:test";
import { packageRuntimeSource } from "../../scripts/package_orion_runtime.mjs";
import { assertRuntimeContent, createRuntimeManifest, fingerprintRuntimeFiles, portableSemanticaLauncher, verifyRuntimeManifest, SAFE_RUNTIME_MAKEFILE } from "../../scripts/orion_runtime_files.mjs";

const project = fileURLToPath(new URL("../../", import.meta.url));
const invoke = promisify(execFile), json = value => JSON.stringify(value, null, 2) + "\n";

async function fixture(t) {
  const root = await realpath(await mkdtemp(join(tmpdir(), "orion-runtime-source-test-")));
  t.after(() => rm(root, { recursive: true, force: true }));
  const save = async (path, body) => { await mkdir(dirname(join(root, path)), { recursive: true }); await writeFile(join(root, path), body); };
  const files = new Map([
    ["pyproject.toml", Buffer.from('[project]\nname="dsh-orion-runtime"\nversion="1.0.0rc1"\n')],
    ["uv.lock", Buffer.from('version=1\n')], ["Makefile", Buffer.from(SAFE_RUNTIME_MAKEFILE)],
    ["services/realtime_qa/api.py", Buffer.from("raise RuntimeError('verification must not import the app')\n")],
    ["scripts/ensure_semantica_runtime.sh", Buffer.from("#!/bin/sh\nexit 0\n")],
  ]);
  for (const path of ["scripts/orion_runtime_manifest.py", "scripts/prepare_orion_runtime.py"]) files.set(path, await readFile(join(project, path)));
  for (const [path, body] of files) await save(path, body);
  const manifest = createRuntimeManifest(files, "1.0.0-rc.1", { required: [...files.keys()] });
  await save("contracts/runtime-source-manifest.json", json(manifest));
  await save("contracts/backend-source-fingerprint.json", json(fingerprintRuntimeFiles(files)));
  await save("package.json", json({ name: "dsh-orion-plugins", version: "1.0.0-rc.1", private: true }));
  return { root, save, files, manifest, options: { projectRoot: root, outputDir: join(root, "dist") } };
}

function unpack(bytes) {
  const tar = gunzipSync(bytes), files = new Map();
  for (let cursor = 0; cursor + 512 <= tar.length && tar[cursor];) {
    const header = tar.subarray(cursor, cursor + 512), string = (offset, length) => header.subarray(offset, offset + length).toString().replace(/\0.*$/s, "");
    const name = string(0, 100), prefix = string(345, 155), size = parseInt(string(124, 12), 8);
    assert.equal(header[156], 48);
    files.set((prefix ? prefix + "/" : "") + name, { body: tar.subarray(cursor + 512, cursor + 512 + size), mode: parseInt(string(100, 8), 8) });
    cursor += 512 + Math.ceil(size / 512) * 512;
  }
  return files;
}

test("runtime archive is reproducible, self-contained and retains executable shell resources", async t => {
  const { options, manifest } = await fixture(t);
  const first = await packageRuntimeSource(options), bytes = await readFile(first.archive);
  const second = await packageRuntimeSource(options);
  assert.equal(first.sha256, second.sha256);
  assert.deepEqual(await readFile(second.archive), bytes);
  const files = unpack(bytes), base = "dsh-orion-runtime-1.0.0-rc.1/";
  assert.equal(files.get(base + "scripts/ensure_semantica_runtime.sh").mode, 0o755);
  assert.equal(files.get(base + "services/realtime_qa/api.py").mode, 0o644);
  assert.equal(JSON.parse(files.get(base + "contracts/runtime-source-manifest.json").body).fingerprint, manifest.fingerprint);
  assert.ok(files.has(base + "uv.lock"));
  assert.equal(first.receipt.actions.virtualEnvironmentCopied, false);
  assert.equal(first.receipt.actions.serviceStart, false);
  assert.ok(first.receipt.distribution.includes("wheel equivalence has not been verified"));
});

test("drift in a script, resource or safe Makefile rejects delivery even at the same version", async t => {
  const { root, options, save } = await fixture(t);
  await packageRuntimeSource(options);
  const previous = await readFile(join(root, "dist/dsh-orion-runtime-1.0.0-rc.1.tar.gz"));
  await save("Makefile", "ontology-s6:\n\t@true\n");
  await assert.rejects(packageRuntimeSource(options), /Runtime source drift/);
  assert.deepEqual(await readFile(join(root, "dist/dsh-orion-runtime-1.0.0-rc.1.tar.gz")), previous);
});

test("runtime source rejects missing required entries, duplicate paths and symlinked resource parents", async t => {
  const { root, manifest } = await fixture(t);
  await assert.rejects(verifyRuntimeManifest(root, { ...manifest, required: [...manifest.required, "scripts/missing.py"] }), /Required runtime input missing/);
  await assert.rejects(verifyRuntimeManifest(root, { ...manifest, files: [...manifest.files, manifest.files[0]] }), /Invalid runtime file record/);
  await rm(join(root, "services"), { recursive: true });
  await mkdir(join(root, "outside/realtime_qa"), { recursive: true });
  await writeFile(join(root, "outside/realtime_qa/api.py"), "outside");
  await symlink(join(root, "outside"), join(root, "services"));
  await assert.rejects(verifyRuntimeManifest(root, manifest), /symlink rejected/);
});

test("business directories, secrets and personal absolute paths cannot enter runtime resources", () => {
  for (const path of ["data/customers.csv", "state/registry.json", ".venv/bin/python", "../escape.py", "harness/.credentials.yml"]) {
    assert.throws(() => assertRuntimeContent(path, Buffer.from("test")), /Unsafe runtime source path/);
  }
  assert.throws(() => assertRuntimeContent("services/config.py", Buffer.from('root="/Users/example/private"')), /personal absolute path/);
  assert.throws(() => assertRuntimeContent("services/config.py", Buffer.from("-----BEGIN PRIVATE KEY-----")), /credential-like material/);
});

test("Python manifest verifier checks actual source bytes without importing app or starting services", async t => {
  const { root, manifest, save } = await fixture(t);
  const args = [join(root, "scripts/orion_runtime_manifest.py"), "--root", root, "--check"];
  const output = JSON.parse((await invoke("python3", args)).stdout);
  assert.equal(output.fingerprint, manifest.fingerprint);
  assert.equal(output.appImported, false);
  assert.equal(output.serviceStarted, false);
  await save("services/realtime_qa/api.py", "raise RuntimeError('changed')\n");
  await assert.rejects(invoke("python3", args), /runtime source content has changed/);
});

test("Profile preparation produces consistent paths and preserves preexisting registry bytes", async t => {
  const { root } = await fixture(t), profile = join(root, "profile"), runtime = join(root, "runtime");
  // Profile must be outside the frozen source root, as it is in a real deployment.
  await mkdir(runtime);
  for (const path of ["services", "scripts", "contracts"]) {
    const { rename } = await import("node:fs/promises"); await rename(join(root, path), join(runtime, path));
  }
  for (const path of ["pyproject.toml", "uv.lock", "Makefile"]) {
    const { rename } = await import("node:fs/promises"); await rename(join(root, path), join(runtime, path));
  }
  const python = join(profile, ".venvs/core/bin/python");
  await mkdir(dirname(python), { recursive: true }); await writeFile(python, "#!/bin/sh\nexit 0\n"); await chmod(python, 0o755);
  await writeFile(join(profile, ".venvs/core/pyvenv.cfg"), "include-system-site-packages = false\n");
  const registry = join(profile, "state/runtime-registry.json");
  await mkdir(dirname(registry), { recursive: true });
  const original = Buffer.from([0xff, 0x00, 0x70]); await writeFile(registry, original);
  const args = [join(runtime, "scripts/prepare_orion_runtime.py"), "--runtime-root", runtime, "--profile-root", profile, "--python", python, "--port", "8199"];
  const prepared = JSON.parse((await invoke("python3", args)).stdout);
  assert.equal(prepared.registryCreated, false);
  assert.equal(prepared.credentialsRead, false);
  assert.deepEqual(await readFile(registry), original);
  const environment = JSON.parse(await readFile(join(profile, "runtime-environment.json")));
  assert.equal(environment.ORION_WREN_PYTHON, join(profile, ".venvs/wren/bin/python"));
  assert.equal(environment.ORION_REALTIME_RUNTIME_REGISTRY, registry);
  assert.equal(environment.ORION_BACKEND_ROOT, runtime);
  assert.equal(environment.ONTOLOGY_AGENT_API_URL, "http://127.0.0.1:8199");
  assert.equal(environment.ORION_RUNTIME_PORT, "8199");
  assert.equal(JSON.parse(await readFile(join(profile, "runtime-manager.json"))).environment, undefined);
  const saved = await readFile(join(profile, "runtime-environment.json"));
  await assert.rejects(invoke("python3", args), /Existing Profile configuration is preserved/);
  assert.deepEqual(await readFile(join(profile, "runtime-environment.json")), saved);
  assert.deepEqual(await readFile(registry), original);
});

test("safe Makefile retains S5/S6 gates and contains no shell token discovery or old launchers", async t => {
  const { root } = await fixture(t);
  assert.doesNotMatch(SAFE_RUNTIME_MAKEFILE, /\$\(shell|\$\(HOME\)|include \.env|CHAT2DB|harness-install|orion_workbench\.py/);
  assert.match(SAFE_RUNTIME_MAKEFILE, /ontology-s5: protege-construction-route/);
  assert.match(SAFE_RUNTIME_MAKEFILE, /ontology-s6: build-manifest-check semantica-runtime/);
  assert.match(SAFE_RUNTIME_MAKEFILE, /--lease-owner orion-platform/);
  const output = await invoke("make", ["-n", "ontology-s6", "ONTOLOGY_PROJECT_ID=test", "ONTOLOGY_WORKFLOW_HOME=/configured/workflows", "SEMANTICA_CLI=/configured/cli", "SEMANTICA_PYTHON=/configured/python", "SEMANTICA_MCP_COMMAND=/configured/mcp"], { cwd: root });
  assert.ok(output.stdout.includes("orion_runtime_manifest.py --check"));
  assert.ok(output.stdout.includes("ensure_semantica_runtime.sh ensure"));
  assert.ok(output.stdout.includes("run_quality_validation_stage.py"));
});

test("real make runtime-check accepts executable and runtime paths containing spaces", async t => {
  const { root, manifest } = await fixture(t), nested = join(root, "source with spaces"), python = join(root, "core environment/bin/python");
  const { rename } = await import("node:fs/promises");
  await mkdir(nested);
  for (const path of ["services", "scripts", "contracts", "pyproject.toml", "uv.lock", "Makefile"]) await rename(join(root, path), join(nested, path));
  await mkdir(dirname(python), { recursive: true });
  await writeFile(python, '#!/bin/sh\nexec python3 "$@"\n'); await chmod(python, 0o755);
  const output = JSON.parse((await invoke("make", ["runtime-check", `PYTHON=${python}`], { cwd: nested })).stdout);
  assert.equal(output.valid, true); assert.equal(output.fingerprint, manifest.fingerprint);
  assert.equal(output.appImported, false);
  assert.equal((SAFE_RUNTIME_MAKEFILE.match(/@"\$\(PYTHON\)"/g) || []).length, 4);
});

test("exported Semantica launcher needs explicit Profile configuration and has no legacy interpreter fallback", async t => {
  const root = await realpath(await mkdtemp(join(tmpdir(), "orion-semantica-export-test-")));
  t.after(() => rm(root, { recursive: true, force: true }));
  const legacy = [
    '#!/bin/sh', 'set -eu',
    'CONFIG_ROOT="${ORION_PROJECT_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"',
    'ACTIVE_CONFIG="${SEMANTICA_ACTIVE_RUNTIME_CONFIG:-$CONFIG_ROOT/.orion-semantica/active-runtime.json}"',
    'exec "$CONFIG_ROOT/.venv/bin/python" "$CONFIG_ROOT/scripts/semantica_runtime_config.py"',
    'SEMANTICA_CLI="${SEMANTICA_CLI:-${SEMANTICA_UPSTREAM_ROOT:-${HOME}/FDE/semantica-upstream-20260908}/.venv/bin/semantica}"',
    'SEMANTICA_PYTHON="${SEMANTICA_PYTHON:-$(dirname -- "$SEMANTICA_CLI")/python}"',
    'ORION_ROOT="${ORION_PROJECT_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)}"',
    'STATE_DIR="${SEMANTICA_STATE_DIR:-$ORION_ROOT/.orion-semantica}"',
    'SYNC_PYTHON="${SEMANTICA_SYNC_PYTHON:-$ORION_ROOT/.venv/bin/python}"',
  ].join("\n") + "\n";
  const body = portableSemanticaLauncher(Buffer.from(legacy));
  assert.doesNotMatch(body.toString(), /\$\{HOME\}|\/FDE\/|\.venv\/bin\/python|\.orion-semantica/);
  assert.throws(() => portableSemanticaLauncher(Buffer.from("different source")), /contract changed/);
  const launcher = join(root, "ensure.sh"), python = join(root, "configured core python");
  await writeFile(launcher, body);
  await writeFile(python, "#!/bin/sh\nexit 99\n"); await chmod(python, 0o755);
  await assert.rejects(invoke("/bin/sh", [launcher], { env: { PATH: process.env.PATH, ORION_WORKFLOW_PYTHON: python } }),
    error => error.code !== 0 && error.code !== 99 && /explicit Profile Semantica runtime config/.test(error.stderr));
});
