"""Pure request/response logic for the Bauhaus API.

Everything here is standard library only and free of any Workers runtime or
FastAPI import. That is deliberate: it is the half of the Worker that has real
edge cases (format negotiation, ETag comparison, archive paging), and keeping it
importable outside Pyodide means it can be unit tested with plain pytest rather
than only through a wrangler dev server.

Ported from worker/src/index.ts; the constants are kept byte-identical to the
TypeScript ones so both implementations can serve the same bucket.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import formatdate, parsedate_to_datetime
from typing import Any, Literal

ImageFormat = Literal["avif", "webp", "jpeg"]

FORMAT_EXT: dict[str, str] = {"avif": ".avif", "webp": ".webp", "jpeg": ".jpg"}

FORMAT_CONTENT_TYPE: dict[str, str] = {
    "avif": "image/avif",
    "webp": "image/webp",
    "jpeg": "image/jpeg",
}

#: Values ``?format=`` accepts. Anything else is a client error, not a fallback.
FORMAT_PARAMS = frozenset({"avif", "webp", "jpeg", "auto"})

INVALID_FORMAT_MESSAGE = "Unsupported format. Use avif, webp, jpeg, or auto"

#: Cache-control for date-specific resources — immutable because the pipeline
#: publishes a date once and refuses to rewrite it (see _assert_unpublished in
#: src/upload.py). Kept byte-identical to IMMUTABLE_CACHE in src/upload.py: R2
#: stores that value on the object and it is preferred over this constant, so
#: this is only the fallback for objects written by something other than the
#: pipeline.
IMMUTABLE_CACHE = "public, max-age=31536000, s-maxage=31536000, immutable"

#: Cache-control for /api/today* — this resolves to a new date every morning.
#: s-maxage must stay below the publish interval so an edge PoP that filled its
#: cache shortly before the 04:00 UTC run cannot keep serving the previous day's
#: artwork for a further 24 hours.
TODAY_CACHE = "public, max-age=300, s-maxage=3600, stale-while-revalidate=604800"

ARCHIVE_DEFAULT_LIMIT = 100
ARCHIVE_MAX_LIMIT = 1000

#: Cap on R2 list() round trips per archive request. At one publish per day and
#: 1000 keys per call this bounds the walk at roughly 68 years of art while
#: stopping a pathologically large bucket from pinning the Worker.
ARCHIVE_MAX_LIST_CALLS = 25

TELEMETRY_BODY_LIMIT = 4096
TELEMETRY_ORIGINS_DEFAULT = "https://kevintcoughlin.com,https://www.kevintcoughlin.com"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_METADATA_KEY_RE = re.compile(r"^metadata/(\d{4})/(\d{2})/(\d{2})\.json$")
_DIGITS_RE = re.compile(r"^\d+$")
_MOBILE_UA_RE = re.compile(r"Mobile|Android|iPhone|iPad", re.IGNORECASE)

NOT_FOUND_MESSAGE = "Not found. Try /api/today, /api/YYYY-MM-DD, /api/archive, or /api/feed.atom"


# ---------------------------------------------------------------------------
# Format negotiation
# ---------------------------------------------------------------------------


def has_invalid_format(params: Mapping[str, str]) -> bool:
    """True when ``?format=`` is present but not a value we support.

    Silently negotiating past a bad value made ``?format=png`` and ``?format=jpg``
    indistinguishable from ``?format=auto``, so a typo in a caller's code looked
    like it worked and quietly served something else.
    """
    param = params.get("format")
    if param is None:
        return False
    return param.lower() not in FORMAT_PARAMS


def negotiate_format(accept: str, params: Mapping[str, str]) -> ImageFormat:
    param = params.get("format")
    if param is not None:
        lowered = param.lower()
        if lowered in ("avif", "webp", "jpeg"):
            return lowered  # type: ignore[return-value]

    # ?format=auto or absent → negotiate via Accept header
    if "image/avif" in accept:
        return "avif"
    if "image/webp" in accept:
        return "webp"
    return "jpeg"


def is_progressive(params: Mapping[str, str]) -> bool:
    return params.get("progressive") == "true"


def is_strip(params: Mapping[str, str]) -> bool:
    return params.get("strip") == "true"


def date_path(date_str: str) -> str:
    """``YYYY-MM-DD`` → ``YYYY/MM/DD``."""
    year, month, day = date_str.split("-")
    return f"{year}/{month}/{day}"


def variant_for_key(key: str) -> str:
    if key.endswith(".progressive.jpg"):
        return "progressive"
    if key.endswith(".stripped.jpg"):
        return "stripped"
    return "baseline"


def candidate_keys(
    base_path: str,
    fmt: ImageFormat,
    progressive: bool = False,
    strip: bool = False,
) -> list[tuple[str, str]]:
    """The R2 keys to try, in order, as ``(key, content_type)`` pairs.

    Collapsing getImageObject/headImageObject into one ordered candidate list
    keeps the GET and conditional-request paths from drifting apart — in the
    TypeScript worker the same fallback chain is spelled out twice.
    """
    candidates: list[tuple[str, str]] = []

    # Explicit JPEG variants should win over negotiated format selection.
    if progressive:
        candidates.append((f"{base_path}.progressive.jpg", "image/jpeg"))
    if strip:
        candidates.append((f"{base_path}.stripped.jpg", "image/jpeg"))

    candidates.append((f"{base_path}{FORMAT_EXT[fmt]}", FORMAT_CONTENT_TYPE[fmt]))

    # Fall back to JPEG if the requested format is unavailable.
    if fmt != "jpeg":
        candidates.append((f"{base_path}.jpg", "image/jpeg"))

    return candidates


# ---------------------------------------------------------------------------
# Conditional requests
# ---------------------------------------------------------------------------


def normalize_etag(etag: str) -> str:
    value = etag.strip()
    if value.startswith("W/"):
        value = value[2:].strip()
    return value


def etag_matches(if_none_match: str, http_etag: str | None) -> bool:
    """True when the If-None-Match header value matches the given ETag."""
    if if_none_match == "*":
        return True
    if not http_etag:
        return False
    target = normalize_etag(http_etag)
    return any(normalize_etag(part) == target for part in if_none_match.split(","))


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


def cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, HEAD, OPTIONS",
    }


def telemetry_cors_headers(origin: str) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST",
        "Access-Control-Allow-Headers": "content-type",
    }


def image_headers(
    key: str,
    http_etag: str | None,
    cache_control: str | None,
    content_type: str,
    today: bool,
) -> dict[str, str]:
    headers = {
        "Content-Type": content_type,
        "Cache-Control": TODAY_CACHE if today else (cache_control or IMMUTABLE_CACHE),
        "Vary": "Accept",
        "X-Variant": variant_for_key(key or ""),
        "Accept-CH": "DPR, Width, Viewport-Width",
        **cors_headers(),
    }
    if http_etag:
        headers["ETag"] = http_etag
    return headers


def json_headers(http_etag: str | None, cache_control: str | None, today: bool) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": TODAY_CACHE if today else (cache_control or IMMUTABLE_CACHE),
        **cors_headers(),
    }
    if http_etag:
        headers["ETag"] = http_etag
    return headers


def binary_headers(http_etag: str | None, content_type: str) -> dict[str, str]:
    headers = {
        "Content-Type": content_type,
        "Cache-Control": IMMUTABLE_CACHE,
        **cors_headers(),
    }
    if http_etag:
        headers["ETag"] = http_etag
    return headers


def not_modified_headers(etag: str, today: bool = False) -> dict[str, str]:
    """Headers for a 304.

    A 304 that omits Cache-Control and Vary invites caches to fall back to their
    own heuristics for how long the stored copy stays fresh, and to ignore that
    these resources are content-negotiated on Accept. Both must match what the
    corresponding 200 would have said.
    """
    return {
        "ETag": etag,
        "Cache-Control": TODAY_CACHE if today else IMMUTABLE_CACHE,
        "Vary": "Accept",
        **cors_headers(),
    }


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def get_allowed_origins(raw: str | None) -> set[str]:
    value = (raw or TELEMETRY_ORIGINS_DEFAULT).strip() or TELEMETRY_ORIGINS_DEFAULT
    return {part.strip() for part in value.split(",") if part.strip()}


def classify_ua(ua: str) -> str:
    return "mobile" if _MOBILE_UA_RE.search(ua) else "desktop"


def split_url(raw: object) -> tuple[str, str]:
    """``(hostname, path)`` for a RUM-reported URL, or empty strings if malformed."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(str(raw or ""))
    except ValueError:
        return "", ""
    # `new URL()` in the Worker throws without a scheme and authority; urlparse
    # is happy to return a bare path, so reject that explicitly to keep the two
    # implementations reporting the same empty strings for the same inputs.
    if not parsed.scheme or not parsed.netloc:
        return "", ""
    return parsed.hostname or "", parsed.path or ""


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def vitals_data_point(data: Mapping[str, object], ua_class: str) -> dict[str, object]:
    host, url_path = split_url(data.get("url"))
    return {
        "blobs": [
            str(data.get("name") or ""),
            str(data.get("rating") or ""),
            str(data.get("navigationType") or ""),
            host,
            url_path,
            ua_class,
        ],
        "doubles": [_as_float(data.get("value"))],
        "indexes": [host],
    }


