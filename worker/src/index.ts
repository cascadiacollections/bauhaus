/**
 * Bauhaus API — Cloudflare Worker serving stylized CC0 artwork from R2.
 *
 * Routes:
 *   GET /api/today       → today's stylized image
 *   GET /api/today.json  → today's metadata
 *   GET /api/:date       → stylized image for YYYY-MM-DD
 *   GET /api/:date/original → original unstylized image
 *   GET /api/:date.json  → metadata for date
 *
 * Query parameters:
 *   ?progressive=true    → serve progressive JPEG variant (falls back to baseline)
 */

interface Env {
  BUCKET: R2Bucket;
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

function corsHeaders(): HeadersInit {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
  };
}

function imageResponse(obj: R2ObjectBody): Response {
  const variant = obj.key && obj.key.endsWith(".progressive.jpg") ? "progressive" : "baseline";
  return new Response(obj.body, {
    headers: {
      "Content-Type": obj.httpMetadata?.contentType ?? "image/jpeg",
      "Cache-Control": obj.httpMetadata?.cacheControl ?? "public, max-age=86400, s-maxage=86400, stale-while-revalidate=3600",
      "X-Variant": variant,
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

/** Fetch an image from R2, trying the progressive variant first when requested. */
async function getImage(bucket: R2Bucket, baseKey: string, progressive: boolean): Promise<R2ObjectBody | null> {
  if (progressive) {
    const progressiveKey = baseKey.replace(/\.jpg$/, ".progressive.jpg");
    const obj = await bucket.get(progressiveKey);
    if (obj) return obj;
  }
  return bucket.get(baseKey);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const progressive = isProgressive(url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    if (request.method !== "GET") {
      return new Response("Method not allowed", { status: 405 });
    }

    // GET /api/today → stylized image
    if (path === "/api/today") {
      const today = await getToday(env.BUCKET);
      const obj = await getImage(env.BUCKET, `stylized/${datePath(today)}.jpg`, progressive);
      if (!obj) return notFound("No image for today");
      return imageResponse(obj);
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

    // GET /api/:date/original → original image
    const origMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\/original$/);
    if (origMatch) {
      const obj = await getImage(env.BUCKET, `originals/${datePath(origMatch[1])}.jpg`, progressive);
      if (!obj) return notFound(`No original for ${origMatch[1]}`);
      return imageResponse(obj);
    }

    // GET /api/:date → stylized image for date
    const dateMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})$/);
    if (dateMatch) {
      const obj = await getImage(env.BUCKET, `stylized/${datePath(dateMatch[1])}.jpg`, progressive);
      if (!obj) return notFound(`No image for ${dateMatch[1]}`);
      return imageResponse(obj);
    }

    return notFound("Not found. Try /api/today or /api/YYYY-MM-DD");
  },
};
