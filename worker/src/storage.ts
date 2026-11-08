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
 *   art-index        dates.json {"dates": [...]} (newest first),
 *                    latest.json {"date": "YYYY-MM-DD"}.
 *
 * Downloads go to https://github.com/{repo}/releases/download/{tag}/{asset},
 * which needs no token and is not subject to api.github.com's per-IP rate
 * limit; GitHub 302s to its asset CDN and fetch() follows. Every download goes
 * through the Cache API so a busy day costs GitHub almost nothing:
 *
 *   day-release assets and objects.json  1 year (published once, immutable)
 *   art-index assets                     INDEX_TTL seconds
 *   a 404 from GitHub                    MISSING_TTL / INDEX_MISSING_TTL
 *                                        seconds, so a day published later
 *                                        becomes visible quickly
 *
 * A 404 is "not found" (get/head return null, as R2 did). Any other non-2xx
 * status or a network failure throws, which index.ts's top-level handler turns
 * into the same 503 "Upstream storage unavailable" an R2 fault produced.
 */

/** Release tag holding dates.json and latest.json. */
export const INDEX_TAG = "art-index";

/** Seconds a day-release asset (and its objects.json) stays cached: one year. */
export const IMMUTABLE_TTL = 31_536_000;

/** Seconds dates.json / latest.json stay cached; they change once a day. */
export const INDEX_TTL = 60;

/** Seconds a 404 for a day-release asset stays cached. */
export const MISSING_TTL = 60;

/**
 * Seconds a 404 for an index asset stays cached. Shorter than MISSING_TTL:
 * GitHub cannot replace an asset atomically, so the publisher's delete-then-
 * upload of latest.json leaves a brief window where it 404s, and caching that
 * for long would report "no artwork published" well after it has been.
 */
export const INDEX_MISSING_TTL = 15;

/**
 * Cache-Control the publisher stored on latest.json in R2 (LATEST_CACHE in
 * src/upload.py). Release assets carry no per-object metadata, so the adapter
 * reports it for the index assets itself.
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
export function storageFor(env: StorageEnv, ctx?: WaitUntil): R2Bucket {
  return env.BUCKET ?? new GitHubReleasesBucket(env, ctx);
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
type IndexLocation = { kind: "index"; asset: "latest.json" | "dates.json" };

/**
 * Where an R2 key lives: a day release (looked up via objects.json) or an
 * index asset. Null for any other key — R2 would have had nothing there.
 */
export function locateKey(key: string): DayLocation | IndexLocation | null {
  if (key === "latest.json" || key === "dates.json") return { kind: "index", asset: key };
  const m = key.match(/^(?:originals|stylized|metadata|manifests)\/(\d{4})\/(\d{2})\/(\d{2})\.[^/]+$/);
  if (!m) return null;
  const date = `${m[1]}-${m[2]}-${m[3]}`;
  return { kind: "day", date, tag: `art-${date}` };
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
  /** objects.json per tag, so one request's several head() calls parse it once. */
  private readonly objectsByTag = new Map<string, Promise<Record<string, ObjectEntry> | null>>();

  constructor(env: StorageEnv, private readonly ctx?: WaitUntil) {
    const repo = env.ASSETS_REPO;
    if (!repo || !REPO_PATTERN.test(repo)) {
      throw new StorageError(`ASSETS_REPO must be "owner/repo", got ${JSON.stringify(repo)}`);
    }
    this.repo = repo;
  }

  async head(key: string): Promise<R2Object | null> {
    const loc = locateKey(key);
    if (!loc) return null;
    if (loc.kind === "index") {
      // Index assets are a few bytes and have no stored etag, so the etag
      // has to come from the bytes themselves.
      return this.getIndex(key, loc.asset);
    }
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
    if (loc.kind === "index") return this.getIndex(key, loc.asset);

    const entry = await this.lookup(loc, key);
    if (!entry) return null;
    const res = await this.download(assetUrl(this.repo, loc.tag, entry.asset), IMMUTABLE_TTL, MISSING_TTL);
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
   * Published metadata keys (`metadata/YYYY/MM/DD.json`), from dates.json, in
   * R2's lexicographic ascending order — which is chronological — and always
   * in one page. Only those keys can be enumerated; the `.json.sig` siblings
   * R2 would also have listed are omitted, and every caller skips them anyway.
   */
  async list(options?: R2ListOptions): Promise<R2Objects> {
    const res = await this.download(assetUrl(this.repo, INDEX_TAG, "dates.json"), INDEX_TTL, INDEX_MISSING_TTL);
    let dates: string[] = [];
    if (res) {
      const parsed = await res.json<{ dates?: unknown }>().catch(() => null);
      if (!parsed || !Array.isArray(parsed.dates)) {
        throw new StorageError("dates.json is malformed");
      }
      dates = parsed.dates.filter((d): d is string => typeof d === "string" && DATE_PATTERN.test(d));
    }

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

  /** latest.json / dates.json as an R2ObjectBody with a sha256 etag of its bytes. */
  private async getIndex(key: string, asset: IndexLocation["asset"]): Promise<AssetObjectBody | null> {
    const res = await this.download(assetUrl(this.repo, INDEX_TAG, asset), INDEX_TTL, INDEX_MISSING_TTL);
    if (!res) return null;
    const bytes = await res.arrayBuffer();
    return new AssetObjectBody(
      key,
      await sha256Hex(bytes),
      bytes.byteLength,
      parseDate(res.headers.get("Last-Modified")),
      { contentType: "application/json", cacheControl: LATEST_CACHE },
      new Response(bytes),
    );
  }

  /** The objects.json entry for `key`, or null when the day or key is absent. */
  private async lookup(loc: DayLocation, key: string): Promise<ObjectEntry | null> {
    let objects = this.objectsByTag.get(loc.tag);
    if (!objects) {
      objects = this.loadObjects(loc.tag);
      this.objectsByTag.set(loc.tag, objects);
    }
    const map = await objects;
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
    const res = await this.download(assetUrl(this.repo, tag, "objects.json"), IMMUTABLE_TTL, MISSING_TTL);
    if (!res) return null;
    const parsed = await res.json<unknown>().catch(() => null);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw new StorageError(`objects.json in ${tag} is malformed`);
    }
    return parsed as Record<string, ObjectEntry>;
  }

  /**
   * Fetches a release asset through the Cache API. Returns null for a 404
   * (cached for `missingTtl` seconds) and throws StorageError for any other
   * failure, which must never be cached.
   */
  private async download(url: string, ttl: number, missingTtl: number): Promise<Response | null> {
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
          headers: { [MISSING_HEADER]: "1", "Cache-Control": `public, max-age=${missingTtl}` },
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
    stored.headers.set(
      "Cache-Control",
      ttl === IMMUTABLE_TTL ? `public, max-age=${ttl}, immutable` : `public, max-age=${ttl}`,
    );
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
