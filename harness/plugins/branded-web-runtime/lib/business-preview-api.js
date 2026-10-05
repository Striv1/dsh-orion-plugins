import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { jsonResponse, readRequestJson } from "./http-response.js";

const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const identifier = (value) => typeof value === "string" && value.trim() === value && value.length > 0 && value.length <= 128;
const payloadReference = (value) => object(value)
  && Object.keys(value).length === 2
  && typeof value.file_name === "string" && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.json$/.test(value.file_name)
  && typeof value.sha256 === "string" && /^sha256:[a-f0-9]{64}$/.test(value.sha256);

// Browser authentication and same-origin protection remain at the existing
// ORION gateway. Only this explicit action crosses the native Python boundary.
export function createBusinessPreviewApi({ runWorkflowTool, safeProjectId, confirmationHeader }) {
  return async (request, response, config) => {
    const send = (status, body) => jsonResponse(response, status, body, request.method);
    if (!["GET", "POST"].includes(request.method)) {
      send(405, { detail: "业务能力试运行接口只允许 GET/POST。" });
      return;
    }
    const write = request.method === "POST";
    if (write && request.headers?.[confirmationHeader] !== "1") {
      send(403, { detail: "缺少业务能力试运行确认标记。" });
      return;
    }
    let args;
    if (write) {
      let body;
      try { body = await readRequestJson(request); }
      catch { send(400, { detail: "试运行请求必须是有效且不超过 16 KiB 的 JSON。" }); return; }
      const allowed = new Set(["project_id", "expected_revision", "payload_file", "plan_id", "case_id", "retry"]);
      if (!object(body) || Object.keys(body).some((key) => !allowed.has(key))
        || typeof body.project_id !== "string" || !safeProjectId(body.project_id)
        || !Number.isSafeInteger(body.expected_revision) || body.expected_revision < 0
        || !payloadReference(body.payload_file) || typeof body.plan_id !== "string" || !/^[a-z][a-z0-9_]{1,40}$/.test(body.plan_id)
        || (body.case_id !== undefined && !identifier(body.case_id))
        || (body.retry !== undefined && typeof body.retry !== "boolean")) {
        send(400, { detail: "请提供有效工程、版本、当前草稿指纹、业务计划和样例；retry 必须是布尔值。" });
        return;
      }
      args = { ...body, retry: body.retry ?? false };
      try {
        const contract = JSON.parse(await readFile(config.workflowActionContract
          ?? join(process.cwd(), "harness", "contracts", "workflow-ui-actions.json"), "utf8"));
        if (contract.schemaVersion !== 1 || contract.actions?.start_business_preview?.endpoint !== "business-preview") {
          send(403, { detail: "当前工程动作契约未启用业务能力试运行。" });
          return;
        }
      } catch {
        send(503, { detail: "业务能力试运行动作契约暂不可用，请重新读取。" });
        return;
      }
    } else {
      const projectId = new URL(request.url, "http://local").searchParams.get("project_id");
      if (!projectId || !safeProjectId(projectId)) {
        send(400, { detail: "需要有效工程标识。" });
        return;
      }
      args = { project_id: projectId };
    }
    try {
      const result = await runWorkflowTool(config, write ? "start_business_preview" : "get_business_preview", args);
      if (result?.ok !== true) {
        send(write ? 409 : 422, { detail: typeof result?.detail === "string" ? result.detail : "业务能力预览未能完成，请重新读取回执。" });
        return;
      }
      if (!object(result.result)) throw new Error("Missing preview receipt");
      send(write && ["STARTING", "RUNNING"].includes(result.result.status) ? 202 : 200, result.result);
    } catch {
      send(503, { detail: write
        ? "试运行请求未能确认，请先重新读取回执；不要重复发起。"
        : "业务能力预览暂时无法读取，请重试。" });
    }
  };
}
