import { describe, expect, it, vi } from "vitest";

import {
  buildAtomFeed,
  canonicalOrigin,
  entryContent,
  entryTimestamp,
  entryTitle,
  escapeXml,
  feedEtag,
  FEED_ENTRY_LIMIT,
  FEED_TITLE,
  type FeedMetadata,
} from "../src/feed";
import worker from "../src/index";

const NOW = Date.parse("2026-10-25T20:00:00Z");

function meta(date: string, overrides: Partial<FeedMetadata> = {}): FeedMetadata {
  return {
    published_date: date,
    generated_at: `${date}T04:09:21.422938+00:00`,
    title: "Sailing Boats Returning to Yabase, Lake Biwa",
    artist: "Utagawa Hiroshige",
    date: "ca. 1835",
    source: "met",
    source_url: "https://www.metmuseum.org/art/collection/search/36534",
    license: "CC0-1.0",
    license_url: "https://creativecommons.org/publicdomain/zero/1.0/",
    style_artist: "Vincent van Gogh",
    style_title: "Wheat Field with Cypresses",
    style_source_url: "https://www.metmuseum.org/art/collection/search/436535",
    variants: [{ type: "stylized", width: 2000, height: 1361 }],
    ...overrides,
  };
}

function dateKey(date: string): string {
  return `metadata/${date.replace(/-/g, "/")}.json`;
}

/** Consecutive dates ending on `last`, oldest first (the order R2 lists them). */
function datesEnding(last: string, count: number): string[] {
  const end = Date.parse(`${last}T00:00:00Z`);
  return Array.from({ length: count }, (_, i) =>
    new Date(end - (count - 1 - i) * 86_400_000).toISOString().slice(0, 10),
  );
}

function feedEnv(metas: Record<string, FeedMetadata>, extraKeys: string[] = []) {
  const keys = [...Object.keys(metas).map(dateKey), ...extraKeys].sort();
  const get = vi.fn(async (key: string) => {
    const date = Object.keys(metas).find((d) => dateKey(d) === key);
    if (!date) return null;
    return { json: async () => metas[date] } as unknown as R2ObjectBody;
  });
  const list = vi.fn(async () => ({
    objects: keys.map((key) => ({ key })),
    truncated: false,
  }) as unknown as R2Objects);
  const bucket = { get, head: vi.fn(async () => null), list };
  return {
    env: {
      BUCKET: bucket as unknown as R2Bucket,
      WEB_VITALS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      WEB_ERRORS: { writeDataPoint() {} } as unknown as AnalyticsEngineDataset,
      ALLOWED_ORIGINS: "",
    },
    get,
    list,
  };
}

function metasFor(dates: string[]): Record<string, FeedMetadata> {
  return Object.fromEntries(dates.map((d) => [d, meta(d)]));
}

function feedRequest(headers: Record<string, string> = {}, method = "GET", base = "https://bauhaus.example") {
  return new Request(`${base}/api/feed.atom`, { method, headers });
}

