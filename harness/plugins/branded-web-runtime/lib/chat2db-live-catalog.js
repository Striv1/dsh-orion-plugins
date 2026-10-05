import { createHash, randomUUID } from "node:crypto";

const PREFIX = "mcp__chat2db__";
const LEVELS = { list_all_databases: "databases", list_all_schemas: "schemas", list_all_tables: "tables" };
const PROTOCOL = "ORION_CATALOG_V1";
const PAGE_SIZE = 50;
const MAX_RESULT_BYTES = 1024 * 1024;
class CatalogError extends Error {}

// MCP returns either structured data or JSON-encoded text (sometimes twice).
// Keep this boundary shared by the browser catalog and the model's native tools.
export function catalogPayload(value) {
  for (let depth = 0; depth < 8; depth += 1) {
    if (typeof value === "string") {
      try { value = JSON.parse(value); continue; } catch { return value; }
    }
    if (value?.structuredContent) { value = value.structuredContent; continue; }
    if (value?.content) {
      const blocks = value.content.filter((item) => item.type === "text");
      if (blocks.length !== 1) throw new CatalogError("Chat2DB 目录回执必须包含唯一的文本或结构化结果。");
      value = blocks[0].text; continue;
    }
    return value;
  }
  throw new CatalogError("Chat2DB 目录回执嵌套过深。");
}

const literal = (value) => `convert_from(decode('${Buffer.from(value, "utf8").toString("hex")}', 'hex'), 'UTF8')`;
const textArgument = (args, key) => {
  const value = args[key];
  if (typeof value !== "string" || !value || value.length > 256 || value.includes("\0")) {
    throw new CatalogError(`Chat2DB 当前目录核对缺少有效的 ${key}。`);
  }
  return value;
};

export const catalogScopeKey = (level, args) => createHash("sha256").update(JSON.stringify([
  level, args.dataSourceId, level === "schemas" ? args.targetDatabaseName : level === "tables" ? args.databaseName : "",
  level === "tables" ? args.schemaName : "",
])).digest("hex").slice(0, 32);

export function postgresCatalogQuery(level, args, offset = 0) {
  if (!Number.isSafeInteger(offset) || offset < 0 || offset % PAGE_SIZE) throw new CatalogError("Chat2DB 目录分页越界。");
  if (level === "schemas") textArgument(args, "targetDatabaseName");
  if (level === "tables") textArgument(args, "databaseName");
  const schema = level === "tables" ? textArgument(args, "schemaName") : "";
  const query = {
    databases: `SELECT datname AS name, 'DATABASE' AS type FROM pg_catalog.pg_database
      WHERE datallowconn AND NOT datistemplate AND has_database_privilege(datname, 'CONNECT')`,
    schemas: `SELECT nspname AS name, 'SCHEMA' AS type FROM pg_catalog.pg_namespace
      WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' AND has_schema_privilege(oid, 'USAGE')`,
    tables: `SELECT c.relname AS name, CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
      WHEN 'f' THEN 'FOREIGN TABLE' ELSE 'TABLE' END AS type
      FROM pg_catalog.pg_class c JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = ${literal(schema)} AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        AND has_schema_privilege(n.oid, 'USAGE') AND has_table_privilege(c.oid, 'SELECT')`,
  }[level];
  if (!query) throw new CatalogError("Chat2DB 不支持的目录层级。");
  // Chat2DB MCP renders only 50 rows and truncates cells at 200 characters.
  // PostgreSQL identifiers fit in 126 hex characters; never aggregate the whole
  // catalog into one cell. Each bounded page carries the same catalog digest.
  return `WITH catalog AS (${query}),
    meta AS (SELECT count(*) AS total, md5(COALESCE(json_agg(catalog ORDER BY name)::text, '[]')) AS fingerprint FROM catalog),
    page AS (SELECT * FROM catalog ORDER BY name LIMIT ${PAGE_SIZE} OFFSET ${offset})
    SELECT '${PROTOCOL}' AS protocol, '${catalogScopeKey(level, args)}' AS scope_key,
      meta.total, meta.fingerprint, COALESCE(encode(convert_to(page.name, 'UTF8'), 'hex'), '-') AS name_hex,
      COALESCE(page.type, 'EMPTY') AS kind FROM meta LEFT JOIN page ON true ORDER BY page.name`;
}

