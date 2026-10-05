import { randomUUID } from "node:crypto";
import { catalogPayload } from "./chat2db-live-catalog.js";

const datasourceArray = (value) => {
  if (Array.isArray(value)) {
    if (value.some((item) => item?.type === "text" && typeof item.text === "string")) {
      return value.flatMap((item) => item?.type === "text" ? datasourceArray(item.text) : []);
    }
    return value;
  }
  if (typeof value === "string") {
    const normalized = value.trim();
    if (!normalized) return [];
    try {
      const parsed = JSON.parse(normalized);
      if (parsed !== value) return datasourceArray(parsed);
    } catch { /* Chat2DB 的精简文本格式在下面解析。 */ }
    return normalized.split(/\r?\n/u).map((line) => {
      const row = {};
      for (const part of line.split(";")) {
        const separator = part.indexOf("=");
        if (separator <= 0) continue;
        row[part.slice(0, separator).trim()] = part.slice(separator + 1).trim();
      }
      return row;
    }).filter((row) => Object.keys(row).length > 0);
  }
  if (!value || typeof value !== "object") return [];
  for (const key of ["datasources", "dataSources", "data", "items", "records", "result", "rows", "content", "text"]) {
    const nested = datasourceArray(value[key]);
    if (nested.length) return nested;
  }
  return [];
};

const normalizeDatasourceRows = (value) => datasourceArray(value).map((item, index) => {
  const row = item && typeof item === "object" ? item : { name: String(item ?? "") };
  const id = String(
    row.id ?? row.datasourceId ?? row.dataSourceId ?? row.alias ?? row.name ?? `datasource-${index + 1}`,
  ).trim();
  const label = String(
    row.name ?? row.alias ?? row.label ?? row.url ?? row.host ?? id,
  ).trim();
  const rawTables = row.tables ?? row.tableNames ?? row.table_scope ?? row.tableScope ?? [];
  const tableMetadata = Array.isArray(rawTables)
    ? rawTables.map((table) => {
      const source = table && typeof table === "object" ? table : { name: table };
      const name = String(source.name ?? source.tableName ?? source.table_name ?? "").trim();
      return {
        name,
        schema: String(source.schema ?? source.schemaName ?? source.tableSchema ?? "").trim(),
        label: String(source.label ?? source.comment ?? source.description ?? "").trim(),
        columns: Number(source.columns ?? source.columnCount ?? source.column_count ?? 0) || null,
        row_estimate: Number(source.rowEstimate ?? source.row_count ?? source.rows ?? 0) || null,
      };
    }).filter((table) => table.name)
    : [];
  const tables = tableMetadata.map((table) => table.schema && !table.name.includes(".")
    ? `${table.schema}.${table.name}`
    : table.name);
  const rawSchemas = row.schemas ?? row.schemaNames ?? row.schema_names ?? [];
  const schemas = Array.isArray(rawSchemas)
    ? rawSchemas.map((schema) => String(schema?.name ?? schema?.schemaName ?? schema).trim()).filter(Boolean)
    : [];
  return {
    id,
    label,
    type: String(row.type ?? row.dbType ?? row.databaseType ?? "").trim(),
    database: String(row.database ?? row.databaseName ?? row.schema ?? "").trim(),
    environment: String(row.environment ?? row.env ?? "").trim(),
    status: String(row.status ?? row.connectionStatus ?? "").trim(),
    schema_count: Number(row.schemaCount ?? row.schema_count ?? schemas.length) || null,
    table_count: Number(row.tableCount ?? row.table_count ?? tables.length) || null,
    schemas,
    tables,
    table_metadata: tableMetadata,
  };
}).filter((item) => item.id && item.label);

export async function loadWorkflowDatasources(context) {
  const tools = context.tools ?? context.get("tools");
  const toolName = "mcp__chat2db__list_all_datasources";
  const mounted = tools?.schemas?.().some((schema) => schema.name === toolName);
  if (!mounted) {
    throw new Error("Chat2DB MCP 当前未连接，无法读取真实数据源；请先恢复 Chat2DB 服务后重试。");
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const result = await tools.execute({
      callId: `workflow-datasources-${randomUUID()}`,
      name: toolName,
      arguments: {},
      signal: controller.signal,
    });
    if (result.isError) {
      const detail = result.error?.message
        ?? result.content?.find((item) => item.type === "text")?.text
        ?? "Chat2DB 未能返回数据源清单";
      throw new Error(String(detail));
    }
    let value = result.value;
    if (typeof value === "string") {
      try { value = JSON.parse(value); } catch { /* 保留原值，下面按空清单处理。 */ }
    }
    if (!datasourceArray(value).length) {
      const textContent = result.content?.find((item) => item.type === "text")?.text;
      if (textContent) {
        try { value = JSON.parse(textContent); } catch { /* 工具可能返回自然语言空态。 */ }
      }
    }
    return normalizeDatasourceRows(value);
  } finally {
    clearTimeout(timeout);
  }
}

