export const REQUIRED_QA_TOOLS = [
  'describe_ontology_capability', 'describe_ontology_query_space',
  'answer_realtime_ontology_question', 'execute_checked_ontology_query',
  'read_ontology_query_results',
  'describe_ontology_analysis', 'analyze_ontology_results',
  'describe_ontology_analytics', 'query_ontology_analytics', 'manage_ontology_analysis_assets', 'manage_ontology_analysis_exports',
].map((name) => `mcp__orion_realtime__${name}`);

// Dynamic registration and agent visibility; remote runtime health is separate.
export function qaToolReadiness(context, sessionId) {
  const agent = context.agents.list().find((item) => item.id === sessionId);
  const registered = new Set(context.tools.schemas().map((item) => item.name));
  const visible = new Set(agent ? context.tools.schemas(agent).map((item) => item.name) : []);
  const missing = REQUIRED_QA_TOOLS.filter((name) => !registered.has(name));
  const hidden = REQUIRED_QA_TOOLS.filter((name) => registered.has(name) && !visible.has(name));
  const ready = Boolean(agent) && !missing.length && !hidden.length;
  return { agent_tools_ready: ready, agent_tools_registered: !missing.length,
    missing_agent_tools: missing, unavailable_agent_tools: hidden,
    agent_tools_status: ready ? 'READY' : !agent ? 'AGENT_NOT_ATTACHED'
      : missing.length ? 'AGENT_TOOLS_NOT_READY' : 'AGENT_TOOLS_NOT_VISIBLE',
    retryable: !ready };
}
