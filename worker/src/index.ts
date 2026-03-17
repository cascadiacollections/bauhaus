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

/** Cache-control for date-specific resources — immutable since content never changes. */
const IMMUTABLE_CACHE = "public, max-age=31536000, s-maxage=31536000, immutable";

/** Cache-control for /api/today* — short-lived since it resolves to a new date each day. */
const TODAY_CACHE = "public, max-age=300, s-maxage=300, stale-while-revalidate=60";

function imageResponse(obj: R2ObjectBody, contentType: string, today = false): Response {
  const variant = obj.key?.endsWith(".progressive.jpg") ? "progressive" : "baseline";
  return new Response(obj.body, {
    headers: {
      "Content-Type": contentType,
      "Cache-Control": today ? TODAY_CACHE : (obj.httpMetadata?.cacheControl ?? IMMUTABLE_CACHE),
      "Vary": "Accept",
      "X-Variant": variant,
      ...corsHeaders(),
    },
  });
}

function jsonResponse(obj: R2ObjectBody, today = false): Response {
  return new Response(obj.body, {
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": today ? TODAY_CACHE : (obj.httpMetadata?.cacheControl ?? IMMUTABLE_CACHE),
      ...corsHeaders(),
    },
  });
}

function notFound(msg: string): Response {
  return new Response(JSON.stringify({ error: msg }), {
    status: 404,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
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
      const result = await getImageObject(env.BUCKET, `stylized/${datePath(today)}`, format, progressive, strip);
      if (!result) return notFound("No image for today");
      return imageResponse(result.obj, result.contentType, true);
    }

    // GET /api/today.json → metadata
    if (path === "/api/today.json") {
      const today = await getToday(env.BUCKET);
      const obj = await env.BUCKET.get(`metadata/${datePath(today)}.json`);
      if (!obj) return notFound("No metadata for today");
      return jsonResponse(obj, true);
    }

    // GET /api/today.manifest.json → responsive manifest
    if (path === "/api/today.manifest.json") {
      const today = await getToday(env.BUCKET);
      const obj = await env.BUCKET.get(`manifests/${datePath(today)}.json`);
      if (!obj) return notFound("No manifest for today");
      return jsonResponse(obj, true);
    }

    // GET /api/:date.manifest.json → responsive manifest for date
    const manifestMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\.manifest\.json$/);
    if (manifestMatch) {
      const obj = await env.BUCKET.get(`manifests/${datePath(manifestMatch[1])}.json`);
      if (!obj) return notFound(`No manifest for ${manifestMatch[1]}`);
      return jsonResponse(obj);
    }

    // GET /api/:date.json → metadata for date
    const jsonMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\.json$/);
    if (jsonMatch) {
      const obj = await env.BUCKET.get(`metadata/${datePath(jsonMatch[1])}.json`);
      if (!obj) return notFound(`No metadata for ${jsonMatch[1]}`);
      return jsonResponse(obj);
    }

    // GET /api/:date/original → original image
    const origMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\/original$/);
    if (origMatch) {
      const result = await getImageObject(env.BUCKET, `originals/${datePath(origMatch[1])}`, format, progressive, strip);
      if (!result) return notFound(`No original for ${origMatch[1]}`);
      return imageResponse(result.obj, result.contentType);
    }

    // GET /api/:date → stylized image for date
    const dateMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})$/);
    if (dateMatch) {
      const result = await getImageObject(env.BUCKET, `stylized/${datePath(dateMatch[1])}`, format, progressive, strip);
      if (!result) return notFound(`No image for ${dateMatch[1]}`);
      return imageResponse(result.obj, result.contentType);
    }

    return notFound("Not found. Try /api/today or /api/YYYY-MM-DD");
  },
};
