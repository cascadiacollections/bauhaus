/**
 * Read-only R2Bucket adapter over GitHub Releases on ASSETS_REPO.
 *
 * R2 is switched off on the account, so the publisher (src/upload.py) writes
 * each day's objects as release assets instead. This class answers the exact
 * subset of the R2Bucket API the routes use — get, head, list — so the route
 * logic (ETags, 304s, HEAD-before-GET, format negotiation, feed, history) is
 * unchanged and still sees R2-shaped objects.
 *
 * Layout (the contract shared with the publisher):
 *
 *   art-YYYY-MM-DD   original.jpg, stylized.jpg, stylized.{avif,webp,...},
 *                    metadata.json, metadata.json.sig, manifest.json,
 *                    objects.json — maps each old R2 key to
 *                    {asset, etag, size, contentType, cacheControl, uploaded}.
 *                    A key absent from objects.json does not exist.
 *                    dates.json — {"dates": [...]}, every date published up
 *                    to and including this one, newest first.
 *
 * There is no index release: the repo has immutable releases, so no asset can
 * ever be replaced. The latest date is found by probing day releases newest
 * first from the current UTC date (LATEST_WINDOW_DAYS); latest.json is
 * synthesized from it, and the archive comes from that day's dates.json.
 *
 * Downloads go to https://github.com/{repo}/releases/download/{tag}/{asset},
 * which needs no token and is not subject to api.github.com's per-IP rate
 * limit; GitHub 302s to its asset CDN and fetch() follows. Every download goes
 * through the Cache API so a busy day costs GitHub almost nothing:
 *
 *   any asset that exists   1 year (releases are immutable)
 *   a 404 from GitHub       MISSING_TTL seconds, so a day published later —
 *                           including today, probed before its publish —
 *                           becomes visible quickly
 *
 * A 404 is "not found" (get/head return null, as R2 did). Any other non-2xx
 * status or a network failure throws, which index.ts's top-level handler turns
 * into the same 503 "Upstream storage unavailable" an R2 fault produced.
 */

/** Seconds a release asset that exists stays cached: one year. */
export const IMMUTABLE_TTL = 31_536_000;

/** Seconds a 404 for a release asset stays cached. */
export const MISSING_TTL = 60;

/**
 * How many UTC days the latest-date probe covers: today, today-1, ...,
 * today-13. Nothing in that window reads as "no artwork published", as a
 * missing latest.json did — /api/health reports stale long before then.
 */
export const LATEST_WINDOW_DAYS = 14;

/**
 * Cache-Control the publisher stored on latest.json in R2 (LATEST_CACHE in
 * src/upload.py), reported on the synthesized latest.json.
 */
export const LATEST_CACHE = "public, max-age=300";

/** Marks a cached negative entry; a cached 200 with this header means 404. */
const MISSING_HEADER = "X-Bauhaus-Missing";

/** An upstream failure that is not "not found" — surfaces as a 503. */
export class StorageError extends Error {
  override name = "StorageError";
}

/** The bindings and vars storage needs. */
export interface StorageEnv {
  /**
   * Optional override, used by tests to inject a fake bucket. Production has
   * no R2 binding (Cloudflare rejects deploys that bind R2 on this account).
   */
  BUCKET?: R2Bucket;
  /** "owner/repo" whose releases hold the art, from wrangler.toml [vars]. */
  ASSETS_REPO?: string;
}

/** Lets cache writes outlive the response instead of delaying it. */
export interface WaitUntil {
  waitUntil(promise: Promise<unknown>): void;
}

/**
 * The single construction point for the Worker's storage. Callers keep their
 * R2Bucket-typed parameters; only this decides what backs them.
 */
export function storageFor(env: StorageEnv, ctx?: WaitUntil, now?: () => Date): R2Bucket {
  return env.BUCKET ?? new GitHubReleasesBucket(env, ctx, now);
}

/** latest.json's bytes, exactly as Python's json.dumps({"date": date}) wrote them. */
export function latestJson(date: string): string {
  return `{"date": "${date}"}`;
}

/** One entry of a day release's objects.json. */
export interface ObjectEntry {
  asset: string;
  etag: string;
  size: number;
  contentType: string;
  cacheControl: string;
  uploaded: string;
}

type DayLocation = { kind: "day"; date: string; tag: string };
type LatestLocation = { kind: "latest" };

/**
 * Where an R2 key lives: a day release (looked up via objects.json) or the
 * synthesized latest.json. Null for any other key — R2 had nothing there.
 */
