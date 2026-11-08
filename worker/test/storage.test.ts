import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import worker from "../src/index";
import {
  assetUrl,
  GitHubReleasesBucket,
  LATEST_CACHE,
  LATEST_WINDOW_DAYS,
  latestJson,
  locateKey,
  MISSING_TTL,
  StorageError,
  storageFor,
} from "../src/storage";

const REPO = "cascadiacollections/bauhaus";
const BASE = `https://github.com/${REPO}/releases/download`;
const IMMUTABLE = "public, max-age=31536000, s-maxage=31536000, immutable";
const TODAY_CACHE = "public, max-age=300, s-maxage=3600, stale-while-revalidate=604800";

const enc = new TextEncoder();

async function sha256Hex(data: string): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", enc.encode(data)));
  return Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("");
}

// ---------------------------------------------------------------------------
// A fake GitHub: release assets by URL, plus a recording fetch.
// ---------------------------------------------------------------------------

interface Release {
  /** asset name → body */
  assets: Record<string, string>;
  /** old R2 key → asset name; becomes objects.json */
  objects?: Record<string, string>;
}

const CONTENT_TYPES: Record<string, string> = {
  jpg: "image/jpeg",
  avif: "image/avif",
  webp: "image/webp",
  json: "application/json",
  sig: "application/pgp-signature",
};

async function buildGitHub(releases: Record<string, Release>): Promise<Map<string, string>> {
  const files = new Map<string, string>();
  for (const [tag, release] of Object.entries(releases)) {
    for (const [asset, body] of Object.entries(release.assets)) {
      files.set(`${BASE}/${tag}/${asset}`, body);
    }
    if (release.objects) {
      const objects: Record<string, unknown> = {};
      for (const [key, asset] of Object.entries(release.objects)) {
        const body = release.assets[asset];
        objects[key] = {
          asset,
          etag: await sha256Hex(body),
          size: enc.encode(body).byteLength,
          contentType: CONTENT_TYPES[asset.split(".").pop()!],
          cacheControl: IMMUTABLE,
          uploaded: "2026-11-08T04:05:06Z",
        };
      }
      files.set(`${BASE}/${tag}/objects.json`, JSON.stringify(objects));
    }
  }
  return files;
}

function stubFetch(files: Map<string, string>, failures: Record<string, number | "network"> = {}) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const failure = failures[url];
    if (failure === "network") throw new TypeError("network down");
    if (typeof failure === "number") return new Response("upstream", { status: failure });
    const body = files.get(url);
    if (body === undefined) return new Response("Not Found", { status: 404 });
    return new Response(body, {
      headers: { "Content-Type": "application/octet-stream", "Last-Modified": "Sun, 08 Nov 2026 04:05:06 GMT" },
    });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** In-memory caches.default honouring max-age, with a controllable clock. */
class FakeCache {
  entries = new Map<string, { response: Response; expires: number }>();
  puts: Array<{ url: string; cacheControl: string | null; missing: boolean }> = [];

  async match(req: RequestInfo | URL): Promise<Response | undefined> {
    const entry = this.entries.get(String(req));
    if (!entry || entry.expires <= Date.now()) return undefined;
    return entry.response.clone();
  }

  async put(req: RequestInfo | URL, response: Response): Promise<void> {
    const cc = response.headers.get("Cache-Control");
    const maxAge = Number(cc?.match(/max-age=(\d+)/)?.[1] ?? 0);
    const buffered = new Response(await response.arrayBuffer(), response);
    this.puts.push({ url: String(req), cacheControl: cc, missing: response.headers.has("X-Bauhaus-Missing") });
    this.entries.set(String(req), { response: buffered, expires: Date.now() + maxAge * 1000 });
  }
}

function makeCtx() {
  const pending: Promise<unknown>[] = [];
  return {
    ctx: { waitUntil: (p: Promise<unknown>) => void pending.push(p) } as unknown as ExecutionContext,
    flush: () => Promise.all(pending),
  };
}

function env() {
  return {
    ASSETS_REPO: REPO,
    WEB_VITALS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
    WEB_ERRORS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
    ALLOWED_ORIGINS: "",
  };
}

