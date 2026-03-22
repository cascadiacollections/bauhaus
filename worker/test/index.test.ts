import { describe, it, expect, vi, beforeEach } from "vitest";
import worker, { negotiateFormat } from "../src/index";

// ---------------------------------------------------------------------------
// Helpers to build mock R2 objects
// ---------------------------------------------------------------------------

function fakeR2Body(
  data: string | object,
  contentType?: string,
  etag?: string,
): R2ObjectBody {
  const body =
    typeof data === "string" ? data : JSON.stringify(data);
  return {
    body: new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(body));
        controller.close();
      },
    }),
    httpMetadata: contentType ? { contentType } : undefined,
    json: () => Promise.resolve(typeof data === "object" ? data : JSON.parse(data)),
    httpEtag: etag,
  } as unknown as R2ObjectBody;
}

function fakeR2Head(key: string, etag: string): R2Object {
  return {
    key,
    httpEtag: etag,
    etag: etag.replace(/"/g, ""),
    httpMetadata: {},
  } as unknown as R2Object;
}

function makeBucket(objects: Record<string, R2ObjectBody>): R2Bucket {
  return {
    get: vi.fn(async (key: string) => objects[key] ?? null),
    head: vi.fn(async (key: string) => {
      const obj = objects[key];
      if (!obj) return null;
      const etag = (obj as unknown as { httpEtag?: string }).httpEtag ?? `"etag-${key}"`;
      return fakeR2Head(key, etag);
    }),
  } as unknown as R2Bucket;
}

function makeRequest(
  path: string,
  opts?: { accept?: string; method?: string },
): Request {
  return new Request(`https://example.com${path}`, {
    method: opts?.method ?? "GET",
    headers: opts?.accept ? { Accept: opts.accept } : {},
  });
}

// ---------------------------------------------------------------------------
// negotiateFormat — pure function tests
// ---------------------------------------------------------------------------

describe("negotiateFormat", () => {
  it("returns avif when ?format=avif", () => {
    const req = makeRequest("/api/2025-01-01?format=avif");
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("avif");
  });

  it("returns webp when ?format=webp", () => {
    const req = makeRequest("/api/2025-01-01?format=webp");
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("webp");
  });

  it("returns jpeg when ?format=jpeg", () => {
    const req = makeRequest("/api/2025-01-01?format=jpeg");
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("jpeg");
  });

  it("is case-insensitive for ?format param", () => {
    const req = makeRequest("/api/2025-01-01?format=AVIF");
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("avif");
  });

  it("returns avif when Accept includes image/avif", () => {
    const req = makeRequest("/api/2025-01-01", {
      accept: "image/avif,image/webp,image/jpeg,*/*",
    });
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("avif");
  });

  it("returns webp when Accept includes image/webp but not avif", () => {
    const req = makeRequest("/api/2025-01-01", {
      accept: "image/webp,image/jpeg,*/*",
    });
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("webp");
  });

  it("returns jpeg when Accept has no avif or webp", () => {
    const req = makeRequest("/api/2025-01-01", {
      accept: "image/jpeg,*/*",
    });
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("jpeg");
  });

  it("returns jpeg when Accept header is absent", () => {
    const req = makeRequest("/api/2025-01-01");
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("jpeg");
  });

  it("?format= overrides Accept header", () => {
    const req = makeRequest("/api/2025-01-01?format=jpeg", {
      accept: "image/avif,image/webp,*/*",
    });
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("jpeg");
  });

  it("?format=auto falls through to Accept negotiation", () => {
    const req = makeRequest("/api/2025-01-01?format=auto", {
      accept: "image/webp,*/*",
    });
    const url = new URL(req.url);
    expect(negotiateFormat(req, url)).toBe("webp");
  });
});

// ---------------------------------------------------------------------------
// Worker fetch handler — integration-style tests with mocked R2
// ---------------------------------------------------------------------------

describe("worker fetch handler", () => {
  const DATE = "2025-06-15";
  const DATE_PATH = "2025/06/15";

  let bucket: R2Bucket;
  let env: { BUCKET: R2Bucket };

  beforeEach(() => {
    bucket = makeBucket({
      "latest.json": fakeR2Body({ date: DATE }),
      [`stylized/${DATE_PATH}.jpg`]: fakeR2Body("jpeg-bytes", "image/jpeg"),
      [`stylized/${DATE_PATH}.avif`]: fakeR2Body("avif-bytes", "image/avif"),
      [`stylized/${DATE_PATH}.webp`]: fakeR2Body("webp-bytes", "image/webp"),
      [`originals/${DATE_PATH}.jpg`]: fakeR2Body("orig-jpeg", "image/jpeg"),
      [`metadata/${DATE_PATH}.json`]: fakeR2Body({ title: "test" }),
    });
    env = { BUCKET: bucket };
  });

  // --- Vary header ---

  it("sets Vary: Accept on image responses", async () => {
    const res = await worker.fetch(makeRequest("/api/today"), env);
    expect(res.headers.get("Vary")).toBe("Accept");
  });

  // --- /api/today ---

  it("serves JPEG for /api/today by default", async () => {
    const res = await worker.fetch(makeRequest("/api/today"), env);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/jpeg");
  });

  it("serves AVIF for /api/today when Accept includes avif", async () => {
    const res = await worker.fetch(
      makeRequest("/api/today", { accept: "image/avif,image/webp,*/*" }),
      env,
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/avif");
  });

  it("serves WebP for /api/today when Accept includes webp only", async () => {
    const res = await worker.fetch(
      makeRequest("/api/today", { accept: "image/webp,*/*" }),
      env,
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/webp");
  });

  it("serves JPEG for /api/today?format=jpeg overriding Accept", async () => {
    const res = await worker.fetch(
      makeRequest("/api/today?format=jpeg", { accept: "image/avif,*/*" }),
      env,
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/jpeg");
  });

  // --- /api/:date ---

  it("serves AVIF for /api/:date when Accept includes avif", async () => {
    const res = await worker.fetch(
      makeRequest(`/api/${DATE}`, { accept: "image/avif,*/*" }),
      env,
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/avif");
  });

  it("serves JPEG for /api/:date by default", async () => {
    const res = await worker.fetch(makeRequest(`/api/${DATE}`), env);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/jpeg");
  });

  // --- /api/:date/original ---

  it("serves JPEG for /api/:date/original (only jpeg exists)", async () => {
    const res = await worker.fetch(
      makeRequest(`/api/${DATE}/original`, { accept: "image/avif,*/*" }),
      env,
    );
    expect(res.status).toBe(200);
    // avif not available for originals → falls back to JPEG
    expect(res.headers.get("Content-Type")).toBe("image/jpeg");
  });

  // --- fallback to JPEG when variant is missing ---

  it("falls back to JPEG when AVIF is unavailable", async () => {
    // Bucket with only JPEG for stylized
    const jpegOnly = makeBucket({
      "latest.json": fakeR2Body({ date: DATE }),
      [`stylized/${DATE_PATH}.jpg`]: fakeR2Body("jpeg-bytes", "image/jpeg"),
    });
    const res = await worker.fetch(
      makeRequest(`/api/${DATE}`, { accept: "image/avif,*/*" }),
      { BUCKET: jpegOnly },
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/jpeg");
  });

  it("returns 404 when no format is available at all", async () => {
    const emptyBucket = makeBucket({
      "latest.json": fakeR2Body({ date: DATE }),
    });
    const res = await worker.fetch(
      makeRequest(`/api/${DATE}`),
      { BUCKET: emptyBucket },
    );
    expect(res.status).toBe(404);
  });

  // --- JSON endpoints are unaffected ---

  it("JSON endpoints are not affected by format negotiation", async () => {
    const res = await worker.fetch(
      makeRequest(`/api/${DATE}.json`, { accept: "image/avif,*/*" }),
      env,
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/json");
  });

  // --- ?format=webp on /api/:date ---

  it("?format=webp serves WebP for /api/:date", async () => {
    const res = await worker.fetch(
      makeRequest(`/api/${DATE}?format=webp`),
      env,
    );
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/webp");
  });
});

// ---------------------------------------------------------------------------
// ETag and conditional requests (If-None-Match / 304 Not Modified)
// ---------------------------------------------------------------------------

describe("ETag and conditional requests", () => {
  const DATE = "2025-06-15";
  const DATE_PATH = "2025/06/15";
  const IMAGE_ETAG = '"abc123"';
  const META_ETAG = '"meta-etag"';
  const MANIFEST_ETAG = '"manifest-etag"';

  let bucket: R2Bucket;
  let env: { BUCKET: R2Bucket };

  beforeEach(() => {
    bucket = makeBucket({
      "latest.json": fakeR2Body({ date: DATE }),
      [`stylized/${DATE_PATH}.jpg`]: fakeR2Body("jpeg-bytes", "image/jpeg", IMAGE_ETAG),
      [`stylized/${DATE_PATH}.avif`]: fakeR2Body("avif-bytes", "image/avif", '"avif-etag"'),
      [`originals/${DATE_PATH}.jpg`]: fakeR2Body("orig-jpeg", "image/jpeg", '"orig-etag"'),
      [`metadata/${DATE_PATH}.json`]: fakeR2Body({ title: "test" }, "application/json", META_ETAG),
      [`manifests/${DATE_PATH}.json`]: fakeR2Body({ variants: [] }, "application/json", MANIFEST_ETAG),
    });
    env = { BUCKET: bucket };
  });

  // --- ETag present in normal responses ---

  it("includes ETag header in image response for /api/:date", async () => {
    const res = await worker.fetch(makeRequest(`/api/${DATE}`), env);
    expect(res.status).toBe(200);
    expect(res.headers.get("ETag")).toBe(IMAGE_ETAG);
  });

  it("includes ETag header in image response for /api/today", async () => {
    const res = await worker.fetch(makeRequest("/api/today"), env);
    expect(res.status).toBe(200);
    expect(res.headers.get("ETag")).toBe(IMAGE_ETAG);
  });

  it("includes ETag header in JSON response for /api/:date.json", async () => {
    const res = await worker.fetch(makeRequest(`/api/${DATE}.json`), env);
    expect(res.status).toBe(200);
    expect(res.headers.get("ETag")).toBe(META_ETAG);
  });

  it("includes ETag header in manifest response for /api/:date.manifest.json", async () => {
    const res = await worker.fetch(makeRequest(`/api/${DATE}.manifest.json`), env);
    expect(res.status).toBe(200);
    expect(res.headers.get("ETag")).toBe(MANIFEST_ETAG);
  });

  // --- 304 Not Modified when If-None-Match matches ---

  it("returns 304 for /api/:date when If-None-Match matches ETag", async () => {
    const req = new Request(`https://example.com/api/${DATE}`, {
      headers: { "If-None-Match": IMAGE_ETAG },
    });
    const res = await worker.fetch(req, env);
    expect(res.status).toBe(304);
    expect(res.headers.get("ETag")).toBe(IMAGE_ETAG);
    expect(res.body).toBeNull();
  });

  it("returns 304 for /api/today when If-None-Match matches ETag", async () => {
    const req = new Request("https://example.com/api/today", {
      headers: { "If-None-Match": IMAGE_ETAG },
    });
    const res = await worker.fetch(req, env);
    expect(res.status).toBe(304);
    expect(res.headers.get("ETag")).toBe(IMAGE_ETAG);
  });

  it("returns 304 for /api/:date.json when If-None-Match matches ETag", async () => {
    const req = new Request(`https://example.com/api/${DATE}.json`, {
      headers: { "If-None-Match": META_ETAG },
    });
    const res = await worker.fetch(req, env);
    expect(res.status).toBe(304);
    expect(res.headers.get("ETag")).toBe(META_ETAG);
  });

  it("returns 304 for /api/:date.manifest.json when If-None-Match matches ETag", async () => {
    const req = new Request(`https://example.com/api/${DATE}.manifest.json`, {
      headers: { "If-None-Match": MANIFEST_ETAG },
    });
    const res = await worker.fetch(req, env);
    expect(res.status).toBe(304);
  });

  // --- 200 with body when If-None-Match does not match ---

  it("returns 200 with body for /api/:date when If-None-Match does not match", async () => {
    const req = new Request(`https://example.com/api/${DATE}`, {
      headers: { "If-None-Match": '"stale-etag"' },
    });
    const res = await worker.fetch(req, env);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/jpeg");
    expect(res.headers.get("ETag")).toBe(IMAGE_ETAG);
  });

  it("returns 200 with body for /api/:date.json when If-None-Match does not match", async () => {
    const req = new Request(`https://example.com/api/${DATE}.json`, {
      headers: { "If-None-Match": '"stale-etag"' },
    });
    const res = await worker.fetch(req, env);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/json");
  });

  // --- head() used instead of get() for cache-hit conditional requests ---

  it("calls head() and NOT get() for the image key on a 304 cache hit", async () => {
    const req = new Request(`https://example.com/api/${DATE}`, {
      headers: { "If-None-Match": IMAGE_ETAG },
    });
    await worker.fetch(req, env);
    const imageKey = `stylized/${DATE_PATH}.jpg`;
    const getCalls: string[] = (bucket.get as ReturnType<typeof vi.fn>).mock.calls.map(
      (c: [string]) => c[0],
    );
    const headCalls: string[] = (bucket.head as ReturnType<typeof vi.fn>).mock.calls.map(
      (c: [string]) => c[0],
    );
    // head() must have been called for the image key
    expect(headCalls).toContain(imageKey);
    // get() must NOT have been called for the image key (only latest.json is allowed)
    expect(getCalls.filter((k) => k !== "latest.json")).not.toContain(imageKey);
  });

  it("calls head() then get() for the image key on a cache miss", async () => {
    const req = new Request(`https://example.com/api/${DATE}`, {
      headers: { "If-None-Match": '"stale-etag"' },
    });
    await worker.fetch(req, env);
    const imageKey = `stylized/${DATE_PATH}.jpg`;
    const getCalls: string[] = (bucket.get as ReturnType<typeof vi.fn>).mock.calls.map(
      (c: [string]) => c[0],
    );
    const headCalls: string[] = (bucket.head as ReturnType<typeof vi.fn>).mock.calls.map(
      (c: [string]) => c[0],
    );
    expect(headCalls).toContain(imageKey);
    expect(getCalls).toContain(imageKey);
  });

  // --- If-None-Match: * wildcard ---

  it("returns 304 when If-None-Match is * and resource exists", async () => {
    const req = new Request(`https://example.com/api/${DATE}`, {
      headers: { "If-None-Match": "*" },
    });
    const res = await worker.fetch(req, env);
    expect(res.status).toBe(304);
  });

  // --- 404 when resource is missing ---

  it("returns 404 when resource is missing and If-None-Match is present", async () => {
    const emptyBucket = makeBucket({ "latest.json": fakeR2Body({ date: DATE }) });
    const req = new Request(`https://example.com/api/${DATE}`, {
      headers: { "If-None-Match": IMAGE_ETAG },
    });
    const res = await worker.fetch(req, { BUCKET: emptyBucket });
    expect(res.status).toBe(404);
  });
});
