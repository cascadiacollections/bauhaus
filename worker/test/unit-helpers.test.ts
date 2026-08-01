import { describe, expect, it } from "vitest";

import worker, {
  classifyUA,
  getAllowedOrigins,
  hasInvalidFormat,
  isProgressive,
  isStrip,
} from "../src/index";

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

describe("format parameter validation", () => {
  it("accepts the supported values", () => {
    for (const value of ["avif", "webp", "jpeg", "auto", "AVIF", "Auto"]) {
      expect(hasInvalidFormat(new URL(`https://x/api/today?format=${value}`))).toBe(false);
    }
  });

  it("accepts an absent format parameter", () => {
    expect(hasInvalidFormat(new URL("https://x/api/today"))).toBe(false);
  });

  // A typo used to fall through to Accept negotiation, so ?format=png behaved
  // exactly like ?format=auto and the caller never learned it was ignored.
  it("rejects unsupported values", () => {
    for (const value of ["png", "jpg", "gif", ""]) {
      expect(hasInvalidFormat(new URL(`https://x/api/today?format=${value}`))).toBe(true);
    }
  });
});

describe("response contract", () => {
  function envWith(bucket: Partial<R2Bucket>) {
    return {
      BUCKET: bucket as R2Bucket,
      WEB_VITALS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      WEB_ERRORS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      ALLOWED_ORIGINS: "",
    };
  }

  const emptyBucket = envWith({ get: async () => null, head: async () => null });

  // 405 used to be text/plain with no CORS headers, so a browser saw an opaque
  // CORS error rather than the JSON shape every other failure returns.
  it("returns 405 as JSON with CORS", async () => {
    const res = await worker.fetch(
      new Request("https://x/api/today", { method: "DELETE" }),
      emptyBucket,
    );
    expect(res.status).toBe(405);
    expect(res.headers.get("Content-Type")).toBe("application/json");
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(await res.json()).toEqual({ error: "Method not allowed" });
  });

  it("returns 400 with CORS for an unsupported format", async () => {
    const res = await worker.fetch(new Request("https://x/api/today?format=png"), emptyBucket);
    expect(res.status).toBe(400);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(await res.json()).toEqual({
      error: "Unsupported format. Use avif, webp, jpeg, or auto",
    });
  });

  it("validates format before resolving the date, so a bad format never hits R2", async () => {
    let reads = 0;
    const counting = envWith({
      get: async () => {
        reads += 1;
        return null;
      },
      head: async () => null,
    });
    await worker.fetch(new Request("https://x/api/today?format=png"), counting);
    expect(reads).toBe(0);
  });

  it("returns 404 as no-store so a later publish is visible", async () => {
    const res = await worker.fetch(new Request("https://x/api/today"), emptyBucket);
    expect(res.status).toBe(404);
    expect(res.headers.get("Cache-Control")).toBe("no-store");
  });
});

describe("health endpoint", () => {
  function envWith(bucket: Partial<R2Bucket>) {
    return {
      BUCKET: bucket as R2Bucket,
      WEB_VITALS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      WEB_ERRORS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      ALLOWED_ORIGINS: "",
    };
  }

  function bucketWithLatest(dateStr: string) {
    return envWith({
      get: async (key: string) =>
        key === "latest.json"
          ? ({ json: async () => ({ date: dateStr }) } as unknown as R2ObjectBody)
          : null,
      head: async () => null,
    });
  }

  function isoDaysAgo(days: number): string {
    return new Date(Date.now() - days * 86_400_000).toISOString().slice(0, 10);
  }

  it("reports ok for artwork published today", async () => {
    const res = await worker.fetch(new Request("https://x/api/health"), bucketWithLatest(isoDaysAgo(0)));
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ status: "ok", stale_days: 0 });
  });

  it("tolerates one day of lag, since the cron runs at 04:00 UTC", async () => {
    const res = await worker.fetch(new Request("https://x/api/health"), bucketWithLatest(isoDaysAgo(1)));
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ status: "ok", stale_days: 1 });
  });

  // The failure this endpoint exists for: the cron silently stopped running,
  // which produces no workflow run and therefore no ntfy notification.
  it("reports stale with a 503 once publishing falls behind", async () => {
    const res = await worker.fetch(new Request("https://x/api/health"), bucketWithLatest(isoDaysAgo(3)));
    expect(res.status).toBe(503);
    expect(await res.json()).toMatchObject({ status: "stale", stale_days: 3 });
  });

  it("reports unhealthy when nothing has been published", async () => {
    const res = await worker.fetch(
      new Request("https://x/api/health"),
      envWith({ get: async () => null, head: async () => null }),
    );
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ status: "unhealthy", error: "no artwork published" });
  });

  it("reports unhealthy when R2 is down", async () => {
    const res = await worker.fetch(
      new Request("https://x/api/health"),
      envWith({
        get: async () => {
          throw new Error("R2 is down");
        },
        head: async () => null,
      }),
    );
    expect(res.status).toBe(503);
    expect(await res.json()).toEqual({ status: "unhealthy", error: "storage unavailable" });
  });

  it("is never cached", async () => {
    const res = await worker.fetch(new Request("https://x/api/health"), bucketWithLatest(isoDaysAgo(0)));
    expect(res.headers.get("Cache-Control")).toBe("no-store");
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
  });
});

describe("metadata signature route", () => {
  function envWith(objects: Record<string, unknown>) {
    return {
      BUCKET: {
        get: async (key: string) => objects[key] ?? null,
        head: async (key: string) =>
          objects[key] ? ({ httpEtag: '"sig-etag"' } as unknown as R2Object) : null,
      } as unknown as R2Bucket,
      WEB_VITALS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      WEB_ERRORS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      ALLOWED_ORIGINS: "",
    };
  }

  const SIG = "-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n";

  function sigObject() {
    return {
      httpEtag: '"sig-etag"',
      body: new ReadableStream({
        start(c) {
          c.enqueue(new TextEncoder().encode(SIG));
          c.close();
        },
      }),
    };
  }

  // The pipeline could always upload metadata/<date>.json.sig, but no route
  // served it, so a signature could never be fetched and verified.
  it("serves the detached signature for a date", async () => {
    const env = envWith({ "metadata/2026/01/02.json.sig": sigObject() });
    const res = await worker.fetch(new Request("https://x/api/2026-01-02.json.sig"), env);
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/pgp-signature");
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(await res.text()).toBe(SIG);
  });

  it("404s with CORS when a date has no signature", async () => {
    const res = await worker.fetch(new Request("https://x/api/2026-01-02.json.sig"), envWith({}));
    expect(res.status).toBe(404);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(await res.json()).toEqual({ error: "No signature for 2026-01-02" });
  });

  it("does not shadow the plain metadata route", async () => {
    const env = envWith({ "metadata/2026/01/02.json.sig": sigObject() });
    const res = await worker.fetch(new Request("https://x/api/2026-01-02.json"), env);
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "No metadata for 2026-01-02" });
  });

  it("supports conditional requests", async () => {
    const env = envWith({ "metadata/2026/01/02.json.sig": sigObject() });
    const res = await worker.fetch(
      new Request("https://x/api/2026-01-02.json.sig", {
        headers: { "If-None-Match": '"sig-etag"' },
      }),
      env,
    );
    expect(res.status).toBe(304);
  });
});
