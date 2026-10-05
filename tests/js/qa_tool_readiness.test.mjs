import test from 'node:test';
import assert from 'node:assert/strict';
import { REQUIRED_QA_TOOLS, qaToolReadiness } from '../../harness/plugins/branded-web-runtime/lib/qa-tool-readiness.js';
const agent = { id: 'qa-session' };
function fixture() {
  const state = { names: [], hidden: [], agents: [agent] };
  const context = { agents: { list: () => state.agents }, tools: { schemas: (scope) => state.names.filter((name) => !scope || !state.hidden.includes(name)).map((name) => ({name})) } };
  return { state, read: () => qaToolReadiness(context, agent.id) };
}
test('missing MCP never implies agent readiness; late registration recovers dynamically', () => {
  const f = fixture(); assert.equal(f.read().agent_tools_ready, false);
  assert.equal(f.read().missing_agent_tools.length, REQUIRED_QA_TOOLS.length);
  f.state.names = [...REQUIRED_QA_TOOLS]; assert.equal(f.read().agent_tools_ready, true);
  f.state.names.pop(); assert.equal(f.read().agent_tools_ready, false);
});
test('global registration cannot hide per-agent restrictions or unattached session', () => {
  const f = fixture(); f.state.names = [...REQUIRED_QA_TOOLS]; f.state.hidden = [REQUIRED_QA_TOOLS[0]];
  assert.equal(f.read().agent_tools_registered, true); assert.equal(f.read().agent_tools_status, 'AGENT_TOOLS_NOT_VISIBLE');
  f.state.agents = []; assert.equal(f.read().agent_tools_status, 'AGENT_NOT_ATTACHED');
});
test('QA prompt forbids label identity and unsupported unsolicited aggregation', async () => {
  const { ontologyQaPrompt } = await import('../../harness/plugins/branded-web-runtime/index.js');
  const text = ontologyQaPrompt({ session_id: 'session-x', project_id: 'ontology-project-x', release_version: '1', realtime_ready: true });
  assert.match(text, /display names and labels are not identity keys/);
  assert.match(text, /Do not add aggregate statistics the user did not request/);
});
test('API blocks missing tools for GET and POST, then recovers same binding without rewriting it', async () => {
  const { serveOntologyQaApi } = await import('../../harness/plugins/branded-web-runtime/index.js');
  const { Readable } = await import('node:stream');
  const sid = 'session-11111111-1111-4111-8111-111111111111';
  const binding = { session_id: sid, project_id: 'ontology-project-1234abcd', realtime_ready: true, release_version: 'v1' };
  let ready = false;
  const state = { ready: Promise.resolve(), bindings: new Map([[sid, binding]]),
    agentToolReadiness: () => ({ agent_tools_ready: ready, retryable: !ready }) };
  async function call(method) {
    const req = Readable.from(method === 'POST' ? [Buffer.from(JSON.stringify({ session_id: sid, project_id: binding.project_id }))] : []);
    Object.assign(req, { method, url: method === 'GET' ? `/orion-ontology-qa-api/session?session_id=${sid}` : '/orion-ontology-qa-api/bind', headers: { 'x-orion-ontology-qa-binding': '1' } });
    let body; const res = { writeHead(status) { this.status = status; }, end(value) { body = JSON.parse(value); } };
    await serveOntologyQaApi(req, res, {}, state); return { status: res.status, body };
  }
  for (const method of ['GET', 'POST']) { const r = await call(method); assert.equal(r.status, 503); assert.equal(r.body.qa_ready, false); }
  ready = true;
  for (const method of ['GET', 'POST']) { const r = await call(method); assert.equal(r.status, 200); assert.equal(r.body.qa_ready, true); assert.equal(r.body.release_version, 'v1'); assert.equal(r.body.verification_scope, 'BOUND_RELEASE_VERIFIED_AT_BINDING'); assert.equal(r.body.runtime_live_probe, false); }
  assert.equal(state.bindings.get(sid), binding); assert.equal(binding.agent_tools_ready, undefined);
});
