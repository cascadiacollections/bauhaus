import { describe, it, expect, vi, beforeEach } from "vitest";
import worker, { negotiateFormat } from "../src/index";

// ---------------------------------------------------------------------------
// Helpers to build mock R2 objects
// ---------------------------------------------------------------------------

function fakeR2Body(
  data: string | object,
  contentType?: string,
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
  } as unknown as R2ObjectBody;
}

function makeBucket(objects: Record<string, R2ObjectBody>): R2Bucket {
  return {
    get: vi.fn(async (key: string) => objects[key] ?? null),
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
