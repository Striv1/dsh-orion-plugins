import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";

import {
  authorizeOrionApiRequest,
  isOrionApiPath,
} from "../../harness/plugins/branded-web-runtime/index.js";

const startServer = async (t) => {
  const server = createServer((request, response) => {
    const pathname = new URL(request.url ?? "/", `http://${request.headers.host}`).pathname;
    const authorizeIndex = (incomingRequest, outgoingResponse) => {
      if (incomingRequest.headers.cookie === "dsh-session=valid") return true;
      outgoingResponse.writeHead(401, { "content-type": "text/plain; charset=utf-8" });
      outgoingResponse.end("dsh web authentication required");
      return false;
    };
    if (
      isOrionApiPath(pathname) &&
      !authorizeOrionApiRequest(request, response, authorizeIndex)
    ) {
      return;
    }
    response.writeHead(200, { "content-type": "application/json; charset=utf-8" });
    response.end(JSON.stringify({ ok: true }));
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  }));
  const address = server.address();
  assert.ok(address && typeof address !== "string");
  return `http://127.0.0.1:${address.port}`;
};

test("ORION API path matcher covers every current gateway without capturing legacy API", () => {
  for (const pathname of [
    "/orion-mcp-api/status",
    "/orion-document-api/jobs",
    "/orion-workflow-api/status",
    "/orion-ontology-qa-api/session",
    "/orion-engineering-session-api",
  ]) {
    assert.equal(isOrionApiPath(pathname), true, pathname);
  }
  assert.equal(isOrionApiPath("/ontology-api/health"), false);
  assert.equal(isOrionApiPath("/orion-workflow-api-malicious"), false);
});

test("ORION API requires the existing DSH browser session over real HTTP", async (t) => {
  const baseUrl = await startServer(t);

  const anonymous = await fetch(`${baseUrl}/orion-workflow-api/status`);
  assert.equal(anonymous.status, 401);
  assert.match(await anonymous.text(), /authentication required/);

  const authenticated = await fetch(`${baseUrl}/orion-document-api/jobs`, {
    headers: { cookie: "dsh-session=valid" },
  });
  assert.equal(authenticated.status, 200);
  assert.deepEqual(await authenticated.json(), { ok: true });
});

test("ORION API fails closed when the DSH authorizer is unavailable", async (t) => {
  const server = createServer((request, response) => {
    authorizeOrionApiRequest(request, response, undefined);
  });
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  t.after(() => new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  }));
  const address = server.address();
  assert.ok(address && typeof address !== "string");

  const response = await fetch(`http://127.0.0.1:${address.port}/orion-mcp-api/status`);
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { detail: "DSH 浏览器会话认证当前不可用" });
});

test("ORION API rejects cross-site writes but preserves same-origin UI writes", async (t) => {
  const baseUrl = await startServer(t);
  const headers = { cookie: "dsh-session=valid", "content-type": "application/json" };

  const crossSite = await fetch(`${baseUrl}/orion-workflow-api/action`, {
    method: "POST",
    headers: { ...headers, origin: "https://attacker.invalid", "sec-fetch-site": "cross-site" },
    body: "{}",
  });
  assert.equal(crossSite.status, 403);
  assert.deepEqual(await crossSite.json(), { detail: "拒绝跨站本体工作台写请求" });

  const sameOrigin = await fetch(`${baseUrl}/orion-workflow-api/action`, {
    method: "POST",
    headers: { ...headers, origin: baseUrl, "sec-fetch-site": "same-origin" },
    body: "{}",
  });
  assert.equal(sameOrigin.status, 200);
  assert.deepEqual(await sameOrigin.json(), { ok: true });
});
