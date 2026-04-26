/**
 * Bauhaus API — Cloudflare Worker serving stylized CC0 artwork from R2.
 *
 * Routes:
 *   GET  /api/today               → today's stylized image
 *   GET  /api/today.json          → today's metadata
 *   GET  /api/today.manifest.json → today's responsive manifest
 *   GET  /api/:date               → stylized image for YYYY-MM-DD
 *   GET  /api/:date/original      → original unstylized image
 *   GET  /api/:date.json          → metadata for date
 *   GET  /api/:date.manifest.json → responsive manifest for date
 *   POST /api/vitals              → ingest Web Vitals RUM (Analytics Engine)
 *   POST /api/err                 → ingest JS error RUM (Analytics Engine)
 *
 * Format negotiation:
 *   ?format=auto|jpeg|avif|webp overrides Accept-header negotiation.
 *   Worker inspects Accept header to pick the best pre-generated variant
 *   (AVIF > WebP > JPEG) and falls back to JPEG when a variant is missing.
 *
 * Query parameters:
 *   ?progressive=true    → serve progressive JPEG variant (falls back to baseline)
 *   ?strip=true          → serve EXIF-stripped JPEG variant (falls back to original)
 */

interface Env {
  BUCKET: R2Bucket;
  WEB_VITALS: AnalyticsEngineDataset;
  WEB_ERRORS: AnalyticsEngineDataset;
  ALLOWED_ORIGINS: string;
}

/** Supported image formats in negotiation priority order: AVIF > WebP > JPEG. */
type ImageFormat = "avif" | "webp" | "jpeg";

const FORMAT_EXT: Record<ImageFormat, string> = {
  avif: ".avif",
  webp: ".webp",
  jpeg: ".jpg",
};

const FORMAT_CONTENT_TYPE: Record<ImageFormat, string> = {
  avif: "image/avif",
  webp: "image/webp",
  jpeg: "image/jpeg",
};

export function negotiateFormat(request: Request, url: URL): ImageFormat {
  const param = url.searchParams.get("format")?.toLowerCase();
  if (param === "avif") return "avif";
  if (param === "webp") return "webp";
  if (param === "jpeg") return "jpeg";

  // ?format=auto or absent → negotiate via Accept header
  const accept = request.headers.get("Accept") ?? "";
  if (accept.includes("image/avif")) return "avif";
  if (accept.includes("image/webp")) return "webp";
  return "jpeg";
}

function datePath(dateStr: string): string {
  // YYYY-MM-DD → YYYY/MM/DD
  const [y, m, d] = dateStr.split("-");
  return `${y}/${m}/${d}`;
}

async function getToday(bucket: R2Bucket): Promise<string> {
  const obj = await bucket.get("latest.json");
  if (!obj) throw new Error("No latest.json found");
  const data: { date: string } = await obj.json();
  return data.date;
}

function isProgressive(url: URL): boolean {
  return url.searchParams.get("progressive") === "true";
}

function isStrip(url: URL): boolean {
  return url.searchParams.get("strip") === "true";
}

function corsHeaders(): HeadersInit {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
  };
}

// ---------------------------------------------------------------------------
// Telemetry helpers
// ---------------------------------------------------------------------------

const TELEMETRY_BODY_LIMIT = 4096;
const TELEMETRY_ORIGINS_DEFAULT =
  "https://kevintcoughlin.com,https://www.kevintcoughlin.com";

function getAllowedOrigins(env: Env): Set<string> {
  const raw = env.ALLOWED_ORIGINS ?? TELEMETRY_ORIGINS_DEFAULT;
  return new Set(raw.split(",").map((s) => s.trim()).filter(Boolean));
}

function telemetryCorsHeaders(origin: string): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST",
    "Access-Control-Allow-Headers": "content-type",
  };
}

function classifyUA(ua: string): "mobile" | "desktop" {
  return /Mobile|Android|iPhone|iPad/i.test(ua) ? "mobile" : "desktop";
}

