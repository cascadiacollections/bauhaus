/**
 * Atom 1.0 feed of recent artwork — GET|HEAD /api/feed.atom.
 *
 * Follows https://kevincox.ca/2022/05/06/rss-feed-best-practices/:
 *   - every URL is absolute https, so nothing depends on a reader resolving
 *     relative links (most do it wrong or not at all);
 *   - entry ids are the date permalinks, which never change, so a reader never
 *     shows an old day as new;
 *   - entry times come from the write-once metadata, so they are stable across
 *     rebuilds and never in the future;
 *   - content is full, self-contained HTML (no scripts, styles or classes) so
 *     the entry reads well inside any reader.
 *
 * Everything here is pure; the route in index.ts does the R2 reads.
 */

/** Entries in the feed. ~2 KB each, so far below the 1 MB readers tolerate. */
export const FEED_ENTRY_LIMIT = 30;

/**
 * Bumped whenever the rendered markup changes, so the ETag (which is computed
 * from the date list alone, before any metadata is read) changes with it and
 * readers re-fetch instead of keeping the old rendering forever.
 */
export const FEED_VERSION = "1";

/** Short and identifiable in a reader's sidebar; free of & < > on purpose. */
export const FEED_TITLE = "Bauhaus — daily stylized art";

const FEED_SUBTITLE =
  "A public-domain artwork restyled after a famous painting, published once a day.";

/** The page that shows the art: kevintcoughlin.com uses today's image as its hero. */
const FEED_ALTERNATE = "https://kevintcoughlin.com/";

const AUTHOR_NAME = "Kevin Coughlin";
const AUTHOR_URI = "https://kevintcoughlin.com/";

/** Display names for metadata `source` values (src/fetch.py's fetchers). */
const SOURCE_NAMES: Record<string, string> = {
  met: "The Metropolitan Museum of Art",
  artic: "Art Institute of Chicago",
  unsplash: "Unsplash",
};

/** The subset of a day's metadata JSON the feed renders. All optional: old days vary. */
export interface FeedMetadata {
  published_date?: string;
  generated_at?: string;
  title?: string;
  artist?: string;
  date?: string;
  source?: string;
  source_url?: string;
  photographer?: string;
  photographer_url?: string;
  license?: string;
  license_url?: string;
  style_artist?: string;
  style_title?: string;
  style_source_url?: string;
  variants?: { type?: string; width?: number; height?: number }[];
}

export interface FeedEntryInput {
  date: string;
  meta: FeedMetadata;
}

// Characters XML 1.0 forbids outright. Metadata comes from museum APIs, and a
// single stray control character makes the whole document unparseable.
const INVALID_XML_CHARS = /[\u0000-\u0008\u000B\u000C\u000E-\u001F￾￿]/g;

/** Escapes text for XML element content and double-quoted attributes. */
export function escapeXml(value: string): string {
  return value
    .replace(INVALID_XML_CHARS, "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

/** HTML text/attribute escaping; same set of characters, `&#39;` for older parsers. */
function escapeHtml(value: string): string {
  return escapeXml(value).replace(/&apos;/g, "&#39;");
}

/** Only absolute http(s) links make it into the feed; anything else is dropped. */
function safeUrl(value: string | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" || url.protocol === "http:" ? url.href : null;
  } catch {
    return null;
  }
}

function clean(value: string | undefined): string {
  return (value ?? "").replace(/\s+/g, " ").trim();
}

/** The scheme-canonical origin feed URLs are built from. Never http. */
export function canonicalOrigin(requestUrl: URL): string {
  return `https://${requestUrl.host}`;
}

/** RFC 3339 in UTC with second precision — the form validators and readers agree on. */
function rfc3339(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, "Z");
}

/**
 * The moment an entry was published, in epoch ms.
 *
 * `generated_at` is written once by the pipeline alongside the art and never
 * rewritten, so it is stable across feed rebuilds. It is trusted only within
 * two days of the publish date (the 04:00 UTC run and its same-day retries);
 * otherwise — missing, a backfill generated months later, a clock-skewed
 * generator — the entry falls back to midnight UTC of its date. A timestamp
 * in the future is never emitted: it would pin the entry to the top of
 * readers that sort by date.
 */
