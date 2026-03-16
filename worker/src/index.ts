/**
 * Bauhaus API — Cloudflare Worker serving stylized CC0 artwork from R2.
 *
 * Routes:
 *   GET /api/today            → today's stylized image (content-negotiated)
 *   GET /api/today.json       → today's metadata
 *   GET /api/today.avif       → today's stylized image (AVIF)
 *   GET /api/today.webp       → today's stylized image (WebP)
 *   GET /api/:date            → stylized image for YYYY-MM-DD (content-negotiated)
 *   GET /api/:date/original   → original unstylized image
 *   GET /api/:date.json       → metadata for date
 *   GET /api/:date.avif       → stylized image (AVIF) for date
 *   GET /api/:date.webp       → stylized image (WebP) for date
 */

interface Env {
  BUCKET: R2Bucket;
}

type ImageFormat = "avif" | "webp" | "jpg";

const FORMAT_CONTENT_TYPES: Record<ImageFormat, string> = {
  avif: "image/avif",
  webp: "image/webp",
  jpg: "image/jpeg",
};

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

function preferredFormat(request: Request): ImageFormat {
  const accept = request.headers.get("Accept") || "";
  if (accept.includes("image/avif")) return "avif";
  if (accept.includes("image/webp")) return "webp";
  return "jpg";
}

function corsHeaders(): HeadersInit {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
  };
}

function imageResponse(obj: R2ObjectBody, format: ImageFormat = "jpg"): Response {
  return new Response(obj.body, {
    headers: {
      "Content-Type": obj.httpMetadata?.contentType ?? FORMAT_CONTENT_TYPES[format],
      "Cache-Control": obj.httpMetadata?.cacheControl ?? "public, max-age=86400, s-maxage=86400, stale-while-revalidate=3600",
      Vary: "Accept",
      ...corsHeaders(),
    },
  });
}

function jsonResponse(obj: R2ObjectBody): Response {
  return new Response(obj.body, {
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": obj.httpMetadata?.cacheControl ?? "public, max-age=86400, s-maxage=86400, stale-while-revalidate=3600",
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

async function getStylizedImage(
  bucket: R2Bucket,
  dp: string,
  format: ImageFormat,
): Promise<{ obj: R2ObjectBody; format: ImageFormat } | null> {
  // Try requested format first, fall back to JPEG
  const formats: ImageFormat[] = format === "jpg" ? ["jpg"] : [format, "jpg"];
  for (const fmt of formats) {
    const obj = await bucket.get(`stylized/${dp}.${fmt}`);
    if (obj) return { obj, format: fmt };
  }
  return null;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    if (request.method !== "GET") {
      return new Response("Method not allowed", { status: 405 });
    }

    // GET /api/today.avif or /api/today.webp → explicit format
    const todayFmtMatch = path.match(/^\/api\/today\.(avif|webp)$/);
    if (todayFmtMatch) {
      const fmt = todayFmtMatch[1] as ImageFormat;
      const today = await getToday(env.BUCKET);
      const result = await getStylizedImage(env.BUCKET, datePath(today), fmt);
      if (!result) return notFound("No image for today");
      return imageResponse(result.obj, result.format);
    }

    // GET /api/today → stylized image (content-negotiated)
    if (path === "/api/today") {
      const today = await getToday(env.BUCKET);
      const fmt = preferredFormat(request);
      const result = await getStylizedImage(env.BUCKET, datePath(today), fmt);
      if (!result) return notFound("No image for today");
      return imageResponse(result.obj, result.format);
    }

    // GET /api/today.json → metadata
    if (path === "/api/today.json") {
      const today = await getToday(env.BUCKET);
      const obj = await env.BUCKET.get(`metadata/${datePath(today)}.json`);
      if (!obj) return notFound("No metadata for today");
      return jsonResponse(obj);
    }

    // GET /api/:date.json → metadata for date
    const jsonMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\.json$/);
    if (jsonMatch) {
      const obj = await env.BUCKET.get(`metadata/${datePath(jsonMatch[1])}.json`);
      if (!obj) return notFound(`No metadata for ${jsonMatch[1]}`);
      return jsonResponse(obj);
    }

    // GET /api/:date.avif or /api/:date.webp → explicit format
    const dateFmtMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\.(avif|webp)$/);
    if (dateFmtMatch) {
      const fmt = dateFmtMatch[2] as ImageFormat;
      const result = await getStylizedImage(env.BUCKET, datePath(dateFmtMatch[1]), fmt);
      if (!result) return notFound(`No image for ${dateFmtMatch[1]}`);
      return imageResponse(result.obj, result.format);
    }

    // GET /api/:date/original → original image
    const origMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\/original$/);
    if (origMatch) {
      const obj = await env.BUCKET.get(`originals/${datePath(origMatch[1])}.jpg`);
      if (!obj) return notFound(`No original for ${origMatch[1]}`);
      return imageResponse(obj);
    }

    // GET /api/:date → stylized image for date (content-negotiated)
    const dateMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})$/);
    if (dateMatch) {
      const fmt = preferredFormat(request);
      const result = await getStylizedImage(env.BUCKET, datePath(dateMatch[1]), fmt);
      if (!result) return notFound(`No image for ${dateMatch[1]}`);
      return imageResponse(result.obj, result.format);
    }

    return notFound("Not found. Try /api/today or /api/YYYY-MM-DD");
  },
};
