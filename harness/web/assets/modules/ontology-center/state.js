(() => {
  if (window.__ORION_SHELL_STATE__) return;

  const PRIMARY_KEY = "orion.shell.primary";
  const SECTION_KEY = "orion.shell.ontologySection";
  const PROJECT_KEY = "orion.engineering.selectedProjectId";
  const STAGE_KEY = "orion.engineering.selectedStage";
  const listeners = new Set();

  const readStorage = (key) => {
    try {
      return window.localStorage.getItem(key);
    } catch (_error) {
      return null;
    }
  };

  const writeStorage = (key, value) => {
    try {
      if (value) window.localStorage.setItem(key, value);
      else window.localStorage.removeItem(key);
    } catch (_error) {
      // Navigation remains usable when browser storage is unavailable.
    }
  };

  const validStage = (value) => /^S[0-7]$/.test(value ?? "") ? value : null;
  const validSection = (value) => {
    if (value === "manage") return "manage";
    if (value === "templates") return "templates";
    return "engineering";
  };

  const storedRoute = () => {
    const primary = readStorage(PRIMARY_KEY) === "ontology" ? "ontology" : "chat";
    return {
      primary,
      section: validSection(readStorage(SECTION_KEY)),
      projectId: readStorage(PROJECT_KEY),
      stage: validStage(readStorage(STAGE_KEY)),
    };
  };

  const parseHash = () => {
    const raw = window.location.hash.replace(/^#\/?/, "");
    const parts = raw.split("/").filter(Boolean).map((part) => decodeURIComponent(part));
    if (parts[0] === "chat") return { primary: "chat", section: "engineering", projectId: null, stage: null };
    if (parts[0] !== "ontology") return storedRoute();
    const section = validSection(parts[1]);
    return {
      primary: "ontology",
      section,
      projectId: section === "engineering" ? parts[2] || null : null,
      stage: section === "engineering" ? validStage(parts[3]) : null,
    };
  };

  const normalize = (route) => ({
    primary: route?.primary === "ontology" ? "ontology" : "chat",
    section: validSection(route?.section),
    projectId: route?.primary === "ontology" && validSection(route?.section) === "engineering"
      ? route?.projectId || null
      : null,
    stage: route?.primary === "ontology" && validSection(route?.section) === "engineering"
      ? validStage(route?.stage)
      : null,
  });

  const routeHash = (route) => {
    if (route.primary === "chat") return "#/chat";
    if (route.section === "manage") return "#/ontology/manage";
    if (route.section === "templates") return "#/ontology/templates";
    const segments = ["#", "ontology", "engineering"];
    if (route.projectId) segments.push(encodeURIComponent(route.projectId));
    if (route.projectId && route.stage) segments.push(route.stage);
    return segments.join("/");
  };

  let current = normalize(parseHash());

  const persist = (route) => {
    writeStorage(PRIMARY_KEY, route.primary);
    writeStorage(SECTION_KEY, route.section);
    writeStorage(PROJECT_KEY, route.section === "engineering" ? route.projectId : null);
    writeStorage(STAGE_KEY, route.section === "engineering" ? route.stage : null);
  };

  const emit = () => {
    const snapshot = { ...current };
    for (const listener of listeners) listener(snapshot);
  };

  const navigate = (route, options = {}) => {
    current = normalize({ ...current, ...route });
    persist(current);
    const nextHash = routeHash(current);
    if (window.location.hash !== nextHash) {
      const method = options.replace ? "replaceState" : "pushState";
      window.history[method](null, "", `${window.location.pathname}${window.location.search}${nextHash}`);
    }
    emit();
  };

  window.addEventListener("hashchange", () => {
    current = normalize(parseHash());
    persist(current);
    emit();
  });

  window.__ORION_SHELL_STATE__ = {
    get: () => ({ ...current }),
    navigate,
    subscribe(listener) {
      listeners.add(listener);
      listener({ ...current });
      return () => listeners.delete(listener);
    },
    ensureUrl() {
      navigate(current, { replace: true });
    },
  };
})();