export function parseLiveCatalog(result, level, args) {
  if (result.isError) throw new CatalogError("Chat2DB 当前目录查询失败；未使用旧缓存替代，请检查连接或权限后重试。");
  const text = catalogPayload(result.value ?? result);
  const count = typeof text === "string" ? text.match(/^rows: (\d+), hasNextPage: false\s*$/mu) : null;
  if (typeof text !== "string" || text.length > 256 * 1024
    || !/^success: true\s*$/mu.test(text) || !/^sqlType: SELECT\s*$/mu.test(text)
    || !count || Number(count[1]) < 1 || Number(count[1]) > PAGE_SIZE) {
    throw new CatalogError("Chat2DB 未返回完整的只读目录查询回执；未使用旧缓存替代。");
  }
  const rows = text.split(/\r?\n/u).filter((line) => /^\d+\t/u.test(line)).map((line) => line.split("\t"));
  if (rows.length !== Number(count[1])) throw new CatalogError("Chat2DB 目录结果被截断或格式不完整。");
  const items = []; let total; let fingerprint;
  for (const [index, protocol, scope, size, digest, hex, type, ...extra] of rows) {
    if (extra.length || protocol !== PROTOCOL || scope !== catalogScopeKey(level, args)
      || !/^\d+$/u.test(size) || !/^[a-f0-9]{32}$/u.test(digest)
      || Number(index) !== items.length + 1 || (total !== undefined && (total !== Number(size) || fingerprint !== digest))) {
      throw new CatalogError("Chat2DB 目录结果与请求范围不符。");
    }
    total = Number(size); fingerprint = digest;
    if (!Number.isSafeInteger(total)) throw new CatalogError("Chat2DB 目录总数无效。");
    if (total === 0 && rows.length === 1 && hex === "-" && type === "EMPTY") break;
    if (!/^(?:[a-f0-9]{2}){1,63}$/u.test(hex) || !["DATABASE", "SCHEMA", "TABLE", "VIEW", "MATERIALIZED VIEW", "FOREIGN TABLE"].includes(type)) {
      throw new CatalogError("Chat2DB 目录标识被截断或无效。");
    }
    let name;
    try { name = new TextDecoder("utf-8", { fatal: true }).decode(Buffer.from(hex, "hex")); }
    catch { throw new CatalogError("Chat2DB 目录标识编码无效。"); }
    if (name.includes("\0")) throw new CatalogError("Chat2DB 目录标识无效。");
    items.push({ name, type });
  }
  if (new Set(items.map((row) => row.name)).size !== items.length || items.length > total) throw new CatalogError("Chat2DB 目录包含重复或多余项。");
  return { items, total, fingerprint };
}

function datasourceType(result, id) {
  if (result.isError) throw new CatalogError("Chat2DB 数据源身份核对失败。");
  const payload = catalogPayload(result.value ?? result);
  if (typeof payload !== "string") throw new CatalogError("Chat2DB 数据源身份回执不完整。");
  const matches = payload.split(/\r?\n/u).map((line) => Object.fromEntries(line.split(";").map((part) => {
    const index = part.indexOf("=");
    return [part.slice(0, index).trim(), part.slice(index + 1).trim()];
  }))).filter((row) => row.id === String(id));
  if (matches.length !== 1) throw new CatalogError("请求的数据源不在 Chat2DB 当前目录中。");
  return matches[0].type?.toUpperCase();
}

