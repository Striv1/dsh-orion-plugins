const endpointId = (value) => value && typeof value === "object" ? value.id : value;
const hopLimit = (value) => Number.isFinite(Number(value)) ? Math.max(0, Math.floor(Number(value))) : 1;

// Every traversal uses the already-visible graph: hidden OWL definitions must
// never become shortcuts between business objects or instances.
const neighborhood = (nodes, links, seeds, hops) => {
  const ids = new Set(nodes.map((node) => node.id));
  const adjacency = new Map();
  const validLinks = links.filter((edge) => ids.has(endpointId(edge.source)) && ids.has(endpointId(edge.target)));
  for (const edge of validLinks) {
    const source = endpointId(edge.source), target = endpointId(edge.target);
    if (!adjacency.has(source)) adjacency.set(source, []);
    if (!adjacency.has(target)) adjacency.set(target, []);
    adjacency.get(source).push(target);
    adjacency.get(target).push(source);
  }
  const distances = new Map();
  for (const id of seeds) if (ids.has(id)) distances.set(id, 0);
  const queue = [...distances.keys()];
  const limit = hopLimit(hops);
  for (let index = 0; index < queue.length; index += 1) {
    const current = queue[index], distance = distances.get(current);
    if (distance >= limit) continue;
    for (const next of adjacency.get(current) || []) {
      if (distances.has(next)) continue;
      distances.set(next, distance + 1);
      queue.push(next);
    }
  }
  return {
    distances,
    edges: validLinks.filter((edge) => distances.has(endpointId(edge.source)) && distances.has(endpointId(edge.target))),
  };
};

export const graphNeighborhood = (data, nodeId, hops = 1) =>
  neighborhood(data?.nodes || [], data?.links || [], [endpointId(nodeId)], hops);

/** Project one release payload into a business model, instance, or OWL view. */
export const projectGraph = (payload, {
  view = "model", showHierarchy = false, filter = "all",
  instanceClassId = null, instanceId = null, hops = 1,
} = {}) => {
  let nodes = payload?.nodes || [];
  let links = payload?.edges || payload?.links || [];
  if (view === "professional") {
    if (filter !== "all") {
      links = links.filter((edge) => edge.kind === filter);
      const endpoints = new Set(links.flatMap((edge) => [endpointId(edge.source), endpointId(edge.target)]));
      nodes = nodes.filter((node) => endpoints.has(node.id));
    }
  } else if (view === "instances") {
    nodes = nodes.filter((node) => node.kind === "individual");
    links = links.filter((edge) => edge.kind === "relationship");
    let seeds = null;
    if (instanceId != null) seeds = [endpointId(instanceId)];
    else if (instanceClassId != null) {
      const classId = endpointId(instanceClassId);
      seeds = (payload?.edges || payload?.links || [])
        .filter((edge) => edge.kind === "instance" && endpointId(edge.target) === classId)
        .map((edge) => endpointId(edge.source));
    }
    if (seeds) {
      const scope = neighborhood(nodes, links, seeds, hops);
      nodes = nodes.filter((node) => scope.distances.has(node.id));
      links = scope.edges;
    }
  } else {
    nodes = nodes.filter((node) => node.kind === "class");
    links = links.filter((edge) => edge.kind === "schema" || (showHierarchy && edge.kind === "subclass"));
  }
  const ids = new Set(nodes.map((node) => node.id));
  return {
    nodes: nodes.map((node) => ({ ...node })),
    links: links
      .filter((edge) => ids.has(endpointId(edge.source)) && ids.has(endpointId(edge.target)))
      .map((edge) => ({ ...edge, source: endpointId(edge.source), target: endpointId(edge.target) })),
  };
};