export function entryTimestamp(entry: FeedEntryInput, nowMs: number): number {
  const midnight = Date.parse(`${entry.date}T00:00:00Z`);
  const generated = Date.parse(entry.meta.generated_at ?? "");
  if (
    !Number.isNaN(generated) &&
    generated >= midnight &&
    generated < midnight + 2 * 86_400_000 &&
    generated <= nowMs
  ) {
    return Math.floor(generated / 1000) * 1000;
  }
  return Math.min(midnight, nowMs);
}

/** "Title — Artist", or whichever of the two exists, or the date. */
export function entryTitle(entry: FeedEntryInput): string {
  const title = clean(entry.meta.title);
  const artist = clean(entry.meta.artist) || clean(entry.meta.photographer);
  if (title && artist) return `${title} — ${artist}`;
  return title || artist || `Bauhaus for ${entry.date}`;
}

function entrySummary(entry: FeedEntryInput): string {
  const title = clean(entry.meta.title) || "Artwork";
  const artist = clean(entry.meta.artist) || clean(entry.meta.photographer);
  const styleArtist = clean(entry.meta.style_artist);
  let text = artist ? `${title} by ${artist}` : title;
  if (styleArtist) text += `, restyled after ${styleArtist}`;
  return `${text}.`;
}

/** The entry's HTML body. Semantic elements only: readers strip classes, styles and scripts. */
export function entryContent(entry: FeedEntryInput, origin: string): string {
  const m = entry.meta;
  const title = clean(m.title);
  const artist = clean(m.artist) || clean(m.photographer);
  const styleArtist = clean(m.style_artist);
  const styleTitle = clean(m.style_title);
  const source = clean(m.source);
  const sourceName = SOURCE_NAMES[source] ?? source;
  const sourceUrl = safeUrl(m.source_url);
  const artistUrl = safeUrl(m.photographer_url);
  const styleUrl = safeUrl(m.style_source_url);
  const licenseUrl = safeUrl(m.license_url);
  const license = clean(m.license);
  const created = clean(m.date);

  const link = (text: string, href: string | null) =>
    href ? `<a href="${escapeHtml(href)}">${text}</a>` : text;

  // ?format=jpeg rather than the negotiated permalink: reader image proxies
  // cache by URL and ignore Vary, so one reader asking for AVIF could hand
  // AVIF to every client behind the same proxy.
  const imgSrc = `${origin}/api/${entry.date}?format=jpeg`;
  const stylized = m.variants?.find((v) => v.type === "stylized");
  const size =
    stylized && Number.isInteger(stylized.width) && Number.isInteger(stylized.height)
      ? ` width="${stylized.width}" height="${stylized.height}"`
      : "";
  let alt = title || "Artwork";
  if (artist) alt += ` by ${artist}`;
  if (styleArtist) alt += `, restyled in the style of ${styleArtist}`;

  const work = title ? `<cite>${escapeHtml(title)}</cite>` : "Untitled";
  let credit = work;
  if (artist) credit += ` by ${link(escapeHtml(artist), artistUrl)}`;
  if (created && created !== entry.date) credit += ` (${escapeHtml(created)})`;
  if (sourceName) credit += `, via ${link(escapeHtml(sourceName), sourceUrl)}`;
  credit += ".";

  const parts = [
    `<figure><img src="${escapeHtml(imgSrc)}" alt="${escapeHtml(alt)}"${size}>` +
      `<figcaption>${credit}</figcaption></figure>`,
  ];
  if (styleArtist || styleTitle) {
    const style = styleTitle ? link(`<cite>${escapeHtml(styleTitle)}</cite>`, styleUrl) : "";
    const by = styleArtist ? escapeHtml(styleArtist) : "";
    parts.push(`<p>Restyled after ${[style, by].filter(Boolean).join(" by ")}.</p>`);
  }
  if (license) parts.push(`<p>License: ${link(escapeHtml(license), licenseUrl)}.</p>`);
  parts.push(
    `<p>${link("Full-resolution image", `${origin}/api/${entry.date}`)} · ` +
      `${link("Original", `${origin}/api/${entry.date}/original`)} · ` +
      `${link("Metadata", `${origin}/api/${entry.date}.json`)}</p>`,
  );
  return parts.join("");
}

