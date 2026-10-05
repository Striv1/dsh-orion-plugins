import { execFile } from "node:child_process";
import { resolve } from "node:path";

/**
 * Transport to the existing Python action boundary. This module owns neither
 * HTTP authorization nor the action allowlist: callers retain their confirmation
 * checks and harness.orion_workflow_action remains the command dispatcher.
 * The environment is supplied by the host so this module has no Harness context
 * or document-scheduler dependency.
 */
export async function runWorkflowAction({
  python,
  projectRoot,
  workflowHome,
  documentInputRoot,
  environment = {},
  timeoutMs = 30000,
}, tool, argumentsPayload, { uiCreateConfirmed = false } = {}) {
  const stdout = await new Promise((resolvePromise, rejectPromise) => {
    execFile(python, [
      "-m", "harness.orion_workflow_action",
      "--payload-json", JSON.stringify({ tool, arguments: argumentsPayload }),
    ], {
      encoding: "utf8",
      maxBuffer: 1024 * 1024,
      timeout: timeoutMs,
      cwd: resolve(projectRoot),
      env: {
        ...environment,
        ORION_WORKFLOW_HOME: resolve(workflowHome),
        ORION_DOCUMENT_INPUT_ROOT: resolve(documentInputRoot),
        ORION_DOCUMENT_INGESTION_ROOT: resolve(documentInputRoot),
        ...(uiCreateConfirmed === true ? { ORION_REQUIRE_UI_CREATE_CONFIRMATION: "0" } : {}),
      },
    }, (error, output, stderr) => {
      if (error) {
        rejectPromise(Object.assign(error, { stdout: output, stderr }));
        return;
      }
      resolvePromise(output);
    });
  });
  return JSON.parse(stdout || "{}");
}
