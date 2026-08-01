import { describe, expect, it } from "vitest";

import worker, { classifyUA, getAllowedOrigins, isProgressive, isStrip } from "../src/index";

describe("worker helper utilities", () => {
  it("detects mobile user agents", () => {
    expect(classifyUA("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")).toBe("mobile");
    expect(classifyUA("Mozilla/5.0 (Linux; Android 14; Pixel 8)")).toBe("mobile");
  });

  it("detects desktop user agents", () => {
    expect(classifyUA("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36")).toBe("desktop");
    expect(classifyUA("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5)")).toBe("desktop");
  });

  it("parses progressive and strip query flags", () => {
    expect(isProgressive(new URL("https://example.com/api/today?progressive=true"))).toBe(true);
    expect(isProgressive(new URL("https://example.com/api/today"))).toBe(false);
    expect(isStrip(new URL("https://example.com/api/today?strip=true"))).toBe(true);
    expect(isStrip(new URL("https://example.com/api/today"))).toBe(false);
  });

  it("uses configured allowed origins and trims whitespace", () => {
    const env = {
      ALLOWED_ORIGINS: "https://example.com, https://cdn.example.com , https://api.example.com",
    } as Parameters<typeof getAllowedOrigins>[0];

    expect([...getAllowedOrigins(env)]).toEqual([
      "https://example.com",
      "https://cdn.example.com",
      "https://api.example.com",
    ]);
  });

  it("falls back to the default allowed origins when none are configured", () => {
    const env = { ALLOWED_ORIGINS: "" } as Parameters<typeof getAllowedOrigins>[0];

    expect([...getAllowedOrigins(env)]).toEqual([
      "https://kevintcoughlin.com",
      "https://www.kevintcoughlin.com",
    ]);
  });
});

describe("upstream failure handling", () => {
  // Every not-found path returns JSON with CORS headers. An unhandled throw
  // would surface as a bare 500 with none, which a browser reports only as an
  // opaque CORS failure — exactly when the API is already unhealthy.
  function envWith(bucket: Partial<R2Bucket>) {
    return {
      BUCKET: bucket as R2Bucket,
      WEB_VITALS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      WEB_ERRORS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      ALLOWED_ORIGINS: "",
    };
  }

  const emptyBucket = envWith({
    get: async () => null,
    head: async () => null,
  });

  const brokenBucket = envWith({
    get: async () => {
      throw new Error("R2 is down");
    },
    head: async () => {
      throw new Error("R2 is down");
    },
  });

  it("returns 404 with CORS when latest.json is missing", async () => {
    const res = await worker.fetch(new Request("https://x/api/today"), emptyBucket);
    expect(res.status).toBe(404);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(await res.json()).toEqual({ error: "No artwork published yet" });
  });

  it("returns 404 for today.json and today.manifest.json too", async () => {
    for (const path of ["/api/today.json", "/api/today.manifest.json"]) {
      const res = await worker.fetch(new Request(`https://x${path}`), emptyBucket);
      expect(res.status).toBe(404);
      expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
    }
  });

  it("returns 503 with CORS when R2 throws", async () => {
    const res = await worker.fetch(new Request("https://x/api/today"), brokenBucket);
    expect(res.status).toBe(503);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    expect(await res.json()).toEqual({ error: "Upstream storage unavailable" });
  });

  it("does not mask a genuine R2 fault as a 404", async () => {
    const res = await worker.fetch(new Request(`https://x/api/2026-01-02`), brokenBucket);
    expect(res.status).toBe(503);
  });
});