export function locateKey(key: string): DayLocation | LatestLocation | null {
  if (key === "latest.json") return { kind: "latest" };
  const m = key.match(/^(?:originals|stylized|metadata|manifests)\/(\d{4})\/(\d{2})\/(\d{2})\.[^/]+$/);
  if (!m) return null;
  const date = `${m[1]}-${m[2]}-${m[3]}`;
  return { kind: "day", date, tag: tagFor(date) };
}

function tagFor(date: string): string {
  return `art-${date}`;
}

const REPO_PATTERN = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const ASSET_PATTERN = /^[A-Za-z0-9_][A-Za-z0-9_.-]*$/;
const DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export function assetUrl(repo: string, tag: string, asset: string): string {
  return `https://github.com/${repo}/releases/download/${encodeURIComponent(tag)}/${encodeURIComponent(asset)}`;
}

function hexToBytes(hex: string): ArrayBuffer {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out.buffer;
}

async function sha256Hex(bytes: ArrayBuffer): Promise<string> {
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
  return Array.from(digest, (b) => b.toString(16).padStart(2, "0")).join("");
}

function parseDate(value: string | null | undefined): Date {
  const ms = value ? Date.parse(value) : NaN;
  return new Date(Number.isNaN(ms) ? 0 : ms);
}

/** R2Object built from release metadata. */
class AssetObject implements R2Object {
  readonly version: string;
  readonly httpEtag: string;
  readonly checksums: R2Checksums;
  readonly storageClass = "Standard";
  readonly customMetadata: Record<string, string> = {};

  constructor(
    readonly key: string,
    readonly etag: string,
    readonly size: number,
    readonly uploaded: Date,
    readonly httpMetadata: R2HTTPMetadata,
  ) {
    // R2 returns etag unquoted and httpEtag as a quoted strong validator.
    this.version = etag;
    this.httpEtag = `"${etag}"`;
    const sha256 = /^[0-9a-f]{64}$/.test(etag) ? hexToBytes(etag) : undefined;
    this.checksums = {
      sha256,
      toJSON: () => (sha256 ? { sha256: etag } : {}),
    };
  }

  writeHttpMetadata(headers: Headers): void {
    if (this.httpMetadata.contentType) headers.set("Content-Type", this.httpMetadata.contentType);
    if (this.httpMetadata.cacheControl) headers.set("Cache-Control", this.httpMetadata.cacheControl);
  }
}

/** R2ObjectBody whose body is the downloaded asset. */
class AssetObjectBody extends AssetObject implements R2ObjectBody {
  constructor(
    key: string,
    etag: string,
    size: number,
    uploaded: Date,
    httpMetadata: R2HTTPMetadata,
    private readonly response: Response,
  ) {
    super(key, etag, size, uploaded, httpMetadata);
  }

  get body(): ReadableStream {
    return this.response.body ?? new ReadableStream({ start: (c) => c.close() });
  }
  get bodyUsed(): boolean {
    return this.response.bodyUsed;
  }
  arrayBuffer(): Promise<ArrayBuffer> {
    return this.response.arrayBuffer();
  }
  async bytes(): Promise<Uint8Array> {
    return new Uint8Array(await this.response.arrayBuffer());
  }
  text(): Promise<string> {
    return this.response.text();
  }
  json<T>(): Promise<T> {
    return this.response.json<T>();
  }
  blob(): Promise<Blob> {
    return this.response.blob();
  }
}

function defaultCache(): Cache | null {
  return typeof caches !== "undefined" && caches?.default ? caches.default : null;
}

function readOnly(): never {
  throw new StorageError("GitHubReleasesBucket is read-only");
}

export class GitHubReleasesBucket implements R2Bucket {
  private readonly repo: string;
  /** objects.json per tag, so one request's several lookups parse it once. */
  private readonly objectsByTag = new Map<string, Promise<Record<string, ObjectEntry> | null>>();
  /** The latest-date probe, run at most once per instance (one per request). */
  private latest?: Promise<string | null>;
  /** The latest day's dates.json, read at most once per instance. */
  private dates?: Promise<string[]>;

  constructor(
    env: StorageEnv,
    private readonly ctx?: WaitUntil,
    private readonly now: () => Date = () => new Date(),
  ) {
    const repo = env.ASSETS_REPO;
    if (!repo || !REPO_PATTERN.test(repo)) {
      throw new StorageError(`ASSETS_REPO must be "owner/repo", got ${JSON.stringify(repo)}`);
    }
    this.repo = repo;
  }