const chat2dbCatalogRows = (value, level) => {
  value = catalogPayload(value);
  if (value?.provider_catalog !== undefined) value = value.provider_catalog;
  const keysByLevel = {
    databases: ["databaseName", "database", "name", "label"],
    schemas: ["schemaName", "schema", "name", "label"],
    tables: ["tableName", "table", "name", "label"],
  };
  const keys = keysByLevel[level] ?? ["name", "label"];
  let sourceRows = datasourceArray(value);
  if (!sourceRows.length && typeof value === "string") {
    let source = value.trim();
    for (let index = 0; index < 2; index += 1) {
      try {
        const parsed = JSON.parse(source);
        if (typeof parsed !== "string") break;
        source = parsed;
      } catch { break; }
    }
    sourceRows = source.split(/\r?\n/u).map((line) => {
      const match = line.trim().match(/^(.*?)\s+\[([^\]]+)\]$/u);
      return match ? { name: match[1].trim(), type: match[2].trim() } : { name: line.trim() };
    }).filter((item) => item.name);
  }
  const seen = new Set();
  return sourceRows.flatMap((item) => {
    const row = item && typeof item === "object" ? item : { name: String(item ?? "") };
    const name = String(keys.map((key) => row[key]).find((candidate) => candidate != null) ?? "").trim();
    if (!name || seen.has(name)) return [];
    seen.add(name);
    return [{
      id: name,
      name,
      comment: String(row.comment ?? row.description ?? row.label ?? "").trim(),
      type: String(row.type ?? row.dataType ?? row.tableType ?? "").trim(),
      columns: Number(row.columnCount ?? row.column_count ?? row.columns ?? 0) || null,
      row_estimate: Number(row.rowEstimate ?? row.row_count ?? row.rows ?? 0) || null,
    }];
  });
};

export async function loadWorkflowChat2dbCatalog(context, level, parameters) {
  const toolByLevel = {
    databases: "mcp__chat2db__list_all_databases",
    schemas: "mcp__chat2db__list_all_schemas",
    tables: "mcp__chat2db__list_all_tables",
  };
  const toolName = toolByLevel[level];
  if (!toolName) throw new Error("不支持的 Chat2DB 元数据层级");
  const tools = context.tools ?? context.get("tools");
  const mounted = tools?.schemas?.().some((schema) => schema.name === toolName);
  if (!mounted) throw new Error(`Chat2DB MCP 未挂载 ${toolName.split("__").at(-1)}，请恢复连接后重试。`);
  const datasourceId = String(parameters.datasourceId ?? "").trim();
  if (!datasourceId) throw new Error("缺少 Chat2DB 数据源编号");
  const argumentsPayload = {
    dataSourceId: /^\d+$/u.test(datasourceId) ? Number(datasourceId) : datasourceId,
  };
  if (level === "schemas" || level === "tables") {
    const databaseName = String(parameters.databaseName ?? "").trim();
    if (!databaseName) throw new Error("缺少数据库名称");
    if (level === "schemas") argumentsPayload.targetDatabaseName = databaseName;
    else argumentsPayload.databaseName = databaseName;
  }
  if (level === "tables") {
    const schemaName = String(parameters.schemaName ?? "").trim();
    if (!schemaName) throw new Error("缺少 Schema 名称");
    argumentsPayload.schemaName = schemaName;
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const result = await tools.execute({
      callId: `workflow-chat2db-${level}-${randomUUID()}`,
      name: toolName,
      arguments: argumentsPayload,
      signal: controller.signal,
    });
    if (result.isError) {
      const detail = result.error?.message
        ?? result.content?.find((item) => item.type === "text")?.text
        ?? `Chat2DB 未能返回${level}`;
      throw new Error(String(detail));
    }
    const payload = catalogPayload(result.value ?? { content: result.content });
    return {
      items: chat2dbCatalogRows(payload, level),
      freshness: payload?.freshness ?? "PROVIDER_CACHE_UNVERIFIED",
      observed_at: payload?.observed_at ?? null,
      warning: payload?.warning ?? "",
    };
  } finally {
    clearTimeout(timeout);
  }
}
