import { computeQaToolVisibilityDeny } from "./agent-mode-context.js";

export const MODE_DISCOVERY_TOOL = "orion_discover_tools";

function discoveryRequest(args) {
  if (args.query !== undefined && (typeof args.query !== "string" || args.query.length > 200)) throw new Error("query 必须是不超过 200 字符的文本。");
  if (args.names !== undefined && !Array.isArray(args.names)) throw new Error("names 必须是工具名数组。");
  const names = args.names ?? [];
  if (names.length > 12 || names.some((name) => typeof name !== "string" || name.length > 128)) throw new Error("每次最多发现 12 个真实工具，每个名称不超过 128 字符。");
  const query = (args.query ?? "").trim().toLowerCase();
  const limit = args.limit ?? Math.max(6, names.length);
  if (!Number.isInteger(limit) || limit < 1 || limit > 12) throw new Error("limit 必须是 1 到 12 的整数。");
  return { query, names, limit };
}

function selectDiscoveryTools(candidates, { query, names, limit }) {
  const tokens = query.split(/\s+/u).filter(Boolean);
  return candidates.map((tool) => ({ tool, score: names.includes(tool.name) ? 1000
    : tokens.reduce((sum, token) => sum + (tool.name.toLowerCase().includes(token) ? 3
      : String(tool.description ?? "").toLowerCase().includes(token) ? 1 : 0), 0) }))
    .filter((entry) => entry.score > 0)
    .sort((a, b) => b.score - a.score || a.tool.name.localeCompare(b.tool.name))
    .slice(0, limit).map((entry) => entry.tool);
}

// This changes only this agent's presentation mask. Existing execution guards,
// other scoped restrictions, and the administrator's policy remain authoritative.
export function installQaToolDiscovery(context, agent, getBinding, rejectExecution) {
  let restriction = null;
  let registration = null;
  let signature = "";
  let identity = "";
  let refreshing = false;
  let disposed = false;
  const discovered = new Set();
  const schemas = () => context.tools.schemas().filter((tool) => typeof tool?.name === "string");

  const refresh = () => {
    if (disposed || refreshing) return;
    refreshing = true;
    try {
      const binding = getBinding();
      const nextIdentity = binding ? `${binding.session_id}:${binding.project_id}:${binding.release_fingerprint}` : "";
      if (identity !== nextIdentity) {
        discovered.clear();
        identity = nextIdentity;
      }
      if (!binding) {
        restriction?.();
        restriction = null;
        registration?.();
        registration = null;
        signature = "";
        return;
      }
      if (!registration) registration = agent.ctx.tools.register({
        name: MODE_DISCOVERY_TOOL,
        description: "按需查找当前问答可用的专业工具。先用 query 查询名称或用途；names 可精确加载最多 12 项。只改变本会话的工具可见性，不执行工具、不解除原权限或发布只读约束。空查询返回专业工具类别概览。业务问数和推理优先用 orion_realtime 的版本绑定能力。",
        parameters: {
          type: "object", additionalProperties: false,
          properties: {
            query: { type: "string", description: "名称或用途，最多 200 字符。" },
            names: { type: "array", description: "最多 12 个工具名，每项最多 128 字符。", items: { type: "string" } },
            limit: { type: "integer", description: "返回数量，1 到 12，默认 6。" },
          },
        },
        output: {
          schema: { type: "object", additionalProperties: true },
          render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }],
        },
        async execute(args) {
          if (!getBinding()) throw new Error("当前会话尚未绑定正式版本。");
          const { query, names, limit } = discoveryRequest(args);
          const globals = schemas();
          const candidates = globals.filter((tool) => tool.name.startsWith("mcp__")
            && !rejectExecution(getBinding(), { name: tool.name, arguments: {} })
            && !computeQaToolVisibilityDeny([tool.name], [tool.name]).includes(tool.name));
          if (!query && !names.length) {
            const namespaces = {};
            for (const tool of candidates) {
              const namespace = tool.name.split("__")[1];
              namespaces[namespace] = (namespaces[namespace] ?? 0) + 1;
            }
            return { namespaces, guidance: "按名称或用途查找所需工具；发现不会执行任何业务动作。" };
          }
          const selected = selectDiscoveryTools(candidates, { query, names, limit });
          for (const tool of selected) discovered.add(tool.name);
          refresh();
          return {
            tools: selected,
            unavailable_names: names.filter((name) => !selected.some((tool) => tool.name === name)),
            execution_policy: "已加载工具仍受原执行守卫与其他会话级限制约束。",
          };
        },
      });
      const deny = computeQaToolVisibilityDeny(schemas().map((tool) => tool.name), [...discovered]).sort();
      const nextSignature = JSON.stringify([identity, deny]);
      if (signature !== nextSignature) {
        restriction?.();
        restriction = null;
        signature = "";
        restriction = deny.length ? agent.ctx.tools.restrict({ deny }) : null;
        signature = nextSignature;
      }
    } catch (error) {
      context.logger?.warn?.(`本体问答工具目录刷新失败：${String(error)}`);
    } finally {
      refreshing = false;
    }
  };
  const removeListener = context.on("tools/change", refresh);
  refresh();
  return {
    refresh,
    dispose() {
      disposed = true;
      removeListener?.();
      restriction?.();
      registration?.();
    },
  };
}

