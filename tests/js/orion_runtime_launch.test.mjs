import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { chmod, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { createRuntimeManifest } from "../../scripts/orion_runtime_files.mjs";

const project = fileURLToPath(new URL("../../", import.meta.url));
const invoke = promisify(execFile), json = value => JSON.stringify(value) + "\n";
const quote = value => "'" + value.replace(/'/g, "'\\''") + "'";

async function fixture(t) {
  const root = await realpath(await mkdtemp(join(tmpdir(), "orion-explicit-launch-")));
  t.after(() => rm(root, { recursive: true, force: true }));
  const runtime = join(root, "source with spaces"), profile = join(root, "Profile with spaces"), home = join(root, "isolated home");
  const save = async (path, body) => { await mkdir(dirname(path), { recursive: true }); await writeFile(path, body); };
  const files = new Map([["services/realtime_qa/api.py", Buffer.from("raise RuntimeError('launch validation must not import app')\n")]]);
  for (const path of ["scripts/orion_runtime_manifest.py", "scripts/prepare_orion_runtime.py", "scripts/launch_orion_harness.py"]) files.set(path, await readFile(join(project, path)));
  for (const [path, body] of files) await save(join(runtime, path), body);
  const manifest = createRuntimeManifest(files, "1.0.0-rc.2", { required: [...files.keys()] });
  await save(join(runtime, "contracts/runtime-source-manifest.json"), json(manifest));
  const python = join(profile, ".venvs/core/bin/python");
  await save(python, "#!/bin/sh\nexit 99\n"); await chmod(python, 0o755);
  await save(join(profile, ".venvs/core/pyvenv.cfg"), "include-system-site-packages = false\n");
  await invoke("python3", [join(runtime, "scripts/prepare_orion_runtime.py"), "--runtime-root", runtime, "--profile-root", profile, "--python", python, "--port", "8099"]);
  const environmentFile = join(profile, "runtime-environment.json"), launcher = join(runtime, "scripts/launch_orion_harness.py");
  const dump = join(root, "inspect launch.py");
  await save(dump, "import json, os, sys\nprint(json.dumps({'pid':os.getpid(),'args':sys.argv[1:],'home':os.environ.get('DSH_HOME'),'mode':os.environ.get('ORION_RUNTIME_MODE'),'backend':os.environ.get('ORION_BACKEND_ROOT'),'python':os.environ.get('ORION_WORKFLOW_PYTHON'),'secretPresent':'ORION_TEST_SECRET' in os.environ,'ambientBusinessPresent':'ORION_UNRELATED_PROJECT' in os.environ,'nodeOptionsPresent':'NODE_OPTIONS' in os.environ}))\n");
  const proxy = `#!/bin/sh\nexec python3 ${quote(dump)} "$@"\n`;
  const node = join(root, "official node with spaces"); await save(node, proxy); await chmod(node, 0o755);
  const dshEntry = join(root, "official sdk/lib/bin.js");
  await save(dshEntry, "// selected official CLI fixture; the process proxy owns inspection\n");
  await save(join(root, "official sdk/package.json"), json({ name: "@deepseek-ai/dsh", version: "0.2.0-rc.2" }));
  const app = join(root, "DeepSeek Harness.app"), userData = join(root, "desktop user data");
  await save(join(app, "Contents/Info.plist"), '<?xml version="1.0"?><plist version="1.0"><dict><key>CFBundleExecutable</key><string>DeepSeek Harness</string><key>CFBundleIdentifier</key><string>com.deepseek.dsh</string><key>CFBundleShortVersionString</key><string>0.2.0-rc.2</string></dict></plist>');
  const appEntry = join(app, "Contents/MacOS/DeepSeek Harness"); await save(appEntry, proxy); await chmod(appEntry, 0o755);
  const common = [launcher, "--environment", environmentFile, "--dsh-home", home];
  const web = ["web", "--node", node, "--dsh-entry", dshEntry, "--port", "3094", "--no-open"];
  return { root, runtime, profile, home, python, manifest, save, environmentFile, launcher, common, web, node, dshEntry, app, userData };
}

test("web exec passes the prepared Profile before startup and strips ambient credentials/business/options", async t => {
  const f = await fixture(t), previous = await readFile(f.environmentFile);
  const run = new Promise((resolve, reject) => {
    const child = execFile("python3", [...f.common, ...f.web], { env: { ...process.env, ORION_TEST_SECRET: "must-stay-out", ORION_UNRELATED_PROJECT: "foreign", NODE_OPTIONS: "invalid inherited flag" } },
      (error, stdout, stderr) => error ? reject(error) : resolve({ pid: child.pid, stdout, stderr }));
  });
  const output = await run, observed = JSON.parse(output.stdout);
  assert.equal(observed.pid, output.pid);
  assert.deepEqual(observed.args, [f.dshEntry, "web", "--port", "3094", "--no-open"]);
  assert.equal(observed.home, f.home); assert.equal(observed.backend, f.runtime); assert.equal(observed.python, f.python);
  assert.equal(observed.mode, "managed"); assert.equal(observed.secretPresent, false);
  assert.equal(observed.ambientBusinessPresent, false); assert.equal(observed.nodeOptionsPresent, false);
  assert.deepEqual(await readFile(f.environmentFile), previous);
});

test("desktop exec selects the official app bundle and preserves explicit user-data path", async t => {
  const f = await fixture(t);
  const result = JSON.parse((await invoke("python3", [...f.common, "desktop", "--app", f.app, "--user-data-dir", f.userData])).stdout);
  assert.deepEqual(result.args, ["--user-data-dir=" + f.userData]);
  assert.equal(result.home, f.home); assert.equal(result.backend, f.runtime);
});

test("invalid mode, unsupported environment, missing Core venv and inconsistent source/state reject before execution", async t => {
  const f = await fixture(t), original = JSON.parse(await readFile(f.environmentFile));
  for (const change of [{ ORION_RUNTIME_MODE: "production-adopt" }, { UNDECLARED_TOKEN: "must-not-echo" },
    { ORION_BACKEND_ROOT: f.profile }, { ORION_WORKFLOW_HOME: join(f.runtime, "state") }, { ORION_WORKFLOW_PYTHON: "/usr/bin/python3" }]) {
    await writeFile(f.environmentFile, json({ ...original, ...change }));
    await assert.rejects(invoke("python3", [...f.common, ...f.web]), error => /Launch rejected/.test(error.stderr) && !/must-not-echo/.test(error.stderr));
  }
  await writeFile(f.environmentFile, json(original));
  await rm(join(f.profile, ".venvs/core/pyvenv.cfg"));
  await assert.rejects(invoke("python3", [...f.common, ...f.web]), /Launch rejected/);
});

test("source drift, wrong official SDK and Home/user-data inside immutable source reject before execution", async t => {
  const f = await fixture(t);
  await assert.rejects(invoke("python3", [f.launcher, "--environment", f.environmentFile, "--dsh-home", join(f.runtime, "home"), ...f.web]), /Launch rejected/);
  await assert.rejects(invoke("python3", [...f.common, "desktop", "--app", f.app, "--user-data-dir", join(f.runtime, "desktop")]), /Launch rejected/);
  await f.save(join(f.root, "official sdk/package.json"), json({ name: "@deepseek-ai/dsh", version: "0.1.7-rc.2" }));
  await assert.rejects(invoke("python3", [...f.common, ...f.web]), /Launch rejected/);
  await f.save(join(f.runtime, "services/realtime_qa/api.py"), "changed source\n");
  await assert.rejects(invoke("python3", [...f.common, ...f.web]), /Launch rejected/);
});
