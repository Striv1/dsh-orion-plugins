// Host-measured context usage for long engineering turns. Without a real
// figure the model guessed "context exhausted" at ~45-60% of a 1M window and
// ended S0-S7 turns early. Keep the host-measured threshold in the prompt, but
// do not inject changing exact counts into the cached system prefix each step.
import { harnessSessionEvents } from "./session-logs.js";

export const CONTEXT_HANDOFF_THRESHOLD = 0.85;
// Goal round caps below this floor block long S0-S7 goals mid-stage (a model
// chose 12 and the goal blocked while S6 was still running).
export const MIN_ENGINEERING_GOAL_ROUNDS = 64;

const count = (value) => (Number.isFinite(value) && value >= 0 ? value : 0);

export function measureContextUsage(events) {
  let window = null;
  let used = null;
  for (let i = events.length - 1; i >= 0 && (window === null || used === null); i -= 1) {
    const event = events[i];
    if (used === null && event?.type === "assistant/message" && event.data?.usage) {
      const u = event.data.usage;
      const total = count(u.inputTokens) + count(u.cacheReadTokens) + count(u.cacheWriteTokens);
      if (total > 0) used = total;
    }
    if (window === null && event?.type === "request/context" && Number.isFinite(event.data?.contextWindow)
      && event.data.contextWindow > 0) window = event.data.contextWindow;
  }
  if (window === null || used === null) return null;
  return { used, window, ratio: Math.min(1, used / window) };
}

export function contextUsagePrompt(events) {
  const usage = measureContextUsage(events);
  if (!usage) return "";
  const near = usage.ratio >= CONTEXT_HANDOFF_THRESHOLD;
  const threshold = Math.round(CONTEXT_HANDOFF_THRESHOLD * 100);
  return `<orion_context_usage band="${near ? "HIGH" : "BELOW_THRESHOLD"}" threshold_percent="${threshold}">`
    + `Host-measured context usage of the last request is ${near ? "at or above" : "below"} the ${threshold}% threshold. `
    + (near
      ? "Usage is high: keep a one-line note of stage, revision and next formal action; the host compacts history automatically, so continue authorized work."
      : "Do not claim the context is exhausted or end the turn for context reasons; continue the authorized work.")
    + "</orion_context_usage>";
}

export function goalRoundCapRejection(execution) {
  if (!["create_goal", "update_goal"].includes(execution?.name)) return undefined;
  const cap = execution.arguments?.max_goal_rounds;
  if (cap === undefined || cap === null || cap === 0) return undefined;
  if (Number.isFinite(cap) && cap >= MIN_ENGINEERING_GOAL_ROUNDS) return undefined;
  return `G-GOAL-ROUND-CAP：工程目标的 max_goal_rounds=${cap} 过低，S0–S7 长任务会在阶段中途被自动置为受阻。请省略 max_goal_rounds（使用宿主默认值 256），或设置不小于 ${MIN_ENGINEERING_GOAL_ROUNDS}。`;
}

export function installContextUsagePrompt(agent, isQaAgent, isEngineeringSession) {
  return agent.ctx.inject(["systemPrompt"], (scope) => scope.systemPrompt.section({
    name: "orion:context-usage", order: 166,
    text: () => (isQaAgent(agent) || !isEngineeringSession(agent.session)
      ? "" : contextUsagePrompt(harnessSessionEvents(agent.session))),
  }));
}
