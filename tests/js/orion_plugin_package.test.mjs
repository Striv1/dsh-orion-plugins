import { pathToFileURL as orionPathToFileURL, fileURLToPath as orionFileURLToPath } from 'node:url';
import { resolve as orionResolve } from 'node:path';
const orionOfficialRuntimeRoot = orionResolve(process.env.ORION_DSH_RUNTIME_ROOT || orionFileURLToPath(new URL('../../', import.meta.url)));
const orionOfficialRuntimeBase = orionPathToFileURL(orionOfficialRuntimeRoot + '/');
import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, realpath, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";
import { gunzipSync } from "node:zlib";
import { assetOrder, namespaceAssetUrls, packageAquaPlugin, packageOrionPlugin, validateOfficialBundle } from "../../scripts/package_orion_plugin.mjs";

const projectRoot = fileURLToPath(new URL("../../", import.meta.url));

async function fixture(t) {
  const root = await mkdtemp(join(tmpdir(), "orion-private-package-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const save = async (path, body) => {
    await mkdir(dirname(join(root, path)), { recursive: true });
    await writeFile(join(root, path), body);
  };
  const packageRoot = "harness/plugins/orion-workbench";
  const runtimeRoot = join(root, "candidate-runtime");
  await save(`${packageRoot}/package.json`, JSON.stringify({ name: "@local/orion-workbench", version: "0.1.0-rc.1", private: true, publishConfig: { access: "restricted" }, type: "module", exports: { ".": "./index.js", "./client": "./client.js", "./skills": "./lib/skills.js" }, dsh: { bundle: { patch: "./cordis.patch.yml" }, client: { platform: "web" } }, peerDependencies: { "@deepseek-ai/dsh": "0.2.0-rc.2", "@deepseek-ai/dsh-skill-filesystem": "0.2.0-rc.2" } }));
  await save(`${packageRoot}/index.js`, 'export { apply } from "./runtime.js";');
  await save(`${packageRoot}/runtime.js`, 'export { apply } from "../branded-web-runtime/index.js";');
  await save(`${packageRoot}/client.js`, "window.__ModuleLoader__.load({id:'@local/orion-workbench',factory(){return {apply(){}}}});");
  await save(`${packageRoot}/lib/native-chat.js`, "export const nativeChat = {};");
  await save(`${packageRoot}/lib/skills.js`, await readFile(join(projectRoot, "harness/plugins/orion-workbench/lib/skills.js")));
  await save(`${packageRoot}/glass.css`, ".orion-workbench { display: block; }");
  await save(`${packageRoot}/cordis.patch.yml`, "- insert:\n    - id: orion-workbench-skills\n      name: '@local/orion-workbench/skills'\n    - id: orion-workbench\n      name: '@local/orion-workbench'\n");
  await save("harness/plugins/branded-web-runtime/index.js", 'import { version } from "./lib/runtime.js"; export function apply() { return version; }');
  await save("harness/plugins/branded-web-runtime/lib/runtime.js", "export const version = 'test';");
  const scriptOrder = ["boot-brand.js", "runtime-polling.js", "brand.js", "modules/engineering/workflow-client.js", "modules/harness-adapter.js", "vendor/echarts.min.js", "modules/ontology-center/ontology-chat.js", "modules/ontology-graph-viewer.js", "modules/ontology-center/file-preview.js", "modules/app-shell.js"];
  await save("harness/web/build_frontend.mjs", `${scriptOrder.map(path => `<script src="/${path}?v=x"></script>`).join("\n")}\n<link rel="stylesheet" href="/brand.css?v=x" />`);
  for (const path of scriptOrder.filter(path => !path.startsWith("vendor/"))) await save(`harness/web/assets/${path}`, `/* ${path} */`);
  await save("harness/web/assets/brand.css", 'a { background: url("/ahs-icon-black.svg"); }');
  await save("harness/web/assets/ahs-icon-black.svg", "<svg/>");
  for (const path of ["three.module.min.js", "three.core.min.js"]) await save(`harness/web/node_modules/three/build/${path}`, "export const three = true;");
  await save("services/ontology_engineering/report_assets/js/echarts.min.js", "window.echarts = {};");
  await save("pyproject.toml", '[project]\nname = "ontology-workorder-agent"\nversion = "0.1.0"\nrequires-python = ">=3.11,<3.14"\n');
  await save("candidate-runtime/node_modules/@deepseek-ai/dsh/package.json", '{"version":"0.2.0-rc.2"}');
  await save("candidate-runtime/node_modules/@deepseek-ai/dsh-skill-filesystem/package.json", '{"version":"0.2.0-rc.2"}');
  await save("candidate-runtime/node_modules/@deepseek-ai/dsh/LICENSE", "MIT License\nOfficial fixture notice\n");
  await save("candidate-runtime/node_modules/@deepseek-ai/dsh-web-app/presets/standard.patch.yml", "- insert:\n    - id: preset-standard\n      name: '@deepseek-ai/dsh-agent-preset'\n      config:\n        id: standard\n        order: 1\n        composition:\n          - id: persona\n            name: '@deepseek-ai/dsh-persona'\n            config:\n              prefix: original\n              suffix: original\n          - id: agent-instructions\n            name: '@deepseek-ai/dsh-agent-instructions'\n");
  for (const id of ["engineering", "ontology-qa"]) {
    await save(`harness/profile/agent-presets/${id}/preset.yml`, `name: ${id}\ndescription: business mode\norder: ${id === "engineering" ? 2 : 3}\n`);
    await save(`harness/profile/agent-presets/${id}/persona.txt`, `${id} governed operations {{cwd}}\n`);
  }
  const browserBuilder = async (_, entry) => ({ body: Buffer.from(`/* compiled ${entry.split("/").at(-1)} */\nwindow.__bundled = true;`), inputs: ["harness/web/assets/input.js"] });
  await save("harness/skills/orion-ontology-engineer/SKILL.md", "---\nname: orion-ontology-engineer\ndescription: Governed engineering with real receipts.\n---\n\nUse [the current stage](references/stage.md).\n");
  await save("harness/skills/orion-ontology-engineer/references/stage.md", "Only matching workflow contracts authorize a stage operation.\n");
  return { root, save, options: { projectRoot: root, runtimeRoot, outputDir: join(root, "output"), browserBuilder,
    bundleValidator: async () => ({ scope: "Packaging fixture only; not an official installation probe." }) } };
}

// Independent archive reader verifies the actual distributed bytes.
function unpack(archive) {
  const tar = gunzipSync(archive), files = new Map();
  let offset = 0;
  const string = buffer => buffer.toString("utf8").replace(/\0.*$/s, "");
  while (offset + 512 <= tar.length && tar[offset] !== 0) {
    const header = tar.subarray(offset, offset + 512);
    const name = string(header.subarray(0, 100)), prefix = string(header.subarray(345, 500));
    const size = parseInt(string(header.subarray(124, 136)).trim(), 8);
    assert.equal(String.fromCharCode(header[156]), "0", "archive only contains ordinary files");
    assert.equal(header.subarray(257, 262).toString(), "ustar");
    const checksum = parseInt(string(header.subarray(148, 156)).trim(), 8);
    const blank = Buffer.from(header); blank.fill(32, 148, 156);
    assert.equal(checksum, blank.reduce((sum, byte) => sum + byte, 0));
    files.set(`${prefix ? `${prefix}/` : ""}${name}`, tar.subarray(offset + 512, offset + 512 + size));
    offset += 512 + Math.ceil(size / 512) * 512;
  }
  return files;
}

test("private candidate archive carries owned assets, backend contracts and both official-derived modes", async t => {
  const { options } = await fixture(t);
  const result = await packageOrionPlugin(options);
  const files = unpack(await readFile(result.archive));
  const content = name => files.get(`package/${name}`)?.toString("utf8");
  assert.match(content("runtime.js"), /\.\/runtime\/index\.js/);
  assert.ok(files.has("package/runtime/lib/runtime.js"));
  const manifest = JSON.parse(content("workbench-assets.json"));
  assert.equal(manifest.mode, "core");
  assert.equal(manifest.backend.kind, "external-services");
  assert.equal(manifest.backend.python.version, "0.1.0");
  assert.match(manifest.backend.expectedBackendFingerprint.sha256, /^[a-f0-9]{64}$/);
  assert.deepEqual(manifest.modules.map(row => row.id), ["engineering", "documents", "registry", "templates", "graph", "qa", "analytics"]);
  assert.deepEqual(manifest.scripts, ["runtime-polling.js", "brand.js", "modules/engineering/workflow-client.js", "vendor/echarts.min.js", "modules/ontology-graph-viewer.js", "modules/ontology-center/file-preview.js"].map(path => `/orion-workbench-assets/${path}`));
  assert.equal(manifest.styles.at(-1), "/orion-workbench-assets/glass.css");
  for (const forbidden of ["boot-brand.js", "modules/app-shell.js", "modules/harness-adapter.js", "modules/ontology-center/ontology-chat.js"]) assert.equal(files.has(`package/assets/${forbidden}`), false);
  assert.match(content("cordis.patch.yml"), /id: preset-engineering/);
  assert.match(content("cordis.patch.yml"), /id: preset-ontology-qa/);
  assert.match(content("cordis.patch.yml"), /name: '@local\/orion-workbench\/skills'/);
  assert.match(content("cordis.patch.yml"), /first load the orion-ontology-qa skill/);
  assert.ok(files.has("package/skills/orion-ontology-engineer/references/stage.md"));
  assert.match(content("skills/orion-ontology-qa/SKILL.md"), /name: orion-ontology-qa/);
  assert.ok(JSON.parse(content("package.json")).files.includes("skills"));
  assert.ok(files.has("package/notices/skills-provenance.json"));
  assert.ok(files.has("package/notices/DeepSeek-standard-preset-LICENSE.txt"));
  assert.equal(files.has("package/node_modules/@deepseek-ai/dsh/package.json"), false);
  assert.equal(JSON.parse(content("package.json")).private, true);
  assert.equal(JSON.parse(content("package.json")).publishConfig.access, "restricted");
});

test("same input produces identical archive bytes and checksum", async t => {
  const { options } = await fixture(t);
  const first = await packageOrionPlugin(options);
  const bytes = await readFile(first.archive);
  const second = await packageOrionPlugin(options);
  assert.equal(first.sha256, second.sha256);
  assert.deepEqual(bytes, await readFile(second.archive));
});

test("standalone preparation can use an audited backend fingerprint without backend source or archive delivery", async t => {
  const { options, save } = await fixture(t);
  await save("services/workflow.py", "def result():\n    return 'matching external service'\n");
  const source = await packageOrionPlugin({ ...options, stageOnly: true });
  const snapshot = source.facts.backendSource;
  await rm(join(options.projectRoot, "services/workflow.py"));
  const standalone = await packageOrionPlugin({ ...options, stageOnly: true, backendSourceSnapshot: snapshot });
  assert.equal(standalone.facts.backendSource.sha256, snapshot.sha256);
  assert.equal(standalone.manifest.orion.externalBackend.expectedBackendFingerprint.sha256, snapshot.sha256);
  assert.equal(standalone.files.has("services/workflow.py"), false);
  await assert.rejects(readFile(join(options.outputDir, "local-orion-workbench-0.1.0-rc.1.tgz")), { code: "ENOENT" });
  const tampered = structuredClone(snapshot); tampered.files[0].sha256 = "a".repeat(64);
  await assert.rejects(packageOrionPlugin({ ...options, stageOnly: true, backendSourceSnapshot: tampered }), /fingerprint snapshot has changed/);
});

test("backend source drift changes its fingerprint even when package version stays fixed", async t => {
  const { options, save } = await fixture(t);
  await save("services/workflow.py", "def result():\n    return 'before'\n");
  const first = await packageOrionPlugin(options);
  await save("services/workflow.py", "def result():\n    return 'after'\n");
  const second = await packageOrionPlugin(options);
  assert.equal(first.receipt.backend.python.version, second.receipt.backend.python.version);
  assert.notEqual(first.receipt.backendSource.sha256, second.receipt.backendSource.sha256);
  assert.notEqual(first.sha256, second.sha256);
  const files = unpack(await readFile(second.archive));
  assert.equal(files.has("package/services/workflow.py"), false);
});

test("symlinked code cannot escape the package allowlist", async t => {
  const { root, options, save } = await fixture(t);
  await save("outside.js", "throw new Error('never include me');");
  await symlink(join(root, "outside.js"), join(root, "harness/plugins/orion-workbench/lib/escape.js"));
  await assert.rejects(packageOrionPlugin(options), /Symlink rejected/);
});

test("credential files and machine-local user paths fail before archive delivery", async t => {
  const { options, save } = await fixture(t);
  await save("harness/plugins/orion-workbench/.credentials.yaml", "refs: {}\n");
  await assert.rejects(packageOrionPlugin(options), /Secret file rejected/);
  await rm(join(options.projectRoot, "harness/plugins/orion-workbench/.credentials.yaml"));
  await save("harness/plugins/orion-workbench/README.md", 'backendRoot = "/Users/example/private-backend"');
  await assert.rejects(packageOrionPlugin(options), /Machine-specific user path rejected/);
});

test("public or mismatched-runtime package cannot masquerade as a private tested candidate", async t => {
  const { root, options } = await fixture(t);
  const path = join(root, "harness/plugins/orion-workbench/package.json");
  const manifest = JSON.parse(await readFile(path, "utf8"));
  await writeFile(path, JSON.stringify({ ...manifest, private: false }));
  await assert.rejects(packageOrionPlugin(options), /requires private:true/);
  await writeFile(path, JSON.stringify({ ...manifest, peerDependencies: { "@deepseek-ai/dsh": "0.1.7-rc.2" } }));
  await assert.rejects(packageOrionPlugin(options), /must match an exact declared DSH peer/);
});

test("asset namespace preserves API URLs and existing dependency order", () => {
  const source = 'url("/fonts/a.woff2"); const logo="/ahs-wordmark-white.svg"; const graph="/vendor/three.js"; fetch("/orion-workflow-api/projects"); fetch("/api/orion/query")';
  const mapped = namespaceAssetUrls(source);
  assert.match(mapped, /\/orion-workbench-assets\/fonts\/a\.woff2/);
  assert.match(mapped, /\/orion-workbench-assets\/ahs-wordmark-white\.svg/);
  assert.match(mapped, /\/orion-workbench-assets\/vendor\/three\.js/);
  assert.match(mapped, /fetch\("\/orion-workflow-api\/projects"\)/);
  assert.match(mapped, /fetch\("\/api\/orion\/query"\)/);
  assert.deepEqual(assetOrder('<script src="/a.js?v=x"></script><script src="/modules/app-shell.js?v=x"></script><script src="/b.js?v=x"></script>').scripts, ["a.js", "b.js"]);
});

test("unscoped dsh name is retained in the package and output filename", async t => {
  const { root, options } = await fixture(t);
  const path = join(root, "harness/plugins/orion-workbench/package.json");
  const manifest = JSON.parse(await readFile(path, "utf8"));
  await writeFile(path, JSON.stringify({ ...manifest, name: "dsh-orion-workbench" }));
  const patchPath = join(root, "harness/plugins/orion-workbench/cordis.patch.yml");
  await writeFile(patchPath, (await readFile(patchPath, "utf8")).replaceAll("@local/orion-workbench", "dsh-orion-workbench"));
  const result = await packageOrionPlugin(options);
  assert.equal(result.archive.split("/").at(-1), "dsh-orion-workbench-0.1.0-rc.1.tgz");
  const files = unpack(await readFile(result.archive));
  assert.equal(JSON.parse(files.get("package/package.json")).name, "dsh-orion-workbench");
});

test("QA skill preserves current answer policy and removes only the rendered persona identity", async t => {
  const { options, save } = await fixture(t);
  const policy = "The pinned release is read-only. Missing values remain unknown.\n\nAnswer only from verified source evidence.\n";
  await save("harness/profile/agent-presets/ontology-qa/persona.txt", `You are ORION, powered by {{model}}. Your directory is {{cwd}}.\n\n${policy}`);
  const result = await packageOrionPlugin(options);
  const files = unpack(await readFile(result.archive));
  const body = files.get("package/skills/orion-ontology-qa/SKILL.md").toString("utf8");
  assert.ok(body.includes(policy));
  assert.equal(body.includes("{{model}}"), false);
  assert.equal(body.includes("{{cwd}}"), false);
  const notice = JSON.parse(files.get("package/notices/skills-provenance.json"));
  assert.equal(notice.provider.includeDefaultRoots, false);
  assert.equal(notice.provider.watch, false);
  assert.equal(notice.provider.root, "skills");
  assert.match(notice.skills.find(row => row.name === "orion-ontology-qa").sourceSha256, /^[a-f0-9]{64}$/);
});

test("missing or escaping skill resources cannot produce a candidate", async t => {
  const { options, save } = await fixture(t);
  await save("harness/skills/orion-ontology-engineer/SKILL.md", "---\nname: orion-ontology-engineer\ndescription: Checked resources.\n---\n\n[Missing](references/missing.md)\n");
  await assert.rejects(packageOrionPlugin(options), /Missing or escaping skill resource/);
  await save("harness/skills/orion-ontology-engineer/SKILL.md", "---\nname: orion-ontology-engineer\ndescription: Checked resources.\n---\n\n[Escapes](../../secret.md)\n");
  await assert.rejects(packageOrionPlugin(options), /Missing or escaping skill resource/);
});

test("official registry discovers both packaged skills from empty Profile scopes and unregisters on disable", async t => {
  const runtimeModules = join(orionOfficialRuntimeRoot, "node_modules");
  try { await readFile(join(runtimeModules, "@deepseek-ai/dsh-skill-filesystem/package.json")); }
  catch (error) { if (error.code === "ENOENT") { t.skip("Pinned official runtime is not installed"); return; } throw error; }
  const { root, options, save } = await fixture(t);
  const result = await packageOrionPlugin(options);
  const installed = join(root, "fresh-profile/node_modules/@local/orion-workbench");
  for (const [path, body] of unpack(await readFile(result.archive))) {
    await save(`fresh-profile/node_modules/@local/orion-workbench/${path.slice("package/".length)}`, body);
  }
  // Test-only dependency link; neither provider roots nor package bytes contain it.
  await symlink(runtimeModules, join(installed, "node_modules"));
  const load = path => import(pathToFileURL(join(runtimeModules, path)).href);
  const [{ Context }, { SkillRegistry }, { createScope }, filesystem, adapter] = await Promise.all([
    load("@deepseek-ai/cordis/lib/index.js"), load("@deepseek-ai/dsh-skill/lib/index.js"), load("@deepseek-ai/dsh-scope/lib/index.js"),
    load("@deepseek-ai/dsh-skill-filesystem/lib/index.js"), import(pathToFileURL(join(installed, "lib/skills.js")).href),
  ]);
  const ctx = new Context();
  const registry = ctx.plugin(SkillRegistry);
  await registry.await();
  const mounted = ctx.plugin(adapter);
  await mounted.await();
  const scopes = [];
  t.after(async () => { await mounted.dispose(); for (const scope of scopes) await scope.dispose(); await registry.dispose(); });
  const cwd = join(root, "empty-workspace");
  await mkdir(cwd);
  for (const id of ["engineering", "ontology-qa"]) {
    const key = { id }, scope = createScope(ctx, key);
    scopes.push(scope);
    // Standing standard presets have their own default provider. Keep its name
    // in each scope, without scanning the real developer's home in this test.
    await scope.ctx.plugin(filesystem, { providerName: "filesystem", includeDefaultRoots: false, watch: false }).await();
    assert.deepEqual((await ctx.skills.list({ cwd, scope: key })).map(row => row.name), ["orion-ontology-engineer", "orion-ontology-qa"]);
    const skill = await ctx.skills.get(id === "engineering" ? "orion-ontology-engineer" : "orion-ontology-qa", { cwd, scope: key });
    assert.equal(skill.provider, "orion-workbench-bundled");
    assert.equal(skill.source, "bundled");
    assert.equal(skill.resourceBase.kind, "directory");
    assert.ok(skill.resourceBase.path.startsWith(await realpath(installed)));
    if (id === "engineering") assert.match(await readFile(join(skill.resourceBase.path, "references/stage.md"), "utf8"), /matching workflow contracts/);
    else assert.match(skill.content, /ontology-qa governed operations/);
  }
  await mounted.dispose();
  assert.deepEqual(await ctx.skills.list({ cwd }), []);
});

test("actual official overlay parser rejects an unquoted negated expression before delivering an archive", async t => {
  const runtimeRoot = orionOfficialRuntimeRoot;
  try { await readFile(join(runtimeRoot, "node_modules/@deepseek-ai/dsh-app-boot/lib/index.js")); }
  catch (error) { if (error.code === "ENOENT") { t.skip("Pinned official app-boot parser is not installed"); return; } throw error; }
  const { options, save } = await fixture(t);
  options.bundleValidator = input => validateOfficialBundle({ ...input, runtimeRoot });
  const valid = await packageOrionPlugin(options);
  assert.equal(valid.receipt.officialBundlePreflight.installationPerformed, false);
  const digest = valid.sha256;
  await save("harness/plugins/orion-workbench/cordis.patch.yml", "- insert:\n    - id: orion-workbench-skills\n      name: '@local/orion-workbench/skills'\n    - id: orion-workbench\n      name: '@local/orion-workbench'\n      disabled: !!js !process.env.ORION_BACKEND_ROOT\n");
  await assert.rejects(packageOrionPlugin(options), /duplication of a tag property|failed to parse overlay/);
  assert.equal((await readFile(`${valid.archive}.sha256`, "utf8")).split(" ")[0], digest);
});

test("real workbench bundle uses official composition and package admission without running its expressions", async t => {
  const runtimeRoot = orionOfficialRuntimeRoot;
  try { await readFile(join(runtimeRoot, "node_modules/@deepseek-ai/dsh-app-boot/lib/index.js")); }
  catch (error) { if (error.code === "ENOENT") { t.skip("Pinned official app-boot parser is not installed"); return; } throw error; }
  const source = join(projectRoot, "harness/plugins/orion-workbench");
  const manifest = JSON.parse(await readFile(join(source, "package.json"), "utf8"));
  const files = new Map([["cordis.patch.yml", await readFile(join(source, "cordis.patch.yml"))],
    ["index.js", Buffer.from("throw new Error('Candidate must not be imported by preflight');")],
    ["lib/skills.js", Buffer.from("throw new Error('Candidate skills must not be imported by preflight');")],
    ["lib/runtime-manager.js", Buffer.from("throw new Error('Candidate runtime manager must not be imported by preflight');")]]);
  const result = await validateOfficialBundle({ files, manifest, runtimeRoot });
  assert.equal(result.version, "0.2.0-rc.2");
  assert.deepEqual(result.composedEntryIds, ["orion-workbench-skills", "orion-runtime-manager", "orion-workbench", "mcp-orion-workflow", "mcp-orion-realtime"]);
  assert.deepEqual(result.componentAdmission.map(row => row.name), [manifest.name, "@deepseek-ai/dsh-mcp-client"]);
  assert.equal(result.expressionEvaluation, false); assert.equal(result.candidateCodeImported, false);
  const badExports = { ...manifest, exports: { ...manifest.exports, "./skills": "./missing.js" } };
  await assert.rejects(validateOfficialBundle({ files, manifest: badExports, runtimeRoot }), /unpublished package entry/);
});

test("official bundle component admission rejects incompatible or unavailable runtime plugins without importing them", async t => {
  const officialRoot = join(orionOfficialRuntimeRoot, "node_modules/@deepseek-ai/dsh-app-boot");
  try { await readFile(join(officialRoot, "lib/index.js")); }
  catch (error) { if (error.code === "ENOENT") { t.skip("Pinned official app-boot parser is not installed"); return; } throw error; }
  const root = await mkdtemp(join(tmpdir(), "orion-component-admission-test-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  const runtimeRoot = join(root, "runtime"), scoped = join(runtimeRoot, "node_modules/@deepseek-ai");
  await mkdir(join(scoped, "dsh"), { recursive: true });
  await writeFile(join(scoped, "dsh/package.json"), '{"name":"@deepseek-ai/dsh","version":"0.2.0-rc.2"}');
  // Use the actual pinned implementation; only component metadata is a fixture.
  await symlink(await realpath(officialRoot), join(scoped, "dsh-app-boot"), "dir");
  const componentRoot = join(runtimeRoot, "node_modules/admission-fixture");
  await mkdir(componentRoot, { recursive: true });
  await writeFile(join(componentRoot, "index.js"), "throw new Error('Preflight must never import component code');");
  const component = { name: "admission-fixture", version: "0.1.0", main: "index.js", peerDependencies: { "@deepseek-ai/dsh-client-ui-slots": "0.1.7-rc.2" } };
  await writeFile(join(componentRoot, "package.json"), JSON.stringify(component));
  const manifest = { name: "dsh-preflight-fixture", version: "0.1.0", peerDependencies: { "@deepseek-ai/dsh": "0.2.0-rc.2" }, exports: { ".": "./index.js" } };
  const files = new Map([["index.js", Buffer.from("throw new Error('Preflight must never import candidate code');")],
    ["cordis.patch.yml", Buffer.from("- insert:\n    - id: fixture\n      name: dsh-preflight-fixture\n    - id: component\n      name: admission-fixture\n")]]);
  await assert.rejects(validateOfficialBundle({ files, manifest, runtimeRoot }), /bundle component compatibility rejected/);
  component.peerDependencies["@deepseek-ai/dsh-client-ui-slots"] = "0.2.0-rc.2";
  await writeFile(join(componentRoot, "package.json"), JSON.stringify(component));
  const result = await validateOfficialBundle({ files, manifest, runtimeRoot });
  assert.deepEqual(result.componentAdmission, [
    { name: manifest.name, version: manifest.version, source: "candidate", compatible: true },
    { name: component.name, version: component.version, source: "runtime", compatible: true },
  ]);
  await rm(componentRoot, { recursive: true, force: true });
  await assert.rejects(validateOfficialBundle({ files, manifest, runtimeRoot }), /cannot resolve profile bundle/);
});

test("Aqua local archive retains original identity, MIT notice and private protection", async t => {
  const { root, save } = await fixture(t);
  const source = "harness/plugins/dsh-client-ui-aqua";
  await save(`${source}/package.json`, JSON.stringify({ name: "dsh-client-ui-aqua", version: "1.3.1-orion-alpha.3", private: true, license: "MIT", publishConfig: { access: "public" }, scripts: { install: "unexpected" }, devDependencies: { unused: "workspace:*" }, exports: { ".": "./lib/index.js", "./src/*": "./src/*" }, dsh: { bundle: { patch: "./cordis.patch.yml" } } }));
  await save(`${source}/cordis.patch.yml`, "- insert: []\n");
  await save(`${source}/lib/index.js`, "export function apply() {}\n");
  await save(`${source}/lib/client.js`, "window.__ModuleLoader__.load({id:'dsh-client-ui-aqua',factory(){return {}}});\n");
  await save(`${source}/lib/types/index.d.ts`, "export declare function apply(): void;\n");
  await save(`${source}/LICENSE`, "MIT License\nCopyright (c) Original Author\n");
  await save(`${source}/README.md`, "Original theme candidate\n");
  const result = await packageAquaPlugin({ projectRoot: root, outputDir: join(root, "aqua-output") });
  const files = unpack(await readFile(result.archive));
  const manifest = JSON.parse(files.get("package/package.json"));
  assert.equal(manifest.name, "dsh-client-ui-aqua");
  assert.equal(manifest.private, true);
  assert.equal(manifest.publishConfig.access, "restricted");
  assert.equal(manifest.scripts, undefined);
  assert.equal(manifest.devDependencies, undefined);
  assert.equal(manifest.exports["./src/*"], undefined);
  assert.match(files.get("package/LICENSE").toString(), /Original Author/);
  assert.ok(files.has("package/lib/types/index.d.ts"));
});
