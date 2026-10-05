/** Only public Harness services; no composer DOM or synthetic user messages. */
export function createSessionBridge(ctx, { sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)), switchAttempts = 50 } = {}) {
  const current = () => {
    const snapshot = ctx.sessions.list.getSnapshot();
    return (snapshot.current && snapshot.byId[snapshot.current] ? snapshot.current : null)
      ?? Object.values(snapshot.byId).find((row) => (row.retainedBy?.mainView ?? 0) > 0)?.id ?? null;
  };
  const workspaces = () => ctx.workspaces.list.getSnapshot().items.map((item) => ({ ...item, sessionIds: [...item.sessionIds] }));
  const currentWorkspace = () => {
    const id = current();
    const session = ctx.sessions.list.getSnapshot().byId[id];
    return workspaces().find((item) => item.sessionIds.includes(id) || item.path === session?.cwd) ?? null;
  };
  return {
    current,
    workspaces,
    currentWorkspace,
    showWorkbench: () => ctx.layout.selectPanel(null),
    contains: (id) => Boolean(ctx.sessions.list.getSnapshot().byId[id]),
    open(id) {
      if (!ctx.sessions.list.getSnapshot().byId[id]) throw new Error("原生会话不存在，请重新打开工作台。");
      ctx.uiWorkspace.openSession(id);
      return current() === id;
    },
    async create(workspaceId = null) {
      const workspace = workspaceId == null ? currentWorkspace() : workspaces().find((item) => item.workspaceId === workspaceId);
      if (workspaceId != null && !workspace) throw new Error("选定的工作区已不可用，请重新选择工作区。");
      const id = await ctx.sessions.create(workspace ? { workspaceId: workspace.workspaceId } : undefined);
      if (!ctx.sessions.list.getSnapshot().byId[id]) throw new Error("新会话尚未完成登记，请刷新后重试。");
      ctx.uiWorkspace.openSession(id);
      for (let attempt = 0; current() !== id; attempt += 1) {
        if (attempt >= switchAttempts) throw new Error("新会话尚未完成切换，请重试");
        await sleep(50);
      }
      return id;
    },
    async selectPreset(id, presetId) {
      // Cordis guards the facade and its traced namespace independently;
      // the consuming plugin declares both remote and remote.agentPresets.
      const result = await ctx.remote.agentPresets.select(id, presetId);
      if (!result?.ok || result.value !== presetId) throw new Error(result?.error?.message ?? "会话模式未通过回读");
      return result.value;
    },
    fillDraft(id, text) {
      if (typeof text !== "string" || !text.trim()) throw new Error("草稿内容为空，未修改当前输入框。");
      if (current() !== id) throw new Error("会话已经切换，未填入草稿");
      const scope = ctx.sessions.scope(id);
      const input = scope?.get("conversation")?.input?.for(scope);
      const before = input?.state?.getSnapshot();
      if (!before || before.phase !== "plain" || typeof input.setDraft !== "function") throw new Error("原生输入框尚未就绪");
      if (before.occurrences?.length) throw new Error("请先处理当前草稿中的文件引用，未覆盖草稿");
      const attachmentIds = [...(before.attachmentIds ?? [])];
      const draft = before.draft?.trim() ? `${before.draft}\n\n${text}` : String(text);
      input.setDraft(draft);
      const after = input.state.getSnapshot();
      if (current() !== id || after.draft !== draft || JSON.stringify(after.attachmentIds ?? []) !== JSON.stringify(attachmentIds)) throw new Error("草稿回读不一致，尚未发送消息");
      return { sessionId: id, draft, attachmentCount: after.attachmentIds?.length ?? 0 };
    },
    activity(id) {
      const binding = ctx.sessions.binding(id);
      const summary = ctx.sessions.list.getSnapshot().byId[id];
      if (!binding) return { available: false, running: null, started: false, blank: false, presetId: summary?.projectionValues?.agentPreset ?? null };
      const snapshot = binding.session.getSnapshot();
      const entries = binding.eventSource.getSnapshot().entries;
      const started = snapshot.running === true || entries.some((entry) => entry.type === "event" && (entry.event?.type === "turn/start" || (entry.event?.type === "user/message" && entry.event.data?.source?.kind === "user")));
      return { available: snapshot.openState === "open" || typeof summary?.blank === "boolean", started, running: snapshot.openState === "open" ? snapshot.running : null, blank: snapshot.blank === true && !started, presetId: summary?.projectionValues?.agentPreset ?? null };
    },
  };
}
