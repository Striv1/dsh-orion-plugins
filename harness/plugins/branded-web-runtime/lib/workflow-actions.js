import { readFile } from "node:fs/promises";
import { resolve } from "node:path";

const contractPromises = new Map();

function validateContract(payload) {
  if (payload?.schemaVersion !== 1 || !payload.actions || typeof payload.actions !== "object") {
    throw new Error("工作流动作契约格式无效。");
  }
  const allowedTools = new Set();
  const workflowActions = new Map();
  for (const [name, config] of Object.entries(payload.actions)) {
    if (!name || typeof config?.endpoint !== "string") {
      throw new Error("工作流动作契约包含无效动作。");
    }
    allowedTools.add(name);
    if (config.endpoint !== "workflow-action") continue;
    if (config.actorField !== null && (typeof config.actorField !== "string" || !config.actorField)) {
      throw new Error(`工作流动作 ${name} 缺少 actorField。`);
    }
    workflowActions.set(name, Object.freeze({ actorField: config.actorField }));
  }
  return Object.freeze({ allowedTools, workflowActions });
}

async function loadWorkflowActionContract(contractPath) {
  const resolvedPath = resolve(contractPath);
  if (!contractPromises.has(resolvedPath)) {
    contractPromises.set(
      resolvedPath,
      readFile(resolvedPath, "utf8")
        .then((source) => validateContract(JSON.parse(source)))
        .catch((error) => {
          contractPromises.delete(resolvedPath);
          throw error;
        }),
    );
  }
  return contractPromises.get(resolvedPath);
}

export { loadWorkflowActionContract };
