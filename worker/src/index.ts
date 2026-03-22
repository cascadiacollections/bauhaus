/**
 * Bauhaus API — Cloudflare Worker serving stylized CC0 artwork from R2.
 *
 * Routes:
 *   GET /api/today               → today's stylized image
 *   GET /api/today.json          → today's metadata
 *   GET /api/today.manifest.json → today's responsive manifest
 *   GET /api/:date               → stylized image for YYYY-MM-DD
 *   GET /api/:date/original      → original unstylized image
 *   GET /api/:date.json          → metadata for date
 *   GET /api/:date.manifest.json → responsive manifest for date
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
function etagMatches(ifNoneMatch: string, httpEtag: string): boolean {
  if (ifNoneMatch === "*") return true;
  return ifNoneMatch.split(",").map((e) => e.trim()).includes(httpEtag);
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