async function handleTelemetry(
  request: Request,
  env: Env,
  path: string,
): Promise<Response> {
  const origin = request.headers.get("Origin") ?? "";
  const allowedOrigins = getAllowedOrigins(env);

  // Handle OPTIONS preflight
  if (request.method === "OPTIONS") {
    if (!allowedOrigins.has(origin)) {
      return new Response(null, { status: 403 });
    }
    return new Response(null, {
      status: 204,
      headers: telemetryCorsHeaders(origin),
    });
  }

  // Only POST accepted
  if (request.method !== "POST") {
    return new Response("Method Not Allowed", { status: 405 });
  }

  // Validate origin
  if (!allowedOrigins.has(origin)) {
    return new Response("Forbidden", { status: 403 });
  }

  // Reject oversized requests early via Content-Length
  const contentLength = parseInt(request.headers.get("content-length") ?? "0", 10);
  if (contentLength > TELEMETRY_BODY_LIMIT) {
    return new Response("Payload Too Large", {
      status: 413,
      headers: telemetryCorsHeaders(origin),
    });
  }

  // Read and size-check the body
  const body = await request.text();
  if (body.length > TELEMETRY_BODY_LIMIT) {
    return new Response("Payload Too Large", {
      status: 413,
      headers: telemetryCorsHeaders(origin),
    });
  }

  let data: Record<string, unknown>;
  try {
    data = JSON.parse(body);
  } catch {
    return new Response("Bad Request", {
      status: 400,
      headers: telemetryCorsHeaders(origin),
    });
  }

  const ua = request.headers.get("User-Agent") ?? "";
  const uaClass = classifyUA(ua);

  if (path === "/api/vitals") {
    let host = "";
    let urlPath = "";
    try {
      const pageUrl = new URL(String(data.url ?? ""));
      host = pageUrl.hostname;
      urlPath = pageUrl.pathname;
    } catch {
      // malformed URL — store empty strings
    }

    env.WEB_VITALS.writeDataPoint({
      blobs: [
        String(data.name ?? ""),
        String(data.rating ?? ""),
        String(data.navigationType ?? ""),
        host,
        urlPath,
        uaClass,
      ],
      doubles: [Number(data.value ?? 0)],
      indexes: [host],
    });
  } else {
    // /api/err
    let host = "";
    let urlPath = "";
    try {
      const sourceUrl = new URL(String(data.source ?? ""));
      host = sourceUrl.hostname;
      urlPath = sourceUrl.pathname;
    } catch {
      // malformed URL — store empty strings
    }

    env.WEB_ERRORS.writeDataPoint({
      blobs: [
        String(data.message ?? ""),
        String(data.source ?? ""),
        host,
        urlPath,
        uaClass,
      ],
      doubles: [Number(data.lineno ?? 0), Number(data.colno ?? 0)],
      indexes: [host],
    });
  }

  return new Response(null, {
    status: 204,
    headers: telemetryCorsHeaders(origin),
  });
}

async function getImageObject(
  bucket: R2Bucket,
  basePath: string,
  format: ImageFormat,
  progressive: boolean = false,
  strip: boolean = false,
): Promise<{ obj: R2ObjectBody; contentType: string } | null> {
  // For JPEG with ?progressive=true, try the progressive variant first
  if (format === "jpeg" && progressive) {
    const obj = await bucket.get(`${basePath}.progressive.jpg`);
    if (obj) return { obj, contentType: "image/jpeg" };
  }

  // For JPEG with ?strip=true, try the stripped variant first
  if (strip) {
    const stripped = await bucket.get(`${basePath}.stripped.jpg`);
    if (stripped) return { obj: stripped, contentType: "image/jpeg" };
  }

  // Try the negotiated format
  const key = `${basePath}${FORMAT_EXT[format]}`;
  const obj = await bucket.get(key);
  if (obj) return { obj, contentType: FORMAT_CONTENT_TYPE[format] };

  // Fall back to JPEG if the requested format is unavailable
  if (format !== "jpeg") {
    const fallback = await bucket.get(`${basePath}.jpg`);
    if (fallback) return { obj: fallback, contentType: "image/jpeg" };
  }

  return null;
}

