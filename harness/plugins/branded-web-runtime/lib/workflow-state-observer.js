import { stat } from "node:fs/promises";
import { resolve, sep } from "node:path";

// Only trusted host reads produce observations; command output is never parsed.
export function createWorkflowStateObserver({ workflowHome, eligible, identity, readStatus, apply,
  nextOrder, changed = () => {}, warn = () => {}, intervalMs = 1500, statFile = stat }) {
  let timer = null, generation = 0, busy = false, marker = null, boundProject = null, warning = null;
  const stop = () => {
    generation++;
    if (timer) clearInterval(timer);
    timer = null;
    marker = null;
    boundProject = null;
  };
  const check = async () => {
    if (!eligible()) { stop(); return; }
    const before = identity();
    if (!before?.projectId || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$/u.test(before.projectId)) return;
    if (boundProject !== before.projectId) { generation++; marker = null; boundProject = before.projectId; }
    if (busy) return;
    busy = true;
    const epoch = generation;
    try {
      const root = resolve(workflowHome), path = resolve(root, before.projectId, "workflow-state.json");
      if (!path.startsWith(root + sep)) throw new Error("WORKFLOW_OBSERVER_PATH_SCOPE");
      const info = await statFile(path);
      const stamp = `${info.mtimeMs}:${info.size}`;
      if (epoch !== generation || !eligible() || identity().projectId !== before.projectId || stamp === marker) return;
      changed();
      const order = nextOrder();
      const status = await readStatus(before.projectId);
      const now = identity();
      if (epoch !== generation || !eligible() || now.projectId !== before.projectId) return;
      if (status?.projectId !== before.projectId || status.handoff?.project_id !== before.projectId
          || !Number.isInteger(status.handoff?.revision)
          || (Number.isInteger(now.revision) && status.handoff.revision < now.revision)) {
        throw new Error("WORKFLOW_OBSERVER_IDENTITY_OR_REVISION_MISMATCH");
      }
      marker = stamp;
      warning = null;
      apply(status, order);
    } catch (error) {
      if (epoch === generation && eligible()) {
        const message = String(error);
        if (message !== warning) warn(`工程原生进度正式回读失败：${message}`);
        warning = message;
      }
    } finally { busy = false; }
  };
  return {
    refresh() {
      if (!eligible()) { stop(); return; }
      if (!timer) { timer = setInterval(() => { void check(); }, intervalMs); timer.unref?.(); }
      void check();
    },
    check,
    stop,
  };
}