export const ENGINEERING_DISCOVERY_MAX_TOOLS = 24;
const isEngineeringDeferredTool = (name) => /^mcp__(?:protege|semantica)__/u.test(name);
const engineeringDiscoveryIdentity = binding => binding ? JSON.stringify([
  binding.session_id, binding.project_id, binding.revision ?? null, binding.current_stage,
]) : "";

// Keep QA's binding/policy unchanged. Engineering only masks these two global
// MCP namespaces; native tools and the existing Workflow/preflight surface stay
// on their normal execution path, including all other restrictions and guards.
export function installEngineeringToolDiscovery(context, agent, getBinding, rejectExecution) {
  let restriction = null;
  let registration = null;
  let signature = "";
  let identity = "";
  let refreshing = false;
  let disposed = false;
  const discovered = new Set();
  const schemas = () => context.tools.schemas().filter(tool => isEngineeringDeferredTool(tool.name));
  const admitted = (binding, tool) => binding.ready !== false
    && !rejectExecution(binding, { name: tool.name, arguments: {} });

  const applyMask = (tools) => {
    const deny = tools.map(tool => tool.name).filter(name => !discovered.has(name)).sort();
    const nextSignature = JSON.stringify([identity, deny]);
    if (signature === nextSignature) return;
    // Install the replacement before lifting our previous mask. Never dispose
    // another owner's restriction, register a shadow, or bypass tool execution.
    const next = deny.length ? agent.ctx.tools.restrict({ deny }) : null;
    const previous = restriction;
    restriction = next;
    signature = nextSignature;
    previous?.();
  };

  const definition = {
    name: MODE_DISCOVERY_TOOL,
    description: "按需加载当前工程阶段允许的 Protégé、Semantica 工具。空查询返回类别和已加载数量；query 按名称或用途搜索，names 精确加载。每次最多 12 项，累计最多 24 项；reset=true 清空本发现集合后可重新选择。发现不执行工具、不授予权限；原生工具与 Workflow 阶段预检、受管执行仍直接调用。",
    parameters: {
      type: "object", additionalProperties: false,
      properties: {
        query: { type: "string", description: "名称或用途，最多 200 字符。" },
        names: { type: "array", description: "最多 12 个工具名，每项最多 128 字符。", items: { type: "string" } },
        limit: { type: "integer", description: "返回数量，1 到 12，默认 6。" },
        reset: { type: "boolean", description: "清空已发现集合，再按本次 query/names 选择；不改变阶段或权限。" },
      },
    },
    output: {
      schema: { type: "object", additionalProperties: true },
      render: (_args, value) => [{ type: "text", text: JSON.stringify(value) }],
    },
    async execute(args) {
      const request = discoveryRequest(args);
      if (args.reset !== undefined && typeof args.reset !== "boolean") throw new Error("reset 必须是布尔值。");
      if (!refresh() || !getBinding()) throw new Error("当前工程阶段的工具发现不可用，请先回读正式状态。");
      const binding = getBinding();
      if (binding.ready === false) throw new Error("请先回读工程正式状态，再发现专业工具。");
      if (args.reset) { discovered.clear(); if (!refresh()) throw new Error("工具集合清理失败，请重试。"); }
      const candidates = schemas().filter(tool => admitted(binding, tool));
      const summary = () => ({ loaded_count: discovered.size, max_loaded: ENGINEERING_DISCOVERY_MAX_TOOLS });
      if (!request.query && !request.names.length) {
        const namespaces = {};
        for (const tool of candidates) {
          const namespace = tool.name.split("__")[1];
          namespaces[namespace] = (namespaces[namespace] ?? 0) + 1;
        }
        return { namespaces, ...summary(), guidance: "按需选择专业工具；其他限制可能使部分工具不可用。reset=true 可清空已发现集合。" };
      }
      const selected = selectDiscoveryTools(candidates, request);
      const additions = selected.filter(tool => !discovered.has(tool.name));
      if (discovered.size + additions.length > ENGINEERING_DISCOVERY_MAX_TOOLS) {
        throw new Error("专业工具累计最多加载 24 项；请用 reset=true 清空集合并选择当前所需工具。");
      }
      for (const tool of selected) discovered.add(tool.name);
      if (!refresh()) {
        for (const tool of additions) discovered.delete(tool.name);
        refresh();
        throw new Error("工程工具可见性刷新失败，请重试。");
      }
      // Read the effective agent view after changing only our own mask. An
      // administrator/stage restriction must not be reported as a loaded tool.
      const visible = new Map(context.tools.schemas(agent).map(tool => [tool.name, tool]));
      const tools = selected.filter(tool => discovered.has(tool.name) && visible.has(tool.name))
        .map(tool => visible.get(tool.name));
      return { tools, unavailable_names: request.names.filter(name => !tools.some(tool => tool.name === name)),
        ...summary(), execution_policy: "仅加载工具定义；实际调用仍经过原阶段、项目、管理员守卫及预检/审批。" };
    },
  };

  function refresh() {
    if (disposed || refreshing) return false;
    refreshing = true;
    try {
      const binding = getBinding();
      const nextIdentity = engineeringDiscoveryIdentity(binding);
      if (identity !== nextIdentity || binding?.ready === false) discovered.clear();
      identity = nextIdentity;
      if (!binding) {
        // Release the shared name first: tools/change can synchronously install
        // the unchanged QA discovery policy when a release binding takes over.
        registration?.(); registration = null;
        restriction?.(); restriction = null;
        signature = "";
        return true;
      }
      const tools = schemas();
      const candidates = new Set(tools.filter(tool => admitted(binding, tool)).map(tool => tool.name));
      for (const name of discovered) if (!candidates.has(name)) discovered.delete(name);
      applyMask(tools);
      const visible = new Set(context.tools.schemas(agent).map(tool => tool.name));
      for (const name of discovered) if (!visible.has(name)) discovered.delete(name);
      applyMask(tools);
      registration ??= agent.ctx.tools.register(definition);
      return true;
    } catch (error) {
      context.logger?.warn?.(`工程工具目录刷新失败：${String(error)}`);
      return false;
    } finally {
      refreshing = false;
    }
  }
  // A transient mask replacement failure must not leave discoveries from an
  // old generation executable. This adds a denial only; all existing guards
  // and pre-execute/approval dependencies still run through the native pipeline.
  const removeGuard = agent.ctx.tools.guard(execution => {
    const binding = getBinding();
    if (binding && isEngineeringDeferredTool(execution.name)
      && (binding.ready === false || identity !== engineeringDiscoveryIdentity(binding) || !discovered.has(execution.name))) {
      return "当前工程专业工具尚未发现或绑定已变化，请回读正式状态后使用 orion_discover_tools 按需加载。";
    }
    return undefined;
  });
  const removeListener = context.on("tools/change", refresh);
  refresh();
  return { refresh, dispose() {
    disposed = true;
    removeListener?.();
    removeGuard?.();
    registration?.();
    restriction?.();
    discovered.clear();
  } };
}