/**
 * Resolves which R2 key and content-type would be served for a given image
 * request, using metadata-only head() calls (Class A ops) instead of get().
 * Used for If-None-Match checks to avoid reading the full object body.
 */
async function headImageObject(
  bucket: R2Bucket,
  basePath: string,
  format: ImageFormat,
  progressive = false,
  strip = false,
): Promise<{ head: R2Object; key: string; contentType: string } | null> {
  if (format === "jpeg" && progressive) {
    const key = `${basePath}.progressive.jpg`;
    const h = await bucket.head(key);
    if (h) return { head: h, key, contentType: "image/jpeg" };
  }

  if (strip) {
    const key = `${basePath}.stripped.jpg`;
    const h = await bucket.head(key);
    if (h) return { head: h, key, contentType: "image/jpeg" };
  }

  const key = `${basePath}${FORMAT_EXT[format]}`;
  const h = await bucket.head(key);
  if (h) return { head: h, key, contentType: FORMAT_CONTENT_TYPE[format] };

  if (format !== "jpeg") {
    const fallbackKey = `${basePath}.jpg`;
    const fh = await bucket.head(fallbackKey);
    if (fh) return { head: fh, key: fallbackKey, contentType: "image/jpeg" };
  }

  return null;
}

/** Returns true when the If-None-Match header value matches the given ETag. */
function normalizeEtag(etag: string): string {
  let value = etag.trim();
  if (value.startsWith("W/")) {
    value = value.slice(2).trim();
  }
  return value;
}

function etagMatches(ifNoneMatch: string, httpEtag: string): boolean {
  if (ifNoneMatch === "*") return true;
  const target = normalizeEtag(httpEtag);
  return ifNoneMatch
    .split(",")
    .some((e) => normalizeEtag(e) === target);
}

/** Cache-control for date-specific resources — immutable since content never changes. */
const IMMUTABLE_CACHE = "public, max-age=31536000, s-maxage=31536000, immutable";

/** Cache-control for /api/today* — short-lived since it resolves to a new date each day. */
const TODAY_CACHE = "public, max-age=300, s-maxage=300, stale-while-revalidate=60";

function imageResponse(obj: R2ObjectBody, contentType: string, today = false): Response {
  const variant = obj.key?.endsWith(".progressive.jpg") ? "progressive" : "baseline";
  const headers: Record<string, string> = {
    "Content-Type": contentType,
    "Cache-Control": today ? TODAY_CACHE : (obj.httpMetadata?.cacheControl ?? IMMUTABLE_CACHE),
    "Vary": "Accept",
    "X-Variant": variant,
    ...corsHeaders(),
  };
  if (obj.httpEtag) headers["ETag"] = obj.httpEtag;
  return new Response(obj.body, { headers });
}

function jsonResponse(obj: R2ObjectBody, today = false): Response {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "Cache-Control": today ? TODAY_CACHE : (obj.httpMetadata?.cacheControl ?? IMMUTABLE_CACHE),
    ...corsHeaders(),
  };
  if (obj.httpEtag) headers["ETag"] = obj.httpEtag;
  return new Response(obj.body, { headers });
}

function notModified(etag: string): Response {
  return new Response(null, {
    status: 304,
    headers: { "ETag": etag, ...corsHeaders() },
  });
}