  async head(key: string): Promise<R2Object | null> {
    const loc = locateKey(key);
    if (!loc) return null;
    if (loc.kind === "latest") return this.getLatest(key);
    const entry = await this.lookup(loc, key);
    if (!entry) return null;
    return new AssetObject(key, entry.etag, entry.size, parseDate(entry.uploaded), {
      contentType: entry.contentType,
      cacheControl: entry.cacheControl,
    });
  }

  get(key: string, options: R2GetOptions & { onlyIf: R2Conditional | Headers }): Promise<R2ObjectBody | R2Object | null>;
  get(key: string, options?: R2GetOptions): Promise<R2ObjectBody | null>;
  async get(key: string, options?: R2GetOptions): Promise<R2ObjectBody | null> {
    if (options?.range !== undefined || options?.onlyIf !== undefined) {
      throw new StorageError("GitHubReleasesBucket.get does not support range or onlyIf");
    }
    const loc = locateKey(key);
    if (!loc) return null;
    if (loc.kind === "latest") return this.getLatest(key);

    const entry = await this.lookup(loc, key);
    if (!entry) return null;
    const res = await this.download(assetUrl(this.repo, loc.tag, entry.asset));
    // Listed in objects.json but not downloadable: treat as absent, as R2
    // would for a key that is not there, rather than serving a broken body.
    if (!res) return null;
    return new AssetObjectBody(
      key,
      entry.etag,
      entry.size,
      parseDate(entry.uploaded),
      { contentType: entry.contentType, cacheControl: entry.cacheControl },
      res,
    );
  }

  /**
   * Published metadata keys (`metadata/YYYY/MM/DD.json`), from the latest
   * day's dates.json, in R2's lexicographic ascending order — which is
   * chronological — and always in one page. Only those keys can be
   * enumerated; the `.json.sig` siblings R2 would also have listed are
   * omitted, and every caller skips them anyway.
   */
  async list(options?: R2ListOptions): Promise<R2Objects> {
    const latest = await this.findLatest();
    const dates = latest === null ? [] : await (this.dates ??= this.loadDates(latest));

    const prefix = options?.prefix ?? "";
    const startAfter = options?.startAfter;
    const keys = [...new Set(dates)]
      .sort()
      .map((date) => ({ date, key: `metadata/${date.replaceAll("-", "/")}.json` }))
      .filter(({ key }) => key.startsWith(prefix) && (startAfter === undefined || key > startAfter));

    return {
      objects: keys.map(
        ({ date, key }) => new AssetObject(key, "", 0, parseDate(`${date}T00:00:00Z`), {}),
      ),
      delimitedPrefixes: [],
      truncated: false,
    };
  }

  put(..._args: unknown[]): Promise<never> {
    return Promise.reject(new StorageError("GitHubReleasesBucket is read-only"));
  }
  delete(..._args: unknown[]): Promise<void> {
    return Promise.reject(new StorageError("GitHubReleasesBucket is read-only"));
  }
  createMultipartUpload(..._args: unknown[]): Promise<never> {
    return Promise.reject(new StorageError("GitHubReleasesBucket is read-only"));
  }
  resumeMultipartUpload(..._args: unknown[]): never {
    return readOnly();
  }

  /** latest.json synthesized from the probe, with a sha256 etag of its bytes. */
  private async getLatest(key: string): Promise<AssetObjectBody | null> {
    const date = await this.findLatest();
    if (date === null) return null;
    const bytes = new TextEncoder().encode(latestJson(date)).buffer as ArrayBuffer;
    return new AssetObjectBody(
      key,
      await sha256Hex(bytes),
      bytes.byteLength,
      parseDate(`${date}T00:00:00Z`),
      { contentType: "application/json", cacheControl: LATEST_CACHE },
      new Response(bytes),
    );
  }

