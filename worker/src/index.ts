/**
 * Bauhaus API — Cloudflare Worker serving stylized CC0 artwork from R2.
 *
 * Routes:
 *   GET /api/today       → today's stylized image
 *   GET /api/today.json  → today's metadata
 *   GET /api/:date       → stylized image for YYYY-MM-DD
 *   GET /api/:date/original → original unstylized image
 *   GET /api/:date.json  → metadata for date
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

function corsHeaders(): HeadersInit {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
  };
}

function imageResponse(obj: R2ObjectBody): Response {
  return new Response(obj.body, {
    headers: {
      "Content-Type": obj.httpMetadata?.contentType ?? "image/jpeg",
      "Cache-Control": obj.httpMetadata?.cacheControl ?? "public, max-age=3600",
      ...corsHeaders(),
    },
  });
}

function jsonResponse(obj: R2ObjectBody): Response {
  return new Response(obj.body, {
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": obj.httpMetadata?.cacheControl ?? "public, max-age=300",
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

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    if (request.method !== "GET") {
      return new Response("Method not allowed", { status: 405 });
    }

    // GET /api/today → stylized image
    if (path === "/api/today") {
      const today = await getToday(env.BUCKET);
      const obj = await env.BUCKET.get(`stylized/${datePath(today)}.jpg`);
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
      const obj = await env.BUCKET.get(`originals/${datePath(origMatch[1])}.jpg`);
      if (!obj) return notFound(`No original for ${origMatch[1]}`);
      return imageResponse(obj);
    }

    // GET /api/:date → stylized image for date
    const dateMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})$/);
    if (dateMatch) {
      const obj = await env.BUCKET.get(`stylized/${datePath(dateMatch[1])}.jpg`);
      if (!obj) return notFound(`No image for ${dateMatch[1]}`);
      return imageResponse(obj);
    }

    return notFound("Not found. Try /api/today or /api/YYYY-MM-DD");
  },
};