/** A fully published day, as the publisher's contract lays it out. */
function day(
  date: string,
  opts: { avif?: boolean; webp?: boolean; sig?: boolean; dates?: string[] } = {},
): [string, Release] {
  const p = date.replaceAll("-", "/");
  const assets: Record<string, string> = {
    "original.jpg": `original-${date}`,
    "stylized.jpg": `stylized-jpg-${date}`,
    "stylized.progressive.jpg": `stylized-progressive-${date}`,
    "stylized.stripped.jpg": `stylized-stripped-${date}`,
    "metadata.json": JSON.stringify({ date, title: `Art ${date}`, artist: "A", generated_at: `${date}T04:00:00Z` }),
    "manifest.json": JSON.stringify({ date, sizes: [] }),
    // Not listed in objects.json, per the contract.
    "dates.json": JSON.stringify({ dates: opts.dates ?? [date] }),
  };
  const objects: Record<string, string> = {
    [`originals/${p}.jpg`]: "original.jpg",
    [`stylized/${p}.jpg`]: "stylized.jpg",
    [`stylized/${p}.progressive.jpg`]: "stylized.progressive.jpg",
    [`stylized/${p}.stripped.jpg`]: "stylized.stripped.jpg",
    [`metadata/${p}.json`]: "metadata.json",
    [`manifests/${p}.json`]: "manifest.json",
  };
  if (opts.avif !== false) {
    assets["stylized.avif"] = `stylized-avif-${date}`;
    objects[`stylized/${p}.avif`] = "stylized.avif";
  }
  if (opts.webp !== false) {
    assets["stylized.webp"] = `stylized-webp-${date}`;
    objects[`stylized/${p}.webp`] = "stylized.webp";
  }
  if (opts.sig) {
    assets["metadata.json.sig"] = `sig-${date}`;
    objects[`metadata/${p}.json.sig`] = "metadata.json.sig";
  }
  return [`art-${date}`, { assets, objects }];
}

/** A clock fixed at noon UTC on `date`. */
function at(date: string): () => Date {
  return () => new Date(`${date}T12:00:00Z`);
}

function bucketAt(date: string, ctx?: ExecutionContext) {
  return new GitHubReleasesBucket({ ASSETS_REPO: REPO }, ctx, at(date));
}

function urls(fetchMock: ReturnType<typeof stubFetch>): string[] {
  return fetchMock.mock.calls.map(([u]) => String(u));
}

let cache: FakeCache;

