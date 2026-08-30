/**
 * Bauhaus API — Cloudflare Worker serving stylized CC0 artwork from R2.
 *
 * Routes:
 *   GET|HEAD /api/today               → today's stylized image
 *   GET|HEAD /api/today.json          → today's metadata
 *   GET|HEAD /api/today.manifest.json → today's responsive manifest
 *   GET|HEAD /api/:date               → stylized image for YYYY-MM-DD
 *   GET|HEAD /api/:date/original      → original unstylized image
 *   GET|HEAD /api/:date.json          → metadata for date
 *   GET|HEAD /api/:date.manifest.json → responsive manifest for date
 *   GET|HEAD /api/archive             → dates that have published artwork
 *   POST     /api/vitals              → ingest Web Vitals RUM (Analytics Engine)
 *   POST     /api/err                 → ingest JS error RUM (Analytics Engine)
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

/** Values `?format=` accepts. Anything else is a client error, not a fallback. */
const FORMAT_PARAMS = new Set(["avif", "webp", "jpeg", "auto"]);

/**
 * True when `?format=` is present but not a value we support.
 *
 * Silently negotiating past a bad value made `?format=png` and `?format=jpg`
 * indistinguishable from `?format=auto`, so a typo in a caller's code looked
 * like it worked and quietly served something else.
 */
export function hasInvalidFormat(url: URL): boolean {
  const param = url.searchParams.get("format");
  if (param === null) return false;
  return !FORMAT_PARAMS.has(param.toLowerCase());
}

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
  if (!obj) throw new NoLatestError("No latest.json found");
  const data: { date: string } = await obj.json();
  return data.date;
}

export function isProgressive(url: URL): boolean {
  return url.searchParams.get("progressive") === "true";
}

export function isStrip(url: URL): boolean {
  return url.searchParams.get("strip") === "true";
}

function corsHeaders(): HeadersInit {
  return {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
  };
}

// ---------------------------------------------------------------------------
// Telemetry helpers
// ---------------------------------------------------------------------------

const TELEMETRY_BODY_LIMIT = 4096;
const TELEMETRY_ORIGINS_DEFAULT =
  "https://kevintcoughlin.com,https://www.kevintcoughlin.com";

export function getAllowedOrigins(env: Env): Set<string> {
  const raw = (env.ALLOWED_ORIGINS ?? TELEMETRY_ORIGINS_DEFAULT).trim();
  const value = raw || TELEMETRY_ORIGINS_DEFAULT;
  return new Set(value.split(",").map((s) => s.trim()).filter(Boolean));
}

function telemetryCorsHeaders(origin: string): Record<string, string> {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST",
    "Access-Control-Allow-Headers": "content-type",
  };
}

