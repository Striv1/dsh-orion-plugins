// Only transport reads may use this helper. Never wrap a mutation or query
// execution: retrying an uncertain write can create duplicate persisted assets.
const FAILURES = {
  EVIDENCE_REFERENCE_MISMATCH: [409, false, false, "证据引用与当前会话或发布身份不一致。"],
  EVIDENCE_UPSTREAM_NOT_FOUND: [404, false, false, "未找到与本次会话和发布身份一致的完整回执；原查询不会重跑。"],
  EVIDENCE_UPSTREAM_REJECTED: [502, false, false, "证据服务拒绝了读取请求；未展示部分回执。"],
  EVIDENCE_SERVICE_UNAVAILABLE: [503, true, true, "证据服务连接暂时中断，请重新读取；原查询不会重跑。"],
  EVIDENCE_READ_TIMEOUT: [504, true, false, "证据读取超时，请重新读取；原查询不会重跑。"],
  EVIDENCE_CONFIG_UNAVAILABLE: [503, false, false, "证据读取服务尚未配置完成。"],
  EVIDENCE_HASH_MISMATCH: [409, false, false, "完整证据回执哈希不一致，未展示详情。"],
  EVIDENCE_IDENTITY_MISMATCH: [409, false, false, "完整证据回执身份不一致，未展示详情。"],
  EVIDENCE_RELEASE_MISMATCH: [409, false, false, "完整证据回执发布版本不一致，未展示详情。"],
  EVIDENCE_RESPONSE_INVALID: [502, false, false, "证据服务返回的内容无效，未展示详情。"],
  EVIDENCE_LOG_CHANGED: [409, true, true, "会话记录仍在更新，请重新读取本次证据；未使用旧记录替代。"],
  EVIDENCE_LOG_UNSUPPORTED: [409, false, false, "当前会话日志格式无法完整解码，未展示不完整证据。"],
  EVIDENCE_READ_FAILED: [503, false, false, "当前证据读取失败，未展示不完整回执。"],
};

export class ReadOnlyProxyError extends Error {
  constructor(code) {
    const safeCode = Object.hasOwn(FAILURES, code) ? code : "EVIDENCE_READ_FAILED";
    const [status, retryable, transient, message] = FAILURES[safeCode];
    super(message);
    this.name = "ReadOnlyProxyError";
    Object.assign(this, { code: safeCode, status, retryable, transient });
  }
}

export function classifyReadOnlyFailure(error) {
  if (error instanceof ReadOnlyProxyError) return error;
  if (error?.message === "Harness session log changed while reading; retry the request") {
    return new ReadOnlyProxyError("EVIDENCE_LOG_CHANGED");
  }
  if (/^(?:Unsupported Harness session|Selected Harness runtime cannot decode|Selected Harness runtime lacks)/u.test(String(error?.message ?? ""))) {
    return new ReadOnlyProxyError("EVIDENCE_LOG_UNSUPPORTED");
  }
  if (error?.name === "TimeoutError" || error?.name === "AbortError") {
    return new ReadOnlyProxyError("EVIDENCE_READ_TIMEOUT");
  }
  const code = error?.cause?.code ?? error?.code;
  if (["ECONNRESET", "ECONNREFUSED", "EPIPE", "UND_ERR_SOCKET", "UND_ERR_CONNECT_TIMEOUT"].includes(code)) {
    return new ReadOnlyProxyError("EVIDENCE_SERVICE_UNAVAILABLE");
  }
  if (code === "ERR_INVALID_URL") return new ReadOnlyProxyError("EVIDENCE_CONFIG_UNAVAILABLE");
  if (error instanceof SyntaxError) return new ReadOnlyProxyError("EVIDENCE_RESPONSE_INVALID");
  return new ReadOnlyProxyError("EVIDENCE_READ_FAILED");
}

export function readOnlyFailureResponse(error) {
  const failure = classifyReadOnlyFailure(error);
  return { status: failure.status, body: { detail: failure.message, code: failure.code, retryable: failure.retryable } };
}

export async function retryReadOnly(operation, {
  maxRetries = 1, delayMs = 120, pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
} = {}) {
  if (!Number.isInteger(maxRetries) || maxRetries < 0 || maxRetries > 2
    || !Number.isFinite(delayMs) || delayMs < 0 || delayMs > 250) {
    throw new TypeError("Invalid bounded read retry budget");
  }
  for (let attempt = 0; ; attempt += 1) {
    try { return await operation(); }
    catch (error) {
      const failure = classifyReadOnlyFailure(error);
      if (!failure.transient || attempt >= maxRetries) throw failure;
      await pause(delayMs);
    }
  }
}