function notFound(msg: string): Response {
  return new Response(JSON.stringify({ error: msg }), {
    status: 404,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}

/**
 * Serves an image response, using R2 head() for If-None-Match checks to avoid
 * reading the full object body when a 304 Not Modified response is appropriate.
 */
async function serveImage(
  request: Request,
  bucket: R2Bucket,
  basePath: string,
  format: ImageFormat,
  progressive: boolean,
  strip: boolean,
  today: boolean,
  notFoundMsg: string,
): Promise<Response> {
  const ifNoneMatch = request.headers.get("If-None-Match");
  if (ifNoneMatch) {
    const headResult = await headImageObject(bucket, basePath, format, progressive, strip);
    if (!headResult) return notFound(notFoundMsg);
    if (etagMatches(ifNoneMatch, headResult.head.httpEtag)) {
      return notModified(headResult.head.httpEtag);
    }
    // ETag doesn't match — fetch the full object using the already-resolved key
    const obj = await bucket.get(headResult.key);
    if (!obj) return notFound(notFoundMsg);
    return imageResponse(obj, headResult.contentType, today);
  }

  const result = await getImageObject(bucket, basePath, format, progressive, strip);
  if (!result) return notFound(notFoundMsg);
  return imageResponse(result.obj, result.contentType, today);
}

/**
 * Serves a JSON response, using R2 head() for If-None-Match checks to avoid
 * reading the full object body when a 304 Not Modified response is appropriate.
 */
async function serveJson(
  request: Request,
  bucket: R2Bucket,
  key: string,
  today: boolean,
  notFoundMsg: string,
): Promise<Response> {
  const ifNoneMatch = request.headers.get("If-None-Match");
  if (ifNoneMatch) {
    const head = await bucket.head(key);
    if (!head) return notFound(notFoundMsg);
    if (etagMatches(ifNoneMatch, head.httpEtag)) {
      return notModified(head.httpEtag);
    }
    const obj = await bucket.get(key);
    if (!obj) return notFound(notFoundMsg);
    return jsonResponse(obj, today);
  }

  const obj = await bucket.get(key);
  if (!obj) return notFound(notFoundMsg);
  return jsonResponse(obj, today);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const progressive = isProgressive(url);
    const strip = isStrip(url);

    // Telemetry endpoints — handled separately (POST only, origin-gated CORS)
    if (path === "/api/vitals" || path === "/api/err") {
      return handleTelemetry(request, env, path);
    }

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    if (request.method !== "GET") {
      return new Response("Method not allowed", { status: 405 });
    }

    const format = negotiateFormat(request, url);

    // GET /api/today → stylized image
    if (path === "/api/today") {
      const today = await getToday(env.BUCKET);
      return serveImage(request, env.BUCKET, `stylized/${datePath(today)}`, format, progressive, strip, true, "No image for today");
    }

    // GET /api/today.json → metadata
    if (path === "/api/today.json") {
      const today = await getToday(env.BUCKET);
      return serveJson(request, env.BUCKET, `metadata/${datePath(today)}.json`, true, "No metadata for today");
    }

    // GET /api/today.manifest.json → responsive manifest
    if (path === "/api/today.manifest.json") {
      const today = await getToday(env.BUCKET);
      return serveJson(request, env.BUCKET, `manifests/${datePath(today)}.json`, true, "No manifest for today");
    }

    // GET /api/:date.manifest.json → responsive manifest for date
    const manifestMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\.manifest\.json$/);
    if (manifestMatch) {
      return serveJson(request, env.BUCKET, `manifests/${datePath(manifestMatch[1])}.json`, false, `No manifest for ${manifestMatch[1]}`);
    }

    // GET /api/:date.json → metadata for date
    const jsonMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\.json$/);
    if (jsonMatch) {
      return serveJson(request, env.BUCKET, `metadata/${datePath(jsonMatch[1])}.json`, false, `No metadata for ${jsonMatch[1]}`);
    }

    // GET /api/:date/original → original image
    const origMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\/original$/);
    if (origMatch) {
      return serveImage(request, env.BUCKET, `originals/${datePath(origMatch[1])}`, format, progressive, strip, false, `No original for ${origMatch[1]}`);
    }

    // GET /api/:date → stylized image for date
    const dateMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})$/);
    if (dateMatch) {
      return serveImage(request, env.BUCKET, `stylized/${datePath(dateMatch[1])}`, format, progressive, strip, false, `No image for ${dateMatch[1]}`);
    }

    return notFound("Not found. Try /api/today or /api/YYYY-MM-DD");
  },
};
