(() => {
  if (window.__ORION_PROJECT_LINEAGE__) return;

  const normalizeProjectName = (value) => {
    const name = String(value ?? "").trim();
    if (!name) return name;
    const base = name.replace(
      /(?:\s*(?:[·•]\s*)?(?:[（(]\s*)?(?:重建|修订)(?:\s*[）)])?)+\s*$/u,
      "",
    ).trim();
    return base || name;
  };

  const projectNeedsAttention = (project) => {
    const status = String(project?.project_status ?? "").toUpperCase();
    if (status === "ARCHIVED") return false;
    if (project?.stage_liveness?.state === "SUSPECTED_INTERRUPTED") return true;
    if (
      status === "BLOCKED_HUMAN"
      || status.endsWith("_FAILED")
      || status === "FAILED"
    ) return true;
    return Object.values(project?.stage_statuses ?? {}).some((stageStatus) => (
      stageStatus === "BLOCKED_HUMAN" || stageStatus === "FAILED"
    ));
  };

  const projectIsActiveWork = (project) => ![
    "PUBLISHED",
    "PACKAGE_PUBLISHED",
    "RELEASE_REVOKED",
    "ARCHIVED",
  ].includes(String(project?.project_status ?? "").toUpperCase());

  const projectVersionLabel = (project) => {
    const releaseVersion = String(project?.release_version ?? "").replace(/^v/iu, "");
    if (releaseVersion) return `v${releaseVersion}`;
    const suggestedVersion = String(project?.suggested_release_version ?? "").replace(/^v/iu, "");
    if (suggestedVersion) return `v${suggestedVersion} 修订中`;
    const basedOnVersion = String(project?.based_on_release_version ?? "").replace(/^v/iu, "");
    if (basedOnVersion) return `基于 v${basedOnVersion}`;
    return "初始建设";
  };

  const lineageLocation = (project, byId) => {
    const ownId = String(project?.project_id ?? "");
    let current = project;
    let depth = 0;
    const seen = new Set(ownId ? [ownId] : []);
    while (current?.parent_project_id) {
      const parentId = String(current.parent_project_id);
      if (!parentId || seen.has(parentId)) {
        return { rootProjectId: ownId, depth: 0, status: "INVALID" };
      }
      seen.add(parentId);
      depth += 1;
      const parent = byId.get(parentId);
      if (!parent) return { rootProjectId: parentId, depth, status: "MISSING_ROOT" };
      current = parent;
    }
    return {
      rootProjectId: String(current?.project_id ?? ownId),
      depth,
      status: "COMPLETE",
    };
  };

  const compareProjectCandidates = (left, right) => {
    const leftWorkRank = projectIsActiveWork(left) ? 0 : left.project_status === "ARCHIVED" ? 2 : 1;
    const rightWorkRank = projectIsActiveWork(right) ? 0 : right.project_status === "ARCHIVED" ? 2 : 1;
    return leftWorkRank - rightWorkRank
      || Number(right.lineage_depth ?? 0) - Number(left.lineage_depth ?? 0)
      || String(right.updated_at ?? "").localeCompare(String(left.updated_at ?? ""));
  };

  const projectUpdatedTimestamp = (project) => {
    const value = Date.parse(String(project?.updated_at ?? ""));
    return Number.isFinite(value) ? value : 0;
  };

  const groupProjects = (entries) => {
    const projects = (entries ?? []).filter((entry) => entry?.project_id);
    const byId = new Map(projects.map((entry) => [String(entry.project_id), entry]));
    const grouped = new Map();
    for (const project of projects) {
      const location = lineageLocation(project, byId);
      const member = {
        ...project,
        lineage_depth: location.depth,
        lineage_status: location.status,
        lineage_root_project_id: location.rootProjectId,
      };
      if (!grouped.has(location.rootProjectId)) grouped.set(location.rootProjectId, []);
      grouped.get(location.rootProjectId).push(member);
    }

    return [...grouped.entries()].map(([rootProjectId, members]) => {
      const childIds = new Set(members.map((member) => member.parent_project_id).filter(Boolean));
      const leaves = members.filter((member) => !childIds.has(member.project_id));
      const activeMembers = members.filter((member) => member.project_status !== "ARCHIVED");
      const activeLeaves = leaves.filter((member) => member.project_status !== "ARCHIVED");
      const candidates = activeLeaves.length
        ? activeLeaves
        : activeMembers.length
          ? activeMembers
          : leaves.length
            ? leaves
            : members;
      const current = candidates.slice().sort(compareProjectCandidates)[0];
      const root = byId.get(rootProjectId) ?? members.slice().sort((left, right) => (
        Number(left.lineage_depth ?? 0) - Number(right.lineage_depth ?? 0)
      ))[0];
      const sortedMembers = members.slice().sort(compareProjectCandidates);
      const visibleMembers = activeMembers.length ? activeMembers : members;
      const latestUpdatedMember = visibleMembers.slice().sort((left, right) => (
        projectUpdatedTimestamp(right) - projectUpdatedTimestamp(left)
        || String(right.updated_at ?? "").localeCompare(String(left.updated_at ?? ""))
      ))[0];
      return {
        rootProjectId,
        displayName: normalizeProjectName(root?.project_name ?? current?.project_name ?? rootProjectId),
        current,
        members: sortedMembers,
        activeMembers,
        archivedMembers: members.filter((member) => member.project_status === "ARCHIVED"),
        projectIds: members.map((member) => String(member.project_id)),
        versionCount: members.length,
        allArchived: activeMembers.length === 0,
        needsAttention: projectNeedsAttention(current),
        activeWork: projectIsActiveWork(current),
        published: current?.project_status === "PUBLISHED",
        projectKind: current?.project_kind ?? root?.project_kind ?? "BUSINESS",
        updatedAt: latestUpdatedMember?.updated_at ?? current?.updated_at ?? "",
        updatedTimestamp: projectUpdatedTimestamp(latestUpdatedMember ?? current),
      };
    }).sort((left, right) => (
      Number(right.updatedTimestamp ?? 0) - Number(left.updatedTimestamp ?? 0)
      || String(right.updatedAt).localeCompare(String(left.updatedAt))
      || String(left.displayName ?? "").localeCompare(String(right.displayName ?? ""), "zh-CN")
    ));
  };

  // Selection is independent of the open project. A current revision remains
  // available in its detail view, but never escapes an explicit list filter.
  const selectProjectGroups = (groups, { kind = "BUSINESS", status = "ALL", matchesSearch = () => true } = {}) => {
    const selected = (groups ?? []).filter((group) => {
      if (group.allArchived) return false;
      if (kind === "BUSINESS" && group.projectKind === "ACCEPTANCE_TEST") return false;
      if (kind === "ACCEPTANCE_TEST" && group.projectKind !== "ACCEPTANCE_TEST") return false;
      if (!matchesSearch(group)) return false;
      if (status === "WAITING") return group.needsAttention;
      if (status === "PUBLISHED") return group.published;
      return true;
    }).sort((left, right) => (
      (Date.parse(right.updatedAt ?? right.current?.updated_at ?? "") || 0)
      - (Date.parse(left.updatedAt ?? left.current?.updated_at ?? "") || 0)
    ));
    return {
      groups: selected,
      recordCount: selected.reduce((count, group) => count + (group.activeMembers?.length ?? 0), 0),
    };
  };

  window.__ORION_PROJECT_LINEAGE__ = {
    groupProjects,
    normalizeProjectName,
    projectIsActiveWork,
    projectNeedsAttention,
    projectVersionLabel,
    selectProjectGroups,
  };
})();
