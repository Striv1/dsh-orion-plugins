import { lstat, readdir } from "node:fs/promises";
import { execFile } from "node:child_process";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { promisify } from "node:util";

const SESSION_ID = /^session-[a-f0-9-]{36}$/;
const LOG_NAME = /^session(?:\.v([1-9][0-9]*))?\.jsonl(\.zstd)?$/;
const MAX_SUPPORTED_GENERATION = 4;
// Each codec generation is resolved from the selected Harness installation.
// 0.1.6-alpha.2 ships only V3; 0.1.7-rc.2 adds V4 (it re-exports V3 too).
const RELEASED_CODECS = {
  3: ["@deepseek-ai/dsh-session-format-v2-to-v3", "releasedV3SessionFormatCodec"],
  4: ["@deepseek-ai/dsh-session-format-v3-to-v4", "releasedV4SessionFormatCodec"],
};
const executeFile = promisify(execFile);

// Tool results changed shape at Session format V4 (Harness 0.1.7-rc.2):
// V3 and older wrap each result in a role:"user" message as
// content:[{type:"tool-result", toolCallId, content, isError?, structuredContent?}];
// V4 persists one role:"tool" message whose own toolCallId/content/isError are
// the result and rejects the wrapper. Return wrapper-equivalent blocks for both.
export function toolResultBlocks(event) {
  const message = event?.data?.message;
  if (!message || typeof message !== "object") return [];
  if (message.role === "tool") {
    if (typeof message.toolCallId !== "string") return [];
    return [{
      type: "tool-result", toolCallId: message.toolCallId,
      content: Array.isArray(message.content) ? message.content : [],
      isError: message.isError === true,
      ...(message.structuredContent !== undefined ? { structuredContent: message.structuredContent } : {}),
    }];
  }
  return Array.isArray(message.content) ? message.content.filter((item) => item?.type === "tool-result") : [];
}

export const toolResultBlock = (event) => toolResultBlocks(event)[0] ?? null;

// Resolve from the selected Harness installation, not a separately downloaded
// codec. The deployed plugin also lives beneath that runtime's node_modules.
export async function readHarnessSessionEvents(log, dshEntry = process.env.DSH_ENTRY) {
  if (!Number.isInteger(log.generation) || log.generation < 0 || log.generation > MAX_SUPPORTED_GENERATION) {
    throw new Error(`Unsupported Harness session generation: ${log.path}`);
  }
  const { stdout } = await executeFile("zstdcat", [log.path], {
    encoding: "utf8", maxBuffer: 64 * 1024 * 1024, timeout: 8000,
  });
  const rows = stdout.split(/\r?\n/u).filter((line) => line.trim()).map((line) => JSON.parse(line));
  if (log.generation < 3) return rows;
  const [header, ...physicalEvents] = rows;
  if (header?.type !== "session" || header.version !== log.generation || header.id !== log.sessionId) {
    throw new Error(`Harness V${log.generation} session header does not match selected log: ${log.path}`);
  }
  const runtimeRequire = createRequire(dshEntry ? resolve(dshEntry) : import.meta.url);
  // async so a synchronous require.resolve miss becomes a rejection we can label.
  const load = async (name) => import(pathToFileURL(runtimeRequire.resolve(name)).href);
  const [codecPackage, codecExport] = RELEASED_CODECS[log.generation];
  const [codecModule, { SessionFormatEventCollector }] = await Promise.all([
    load(codecPackage).catch((error) => {
      throw new Error(`Selected Harness runtime cannot decode session format V${log.generation} (${codecPackage}): ${error.message}`);
    }),
    load("@deepseek-ai/dsh-session-format"),
  ]);
  const codec = codecModule[codecExport];
  if (!codec || codec.version !== log.generation || typeof codec.createDecoder !== "function") {
    throw new Error(`Selected Harness runtime lacks ${codecExport} for session format V${log.generation}`);
  }
  // V3/V4 retain V2's physical provenance compression, so parsed JSONL rows are
  // not logical events. The official codec expands ranges and rejects malformed
  // envelopes/structural rows; never reinterpret them as older evidence.
  const decoder = codec.createDecoder(header, "strict");
  const collector = new SessionFormatEventCollector();
  for (const row of physicalEvents) decoder.decodeRow(row, collector);
  decoder.finish(collector);
  return collector.values;
}

// v0.1.3 replaces Session.events with an immutable snapshot. Take a fresh
// snapshot for each observation so newly appended delivery/status events count.
export function harnessSessionEvents(session) {
  const events = typeof session?.snapshotEvents === "function"
    ? session.snapshotEvents() : session?.events ?? [];
  if (!Array.isArray(events)) throw new Error("Harness session events must be an array");
  return events;
}

async function entries(path) {
  try { return await readdir(path, { withFileTypes: true }); }
  catch (error) {
    if (error.code === "ENOENT") return [];
    throw error;
  }
}