function entryCategories(entry: FeedEntryInput): string[] {
  const categories: string[] = [];
  const source = clean(entry.meta.source);
  if (source) {
    const label = SOURCE_NAMES[source];
    categories.push(
      label
        ? `<category term="${escapeXml(source)}" label="${escapeXml(label)}"/>`
        : `<category term="${escapeXml(source)}"/>`,
    );
  }
  const styleArtist = clean(entry.meta.style_artist);
  if (styleArtist) {
    categories.push(
      `<category term="${escapeXml(styleArtist)}" label="${escapeXml(`Style: ${styleArtist}`)}"/>`,
    );
  }
  return categories;
}

/** Newest-entry time in epoch ms; drives `<updated>`, Last-Modified and If-Modified-Since. */
export function feedUpdated(entries: FeedEntryInput[], nowMs: number): number {
  return Math.max(...entries.map((e) => entryTimestamp(e, nowMs)));
}

/**
 * Renders the document. `entries` must be non-empty and newest first; the
 * caller decides what to do with an empty archive.
 */
export function buildAtomFeed(entries: FeedEntryInput[], origin: string, nowMs: number): string {
  const feedUrl = `${origin}/api/feed.atom`;
  const lines = [
    `<?xml version="1.0" encoding="utf-8"?>`,
    `<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en">`,
    `  <title>${escapeXml(FEED_TITLE)}</title>`,
    `  <subtitle>${escapeXml(FEED_SUBTITLE)}</subtitle>`,
    `  <id>${escapeXml(feedUrl)}</id>`,
    `  <link rel="self" type="application/atom+xml" href="${escapeXml(feedUrl)}"/>`,
    `  <link rel="alternate" type="text/html" href="${escapeXml(FEED_ALTERNATE)}"/>`,
    `  <updated>${rfc3339(feedUpdated(entries, nowMs))}</updated>`,
    `  <author><name>${escapeXml(AUTHOR_NAME)}</name><uri>${escapeXml(AUTHOR_URI)}</uri></author>`,
  ];

  for (const entry of entries) {
    const permalink = `${origin}/api/${entry.date}`;
    const time = rfc3339(entryTimestamp(entry, nowMs));
    lines.push(
      `  <entry>`,
      `    <title>${escapeXml(entryTitle(entry))}</title>`,
      `    <id>${escapeXml(permalink)}</id>`,
      `    <link rel="alternate" href="${escapeXml(permalink)}"/>`,
      `    <published>${time}</published>`,
      `    <updated>${time}</updated>`,
      ...entryCategories(entry).map((c) => `    ${c}`),
      `    <summary>${escapeXml(entrySummary(entry))}</summary>`,
      `    <content type="html">${escapeXml(entryContent(entry, origin))}</content>`,
      `  </entry>`,
    );
  }

  lines.push(`</feed>`, "");
  return lines.join("\n");
}

/**
 * Strong ETag computed from the dates alone, before any metadata is read, so
 * a conditional request that matches costs one R2 list and nothing else.
 *
 * That is sound because every input to the body is fixed once the date list
 * is: published metadata is write-once (src/upload.py refuses to rewrite a
 * date), and the remaining inputs — the origin and the renderer — are covered
 * by including them in the hash.
 */
export async function feedEtag(dates: string[], origin: string): Promise<string> {
  const data = new TextEncoder().encode(`${FEED_VERSION}\n${origin}\n${dates.join("\n")}`);
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", data));
  const hex = [...digest.slice(0, 16)].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `"feed-${hex}"`;
}