beforeEach(() => {
  cache = new FakeCache();
  vi.stubGlobal("caches", { default: cache });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// Key mapping
// ---------------------------------------------------------------------------

describe("locateKey", () => {
  it.each([
    ["originals/2026/11/08.jpg", "art-2026-11-08"],
    ["stylized/2026/11/08.jpg", "art-2026-11-08"],
    ["stylized/2026/11/08.avif", "art-2026-11-08"],
    ["stylized/2026/11/08.webp", "art-2026-11-08"],
    ["stylized/2026/11/08.progressive.jpg", "art-2026-11-08"],
    ["stylized/2026/11/08.stripped.jpg", "art-2026-11-08"],
    ["metadata/2026/11/08.json", "art-2026-11-08"],
    ["metadata/2026/11/08.json.sig", "art-2026-11-08"],
    ["manifests/2026/11/08.json", "art-2026-11-08"],
  ])("maps %s to release %s", (key, tag) => {
    expect(locateKey(key)).toEqual({ kind: "day", date: "2026-11-08", tag });
  });

  it("maps latest.json to the synthesized pointer", () => {
    expect(locateKey("latest.json")).toEqual({ kind: "latest" });
  });

  it.each(["other/2026/11/08.jpg", "stylized/2026/11.jpg", "objects.json", "dates.json", "../latest.json"])(
    "rejects %s",
    (key) => {
      expect(locateKey(key)).toBeNull();
    },
  );

  it("builds release download URLs, not api.github.com", () => {
    expect(assetUrl(REPO, "art-2026-11-08", "stylized.webp")).toBe(
      `${BASE}/art-2026-11-08/stylized.webp`,
    );
  });
});

describe("GitHubReleasesBucket construction", () => {
  it("rejects a missing or malformed ASSETS_REPO", () => {
    expect(() => new GitHubReleasesBucket({})).toThrow(StorageError);
    expect(() => new GitHubReleasesBucket({ ASSETS_REPO: "https://github.com/x/y" })).toThrow(StorageError);
  });

  it("storageFor prefers an injected bucket and otherwise builds the adapter", () => {
    const injected = {} as R2Bucket;
    expect(storageFor({ BUCKET: injected })).toBe(injected);
    expect(storageFor({ ASSETS_REPO: REPO })).toBeInstanceOf(GitHubReleasesBucket);
  });

  it("is read-only", async () => {
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    await expect(bucket.put("k", "v")).rejects.toThrow(StorageError);
    await expect(bucket.delete("k")).rejects.toThrow(StorageError);
  });
});

// ---------------------------------------------------------------------------
// head / get
// ---------------------------------------------------------------------------

describe("head and get for day keys", () => {
  const D = "2026-11-08";

  it("looks the key up in objects.json and returns R2-shaped metadata", async () => {
    const fetchMock = stubFetch(await buildGitHub(Object.fromEntries([day(D)])));
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    const etag = await sha256Hex(`stylized-webp-${D}`);

    const head = await bucket.head("stylized/2026/11/08.webp");
    expect(head).not.toBeNull();
    expect(head!.key).toBe("stylized/2026/11/08.webp");
    expect(head!.etag).toBe(etag);
    expect(head!.httpEtag).toBe(`"${etag}"`);
    expect(head!.size).toBe(`stylized-webp-${D}`.length);
    expect(head!.uploaded).toEqual(new Date("2026-11-08T04:05:06Z"));
    expect(head!.httpMetadata).toEqual({ contentType: "image/webp", cacheControl: IMMUTABLE });

    const headers = new Headers();
    head!.writeHttpMetadata(headers);
    expect(headers.get("Content-Type")).toBe("image/webp");
    expect(headers.get("Cache-Control")).toBe(IMMUTABLE);

    // head() reads objects.json only — never the asset bytes.
    expect(fetchMock.mock.calls.map(([u]) => String(u))).toEqual([`${BASE}/art-${D}/objects.json`]);
  });

  it("never fetches asset bytes on head(), for any key", async () => {
    const fetchMock = stubFetch(await buildGitHub(Object.fromEntries([day(D, { sig: true })])));
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    for (const key of [
      "originals/2026/11/08.jpg",
      "stylized/2026/11/08.jpg",
      "stylized/2026/11/08.avif",
      "stylized/2026/11/08.progressive.jpg",
      "stylized/2026/11/08.stripped.jpg",
      "metadata/2026/11/08.json",
      "metadata/2026/11/08.json.sig",
      "manifests/2026/11/08.json",
    ]) {
      expect(await bucket.head(key)).not.toBeNull();
    }
    const urls = new Set(fetchMock.mock.calls.map(([u]) => String(u)));
    expect([...urls]).toEqual([`${BASE}/art-${D}/objects.json`]);
  });

  it.each([
    ["originals/2026/11/08.jpg", "original.jpg"],
    ["stylized/2026/11/08.jpg", "stylized.jpg"],
    ["stylized/2026/11/08.avif", "stylized.avif"],
    ["stylized/2026/11/08.webp", "stylized.webp"],
    ["stylized/2026/11/08.progressive.jpg", "stylized.progressive.jpg"],
    ["stylized/2026/11/08.stripped.jpg", "stylized.stripped.jpg"],
    ["metadata/2026/11/08.json", "metadata.json"],
    ["metadata/2026/11/08.json.sig", "metadata.json.sig"],
    ["manifests/2026/11/08.json", "manifest.json"],
  ])("get(%s) downloads asset %s", async (key, asset) => {
    const files = await buildGitHub(Object.fromEntries([day(D, { sig: true })]));
    const fetchMock = stubFetch(files);
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });

    const obj = await bucket.get(key);
    expect(obj).not.toBeNull();
    expect(await obj!.text()).toBe(files.get(`${BASE}/art-${D}/${asset}`));
    expect(fetchMock.mock.calls.map(([u]) => String(u))).toContain(`${BASE}/art-${D}/${asset}`);
  });

  it("exposes the body as a stream and supports json() and arrayBuffer()", async () => {
    stubFetch(await buildGitHub(Object.fromEntries([day(D)])));
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });

    const streamed = await bucket.get("stylized/2026/11/08.jpg");
    expect(streamed!.body).toBeInstanceOf(ReadableStream);
    expect(await new Response(streamed!.body).text()).toBe(`stylized-jpg-${D}`);

    const meta = await bucket.get("metadata/2026/11/08.json");
    expect((await meta!.json<{ date: string }>()).date).toBe(D);

    const buf = await (await bucket.get("manifests/2026/11/08.json"))!.arrayBuffer();
    expect(new TextDecoder().decode(buf)).toContain(D);
  });

  it("returns null for a key absent from objects.json", async () => {
    stubFetch(await buildGitHub(Object.fromEntries([day(D, { avif: false })])));
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    expect(await bucket.head("stylized/2026/11/08.avif")).toBeNull();
    expect(await bucket.get("stylized/2026/11/08.avif")).toBeNull();
    expect(await bucket.head("metadata/2026/11/08.json.sig")).toBeNull();
  });

  it("returns null when the day's release does not exist", async () => {
    stubFetch(new Map());
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    expect(await bucket.head("stylized/2026/11/08.jpg")).toBeNull();
    expect(await bucket.get("metadata/2026/11/08.json")).toBeNull();
  });

  it("returns null for keys outside the layout without fetching", async () => {
    const fetchMock = stubFetch(new Map());
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    expect(await bucket.get("unknown/thing")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("parses objects.json once per request across several head() calls", async () => {
    const fetchMock = stubFetch(await buildGitHub(Object.fromEntries([day(D)])));
    vi.stubGlobal("caches", undefined);
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    await bucket.head("stylized/2026/11/08.progressive.jpg");
    await bucket.head("stylized/2026/11/08.avif");
    await bucket.head("stylized/2026/11/08.jpg");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([500, 502, 503, 403, 429])("throws StorageError on upstream %i", async (status) => {
    stubFetch(new Map(), { [`${BASE}/art-${D}/objects.json`]: status });
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    await expect(bucket.head("stylized/2026/11/08.jpg")).rejects.toThrow(StorageError);
  });

  it("throws StorageError on a network failure", async () => {
    stubFetch(new Map(), { [`${BASE}/art-${D}/objects.json`]: "network" });
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    await expect(bucket.get("stylized/2026/11/08.jpg")).rejects.toThrow(StorageError);
  });

  it("throws StorageError when the asset download fails after the lookup", async () => {
    const files = await buildGitHub(Object.fromEntries([day(D)]));
    stubFetch(files, { [`${BASE}/art-${D}/stylized.jpg`]: 502 });
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    await expect(bucket.get("stylized/2026/11/08.jpg")).rejects.toThrow(StorageError);
  });

  it("throws StorageError on a malformed objects.json", async () => {
    stubFetch(new Map([[`${BASE}/art-${D}/objects.json`, "not json"]]));
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    await expect(bucket.head("stylized/2026/11/08.jpg")).rejects.toThrow(StorageError);
  });
});

describe("latest-date probe", () => {
  const D = "2026-11-08";

  it("finds today when today's release exists, with one fetch", async () => {
    const fetchMock = stubFetch(await buildGitHub(Object.fromEntries([day(D), day("2026-11-07")])));
    const obj = await bucketAt(D).get("latest.json");
    expect(await obj!.json()).toEqual({ date: D });
    expect(urls(fetchMock)).toEqual([`${BASE}/art-${D}/objects.json`]);
  });

  it("finds yesterday when today 404s, and stops at the first hit", async () => {
    const fetchMock = stubFetch(
      await buildGitHub(Object.fromEntries([day("2026-11-07"), day("2026-11-06"), day("2026-11-05")])),
    );
    const obj = await bucketAt(D).get("latest.json");
    expect(await obj!.json()).toEqual({ date: "2026-11-07" });
    expect(urls(fetchMock)).toEqual([
      `${BASE}/art-2026-11-08/objects.json`,
      `${BASE}/art-2026-11-07/objects.json`,
    ]);
  });

  it("probes in UTC across a month boundary", async () => {
    const fetchMock = stubFetch(await buildGitHub(Object.fromEntries([day("2026-10-30")])));
    const bucket = new GitHubReleasesBucket({ ASSETS_REPO: REPO }, undefined, () => new Date("2026-11-01T00:30:00Z"));
    expect(await (await bucket.get("latest.json"))!.json()).toEqual({ date: "2026-10-30" });
    expect(urls(fetchMock)).toEqual([
      `${BASE}/art-2026-11-01/objects.json`,
      `${BASE}/art-2026-10-31/objects.json`,
      `${BASE}/art-2026-10-30/objects.json`,
    ]);
  });

  it(`finds a day ${LATEST_WINDOW_DAYS - 1} days back but not ${LATEST_WINDOW_DAYS}`, async () => {
    stubFetch(await buildGitHub(Object.fromEntries([day("2026-10-26")])));
    expect(await (await bucketAt(D).get("latest.json"))!.json()).toEqual({ date: "2026-10-26" });

    cache = new FakeCache();
    vi.stubGlobal("caches", { default: cache });
    stubFetch(await buildGitHub(Object.fromEntries([day("2026-10-25")])));
    expect(await bucketAt(D).get("latest.json")).toBeNull();
  });

  it("returns null and an empty list when the whole window is unpublished", async () => {
    const fetchMock = stubFetch(new Map());
    const bucket = bucketAt(D);
    expect(await bucket.get("latest.json")).toBeNull();
    expect(await bucket.head("latest.json")).toBeNull();
    const page = await bucket.list({ prefix: "metadata/" });
    expect(page.objects).toEqual([]);
    expect(page.truncated).toBe(false);
    expect(LATEST_WINDOW_DAYS).toBe(14);
    expect(urls(fetchMock)).toHaveLength(LATEST_WINDOW_DAYS);
    expect(urls(fetchMock).at(-1)).toBe(`${BASE}/art-2026-10-26/objects.json`);
  });

  it("throws StorageError on a non-404 failure instead of skipping the day", async () => {
    vi.stubGlobal("caches", undefined);
    stubFetch(await buildGitHub(Object.fromEntries([day("2026-11-06")])), {
      [`${BASE}/art-2026-11-07/objects.json`]: 502,
    });
    await expect(bucketAt(D).get("latest.json")).rejects.toThrow(StorageError);
    stubFetch(new Map(), { [`${BASE}/art-${D}/objects.json`]: "network" });
    await expect(bucketAt(D).list({ prefix: "metadata/" })).rejects.toThrow(StorageError);
  });

  it("probes at most once per instance", async () => {
    vi.stubGlobal("caches", undefined);
    const fetchMock = stubFetch(await buildGitHub(Object.fromEntries([day("2026-11-06", { dates: ["2026-11-06"] })])));
    const bucket = bucketAt(D);
    await Promise.all([bucket.get("latest.json"), bucket.head("latest.json"), bucket.list({ prefix: "metadata/" })]);
    await bucket.get("latest.json");
    await bucket.list({ prefix: "metadata/" });
    // Three objects.json probes and one dates.json, never repeated.
    expect(urls(fetchMock)).toEqual([
      `${BASE}/art-2026-11-08/objects.json`,
      `${BASE}/art-2026-11-07/objects.json`,
      `${BASE}/art-2026-11-06/objects.json`,
      `${BASE}/art-2026-11-06/dates.json`,
    ]);
  });

  it("reuses the probe's objects.json for the latest day's lookups", async () => {
    vi.stubGlobal("caches", undefined);
    const fetchMock = stubFetch(await buildGitHub(Object.fromEntries([day(D)])));
    const bucket = bucketAt(D);
    await bucket.get("latest.json");
    await bucket.head("stylized/2026/11/08.avif");
    expect(urls(fetchMock)).toEqual([`${BASE}/art-${D}/objects.json`]);
  });
});

describe("synthesized latest.json", () => {
  const D = "2026-11-08";

  it("has json.dumps bytes and a sha256 etag of them", async () => {
    stubFetch(await buildGitHub(Object.fromEntries([day(D)])));
    const bytes = '{"date": "2026-11-08"}';
    expect(latestJson(D)).toBe(bytes);
    const etag = await sha256Hex(bytes);

    const obj = await bucketAt(D).get("latest.json");
    expect(await obj!.text()).toBe(bytes);
    expect(obj!.key).toBe("latest.json");
    expect(obj!.etag).toBe(etag);
    expect(obj!.httpEtag).toBe(`"${etag}"`);
    expect(obj!.size).toBe(bytes.length);
    expect(obj!.httpMetadata).toEqual({ contentType: "application/json", cacheControl: LATEST_CACHE });

    const head = await bucketAt(D).head("latest.json");
    expect(head!.httpEtag).toBe(`"${etag}"`);
  });

  it("matches the publisher's LATEST_CACHE", () => {
    expect(LATEST_CACHE).toBe("public, max-age=300");
  });
});

describe("list", () => {
  const D = "2026-11-08";

  it("lists metadata keys from the latest day's dates.json, ascending, in one page", async () => {
    const fetchMock = stubFetch(
      await buildGitHub(Object.fromEntries([day(D, { dates: [D, "2026-01-01", "2025-12-31"] })])),
    );
    const page = await bucketAt(D).list({ prefix: "metadata/" });
    expect(page.objects.map((o) => o.key)).toEqual([
      "metadata/2025/12/31.json",
      "metadata/2026/01/01.json",
      "metadata/2026/11/08.json",
    ]);
    expect(page.truncated).toBe(false);
    expect(page.delimitedPrefixes).toEqual([]);
    expect(urls(fetchMock)).toEqual([`${BASE}/art-${D}/objects.json`, `${BASE}/art-${D}/dates.json`]);
  });

  it("reads the snapshot of the latest day, not an older one", async () => {
    stubFetch(
      await buildGitHub(
        Object.fromEntries([
          day("2026-11-07", { dates: ["2026-11-07", "2026-11-06"] }),
          day("2026-11-06", { dates: ["2026-11-06"] }),
        ]),
      ),
    );
    const page = await bucketAt(D).list({ prefix: "metadata/" });
    expect(page.objects.map((o) => o.key)).toEqual(["metadata/2026/11/06.json", "metadata/2026/11/07.json"]);
  });

  it("ignores the cursor and returns the same single page", async () => {
    stubFetch(await buildGitHub(Object.fromEntries([day(D)])));
    const page = await bucketAt(D).list({ prefix: "metadata/", cursor: "anything" });
    expect(page.objects.map((o) => o.key)).toEqual(["metadata/2026/11/08.json"]);
  });

  it("filters by prefix", async () => {
    stubFetch(await buildGitHub(Object.fromEntries([day(D)])));
    expect((await bucketAt(D).list({ prefix: "stylized/" })).objects).toEqual([]);
    expect((await bucketAt(D).list({ prefix: "metadata/2026/11/" })).objects).toHaveLength(1);
  });

  it("falls back to the latest day alone when its dates.json is missing", async () => {
    const files = await buildGitHub(Object.fromEntries([day(D)]));
    files.delete(`${BASE}/art-${D}/dates.json`);
    stubFetch(files);
    vi.spyOn(console, "error").mockImplementation(() => {});
    const page = await bucketAt(D).list({ prefix: "metadata/" });
    expect(page.objects.map((o) => o.key)).toEqual(["metadata/2026/11/08.json"]);
  });

  it("throws StorageError when dates.json is malformed or fails upstream", async () => {
    const files = await buildGitHub(Object.fromEntries([day(D)]));
    files.set(`${BASE}/art-${D}/dates.json`, JSON.stringify({ nope: 1 }));
    stubFetch(files);
    await expect(bucketAt(D).list({ prefix: "metadata/" })).rejects.toThrow(StorageError);
    stubFetch(files, { [`${BASE}/art-${D}/dates.json`]: 500 });
    await expect(bucketAt(D).list({ prefix: "metadata/" })).rejects.toThrow(StorageError);
  });
});

// ---------------------------------------------------------------------------
// Caching
// ---------------------------------------------------------------------------

describe("caching", () => {
  const D = "2026-11-08";

  it("caches every asset that exists for a year and probe misses for MISSING_TTL", async () => {
    stubFetch(await buildGitHub(Object.fromEntries([day("2026-11-07")])));
    const { ctx, flush } = makeCtx();
    const bucket = bucketAt(D, ctx);
    await (await bucket.get("stylized/2026/11/07.jpg"))!.text();
    await bucket.get("latest.json");
    await bucket.list({ prefix: "metadata/" });
    await flush();

    const byUrl = Object.fromEntries(cache.puts.map((p) => [p.url, p.cacheControl]));
    expect(byUrl).toEqual({
      [`${BASE}/art-2026-11-08/objects.json`]: `public, max-age=${MISSING_TTL}`,
      [`${BASE}/art-2026-11-07/objects.json`]: "public, max-age=31536000, immutable",
      [`${BASE}/art-2026-11-07/stylized.jpg`]: "public, max-age=31536000, immutable",
      [`${BASE}/art-2026-11-07/dates.json`]: "public, max-age=31536000, immutable",
    });
    expect(cache.puts.find((p) => p.url.includes("2026-11-08"))!.missing).toBe(true);
  });

  it("answers a warm probe from the cache without calling GitHub", async () => {
    const fetchMock = stubFetch(await buildGitHub(Object.fromEntries([day("2026-11-07")])));
    const first = makeCtx();
    await bucketAt(D, first.ctx).get("latest.json");
    await first.flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(await (await bucketAt(D).get("latest.json"))!.json()).toEqual({ date: "2026-11-07" });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("answers repeat requests from the cache without calling GitHub", async () => {
    const fetchMock = stubFetch(await buildGitHub(Object.fromEntries([day(D)])));
    const first = makeCtx();
    const a = new GitHubReleasesBucket({ ASSETS_REPO: REPO }, first.ctx);
    expect(await (await a.get("stylized/2026/11/08.jpg"))!.text()).toBe(`stylized-jpg-${D}`);
    await first.flush();
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const b = new GitHubReleasesBucket({ ASSETS_REPO: REPO });
    expect(await (await b.get("stylized/2026/11/08.jpg"))!.text()).toBe(`stylized-jpg-${D}`);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("caches a 404 briefly so a day published later becomes visible", async () => {
    vi.useFakeTimers({ now: Date.parse("2026-11-08T03:59:00Z"), toFake: ["Date"] });
    const files = new Map<string, string>();
    const fetchMock = stubFetch(files);

    const first = makeCtx();
    expect(await new GitHubReleasesBucket({ ASSETS_REPO: REPO }, first.ctx).head("stylized/2026/11/08.jpg")).toBeNull();
    await first.flush();
    expect(cache.puts).toEqual([
      { url: `${BASE}/art-${D}/objects.json`, cacheControl: `public, max-age=${MISSING_TTL}`, missing: true },
    ]);
    expect(MISSING_TTL).toBeLessThanOrEqual(60);

    // The day publishes; the negative entry still answers inside its TTL.
    for (const [k, v] of await buildGitHub(Object.fromEntries([day(D)]))) files.set(k, v);
    expect(await new GitHubReleasesBucket({ ASSETS_REPO: REPO }).head("stylized/2026/11/08.jpg")).toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // Once it expires, the new release is seen.
    vi.setSystemTime(Date.now() + (MISSING_TTL + 1) * 1000);
    expect(await new GitHubReleasesBucket({ ASSETS_REPO: REPO }).head("stylized/2026/11/08.jpg")).not.toBeNull();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("never caches upstream errors", async () => {
    stubFetch(new Map(), { [`${BASE}/art-${D}/objects.json`]: 503 });
    const { ctx, flush } = makeCtx();
    await expect(new GitHubReleasesBucket({ ASSETS_REPO: REPO }, ctx).head("stylized/2026/11/08.jpg")).rejects.toThrow();
    await flush();
    expect(cache.puts).toEqual([]);
  });

  it("works without a Cache API", async () => {
    vi.stubGlobal("caches", undefined);
    stubFetch(await buildGitHub(Object.fromEntries([day(D)])));
    const obj = await new GitHubReleasesBucket({ ASSETS_REPO: REPO }).get("stylized/2026/11/08.jpg");
    expect(await obj!.text()).toBe(`stylized-jpg-${D}`);
  });
});

// ---------------------------------------------------------------------------
// End to end: the real routes over the adapter and a fake GitHub
// ---------------------------------------------------------------------------

describe("routes over GitHub Releases", () => {
  const D = "2026-11-08";
  const PREV = "2026-11-07";

  beforeEach(() => {
    vi.useFakeTimers({ now: Date.parse(`${D}T12:00:00Z`), toFake: ["Date"] });
  });

  async function setup(failures: Record<string, number | "network"> = {}) {
    const files = await buildGitHub(
      Object.fromEntries([day(D, { sig: true, dates: [D, PREV] }), day(PREV, { avif: false, dates: [PREV] })]),
    );
    const fetchMock = stubFetch(files, failures);
    return { files, fetchMock };
  }

  function req(path: string, init: RequestInit = {}) {
    return new Request(`https://bauhaus.example${path}`, init);
  }

  it("serves /api/today with negotiated format, strong ETag and today's cache", async () => {
    await setup();
    const res = await worker.fetch(req("/api/today", { headers: { Accept: "image/avif" } }), env());
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/avif");
    expect(res.headers.get("ETag")).toBe(`"${await sha256Hex(`stylized-avif-${D}`)}"`);
    expect(res.headers.get("Cache-Control")).toBe(TODAY_CACHE);
    expect(res.headers.get("X-Variant")).toBe("baseline");
    expect(await res.text()).toBe(`stylized-avif-${D}`);
  });

  it("falls back to JPEG when the day has no AVIF", async () => {
    await setup();
    const res = await worker.fetch(req(`/api/${PREV}?format=avif`), env());
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("image/jpeg");
    expect(res.headers.get("Cache-Control")).toBe(IMMUTABLE);
    expect(await res.text()).toBe(`stylized-jpg-${PREV}`);
  });

  it("serves progressive and stripped variants and the original", async () => {
    await setup();
    const prog = await worker.fetch(req(`/api/${D}?progressive=true`), env());
    expect(prog.headers.get("X-Variant")).toBe("progressive");
    expect(await prog.text()).toBe(`stylized-progressive-${D}`);
    const strip = await worker.fetch(req(`/api/${D}?strip=true`), env());
    expect(strip.headers.get("X-Variant")).toBe("stripped");
    const orig = await worker.fetch(req(`/api/${D}/original`), env());
    expect(await orig.text()).toBe(`original-${D}`);
  });

  it("answers If-None-Match with a 304 without downloading the image", async () => {
    const { fetchMock } = await setup();
    const etag = `"${await sha256Hex(`stylized-webp-${D}`)}"`;
    const res = await worker.fetch(req(`/api/${D}?format=webp`, { headers: { "If-None-Match": etag } }), env());
    expect(res.status).toBe(304);
    expect(res.headers.get("ETag")).toBe(etag);
    expect(res.headers.get("Cache-Control")).toBe(IMMUTABLE);
    const urls = fetchMock.mock.calls.map(([u]) => String(u));
    expect(urls.some((u) => u.endsWith("/stylized.webp"))).toBe(false);
  });

  it("answers HEAD from objects.json alone", async () => {
    const { fetchMock } = await setup();
    const res = await worker.fetch(req(`/api/${D}.json`, { method: "HEAD" }), env());
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/json");
    expect(res.headers.get("ETag")).toMatch(/^"[0-9a-f]{64}"$/);
    expect(fetchMock.mock.calls.map(([u]) => String(u))).toEqual([`${BASE}/art-${D}/objects.json`]);
  });

  it("serves metadata, manifest and signature routes", async () => {
    await setup();
    const meta = await worker.fetch(req("/api/today.json"), env());
    expect(((await meta.json()) as { date: string }).date).toBe(D);
    const manifest = await worker.fetch(req(`/api/${PREV}.manifest.json`), env());
    expect(((await manifest.json()) as { date: string }).date).toBe(PREV);
    const sig = await worker.fetch(req(`/api/${D}.json.sig`), env());
    expect(sig.headers.get("Content-Type")).toBe("application/pgp-signature");
    expect(await sig.text()).toBe(`sig-${D}`);
    const noSig = await worker.fetch(req(`/api/${PREV}.json.sig`), env());
    expect(noSig.status).toBe(404);
  });

  it("404s for an unpublished date", async () => {
    await setup();
    const res = await worker.fetch(req("/api/2020-01-01"), env());
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: "No image for 2020-01-01" });
  });

  it("serves the archive and feed from dates.json", async () => {
    await setup();
    const health = await worker.fetch(req("/api/health"), env());
    expect(await health.json()).toEqual({ status: "ok", date: D, stale_days: 0 });
    const archive = await worker.fetch(req("/api/archive"), env());
    expect(await archive.json()).toEqual({ dates: [D, PREV], count: 2, total: 2 });
    const feed = await worker.fetch(req("/api/feed.atom"), env());
    expect(feed.status).toBe(200);
    const xml = await feed.text();
    expect(xml.indexOf(`Art ${D}`)).toBeLessThan(xml.indexOf(`Art ${PREV}`));
  });

  it("reports nothing published when the releases do not exist yet", async () => {
    stubFetch(new Map());
    expect((await worker.fetch(req("/api/today"), env())).status).toBe(404);
    expect(await (await worker.fetch(req("/api/archive"), env())).json()).toEqual({ dates: [], count: 0, total: 0 });
    const health = await worker.fetch(req("/api/health"), env());
    expect(health.status).toBe(503);
    expect(await health.json()).toEqual({ status: "unhealthy", error: "no artwork published" });
  });

  it("maps an upstream failure to the same 503 an R2 fault produced", async () => {
    await setup({ [`${BASE}/art-${D}/objects.json`]: 500 });
    vi.spyOn(console, "error").mockImplementation(() => {});
    const res = await worker.fetch(req("/api/today"), env());
    expect(res.status).toBe(503);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(await res.json()).toEqual({ error: "Upstream storage unavailable" });

    const health = await worker.fetch(req("/api/health"), env());
    expect(health.status).toBe(503);
    expect(await health.json()).toEqual({ status: "unhealthy", error: "storage unavailable" });
  });

  it("reads every key the history cron commits", async () => {
    await setup();
    const bucket = storageFor({ ASSETS_REPO: REPO });
    for (const key of ["metadata/2026/11/08.json", "metadata/2026/11/08.json.sig", "manifests/2026/11/08.json"]) {
      expect(await bucket.get(key)).not.toBeNull();
    }
  });
});
