const WRITE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

/** The native plugin's opt-in write mode never changes legacy 3081 behavior. */
export function nativePluginWriteRejection(config, method, pathname) {
  if (config.nativePlugin !== true || !WRITE_METHODS.has(String(method).toUpperCase())) return null;
  // Session binding and checked analytics POSTs are read operations against a
  // release. Persistent analytics assets have their own action-level check.
  if (pathname.startsWith("/orion-ontology-qa-api/")) return null;
  if (config.readOnly !== false) return "当前 ORION 插件为只读模式，业务写入已关闭。";
  if (!config.workflowActor) return "业务写入需要明确配置操作人。";
  return null;
}

export function nativeAnalyticsWriteRejection(config, kind, action) {
  if (config.nativePlugin !== true || config.readOnly === false || kind !== "assets") return null;
  if (["list", "search", "get"].includes(action)) return null;
  return "当前 ORION 插件为只读模式，保存分析资产及更新基线已关闭。";
}
