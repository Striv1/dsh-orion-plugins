(() => {
  if (window.__ORION_ENGINEERING_WORKFLOW_CLIENT__) return;

  const WORKFLOW_BASE = "/orion-workflow-api";

  const request = (method, path, payload = null, { confirmation = payload !== null } = {}) =>
    new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(method, `${WORKFLOW_BASE}${path}`, true);
      xhr.setRequestHeader("Accept", "application/json");
      if (payload !== null) {
        xhr.setRequestHeader("Content-Type", "application/json");
        if (confirmation) xhr.setRequestHeader("x-orion-workflow-confirmation", "1");
      }
      xhr.onload = () => {
        let response = {};
        try {
          response = JSON.parse(xhr.responseText || "{}");
        } catch (_error) {
          response = {};
        }
        if (xhr.status < 200 || xhr.status >= 300) {
          reject(new Error(response.detail ?? `HTTP ${xhr.status}`));
          return;
        }
        resolve(response);
      };
      xhr.onerror = () => reject(new Error("工程请求未能到达本机 3081 工作流"));
      xhr.send(payload === null ? null : JSON.stringify(payload));
    });

  const status = (projectId = null) => request(
    "GET",
    projectId ? `/status?${new URLSearchParams({ project_id: projectId })}` : "/status",
  );

  window.__ORION_ENGINEERING_WORKFLOW_CLIENT__ = Object.freeze({
    base: WORKFLOW_BASE,
    status,
    datasources: () => request("GET", "/datasources"),
    chat2dbCatalog: (level, parameters) => request(
      "GET",
      `/chat2db-catalog?${new URLSearchParams({ level, ...parameters })}`,
    ),
    create: (payload) => request("POST", "/create", payload),
    action: (tool, argumentsPayload) => request("POST", "/action", {
      tool,
      arguments: argumentsPayload,
    }),
    confirmation: (payload) => request("POST", "/confirmation", payload),
    retryContinuation: (projectId, eventId) => request("POST", "/continuation", {
      project_id: projectId,
      event_id: eventId,
    }),
  });
})();