  /**
   * The newest published date: the first of today, today-1, ... (UTC,
   * LATEST_WINDOW_DAYS in all) whose objects.json exists, or null. Sequential
   * so it stops at the first hit; hits are cached for a year and misses for
   * MISSING_TTL, so a warm probe is only Cache API lookups. A non-404 failure
   * rejects rather than skipping the day — skipping would report an older
   * day as latest during a GitHub outage.
   */
  private findLatest(): Promise<string | null> {
    this.latest ??= (async () => {
      const now = this.now();
      for (let back = 0; back < LATEST_WINDOW_DAYS; back++) {
        const date = new Date(
          Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - back),
        ).toISOString().slice(0, 10);
        if (await this.objectsFor(tagFor(date))) return date;
      }
      return null;
    })();
    return this.latest;
  }

  /** The dates in `latest`'s dates.json snapshot, always including `latest`. */
  private async loadDates(latest: string): Promise<string[]> {
    const tag = tagFor(latest);
    const res = await this.download(assetUrl(this.repo, tag, "dates.json"));
    if (!res) {
      // A publishing fault: the release has objects.json but no dates.json,
      // and immutable releases mean it will never gain one. Serve the one day
      // known to exist rather than an empty archive or a day-long 503.
      console.error(`dates.json missing from ${tag}`);
      return [latest];
    }
    const parsed = await res.json<{ dates?: unknown }>().catch(() => null);
    if (!parsed || !Array.isArray(parsed.dates)) {
      throw new StorageError(`dates.json in ${tag} is malformed`);
    }
    const dates = parsed.dates.filter((d): d is string => typeof d === "string" && DATE_PATTERN.test(d));
    return dates.includes(latest) ? dates : [...dates, latest];
  }

  /** objects.json for a release, fetched once per instance; null if it 404s. */
  private objectsFor(tag: string): Promise<Record<string, ObjectEntry> | null> {
    let objects = this.objectsByTag.get(tag);
    if (!objects) {
      objects = this.loadObjects(tag);
      this.objectsByTag.set(tag, objects);
    }
    return objects;
  }

  /** The objects.json entry for `key`, or null when the day or key is absent. */
  private async lookup(loc: DayLocation, key: string): Promise<ObjectEntry | null> {
    const map = await this.objectsFor(loc.tag);
    if (!map || !Object.hasOwn(map, key)) return null;
    const entry = map[key];
    if (
      !entry ||
      typeof entry.asset !== "string" ||
      !ASSET_PATTERN.test(entry.asset) ||
      typeof entry.etag !== "string" ||
      !/^[0-9a-f]+$/.test(entry.etag)
    ) {
      throw new StorageError(`objects.json entry for ${key} in ${loc.tag} is malformed`);
    }
    return entry;
  }

  private async loadObjects(tag: string): Promise<Record<string, ObjectEntry> | null> {
    const res = await this.download(assetUrl(this.repo, tag, "objects.json"));
    if (!res) return null;
    const parsed = await res.json<unknown>().catch(() => null);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new StorageError(`objects.json in ${tag} is malformed`);
    }
    return parsed as Record<string, ObjectEntry>;
  }

  /**
   * Fetches a release asset through the Cache API. Returns null for a 404
   * (cached for MISSING_TTL) and throws StorageError for any other failure,
   * which is never cached. Assets that exist are cached for IMMUTABLE_TTL.
   */
  private async download(url: string): Promise<Response | null> {
    const cache = defaultCache();
    if (cache) {
      const hit = await cache.match(url);
      if (hit) {
        if (hit.headers.has(MISSING_HEADER)) return null;
        return hit;
      }
    }

    let res: Response;
    try {
      res = await fetch(url, { redirect: "follow" });
    } catch (err) {
      throw new StorageError(`fetch ${url} failed: ${err instanceof Error ? err.message : String(err)}`);
    }

    if (res.status === 404) {
      await res.body?.cancel();
      if (cache) {
        // Stored as a marked 200: negative entries survive any cache that
        // declines to store error statuses.
        this.store(cache, url, new Response(null, {
          headers: { [MISSING_HEADER]: "1", "Cache-Control": `public, max-age=${MISSING_TTL}` },
        }));
      }
      return null;
    }
    if (!res.ok) {
      await res.body?.cancel();
      throw new StorageError(`GitHub returned ${res.status} for ${url}`);
    }
    if (!cache) return res;

    const stored = new Response(res.body, res);
    stored.headers.set("Cache-Control", `public, max-age=${IMMUTABLE_TTL}, immutable`);
    // Either would stop cache.put() storing the response.
    stored.headers.delete("Set-Cookie");
    stored.headers.delete("Vary");
    this.store(cache, url, stored.clone());
    return stored;
  }

  private store(cache: Cache, url: string, response: Response): void {
    const put = cache.put(url, response).catch((err: unknown) => {
      console.error("cache put failed", url, err);
    });
    // Without a waitUntil the write still runs, but may be cut short once
    // the invocation ends; production always passes the ExecutionContext.
    this.ctx?.waitUntil(put);
  }
}