describe("feed helpers", () => {
  it("escapes the five XML specials and drops characters XML forbids", () => {
    expect(escapeXml(`a&b<c>d"e'f\u0001g`)).toBe("a&amp;b&lt;c&gt;d&quot;e&apos;fg");
  });

  it("canonicalizes the origin to https", () => {
    expect(canonicalOrigin(new URL("http://bauhaus.example/api/feed.atom"))).toBe("https://bauhaus.example");
  });

  it("uses generated_at, truncated to the second, as the entry time", () => {
    const t = entryTimestamp({ date: "2026-10-24", meta: meta("2026-10-24") }, NOW);
    expect(new Date(t).toISOString()).toBe("2026-10-24T04:09:21.000Z");
  });

  it.each([
    ["missing", undefined],
    ["unparseable", "yesterday"],
    ["before the publish date", "2026-10-20T04:00:00Z"],
    ["long after the publish date (backfill)", "2026-12-01T04:00:00Z"],
  ])("falls back to midnight UTC when generated_at is %s", (_label, generated_at) => {
    const t = entryTimestamp({ date: "2026-10-24", meta: meta("2026-10-24", { generated_at }) }, NOW);
    expect(new Date(t).toISOString()).toBe("2026-10-24T00:00:00.000Z");
  });

  it("never returns a time in the future", () => {
    const entry = { date: "2026-10-25", meta: meta("2026-10-25", { generated_at: "2026-10-25T23:00:00Z" }) };
    expect(entryTimestamp(entry, NOW)).toBe(Date.parse("2026-10-25T00:00:00Z"));
  });

  it("titles entries 'Title — Artist', falling back to the photographer", () => {
    expect(entryTitle({ date: "2026-10-24", meta: meta("2026-10-24") })).toBe(
      "Sailing Boats Returning to Yabase, Lake Biwa — Utagawa Hiroshige",
    );
    expect(
      entryTitle({ date: "2026-06-09", meta: { title: "Scenery of mountain", photographer: "John Lee" } }),
    ).toBe("Scenery of mountain — John Lee");
    expect(entryTitle({ date: "2026-06-09", meta: {} })).toBe("Bauhaus for 2026-06-09");
  });

  it("renders absolute, semantic, class-free content with full credit", () => {
    const html = entryContent({ date: "2026-10-24", meta: meta("2026-10-24") }, "https://bauhaus.example");
    expect(html).toContain(
      '<img src="https://bauhaus.example/api/2026-10-24?format=jpeg" ' +
        'alt="Sailing Boats Returning to Yabase, Lake Biwa by Utagawa Hiroshige, restyled in the style of Vincent van Gogh" ' +
        'width="2000" height="1361">',
    );
    expect(html).toContain("<cite>Sailing Boats Returning to Yabase, Lake Biwa</cite> by Utagawa Hiroshige (ca. 1835)");
    expect(html).toContain('<a href="https://www.metmuseum.org/art/collection/search/36534">The Metropolitan Museum of Art</a>');
    expect(html).toContain("<cite>Wheat Field with Cypresses</cite></a> by Vincent van Gogh");
    expect(html).toContain('<a href="https://creativecommons.org/publicdomain/zero/1.0/">CC0-1.0</a>');
    expect(html).not.toMatch(/class=|style=|<script/i);
    expect(html).not.toMatch(/(src|href)="(?!https:\/\/)/);
  });

  it("escapes hostile metadata and drops non-http links", () => {
    const html = entryContent(
      {
        date: "2026-10-24",
        meta: meta("2026-10-24", {
          title: `<script>alert("x")</script> & co`,
          source_url: "javascript:alert(1)",
        }),
      },
      "https://bauhaus.example",
    );
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt; &amp; co");
    expect(html).not.toContain("javascript:");
  });

  it("builds a document with stable ids, self link and newest-entry updated", () => {
    const xml = buildAtomFeed(
      [
        { date: "2026-10-24", meta: meta("2026-10-24") },
        { date: "2026-10-23", meta: meta("2026-10-23") },
      ],
      "https://bauhaus.example",
      NOW,
    );
    expect(xml.startsWith('<?xml version="1.0" encoding="utf-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom"')).toBe(true);
    expect(xml).toContain(`<title>${FEED_TITLE}</title>`);
    expect(xml).toContain("<id>https://bauhaus.example/api/feed.atom</id>");
    expect(xml).toContain('<link rel="self" type="application/atom+xml" href="https://bauhaus.example/api/feed.atom"/>');
    expect(xml).toContain('<link rel="alternate" type="text/html" href="https://kevintcoughlin.com/"/>');
    expect(xml).toContain("  <updated>2026-10-24T04:09:21Z</updated>");
    expect(xml).toContain("<id>https://bauhaus.example/api/2026-10-23</id>");
    expect(xml).toContain("<published>2026-10-23T04:09:21Z</published>");
    expect(xml).toContain('<category term="met" label="The Metropolitan Museum of Art"/>');
    expect(xml).toContain('<category term="Vincent van Gogh" label="Style: Vincent van Gogh"/>');
    // Entry HTML is escaped once into the XML, so no raw markup leaks into it.
    expect(xml).toContain('<content type="html">&lt;figure&gt;&lt;img src=&quot;https://bauhaus.example/api/2026-10-24?format=jpeg&quot;');
    expect(xml.match(/<entry>/g)).toHaveLength(2);
  });

  it("is byte-identical across rebuilds", () => {
    const entries = [{ date: "2026-10-24", meta: meta("2026-10-24") }];
    expect(buildAtomFeed(entries, "https://b.example", NOW)).toBe(
      buildAtomFeed(entries, "https://b.example", NOW + 86_400_000),
    );
  });

  it("derives the ETag from the dates and origin", async () => {
    const a = await feedEtag(["2026-10-24", "2026-10-23"], "https://b.example");
    expect(a).toMatch(/^"feed-[0-9a-f]{32}"$/);
    expect(await feedEtag(["2026-10-24", "2026-10-23"], "https://b.example")).toBe(a);
    expect(await feedEtag(["2026-10-25", "2026-10-24"], "https://b.example")).not.toBe(a);
    expect(await feedEtag(["2026-10-24", "2026-10-23"], "https://c.example")).not.toBe(a);
  });
});

describe("GET /api/feed.atom", () => {
  it("serves Atom with caching and CORS headers", async () => {
    const { env } = feedEnv(metasFor(["2026-10-23", "2026-10-24"]));
    const res = await worker.fetch(feedRequest(), env);

    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toBe("application/atom+xml; charset=utf-8");
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=3600");
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
    expect(res.headers.get("ETag")).toMatch(/^"feed-[0-9a-f]{32}"$/);
    expect(res.headers.get("Last-Modified")).toBe("Sat, 24 Oct 2026 04:09:21 GMT");
    const body = await res.text();
    expect(body.indexOf("2026-10-24</id>")).toBeLessThan(body.indexOf("2026-10-23</id>"));
  });

  it("canonicalizes http requests to https URLs in the document", async () => {
    const { env } = feedEnv(metasFor(["2026-10-24"]));
    const body = await (await worker.fetch(feedRequest({}, "GET", "http://bauhaus.example"), env)).text();
    expect(body).toContain("<id>https://bauhaus.example/api/feed.atom</id>");
    expect(body).not.toContain("http://bauhaus.example");
  });

  it(`includes only the newest ${FEED_ENTRY_LIMIT} days, ignoring signatures`, async () => {
    const dates = datesEnding("2026-10-24", FEED_ENTRY_LIMIT + 5);
    const { env, get } = feedEnv(metasFor(dates), [`${dateKey("2026-10-24")}.sig`]);
    const body = await (await worker.fetch(feedRequest(), env)).text();

    expect(body.match(/<entry>/g)).toHaveLength(FEED_ENTRY_LIMIT);
    expect(body).toContain(`/api/${dates[dates.length - 1]}</id>`);
    expect(body).not.toContain(`/api/${dates[4]}</id>`);
    expect(get).toHaveBeenCalledTimes(FEED_ENTRY_LIMIT);
    expect(body.length).toBeLessThan(1_000_000);
  });

  it("skips a listed date whose metadata cannot be read", async () => {
    const { env, get } = feedEnv(metasFor(["2026-10-23", "2026-10-24"]));
    get.mockImplementationOnce(async () => null);
    const body = await (await worker.fetch(feedRequest(), env)).text();
    expect(body.match(/<entry>/g)).toHaveLength(1);
    expect(body).toContain("/api/2026-10-23</id>");
  });

  it("returns 304 for a matching If-None-Match without reading metadata", async () => {
    const { env, get } = feedEnv(metasFor(["2026-10-23", "2026-10-24"]));
    const etag = (await worker.fetch(feedRequest(), env)).headers.get("ETag")!;
    get.mockClear();

    const res = await worker.fetch(feedRequest({ "If-None-Match": `W/${etag}` }), env);
    expect(res.status).toBe(304);
    expect(res.headers.get("ETag")).toBe(etag);
    expect(res.headers.get("Cache-Control")).toBe("public, max-age=3600");
    expect(await res.text()).toBe("");
    expect(get).not.toHaveBeenCalled();
  });

  it("returns 200 when a new day changes the ETag", async () => {
    const { env } = feedEnv(metasFor(["2026-10-23"]));
    const etag = (await worker.fetch(feedRequest(), env)).headers.get("ETag")!;
    const { env: next } = feedEnv(metasFor(["2026-10-23", "2026-10-24"]));
    const res = await worker.fetch(feedRequest({ "If-None-Match": etag }), next);
    expect(res.status).toBe(200);
  });

  it("returns 304 for If-Modified-Since at or after the newest entry", async () => {
    const { env, get } = feedEnv(metasFor(["2026-10-23", "2026-10-24"]));
    const res = await worker.fetch(feedRequest({ "If-Modified-Since": "Sat, 24 Oct 2026 04:09:21 GMT" }), env);
    expect(res.status).toBe(304);
    expect(res.headers.get("Last-Modified")).toBe("Sat, 24 Oct 2026 04:09:21 GMT");
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("returns 200 for an older If-Modified-Since", async () => {
    const { env } = feedEnv(metasFor(["2026-10-23", "2026-10-24"]));
    const res = await worker.fetch(feedRequest({ "If-Modified-Since": "Sat, 24 Oct 2026 04:09:20 GMT" }), env);
    expect(res.status).toBe(200);
  });

  it("ignores If-Modified-Since when If-None-Match is present", async () => {
    const { env } = feedEnv(metasFor(["2026-10-24"]));
    const res = await worker.fetch(
      feedRequest({ "If-None-Match": '"stale"', "If-Modified-Since": "Sun, 25 Oct 2026 00:00:00 GMT" }),
      env,
    );
    expect(res.status).toBe(200);
  });

  it("supports HEAD with the GET headers and no body, reading one entry", async () => {
    const { env, get } = feedEnv(metasFor(["2026-10-23", "2026-10-24"]));
    const full = await worker.fetch(feedRequest(), env);
    get.mockClear();

    const res = await worker.fetch(feedRequest({}, "HEAD"), env);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe("");
    for (const h of ["Content-Type", "Cache-Control", "ETag", "Last-Modified", "Access-Control-Allow-Origin"]) {
      expect(res.headers.get(h)).toBe(full.headers.get(h));
    }
    expect(get).toHaveBeenCalledTimes(1);
  });

  it("returns 404 JSON when nothing is published", async () => {
    const { env } = feedEnv({});
    const res = await worker.fetch(feedRequest(), env);
    expect(res.status).toBe(404);
    expect(res.headers.get("Access-Control-Allow-Origin")).toBe("*");
  });
});