export function installLiveChat2dbCatalog(context) {
  return context.on("tools/post-execute", async (execution, result, next) => {
    const level = LEVELS[execution.name.slice(PREFIX.length)];
    if (!execution.name.startsWith(PREFIX) || !level || result.isError) return next();
    const prior = await next();
    if (prior.kind !== "accept" || prior.content !== undefined || prior.value !== undefined) return prior;
    const args = execution.arguments;
    const signal = AbortSignal.any([execution.signal, AbortSignal.timeout(15000)]);
    try {
      if (!Number.isSafeInteger(args?.dataSourceId) || args.dataSourceId <= 0) throw new CatalogError("数据源编号无效。");
      const call = async (name, argumentsPayload) => {
        signal.throwIfAborted();
        const response = await context.tools.execute({ name: PREFIX + name, arguments: argumentsPayload,
          callId: `catalog-${randomUUID()}`, agent: execution.agent, parent: execution.token,
          rootCallId: execution.rootCallId, signal });
        signal.throwIfAborted();
        return response;
      };
      const engine = datasourceType(await call("list_all_datasources", {}), args.dataSourceId);
      if (engine !== "POSTGRESQL") {
        // Retain existing engines without silently presenting their cache as live.
        return { kind: "accept", value: { content: [{ type: "text", text: JSON.stringify({
          freshness: "PROVIDER_CACHE_UNVERIFIED", engine,
          warning: "当前连接器未提供已验证的目录刷新合同；目录可能过期。",
          provider_catalog: catalogPayload(prior.value ?? result.value ?? result),
        }) }] }, additionalContexts: prior.additionalContexts };
      }
      let databaseName = level === "schemas" ? args.targetDatabaseName : args.databaseName;
      let candidates = [];
      if (level === "databases") {
        const cached = catalogPayload(prior.value ?? result.value ?? result);
        const names = typeof cached === "string" ? cached.split(/\r?\n/u)
          .map((line) => line.replace(/\s+\[[^\]]+\]$/u, "").trim()).filter(Boolean) : [];
        if (!names.length || names.some((name) => /^No databases/i.test(name))) throw new CatalogError("Chat2DB 没有可用于只读目录查询的已登记数据库。");
        candidates = [...new Set([...(names.includes("postgres") ? ["postgres"] : []), ...names])].slice(0, 3);
        databaseName = candidates.shift();
      }
      const items = []; let total; let fingerprint; let bytes = 0;
      do {
        const result = await call("execute_sql", {
          dataSourceId: args.dataSourceId, databaseName, ...(level === "tables" ? { schemaName: args.schemaName } : {}),
          sql: postgresCatalogQuery(level, args, items.length), pageSize: PAGE_SIZE + 1,
        });
        // The native provider reports unavailable target databases as a successful
        // MCP response containing this connection error. Policy/guard errors are
        // isError and must never trigger a different target. No target is retried.
        if (level === "databases" && total === undefined && candidates.length && !result.isError
          && /^MCP tool 'execute_sql' failed: connection\.error\s*$/u.test(catalogPayload(result.value ?? result))) {
          databaseName = candidates.shift(); continue;
        }
        const page = parseLiveCatalog(result, level, args);
        if (total !== undefined && (page.total !== total || page.fingerprint !== fingerprint)) throw new CatalogError("Chat2DB 目录在分页期间发生变化；本次结果未通过核验，请重新读取。");
        total = page.total; fingerprint = page.fingerprint;
        if (page.items.length !== Math.min(PAGE_SIZE, total - items.length)) throw new CatalogError("Chat2DB 目录分页不完整。");
        bytes += Buffer.byteLength(JSON.stringify(page.items), "utf8");
        if (bytes > MAX_RESULT_BYTES) throw new CatalogError("Chat2DB 目录超过1MiB结果预算，未冻结不完整范围；此来源需要更聚焦的目录入口。");
        items.push(...page.items);
      } while (total === undefined || items.length < total);
      if (new Set(items.map((row) => row.name)).size !== total) throw new CatalogError("Chat2DB 目录分页出现重复或缺失项。");
      const live = { items, freshness: "LIVE", observed_at: new Date().toISOString(), fingerprint,
        datasource_id: String(args.dataSourceId), database: level === "databases" ? "" : databaseName,
        schema: level === "tables" ? args.schemaName : "", level,
        evidence: "Chat2DB execute_sql / PostgreSQL catalog / readonly" };
      return { kind: "accept", value: { content: [{ type: "text", text: JSON.stringify(live) }], structuredContent: live },
        additionalContexts: prior.additionalContexts };
    } catch (error) {
      // Let the native registry produce ABORTED. Blocking here would mask its
      // cancellation classifier with a generic post-execute error.
      if (execution.signal.aborted) return prior;
      if (signal.aborted) return { kind: "block", feedback: [{ type: "text", text: "Chat2DB 当前目录在15秒预算内未完成核验；未返回部分目录或旧缓存。" }] };
      // Do not relay provider errors which can contain a connection URL or SQL.
      const safe = error instanceof CatalogError ? error.message
        : "Chat2DB 当前目录核对未完成；请检查连接、权限和目录范围后重试。旧缓存未被标记为有效。";
      return { kind: "block", feedback: [{ type: "text", text: safe }] };
    }
  });
}