export function classifyUA(ua: string): "mobile" | "desktop" {
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
  // Explicit JPEG variants should win over negotiated format selection.
  if (progressive) {
    const obj = await bucket.get(`${basePath}.progressive.jpg`);
    if (obj) return { obj, contentType: "image/jpeg" };
  }

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
  if (progressive) {
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

/**
 * Cache-control for date-specific resources — immutable because the pipeline
 * publishes a date once and refuses to rewrite it (see _assert_unpublished in
 * src/upload.py). Kept byte-identical to IMMUTABLE_CACHE in src/upload.py: R2
 * stores that value on the object and it is preferred over this constant, so
 * this is only the fallback for objects written by something other than the
 * pipeline.
 */
const IMMUTABLE_CACHE = "public, max-age=31536000, s-maxage=31536000, immutable";

/**
 * Cache-control for /api/today* — this resolves to a new date every morning.
 *
 * s-maxage must stay below the publish interval. At the old 86400 an edge PoP
 * that filled its cache shortly before the 04:00 UTC run kept serving the
 * previous day's artwork for a further 24 hours, so viewers behind that PoP
 * never saw a day's art at all. One hour bounds the staleness while still
 * absorbing the overwhelming majority of traffic.
 */
const TODAY_CACHE = "public, max-age=300, s-maxage=3600, stale-while-revalidate=604800";

// ---------------------------------------------------------------------------
// Archive index
// ---------------------------------------------------------------------------

/** Dates returned by /api/archive when the caller does not ask for a size. */
const ARCHIVE_DEFAULT_LIMIT = 100;

/** Ceiling on ?limit=. A year of art is 365 dates, so this is several pages. */
const ARCHIVE_MAX_LIMIT = 1000;

/**
 * Cap on R2 list() round trips per archive request.
 *
 * The whole `metadata/` prefix is drained so the newest dates can be served
 * first — R2 lists ascending, and the newest page is the one every caller
 * wants. At one publish per day and 1000 keys per call, this bounds the walk
 * at roughly 68 years of art while stopping a pathologically large bucket
 * (a stray prefix, a bulk import) from pinning the Worker.
 */
const ARCHIVE_MAX_LIST_CALLS = 25;

/**
 * Extracts the publish date from a metadata object key, or null if the key is
 * not one.
 *
 * Deliberately strict about the `.json` suffix: the same prefix also holds
 * `<date>.json.sig` when signing is enabled, and counting both would report
 * every signed day twice.
 */
export function dateFromMetadataKey(key: string): string | null {
  const match = key.match(/^metadata\/(\d{4})\/(\d{2})\/(\d{2})\.json$/);
  return match ? `${match[1]}-${match[2]}-${match[3]}` : null;
}

export interface ArchiveQuery {
  limit: number;
  before: string | null;
}

/**
 * Parses ?limit= and ?before=, or returns an error message for the caller.
 *
 * Invalid values are rejected rather than clamped, for the same reason
 * `?format=` is: a silently corrected `?limit=abc` looks like it worked and
 * quietly returns a different page than the caller asked for.
 */
export function parseArchiveQuery(url: URL): ArchiveQuery | { error: string } {
  const rawLimit = url.searchParams.get("limit");
  let limit = ARCHIVE_DEFAULT_LIMIT;
  if (rawLimit !== null) {
    if (!/^\d+$/.test(rawLimit)) {
      return { error: "limit must be a positive integer" };
    }
    limit = parseInt(rawLimit, 10);
    if (limit < 1 || limit > ARCHIVE_MAX_LIMIT) {
      return { error: `limit must be between 1 and ${ARCHIVE_MAX_LIMIT}` };
    }
  }

  const before = url.searchParams.get("before");
  if (before !== null && !/^\d{4}-\d{2}-\d{2}$/.test(before)) {
    return { error: "before must be a date in YYYY-MM-DD form" };
  }

  return { limit, before };
}

/**
 * Every published date, oldest first.
 *
 * R2 lists keys in lexicographic order and the keys are `metadata/YYYY/MM/DD`,
 * so lexicographic order is already chronological order — no sorting needed.
 */
async function listPublishedDates(
  bucket: R2Bucket,
): Promise<{ dates: string[]; complete: boolean }> {
  const dates: string[] = [];
  let cursor: string | undefined;
  let complete = false;

  for (let call = 0; call < ARCHIVE_MAX_LIST_CALLS; call++) {
    const page = await bucket.list({ prefix: "metadata/", cursor });
    for (const object of page.objects) {
      const date = dateFromMetadataKey(object.key);
      if (date) dates.push(date);
    }
    if (!page.truncated) {
      complete = true;
      break;
    }
    cursor = page.cursor;
  }

  return { dates, complete };
}

/**
 * Builds one page of the archive index from the full date list.
 *
 * Paging is by date rather than an opaque cursor: dates are immutable and
 * meaningful, so `?before=` survives new publishes, can be constructed by hand,
 * and means the same thing tomorrow as it does today.
 */
export interface ArchivePage {
  dates: string[];
  count: number;
  total: number;
  next?: string;
  truncated?: true;
}

export function buildArchivePage(
  listing: { dates: string[]; complete: boolean },
  query: ArchiveQuery,
): ArchivePage {
  const newestFirst = [...listing.dates].reverse();
  const eligible = query.before === null
    ? newestFirst
    : newestFirst.filter((date) => date < query.before!);
  const page = eligible.slice(0, query.limit);

  const body: ArchivePage = {
    dates: page,
    count: page.length,
    total: listing.dates.length,
  };
  if (eligible.length > page.length) {
    body.next = `/api/archive?limit=${query.limit}&before=${page[page.length - 1]}`;
  }
  // The walk stopped at ARCHIVE_MAX_LIST_CALLS, so `total` counts what was
  // seen rather than what exists. Say so instead of reporting a short count as
  // if it were the whole archive.
  if (!listing.complete) body.truncated = true;
  return body;
}

/**
 * GET|HEAD /api/archive — which dates have published artwork.
 *
 * Without this, the archive is undiscoverable: every other date endpoint
 * requires the caller to already know the date, so a gallery or a "random past
 * day" consumer has nothing to enumerate and has to guess.
 */
async function serveArchive(
  bucket: R2Bucket,
  url: URL,
  isHead: boolean,
): Promise<Response> {
  const query = parseArchiveQuery(url);
  if ("error" in query) return errorResponse(400, query.error);

  const body = buildArchivePage(await listPublishedDates(bucket), query);
  const headers = {
    "Content-Type": "application/json",
    // The newest page gains an entry every morning, so this tracks /api/today
    // rather than the immutable per-date resources it indexes.
    "Cache-Control": TODAY_CACHE,
    ...corsHeaders(),
  };

  return new Response(isHead ? null : JSON.stringify(body), { status: 200, headers });
}

/** Builds the shared response headers for image endpoints. */
function buildImageHeaders(
  key: string,
  httpEtag: string | undefined,
  httpMetadata: R2HTTPMetadata | undefined,
  contentType: string,
  today: boolean,
): Record<string, string> {
  const variant = key?.endsWith(".progressive.jpg")
    ? "progressive"
    : key?.endsWith(".stripped.jpg")
      ? "stripped"
      : "baseline";
  const headers: Record<string, string> = {
    "Content-Type": contentType,
    "Cache-Control": today ? TODAY_CACHE : (httpMetadata?.cacheControl ?? IMMUTABLE_CACHE),
    "Vary": "Accept",
    "X-Variant": variant,
    "Accept-CH": "DPR, Width, Viewport-Width",
    ...corsHeaders(),
  };
  if (httpEtag) headers["ETag"] = httpEtag;
  return headers;
}

function imageResponse(obj: R2ObjectBody, contentType: string, today = false): Response {
  const headers = buildImageHeaders(obj.key ?? "", obj.httpEtag, obj.httpMetadata, contentType, today);
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

function notModified(etag: string, today = false): Response {
  // A 304 that omits Cache-Control and Vary invites caches to fall back to
  // their own heuristics for how long the stored copy stays fresh, and to
  // ignore that these resources are content-negotiated on Accept. Both must
  // match what the corresponding 200 would have said.
  return new Response(null, {
    status: 304,
    headers: {
      "ETag": etag,
      "Cache-Control": today ? TODAY_CACHE : IMMUTABLE_CACHE,
      "Vary": "Accept",
      ...corsHeaders(),
    },
  });
}

/** JSON error with CORS — the single shape every non-telemetry failure uses. */
function errorResponse(status: number, msg: string, cacheControl = "no-store"): Response {
  return new Response(JSON.stringify({ error: msg }), {
    status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": cacheControl,
      ...corsHeaders(),
    },
  });
}

function notFound(msg: string): Response {
  return errorResponse(404, msg);
}

/**
 * Serves an image response, using R2 head() for If-None-Match checks to avoid
 * reading the full object body when a 304 Not Modified response is appropriate.
 * When isHead is true, returns the same headers as GET but without a body.
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
  isHead = false,
): Promise<Response> {
  const ifNoneMatch = request.headers.get("If-None-Match");

  if (isHead || ifNoneMatch) {
    const headResult = await headImageObject(bucket, basePath, format, progressive, strip);
    if (!headResult) return notFound(notFoundMsg);
    if (ifNoneMatch && etagMatches(ifNoneMatch, headResult.head.httpEtag)) {
      return notModified(headResult.head.httpEtag, today);
    }
    if (isHead) {
      const headers = buildImageHeaders(
        headResult.key,
        headResult.head.httpEtag,
        headResult.head.httpMetadata,
        headResult.contentType,
        today,
      );
      return new Response(null, { status: 200, headers });
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
 * When isHead is true, returns the same headers as GET but without a body.
 */
async function serveJson(
  request: Request,
  bucket: R2Bucket,
  key: string,
  today: boolean,
  notFoundMsg: string,
  isHead = false,
): Promise<Response> {
  const ifNoneMatch = request.headers.get("If-None-Match");

  if (isHead || ifNoneMatch) {
    const head = await bucket.head(key);
    if (!head) return notFound(notFoundMsg);
    if (ifNoneMatch && etagMatches(ifNoneMatch, head.httpEtag)) {
      return notModified(head.httpEtag, today);
    }
    if (isHead) {
      const headers: Record<string, string> = {
        "Content-Type": "application/json",
        "Cache-Control": today ? TODAY_CACHE : (head.httpMetadata?.cacheControl ?? IMMUTABLE_CACHE),
        ...corsHeaders(),
      };
      if (head.httpEtag) headers["ETag"] = head.httpEtag;
      return new Response(null, { status: 200, headers });
    }
    const obj = await bucket.get(key);
    if (!obj) return notFound(notFoundMsg);
    return jsonResponse(obj, today);
  }

  const obj = await bucket.get(key);
  if (!obj) return notFound(notFoundMsg);
  return jsonResponse(obj, today);
}

/**
 * Serves a non-JSON, non-image object (currently the detached PGP signature)
 * with the same conditional-request handling as the other endpoints.
 */
async function serveBinary(
  request: Request,
  bucket: R2Bucket,
  key: string,
  contentType: string,
  notFoundMsg: string,
  isHead = false,
): Promise<Response> {
  const ifNoneMatch = request.headers.get("If-None-Match");

  const headers = (etag: string | undefined): Record<string, string> => {
    const h: Record<string, string> = {
      "Content-Type": contentType,
      "Cache-Control": IMMUTABLE_CACHE,
      ...corsHeaders(),
    };
    if (etag) h["ETag"] = etag;
    return h;
  };

  if (isHead || ifNoneMatch) {
    const head = await bucket.head(key);
    if (!head) return notFound(notFoundMsg);
    if (ifNoneMatch && etagMatches(ifNoneMatch, head.httpEtag)) {
      return notModified(head.httpEtag);
    }
    if (isHead) return new Response(null, { status: 200, headers: headers(head.httpEtag) });
  }

  const obj = await bucket.get(key);
  if (!obj) return notFound(notFoundMsg);
  return new Response(obj.body, { headers: headers(obj.httpEtag) });
}

/** 503 for upstream failures — same JSON+CORS shape as notFound(). */
function unavailable(msg: string): Response {
  return errorResponse(503, msg);
}

/** Raised when latest.json is missing, to separate "not published yet" from a genuine R2 fault. */
class NoLatestError extends Error {}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // Without this, an R2 fault or a missing latest.json escapes as a bare 500
    // carrying no CORS headers, so browsers see an opaque CORS failure rather
    // than the JSON error every other path returns — precisely when the API is
    // already unhealthy.
    try {
      return await handle(request, env);
    } catch (err) {
      if (err instanceof NoLatestError) {
        return notFound("No artwork published yet");
      }
      console.error("Unhandled error", err);
      return unavailable("Upstream storage unavailable");
    }
  },
};