// Harness keeps migrated generations immutable. Always select the highest
// canonical generation, never a stale predecessor or a partially staged file.
// ORION's log readers use zstd; an unknown format must not become empty evidence.
async function selectSessionLog(sessionRoot, sessionId) {
  const generations = (await entries(sessionRoot)).flatMap((entry) => {
    const match = LOG_NAME.exec(entry.name);
    if (!entry.isFile() || !match) return [];
    const generation = Number(match[1] ?? 0);
    if (!Number.isSafeInteger(generation)) {
      throw new Error(`Unsupported Harness session generation: ${join(sessionRoot, entry.name)}`);
    }
    return [{ name: entry.name, generation, compressed: Boolean(match[2]) }];
  }).sort((left, right) => right.generation - left.generation);
  const selected = generations[0];
  if (!selected) return null;
  if (selected.generation > MAX_SUPPORTED_GENERATION || generations.some((item) => !item.compressed)) {
    throw new Error(`Unsupported Harness session log format: ${join(sessionRoot, selected.name)}`);
  }
  const path = join(sessionRoot, selected.name);
  const metadata = await lstat(path, { bigint: true }).catch((error) => {
    if (error.code === "ENOENT") return null;
    throw error;
  });
  if (!metadata?.isFile()) return null;
  return {
    path, sessionId, generation: selected.generation,
    mtimeMs: Number(metadata.mtimeNs) / 1e6, size: Number(metadata.size),
    // Nanosecond timestamps and inode identity also catch same-size rewrites
    // and atomic replacement where the original mtime has been preserved.
    fingerprint: [metadata.dev, metadata.ino, metadata.size, metadata.mtimeNs, metadata.ctimeNs].join(":"),
  };
}

export async function listHarnessSessionLogs(dshHome) {
  const root = join(resolve(dshHome), "sessions");
  const logs = [];
  for (const workspace of await entries(root)) {
    if (!workspace.isDirectory()) continue;
    const workspaceRoot = join(root, workspace.name);
    for (const session of await entries(workspaceRoot)) {
      if (!session.isDirectory() || !SESSION_ID.test(session.name)) continue;
      const log = await selectSessionLog(join(workspaceRoot, session.name), session.name);
      if (log) logs.push(log);
    }
  }
  return logs.sort((left, right) => right.mtimeMs - left.mtimeMs);
}

// A source lookup must not enumerate every other session and its generations.
// Read the workspace names, then address only the requested session directory.
// Do this on every read: a TTL here could conceal a newly migrated generation.
export async function findHarnessSessionLog(dshHome, sessionId) {
  if (!SESSION_ID.test(sessionId)) return null;
  const root = join(resolve(dshHome), "sessions");
  let latest = null;
  for (const workspace of await entries(root)) {
    if (!workspace.isDirectory()) continue;
    const sessionRoot = join(root, workspace.name, sessionId);
    const directory = await lstat(sessionRoot).catch((error) => {
      if (error.code === "ENOENT") return null;
      throw error;
    });
    if (!directory?.isDirectory()) continue;
    const log = await selectSessionLog(sessionRoot, sessionId);
    if (log && (!latest || log.mtimeMs > latest.mtimeMs)) latest = log;
  }
  return latest;
}

// Cache only decoded logical events. Release binding, source filtering and
// evidence projection belong to the caller and must run for every request.
export function createHarnessSessionEventReader({
  readEvents = readHarnessSessionEvents,
  maxEntries = 16, maxBytes = 16 * 1024 * 1024,
  maxEntryBytes = 2 * 1024 * 1024, maxCompressedBytes = 2 * 1024 * 1024,
} = {}) {
  const cache = new Map();
  const pending = new Map();
  let bytes = 0;
  const remove = (key) => {
    const item = cache.get(key);
    if (item) { bytes -= item.bytes; cache.delete(key); }
  };
  const identity = (log) => log ? `${log.path}:${log.generation}:${log.fingerprint}` : null;
  return async function readSession(dshHome, sessionId) {
    const home = resolve(dshHome);
    const dshEntry = process.env.DSH_ENTRY ? resolve(process.env.DSH_ENTRY) : undefined;
    const key = JSON.stringify([home, sessionId, dshEntry ?? import.meta.url]);
    const log = await findHarnessSessionLog(home, sessionId);
    if (!log) { remove(key); return []; }
    const stamp = identity(log);
    const existing = cache.get(key);
    if (existing?.stamp === stamp) {
      cache.delete(key); cache.set(key, existing);
      return existing.events;
    }
    remove(key);
    const inflightKey = `${key}:${stamp}`;
    if (pending.has(inflightKey)) return pending.get(inflightKey);
    const operation = (async () => {
      const events = await readEvents(log, dshEntry);
      // Never save or serve an observation known to have changed while decoding.
      const current = await findHarnessSessionLog(home, sessionId);
      if (identity(current) !== stamp) throw new Error("Harness session log changed while reading; retry the request");
      if (log.size <= maxCompressedBytes) {
        // A conservative serialized-size budget, not an exact V8 heap measure.
        const weight = Buffer.byteLength(JSON.stringify(events), "utf8") * 4;
        if (weight <= maxEntryBytes && weight <= maxBytes && maxEntries > 0) {
          while (cache.size >= maxEntries || bytes + weight > maxBytes) remove(cache.keys().next().value);
          remove(key);
          cache.set(key, { stamp, events, bytes: weight });
          bytes += weight;
        }
      }
      return events;
    })();
    // Bound retained in-flight entries too. Overflow reads still work uncached.
    if (pending.size < maxEntries) pending.set(inflightKey, operation);
    try { return await operation; }
    finally { if (pending.get(inflightKey) === operation) pending.delete(inflightKey); }
  };
}

export const readCachedHarnessSessionEvents = createHarnessSessionEventReader();