def error_data_point(data: Mapping[str, object], ua_class: str) -> dict[str, object]:
    host, url_path = split_url(data.get("source"))
    return {
        "blobs": [
            str(data.get("message") or ""),
            str(data.get("source") or ""),
            host,
            url_path,
            ua_class,
        ],
        "doubles": [_as_float(data.get("lineno")), _as_float(data.get("colno"))],
        "indexes": [host],
    }


# ---------------------------------------------------------------------------
# Archive index
# ---------------------------------------------------------------------------


def date_from_metadata_key(key: str) -> str | None:
    """The publish date for a metadata object key, or None if the key is not one.

    Deliberately strict about the ``.json`` suffix: the same prefix also holds
    ``<date>.json.sig`` when signing is enabled, and counting both would report
    every signed day twice.
    """
    match = _METADATA_KEY_RE.match(key)
    return f"{match[1]}-{match[2]}-{match[3]}" if match else None


class ArchiveQueryError(ValueError):
    """Raised for a malformed ``?limit=`` or ``?before=``."""


def parse_archive_query(params: Mapping[str, str]) -> tuple[int, str | None]:
    """Parse ``?limit=`` and ``?before=`` into ``(limit, before)``.

    Invalid values are rejected rather than clamped, for the same reason
    ``?format=`` is: a silently corrected ``?limit=abc`` looks like it worked and
    quietly returns a different page than the caller asked for.
    """
    raw_limit = params.get("limit")
    limit = ARCHIVE_DEFAULT_LIMIT
    if raw_limit is not None:
        if not _DIGITS_RE.match(raw_limit):
            raise ArchiveQueryError("limit must be a positive integer")
        limit = int(raw_limit)
        if limit < 1 or limit > ARCHIVE_MAX_LIMIT:
            raise ArchiveQueryError(f"limit must be between 1 and {ARCHIVE_MAX_LIMIT}")

    before = params.get("before")
    if before is not None and not DATE_RE.match(before):
        raise ArchiveQueryError("before must be a date in YYYY-MM-DD form")

    return limit, before


