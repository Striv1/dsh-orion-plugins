export const MIME = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json",
  ".owl": "application/rdf+xml; charset=utf-8",
  ".png": "image/png",
  ".rdf": "application/rdf+xml; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ttl": "text/turtle; charset=utf-8",
  ".ttf": "font/ttf",
  ".webmanifest": "application/manifest+json",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

export const jsonResponse = (response, status, payload, method = "GET") => {
  response.writeHead(status, {
    "cache-control": "no-store",
    "content-type": "application/json; charset=utf-8",
  });
  response.end(method === "HEAD" ? undefined : JSON.stringify(payload));
};

export const readRequestJson = async (request) => {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 16 * 1024) throw new Error("请求内容过大");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
};
