(() => {
  if (window.__ORION_ONTOLOGY_ENGINEERING__) return;

  let pendingTimer = null;

  const bridge = () => window.__ORION_ENGINEERING_BRIDGE__ ?? null;

  const withBridge = (callback) => {
    const current = bridge();
    if (current) {
      callback(current);
      return;
    }
    window.clearTimeout(pendingTimer);
    pendingTimer = window.setTimeout(() => withBridge(callback), 40);
  };

  const open = (route = {}) => withBridge((current) => current.open({
    workspace: route.projectId ? "stage" : "projects",
    projectId: route.projectId ?? null,
    stage: route.stage ?? null,
  }));

  const close = () => withBridge((current) => current.close());

  window.addEventListener("orion:engineering-ready", () => {
    window.clearTimeout(pendingTimer);
    pendingTimer = null;
  });

  window.__ORION_ONTOLOGY_ENGINEERING__ = { open, close };
})();