def build_archive_page(
    dates: list[str],
    complete: bool,
    limit: int,
    before: str | None,
) -> dict[str, object]:
    """One page of the archive index, newest first.

    Paging is by date rather than an opaque cursor: dates are immutable and
    meaningful, so ``?before=`` survives new publishes, can be constructed by
    hand, and means the same thing tomorrow as it does today.
    """
    newest_first = list(reversed(dates))
    eligible = newest_first if before is None else [d for d in newest_first if d < before]
    page = eligible[:limit]

    body: dict[str, object] = {"dates": page, "count": len(page), "total": len(dates)}
    if len(eligible) > len(page):
        body["next"] = f"/api/archive?limit={limit}&before={page[-1]}"
    # The walk stopped at ARCHIVE_MAX_LIST_CALLS, so `total` counts what was seen
    # rather than what exists. Say so instead of reporting a short count as if it
    # were the whole archive.
    if not complete:
        body["truncated"] = True
    return body


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


def health_payload(published_date: str, now_epoch_s: float) -> tuple[int, dict[str, object]]:
    """``(status_code, body)`` for /api/health given the published date."""
    from datetime import UTC, datetime

    try:
        published = datetime.strptime(published_date, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        stale_days: int | None = None
    else:
        stale_days = int((now_epoch_s - published.timestamp()) // 86_400)

    healthy = stale_days is not None and stale_days <= 1
    body: dict[str, object] = {
        "status": "ok" if healthy else "stale",
        "date": published_date,
        "stale_days": stale_days,
    }
    return (200 if healthy else 503), body


# ---------------------------------------------------------------------------
# Atom feed
# ---------------------------------------------------------------------------
#
# Ported from worker/src/feed.ts, which follows
# https://kevincox.ca/2022/05/06/rss-feed-best-practices/:
#   - every URL is absolute https, so nothing depends on a reader resolving
#     relative links (most do it wrong or not at all);
#   - entry ids are the date permalinks, which never change, so a reader never
#     shows an old day as new;
#   - entry times come from the write-once metadata, so they are stable across
#     rebuilds and never in the future;
#   - content is full, self-contained HTML (no scripts, styles or classes) so
#     the entry reads well inside any reader.
#
# Times are epoch milliseconds held in a float, as in JavaScript, so that an
# unparseable publish date propagates as NaN exactly where the Worker's does.

#: Entries in the feed. ~2 KB each, so far below the 1 MB readers tolerate.
FEED_ENTRY_LIMIT = 30

#: Bumped whenever the rendered markup changes, so the ETag (which is computed
#: from the date list alone, before any metadata is read) changes with it and
#: readers re-fetch instead of keeping the old rendering forever. Must stay equal
#: to FEED_VERSION in worker/src/feed.ts, or the two Workers hand out different
#: validators for the same document.
FEED_VERSION = "1"

#: Short and identifiable in a reader's sidebar; free of & < > on purpose.
FEED_TITLE = "Bauhaus — daily stylized art"

_FEED_SUBTITLE = "A public-domain artwork restyled after a famous painting, published once a day."

#: The page that shows the art: kevintcoughlin.com uses today's image as its hero.
_FEED_ALTERNATE = "https://kevintcoughlin.com/"

_AUTHOR_NAME = "Kevin Coughlin"
_AUTHOR_URI = "https://kevintcoughlin.com/"

#: Cache-Control for /api/feed.atom. An hour is what the RSS best-practices guide
#: recommends for a daily feed: readers that honour it poll 24 times a day, and
#: the conditional request each one makes is answered from the R2 listing alone
#: (see feed_etag).
FEED_CACHE = "public, max-age=3600"

#: Display names for metadata ``source`` values (src/fetch.py's fetchers).
_SOURCE_NAMES: dict[str, str] = {
    "met": "The Metropolitan Museum of Art",
    "artic": "Art Institute of Chicago",
    "unsplash": "Unsplash",
}

# Characters XML 1.0 forbids outright. Metadata comes from museum APIs, and a
# single stray control character makes the whole document unparseable.
_INVALID_XML_CHARS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

# JavaScript's \s, which is not Python's: it includes U+FEFF and excludes
# U+001C–U+001F and U+0085. Spelled out so clean() collapses the same runs.
_JS_WHITESPACE_RE = re.compile("[\t\n\v\f\r    -     　﻿]+")

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DAY_MS = 86_400_000


@dataclass(frozen=True)
class FeedEntry:
    """One published day: its date and its metadata JSON (all fields optional)."""

    date: str
    meta: Mapping[str, Any]


def escape_xml(value: str) -> str:
    """Escapes text for XML element content and double-quoted attributes."""
    return (
        _INVALID_XML_CHARS_RE.sub("", value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _escape_html(value: str) -> str:
    """HTML text/attribute escaping; same set of characters, ``&#39;`` for older parsers."""
    return escape_xml(value).replace("&apos;", "&#39;")


def _clean(value: object) -> str:
    """Collapse whitespace runs and trim, treating a missing field as empty.

    A present but non-string field is an error, as it is in the Worker (where
    ``.replace`` is not a function on it), so both answer 503 rather than one
    of them rendering a guess.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"expected a string metadata field, got {type(value).__name__}")
    return _JS_WHITESPACE_RE.sub(" ", value).strip(" ")


# -- URL handling ------------------------------------------------------------

_DEFAULT_PORTS = {"http": "80", "https": "443"}
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_FORBIDDEN_HOST_CHARS = frozenset(" #%/:<>?@[\\]^|\x7f") | {chr(c) for c in range(0x20)}
# WHATWG percent-encode sets for special schemes, minus the C0/non-ASCII range
# that _percent_encode always encodes.
_PATH_ENCODE = frozenset(' "#<>?`{}')
_QUERY_ENCODE = frozenset(" \"#<>'")
_FRAGMENT_ENCODE = frozenset(' "<>`')
_DOT_SEGMENTS = frozenset({".", "%2e"})
_DOUBLE_DOT_SEGMENTS = frozenset({"..", ".%2e", "%2e.", "%2e%2e"})


def _percent_encode(value: str, encode_set: frozenset[str]) -> str:
    out: list[str] = []
    for ch in value:
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            ch, code = "�", 0xFFFD
        if code < 0x20 or code > 0x7E or ch in encode_set:
            out.extend(f"%{byte:02X}" for byte in ch.encode())
        else:
            out.append(ch)
    return "".join(out)


def _normalize_path(path: str) -> str:
    segments = path.split("/")[1:] if path else [""]
    out: list[str] = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        lowered = segment.lower()
        if lowered in _DOUBLE_DOT_SEGMENTS:
            if out:
                out.pop()
            if last:
                out.append("")
        elif lowered in _DOT_SEGMENTS:
            if last:
                out.append("")
        else:
            out.append(segment)
    return "/" + "/".join(out)


def _normalize_host(host: str) -> str | None:
    if not host:
        return None
    if host.startswith("["):
        # IPv6 literals are passed through as written (lowercased); the Worker
        # would also compress them, but no metadata link is an IP literal.
        return host.lower() if host.endswith("]") else None
    if any(ch in _FORBIDDEN_HOST_CHARS for ch in host):
        return None
    if host.isascii():
        return host.lower()
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return None


def http_url_href(value: object) -> str | None:
    """The serialized form of an absolute http(s) URL, or None.

    Stands in for ``new URL(value).href`` restricted to http and https: the
    WHATWG parser's normalizations that a metadata link can plausibly hit —
    scheme and host case, default ports, an empty path becoming ``/``, dot
    segments, backslashes, and percent-encoding of spaces, quotes and non-ASCII
    — are reproduced. It is not a full WHATWG parser (IPv4 shorthand and IPv6
    compression are not), so exotic input may serialize differently from the
    Worker's, but it is never less strict about what counts as http(s).
    """
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip("".join(chr(c) for c in range(0x21)))
    raw = raw.replace("\t", "").replace("\n", "").replace("\r", "")

    match = _SCHEME_RE.match(raw)
    if not match:
        return None
    scheme = match[1].lower()
    if scheme not in _DEFAULT_PORTS:
        return None
    rest = raw[match.end() :]

    fragment: str | None = None
    if "#" in rest:
        rest, fragment = rest.split("#", 1)
    query: str | None = None
    if "?" in rest:
        rest, query = rest.split("?", 1)

    # Special schemes treat \ as / and ignore any run of slashes before the host.
    rest = rest.replace("\\", "/").lstrip("/")
    authority, slash, path = rest.partition("/")
    path = slash + path

    userinfo, at, hostport = authority.rpartition("@")
    if hostport.startswith("["):
        end = hostport.find("]")
        host, port = (hostport[: end + 1], hostport[end + 2 :]) if end != -1 else (hostport, "")
        if end != -1 and hostport[end + 1 : end + 2] not in ("", ":"):
            return None
    else:
        host, _, port = hostport.partition(":")
    normalized_host = _normalize_host(host)
    if normalized_host is None:
        return None
    if port:
        if not port.isascii() or not port.isdigit() or int(port) > 65535:
            return None
        port = str(int(port))
        if port == _DEFAULT_PORTS[scheme]:
            port = ""

    href = f"{scheme}://"
    if at:
        href += f"{userinfo}@"
    href += normalized_host + (f":{port}" if port else "")
    href += _percent_encode(_normalize_path(path), _PATH_ENCODE)
    if query is not None:
        href += "?" + _percent_encode(query, _QUERY_ENCODE)
    if fragment is not None:
        href += "#" + _percent_encode(fragment, _FRAGMENT_ENCODE)
    return href


def canonical_origin(scheme: str, netloc: str) -> str:
    """The scheme-canonical origin feed URLs are built from. Never http.

    Mirrors ``https://${url.host}``: the host is lowercased and a port that is
    the default for the request's own scheme is dropped, as URL.host does.
    """
    host = netloc.lower()
    if not host.endswith("]") and ":" in host:
        name, port = host.rsplit(":", 1)
        if port == "" or port == _DEFAULT_PORTS.get(scheme.lower()):
            host = name
    return f"https://{host}"


# -- Times -------------------------------------------------------------------


def parse_date_ms(value: object) -> float:
    """Epoch ms for an ISO 8601 or RFC 2822/HTTP date, or NaN — Date.parse's contract.

    A time without an offset is read as UTC, which is what Date.parse does in
    the Workers runtime (whose local time zone is UTC).
    """
    if not isinstance(value, str):
        return math.nan
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return math.nan
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return float((parsed - _EPOCH) // timedelta(milliseconds=1))


def _rfc3339(ms: float) -> str:
    """RFC 3339 in UTC with second precision — the form validators and readers agree on."""
    if math.isnan(ms):
        # Date#toISOString throws on an invalid date; this surfaces as a 503.
        raise ValueError("invalid time value")
    t = _EPOCH + timedelta(milliseconds=ms)
    return f"{t.year:04d}-{t.month:02d}-{t.day:02d}T{t.hour:02d}:{t.minute:02d}:{t.second:02d}Z"


def http_date(ms: float) -> str:
    """``new Date(ms).toUTCString()``, e.g. ``Sat, 24 Oct 2026 04:09:21 GMT``."""
    if math.isnan(ms):
        return "Invalid Date"
    return formatdate(math.floor(ms / 1000), usegmt=True)


def entry_timestamp(entry: FeedEntry, now_ms: float) -> float:
    """The moment an entry was published, in epoch ms.

    ``generated_at`` is written once by the pipeline alongside the art and never
    rewritten, so it is stable across feed rebuilds. It is trusted only within
    two days of the publish date (the 04:00 UTC run and its same-day retries);
    otherwise — missing, a backfill generated months later, a clock-skewed
    generator — the entry falls back to midnight UTC of its date. A timestamp in
    the future is never emitted: it would pin the entry to the top of readers
    that sort by date.
    """
    midnight = parse_date_ms(f"{entry.date}T00:00:00Z")
    generated = parse_date_ms(entry.meta.get("generated_at"))
    if (
        not math.isnan(generated)
        and midnight <= generated < midnight + 2 * _DAY_MS
        and generated <= now_ms
    ):
        return float(math.floor(generated / 1000) * 1000)
    # Math.min propagates NaN; Python's min() would not reliably.
    return math.nan if math.isnan(midnight) else min(midnight, now_ms)


def feed_updated(entries: Sequence[FeedEntry], now_ms: float) -> float:
    """Newest-entry time in epoch ms; drives ``<updated>``, Last-Modified and If-Modified-Since."""
    times = [entry_timestamp(e, now_ms) for e in entries]
    return math.nan if any(math.isnan(t) for t in times) else max(times)


def not_modified_since(if_modified_since: str, last_modified_ms: float) -> bool:
    """True when If-Modified-Since is at or after the last modification (second precision)."""
    since = parse_date_ms(if_modified_since)
    return not math.isnan(since) and math.floor(last_modified_ms / 1000) * 1000 <= since


# -- Rendering ---------------------------------------------------------------


def _artist(meta: Mapping[str, Any]) -> str:
    return _clean(meta.get("artist")) or _clean(meta.get("photographer"))


def entry_title(entry: FeedEntry) -> str:
    """"Title — Artist", or whichever of the two exists, or the date."""
    title = _clean(entry.meta.get("title"))
    artist = _artist(entry.meta)
    if title and artist:
        return f"{title} — {artist}"
    return title or artist or f"Bauhaus for {entry.date}"


def _entry_summary(entry: FeedEntry) -> str:
    title = _clean(entry.meta.get("title")) or "Artwork"
    artist = _artist(entry.meta)
    style_artist = _clean(entry.meta.get("style_artist"))
    text = f"{title} by {artist}" if artist else title
    if style_artist:
        text += f", restyled after {style_artist}"
    return f"{text}."


def _js_integer(value: object) -> str | None:
    """``String(value)`` when ``Number.isInteger(value)``, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return None


def _stylized_size(meta: Mapping[str, Any]) -> str:
    variants = meta.get("variants")
    if not isinstance(variants, list):
        return ""
    stylized = next(
        (v for v in variants if isinstance(v, Mapping) and v.get("type") == "stylized"), None
    )
    if stylized is None:
        return ""
    width = _js_integer(stylized.get("width"))
    height = _js_integer(stylized.get("height"))
    return f' width="{width}" height="{height}"' if width and height else ""


def _link(text: str, href: str | None) -> str:
    return f'<a href="{_escape_html(href)}">{text}</a>' if href else text


def entry_content(entry: FeedEntry, origin: str) -> str:
    """The entry's HTML body. Semantic elements only: readers strip classes, styles and scripts."""
    m = entry.meta
    title = _clean(m.get("title"))
    artist = _artist(m)
    style_artist = _clean(m.get("style_artist"))
    style_title = _clean(m.get("style_title"))
    source = _clean(m.get("source"))
    source_name = _SOURCE_NAMES.get(source, source)
    source_url = http_url_href(m.get("source_url"))
    artist_url = http_url_href(m.get("photographer_url"))
    style_url = http_url_href(m.get("style_source_url"))
    license_url = http_url_href(m.get("license_url"))
    license_name = _clean(m.get("license"))
    created = _clean(m.get("date"))

    # ?format=jpeg rather than the negotiated permalink: reader image proxies
    # cache by URL and ignore Vary, so one reader asking for AVIF could hand
    # AVIF to every client behind the same proxy.
    img_src = f"{origin}/api/{entry.date}?format=jpeg"
    alt = title or "Artwork"
    if artist:
        alt += f" by {artist}"
    if style_artist:
        alt += f", restyled in the style of {style_artist}"

    credit = f"<cite>{_escape_html(title)}</cite>" if title else "Untitled"
    if artist:
        credit += f" by {_link(_escape_html(artist), artist_url)}"
    if created and created != entry.date:
        credit += f" ({_escape_html(created)})"
    if source_name:
        credit += f", via {_link(_escape_html(source_name), source_url)}"
    credit += "."

    parts = [
        f'<figure><img src="{_escape_html(img_src)}" alt="{_escape_html(alt)}"{_stylized_size(m)}>'
        f"<figcaption>{credit}</figcaption></figure>"
    ]
    if style_artist or style_title:
        style = _link(f"<cite>{_escape_html(style_title)}</cite>", style_url) if style_title else ""
        by = _escape_html(style_artist) if style_artist else ""
        parts.append(f"<p>Restyled after {' by '.join(p for p in (style, by) if p)}.</p>")
    if license_name:
        parts.append(f"<p>License: {_link(_escape_html(license_name), license_url)}.</p>")
    parts.append(
        f"<p>{_link('Full-resolution image', f'{origin}/api/{entry.date}')} · "
        f"{_link('Original', f'{origin}/api/{entry.date}/original')} · "
        f"{_link('Metadata', f'{origin}/api/{entry.date}.json')}</p>"
    )
    return "".join(parts)


def _entry_categories(entry: FeedEntry) -> list[str]:
    categories: list[str] = []
    source = _clean(entry.meta.get("source"))
    if source:
        label = _SOURCE_NAMES.get(source)
        categories.append(
            f'<category term="{escape_xml(source)}" label="{escape_xml(label)}"/>'
            if label
            else f'<category term="{escape_xml(source)}"/>'
        )
    style_artist = _clean(entry.meta.get("style_artist"))
    if style_artist:
        categories.append(
            f'<category term="{escape_xml(style_artist)}" label="{escape_xml(f"Style: {style_artist}")}"/>'
        )
    return categories


def build_atom_feed(entries: Sequence[FeedEntry], origin: str, now_ms: float) -> str:
    """Renders the document, byte-identical to the Worker's.

    ``entries`` must be non-empty and newest first; the caller decides what to do
    with an empty archive.
    """
    feed_url = f"{origin}/api/feed.atom"
    lines = [
        '<?xml version="1.0" encoding="utf-8"?>',
        '<feed xmlns="http://www.w3.org/2005/Atom" xml:lang="en">',
        f"  <title>{escape_xml(FEED_TITLE)}</title>",
        f"  <subtitle>{escape_xml(_FEED_SUBTITLE)}</subtitle>",
        f"  <id>{escape_xml(feed_url)}</id>",
        f'  <link rel="self" type="application/atom+xml" href="{escape_xml(feed_url)}"/>',
        f'  <link rel="alternate" type="text/html" href="{escape_xml(_FEED_ALTERNATE)}"/>',
        f"  <updated>{_rfc3339(feed_updated(entries, now_ms))}</updated>",
        f"  <author><name>{escape_xml(_AUTHOR_NAME)}</name><uri>{escape_xml(_AUTHOR_URI)}</uri></author>",
    ]

    for entry in entries:
        permalink = f"{origin}/api/{entry.date}"
        time = _rfc3339(entry_timestamp(entry, now_ms))
        lines.extend(
            [
                "  <entry>",
                f"    <title>{escape_xml(entry_title(entry))}</title>",
                f"    <id>{escape_xml(permalink)}</id>",
                f'    <link rel="alternate" href="{escape_xml(permalink)}"/>',
                f"    <published>{time}</published>",
                f"    <updated>{time}</updated>",
                *(f"    {c}" for c in _entry_categories(entry)),
                f"    <summary>{escape_xml(_entry_summary(entry))}</summary>",
                f'    <content type="html">{escape_xml(entry_content(entry, origin))}</content>',
                "  </entry>",
            ]
        )

    lines.extend(["</feed>", ""])
    return "\n".join(lines)


def feed_etag(dates: Sequence[str], origin: str) -> str:
    """Strong ETag computed from the dates alone, before any metadata is read.

    A conditional request that matches therefore costs one R2 list and nothing
    else. That is sound because every input to the body is fixed once the date
    list is: published metadata is write-once (src/upload.py refuses to rewrite a
    date), and the remaining inputs — the origin and the renderer — are covered
    by including them in the hash. Byte-identical to feedEtag in feed.ts.
    """
    data = f"{FEED_VERSION}\n{origin}\n" + "\n".join(dates)
    return f'"feed-{hashlib.sha256(data.encode()).digest()[:16].hex()}"'


def feed_headers(etag: str, last_modified_ms: float | None = None) -> dict[str, str]:
    headers = {
        "Content-Type": "application/atom+xml; charset=utf-8",
        "Cache-Control": FEED_CACHE,
        "ETag": etag,
        **cors_headers(),
    }
    if last_modified_ms is not None:
        headers["Last-Modified"] = http_date(last_modified_ms)
    return headers
