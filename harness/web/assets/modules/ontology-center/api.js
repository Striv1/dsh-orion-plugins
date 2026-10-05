(() => {
  if (window.__ORION_ONTOLOGY_API__) return;

  const BASE = "/orion-workflow-api";
  let dashboardCache = null;
  let dashboardFetchedAt = 0;
  let dashboardInFlight = null;
  let catalogCache = null;
  let catalogFetchedAt = 0;
  let catalogInFlight = null;

  const requestJson = async (path) => {
    const response = await fetch(path, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`工程状态读取失败（${response.status}）`);
    return response.json();
  };

  const dashboard = async (force = false) => {
    const now = Date.now();
    if (!force && dashboardCache && now - dashboardFetchedAt < 15000) return dashboardCache;
    if (dashboardInFlight) return dashboardInFlight;
    dashboardInFlight = requestJson(`${BASE}/status`)
      .then((payload) => {
        dashboardCache = payload;
        dashboardFetchedAt = Date.now();
        return payload;
      })
      .finally(() => {
        dashboardInFlight = null;
      });
    return dashboardInFlight;
  };

  const projectDashboard = (projectId) => requestJson(
    `${BASE}/status?${new URLSearchParams({ project_id: projectId })}`,
  );

  const releaseFrom = (payload) => {
    const event = [...(payload.events ?? [])].reverse().find(
      (item) => item.event_type === "ONTOLOGY_PACKAGE_PUBLISHED",
    );
    return payload.publication ?? event?.details ?? {};
  };

  const releaseArtifactFrom = (payload) => (
    (payload.artifacts ?? []).find((item) => item.path === "07-release/release-report.html")
    ?? (payload.artifacts ?? []).find((item) => item.path?.startsWith("07-release/") && item.path?.endsWith(".html"))
    ?? null
  );

  const versionParts = (version) => {
    const match = String(version ?? "").match(/^(\d+)\.(\d+)\.(\d+)/);
    return match ? match.slice(1).map(Number) : [0, 0, 0];
  };

  const compareReleases = (left, right) => {
    const leftParts = versionParts(left.version);
    const rightParts = versionParts(right.version);
    for (let index = 0; index < 3; index += 1) {
      if (leftParts[index] !== rightParts[index]) return rightParts[index] - leftParts[index];
    }
    return String(right.publishedAt ?? "").localeCompare(String(left.publishedAt ?? ""));
  };

  const compactOntologyName = (value) => {
    const name = String(value ?? "").trim();
    if (!name) return name;
    const base = name.replace(
      /(?:\s*(?:[·•]\s*)?(?:[（(]\s*)?(?:重建|修订)(?:\s*[）)])?)+\s*$/u,
      "",
    ).trim();
    return base || name;
  };

  const groupReleases = (releases) => {
    const groups = new Map();
    releases.forEach((release) => {
      const key = release.ontologyIri || `project:${release.sourceProjectId}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(release);
    });
    return [...groups.entries()].flatMap(([ontologyKey, versions]) => {
      versions.sort(compareReleases);
      const current = versions.find((item) => item.releaseStatus === "PUBLISHED");
      if (!current) return [];
      const versionHistory = versions.map((item) => ({
        ...item,
        versionStatus: item.releaseStatus === "RELEASE_REVOKED"
          ? "REVOKED"
          : item === current ? "CURRENT" : "HISTORICAL",
      })).sort((left, right) => {
        const statusOrder = { CURRENT: 0, HISTORICAL: 1, REVOKED: 2 };
        const statusDifference = statusOrder[left.versionStatus] - statusOrder[right.versionStatus];
        return statusDifference || compareReleases(left, right);
      });
      return [{
        ...current,
        ontologyKey,
        versions: versionHistory,
        versionCount: versionHistory.length,
        revokedVersionCount: versionHistory.filter((item) => item.versionStatus === "REVOKED").length,
      }];
    }).sort((left, right) => String(right.publishedAt ?? "").localeCompare(String(left.publishedAt ?? "")));
  };

  const catalog = async (force = false, dashboardPayload = null) => {
    const now = Date.now();
    if (!force && catalogCache && now - catalogFetchedAt < 15000) return catalogCache;
    if (catalogInFlight) return catalogInFlight;
    // The center has already fetched this snapshot. Reuse it instead of
    // issuing another forced top-level status request in the same refresh.
    catalogInFlight = (dashboardPayload ? Promise.resolve(dashboardPayload) : dashboard(force))
      .then(async (payload) => {
        const released = (payload.projects ?? []).filter((item) => (
          item.project_status === "PUBLISHED" || item.project_status === "RELEASE_REVOKED"
        ));
        const releases = [];
        for (const project of released.slice(0, 50)) {
          const detail = await projectDashboard(project.project_id);
          const release = releaseFrom(detail);
          const artifact = releaseArtifactFrom(detail);
          if (!release.release_version && !release.version) continue;
          const stageContractVersion = detail.state?.stage_contract_version
            ?? detail.project?.stage_contract_version ?? detail.stage_contract_version
            ?? project.stage_contract_version ?? null;
          const stageContracts = [detail.stage_contracts, detail.state?.stage_contracts, detail.project?.stage_contracts]
            .find((item) => item?.contract_version === stageContractVersion) ?? null;
          releases.push({
            ontologyId: project.project_id,
            ontologyIri: detail.ontologyIri ?? release.ontology_iri ?? null,
            name: compactOntologyName(
              detail.ontologyTitle ?? project.project_name ?? project.project_id,
            ),
            version: release.release_version ?? release.version ?? "正式发布",
            status: project.project_status,
            releaseStatus: project.project_status,
            publishedAt: release.published_at ?? release.released_at ?? project.updated_at,
            sourceProjectId: project.project_id,
            classes: detail.ontologyStats?.classes ?? "待核对",
            objectProperties: detail.ontologyStats?.objectProperties ?? "待核对",
            dataProperties: detail.ontologyStats?.dataProperties ?? "待核对",
            qualityStatus: detail.state?.stage_statuses?.S6 === "PASSED" ? "质量验证通过" : "待回读质量结果",
            releaseContract: detail.releaseContract ?? detail.release_contract ?? null,
            runtimeService: detail.runtime_service ?? detail.runtimeService ?? null,
            stage_contract_version: stageContractVersion,
            stage_contracts: stageContracts,
            artifact,
            artifacts: detail.artifacts ?? [],
            storageStatus: detail.storageStatus ?? null,
          });
        }
        const assets = groupReleases(releases);
        catalogCache = assets;
        catalogFetchedAt = Date.now();
        return assets;
      })
      .finally(() => {
        catalogInFlight = null;
      });
    return catalogInFlight;
  };

  const artifactUrl = (projectId, path) => `${BASE}/artifact?${new URLSearchParams({
    project_id: projectId,
    path,
  })}`;

  const revealArtifact = async (projectId, path) => {
    const response = await fetch(`${BASE}/artifact-reveal`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "x-orion-artifact-reveal": "1",
      },
      body: JSON.stringify({ project_id: projectId, path }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail ?? `文件定位失败（${response.status}）`);
    return payload;
  };

  const businessQuality = (projectId) => requestJson(
    `${BASE}/business-quality?${new URLSearchParams({ project_id: projectId })}`,
  );

  const graph = (projectId) => requestJson(
    `${BASE}/graph?${new URLSearchParams({ project_id: projectId })}`,
  );

  const templates = () => requestJson(`${BASE}/templates`);

  const templateGraph = (templateId) => requestJson(
    `${BASE}/template-graph?${new URLSearchParams({ template_id: templateId })}`,
  );

  const templateArtifactUrl = (templateId) => `${BASE}/template-artifact?${new URLSearchParams({
    template_id: templateId,
  })}`;

  const semanticaStatus = (projectId) => requestJson(
    `${BASE}/semantica/status?${new URLSearchParams({ project_id: projectId })}`,
  );

  const semanticaContext = (projectId, nodeId, hops = 1) => requestJson(
    `${BASE}/semantica/context?${new URLSearchParams({
      project_id: projectId,
      node_id: nodeId,
      hops: String(hops),
    })}`,
  );

  const openProtege = async (projectId) => {
    const response = await fetch(`${BASE}/protege-open`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "x-orion-protege-open": "1",
      },
      body: JSON.stringify({ project_id: projectId }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail ?? `Protégé 启动失败（${response.status}）`);
    return payload;
  };

  const openTemplateProtege = async (templateId) => {
    const response = await fetch(`${BASE}/template-protege-open`, {
      method: "POST",
      credentials: "same-origin",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "x-orion-protege-open": "1",
      },
      body: JSON.stringify({ template_id: templateId }),
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload.detail ?? `Protégé 启动失败（${response.status}）`);
    return payload;
  };

  window.__ORION_ONTOLOGY_API__ = {
    dashboard,
    projectDashboard,
    businessQuality,
    catalog,
    artifactUrl,
    revealArtifact,
    graph,
    templates,
    templateGraph,
    templateArtifactUrl,
    semanticaStatus,
    semanticaContext,
    openProtege,
    openTemplateProtege,
  };
})();