async function handle(request: Request, env: Env): Promise<Response> {
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

    const isHead = request.method === "HEAD";

    if (request.method !== "GET" && !isHead) {
      return errorResponse(405, "Method not allowed");
    }

    // GET /api/health → publish freshness, for an external uptime monitor.
    //
    // The failure mode with no signal at all is the cron simply not running:
    // GitHub disables scheduled workflows after 60 days of repo inactivity,
    // and a skipped or failed schedule produces no run, so the ntfy hooks in
    // generate.yml never fire. Staleness measured at the serving end catches
    // that, plus R2 write failures and publish bugs, in one probe.
    if (path === "/api/health") {
      let today: string;
      try {
        today = await getToday(env.BUCKET);
      } catch (err) {
        // Never published, or R2 is down — either way this probe must report
        // unhealthy rather than the 404 the generic handler would produce.
        const reason = err instanceof NoLatestError ? "no artwork published" : "storage unavailable";
        return new Response(JSON.stringify({ status: "unhealthy", error: reason }), {
          status: 503,
          headers: { "Content-Type": "application/json", "Cache-Control": "no-store", ...corsHeaders() },
        });
      }
      const publishedMs = Date.parse(`${today}T00:00:00Z`);
      const staleDays = Number.isNaN(publishedMs)
        ? null
        : Math.floor((Date.now() - publishedMs) / 86_400_000);
      const healthy = staleDays !== null && staleDays <= 1;
      return new Response(
        JSON.stringify({ status: healthy ? "ok" : "stale", date: today, stale_days: staleDays }),
        {
          status: healthy ? 200 : 503,
          headers: {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
            ...corsHeaders(),
          },
        },
      );
    }

    // GET|HEAD /api/archive → the dates that have published artwork
    if (path === "/api/archive") {
      return serveArchive(env.BUCKET, url, isHead);
    }

    if (hasInvalidFormat(url)) {
      return errorResponse(400, "Unsupported format. Use avif, webp, jpeg, or auto");
    }

    const format = negotiateFormat(request, url);

    // GET|HEAD /api/today → stylized image
    if (path === "/api/today") {
      const today = await getToday(env.BUCKET);
      return serveImage(request, env.BUCKET, `stylized/${datePath(today)}`, format, progressive, strip, true, "No image for today", isHead);
    }

    // GET|HEAD /api/today.json → metadata
    if (path === "/api/today.json") {
      const today = await getToday(env.BUCKET);
      return serveJson(request, env.BUCKET, `metadata/${datePath(today)}.json`, true, "No metadata for today", isHead);
    }

    // GET|HEAD /api/today.manifest.json → responsive manifest
    if (path === "/api/today.manifest.json") {
      const today = await getToday(env.BUCKET);
      return serveJson(request, env.BUCKET, `manifests/${datePath(today)}.json`, true, "No manifest for today", isHead);
    }

    // GET|HEAD /api/:date.manifest.json → responsive manifest for date
    const manifestMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\.manifest\.json$/);
    if (manifestMatch) {
      return serveJson(request, env.BUCKET, `manifests/${datePath(manifestMatch[1])}.json`, false, `No manifest for ${manifestMatch[1]}`, isHead);
    }

    // GET|HEAD /api/:date.json.sig → detached PGP signature over the metadata
    //
    // The pipeline has always been able to upload this object, but nothing
    // served it, so a published signature could never actually be fetched and
    // checked — signing was unverifiable by construction.
    const sigMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\.json\.sig$/);
    if (sigMatch) {
      return serveBinary(
        request,
        env.BUCKET,
        `metadata/${datePath(sigMatch[1])}.json.sig`,
        "application/pgp-signature",
        `No signature for ${sigMatch[1]}`,
        isHead,
      );
    }

    // GET|HEAD /api/:date.json → metadata for date
    const jsonMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\.json$/);
    if (jsonMatch) {
      return serveJson(request, env.BUCKET, `metadata/${datePath(jsonMatch[1])}.json`, false, `No metadata for ${jsonMatch[1]}`, isHead);
    }

    // GET|HEAD /api/:date/original → original image
    const origMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})\/original$/);
    if (origMatch) {
      return serveImage(request, env.BUCKET, `originals/${datePath(origMatch[1])}`, format, progressive, strip, false, `No original for ${origMatch[1]}`, isHead);
    }

    // GET|HEAD /api/:date → stylized image for date
    const dateMatch = path.match(/^\/api\/(\d{4}-\d{2}-\d{2})$/);
    if (dateMatch) {
      return serveImage(request, env.BUCKET, `stylized/${datePath(dateMatch[1])}`, format, progressive, strip, false, `No image for ${dateMatch[1]}`, isHead);
    }

    return notFound("Not found. Try /api/today, /api/YYYY-MM-DD, or /api/archive");
}
