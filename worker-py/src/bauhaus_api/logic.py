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

import re
from collections.abc import Mapping
from typing import Literal

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

NOT_FOUND_MESSAGE = "Not found. Try /api/today, /api/YYYY-MM-DD, or /api/archive"


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


def _as_float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
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
