"""Bauhaus API — FastAPI port of the Cloudflare Worker that serves stylized CC0
artwork from R2.

Routes (identical to worker/src/index.ts):
    GET|HEAD /api/today               → today's stylized image
    GET|HEAD /api/today.json          → today's metadata
    GET|HEAD /api/today.manifest.json → today's responsive manifest
    GET|HEAD /api/:date               → stylized image for YYYY-MM-DD
    GET|HEAD /api/:date/original      → original unstylized image
    GET|HEAD /api/:date.json          → metadata for date
    GET|HEAD /api/:date.json.sig      → detached PGP signature over the metadata
    GET|HEAD /api/:date.manifest.json → responsive manifest for date
    GET|HEAD /api/archive             → dates that have published artwork
    GET      /api/health              → publish freshness, for an uptime monitor
    POST     /api/vitals              → ingest Web Vitals RUM (Analytics Engine)
    POST     /api/err                 → ingest JS error RUM (Analytics Engine)

The route ordering below is load bearing. Starlette matches in registration
order, so the literal ``/api/today*`` and ``/api/:date.json.sig`` paths must be
declared before the ``/api/{date}.json`` and ``/api/{date}`` patterns that would
otherwise swallow them.
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence

from fastapi import FastAPI, Request, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from .logic import (
    ARCHIVE_MAX_LIST_CALLS,
    INVALID_FORMAT_MESSAGE,
    NOT_FOUND_MESSAGE,
    TELEMETRY_BODY_LIMIT,
    TODAY_CACHE,
    ArchiveQueryError,
    ImageFormat,
    binary_headers,
    build_archive_page,
    candidate_keys,
    classify_ua,
    cors_headers,
    date_from_metadata_key,
    date_path,
    error_data_point,
    etag_matches,
    get_allowed_origins,
    has_invalid_format,
    health_payload,
    image_headers,
    is_progressive,
    is_strip,
    json_headers,
    negotiate_format,
    not_modified_headers,
    parse_archive_query,
    telemetry_cors_headers,
    vitals_data_point,
)
from .logic import DATE_RE as _DATE_RE

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class NoLatestError(Exception):
    """Raised when latest.json is missing.

    Separates "not published yet" from a genuine R2 fault, so the former can
    still answer 404 while the latter answers 503.
    """


# ---------------------------------------------------------------------------
# JS interop
# ---------------------------------------------------------------------------


def _to_js(obj: object) -> object:
    """Convert a Python dict to a plain JavaScript object.

    Analytics Engine's writeDataPoint takes a JS object literal; a bare Pyodide
    dict proxy is not one. Imported lazily, and returned unchanged when Pyodide
    is absent, so the module stays importable under plain CPython for tests.
    """
    try:
        from js import Object
        from pyodide.ffi import to_js
    except ImportError:
        return obj

    return to_js(obj, dict_converter=Object.fromEntries)


def _etag(obj: object) -> str | None:
    return getattr(obj, "httpEtag", None)


def _cache_control(obj: object) -> str | None:
    """The Cache-Control R2 stored on the object, if any.

    The pipeline writes this at upload time, and it is preferred over the
    Worker's own constant so a change in src/upload.py takes effect without a
    redeploy.
    """
    metadata = getattr(obj, "httpMetadata", None)
    if metadata is None:
        return None
    return getattr(metadata, "cacheControl", None)


async def _body_bytes(obj: object) -> bytes:
    """Read an R2 object body into memory.

    Unlike the TypeScript worker — which hands R2's ReadableStream straight to
    the Response and never touches the bytes — an ASGI app has to materialize
    the body. Published images are a few hundred KB, well inside the Worker
    memory limit, but this is the one real efficiency regression of the port.
    """
    blob = await obj.blob()  # type: ignore[attr-defined]
    return await blob.bytes()


async def _get_today(bucket: object) -> str:
    obj = await bucket.get("latest.json")  # type: ignore[attr-defined]
    if obj is None:
        raise NoLatestError("No latest.json found")
    blob = await obj.blob()
    return json.loads(await blob.text())["date"]


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _json_body(payload: object) -> bytes:
    # separators match JSON.stringify, so both implementations emit byte-identical
    # bodies and therefore the same Content-Length for the same input.
    return json.dumps(payload, separators=(",", ":")).encode()


def _error_response(status: int, msg: str, cache_control: str = "no-store") -> Response:
    """JSON error with CORS — the single shape every non-telemetry failure uses."""
    return Response(
        content=_json_body({"error": msg}),
        status_code=status,
        headers={
            "Content-Type": "application/json",
            "Cache-Control": cache_control,
            **cors_headers(),
        },
    )


def _not_found(msg: str) -> Response:
    return _error_response(404, msg)


def _not_modified(etag: str, today: bool = False) -> Response:
    return Response(status_code=304, headers=not_modified_headers(etag, today))


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(_request: Request, exc: StarletteHTTPException) -> Response:
    # Starlette's default renders {"detail": ...} as plain text or JSON with a
    # different key and no CORS headers, which browsers would see as an opaque
    # CORS failure rather than the API's documented error shape.
    detail = "Method not allowed" if exc.status_code == 405 else str(exc.detail)
    return _error_response(exc.status_code, detail)


@app.middleware("http")
async def _catch_upstream_failures(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Turn an R2 fault or a missing latest.json into the API's JSON error shape.

    Without this, either escapes as a bare 500 carrying no CORS headers, so
    browsers see an opaque CORS failure rather than the JSON error every other
    path returns — precisely when the API is already unhealthy.
    """
    try:
        return await call_next(request)
    except NoLatestError:
        return _not_found("No artwork published yet")
    except Exception:
        print("Unhandled error", traceback.format_exc())
        return _error_response(503, "Upstream storage unavailable")


def _check_format(request: Request) -> None:
    if has_invalid_format(request.query_params):
        raise StarletteHTTPException(status_code=400, detail=INVALID_FORMAT_MESSAGE)


def _negotiate(request: Request) -> ImageFormat:
    return negotiate_format(request.headers.get("accept", ""), request.query_params)


# ---------------------------------------------------------------------------
# Object serving
# ---------------------------------------------------------------------------


async def _first_present(
    fetch: Callable[[str], Awaitable[object | None]],
    candidates: Sequence[tuple[str, str]],
) -> tuple[object, str, str] | None:
    """The first candidate key that exists, as ``(object, key, content_type)``."""
    for key, content_type in candidates:
        obj = await fetch(key)
        if obj is not None:
            return obj, key, content_type
    return None


async def _serve_image(
    request: Request,
    bucket: object,
    base_path: str,
    today: bool,
    not_found_msg: str,
) -> Response:
    """Serve an image, using R2 head() for If-None-Match and HEAD requests.

    head() is a Class A operation that returns metadata only, so a 304 or a HEAD
    never pays to read the object body out of R2.
    """
    _check_format(request)
    candidates = candidate_keys(
        base_path,
        _negotiate(request),
        is_progressive(request.query_params),
        is_strip(request.query_params),
    )
    if_none_match = request.headers.get("if-none-match")
    is_head = request.method == "HEAD"

    if is_head or if_none_match:
        found = await _first_present(bucket.head, candidates)  # type: ignore[attr-defined]
        if found is None:
            return _not_found(not_found_msg)
        head, key, content_type = found

        if if_none_match and etag_matches(if_none_match, _etag(head)):
            return _not_modified(_etag(head) or "", today)

        headers = image_headers(key, _etag(head), _cache_control(head), content_type, today)
        if is_head:
            return Response(status_code=200, headers=headers)

        # ETag did not match — read the full object using the resolved key.
        obj = await bucket.get(key)  # type: ignore[attr-defined]
        if obj is None:
            return _not_found(not_found_msg)
        return Response(content=await _body_bytes(obj), headers=headers)

    found = await _first_present(bucket.get, candidates)  # type: ignore[attr-defined]
    if found is None:
        return _not_found(not_found_msg)
    obj, key, content_type = found
    return Response(
        content=await _body_bytes(obj),
        headers=image_headers(key, _etag(obj), _cache_control(obj), content_type, today),
    )


async def _serve_object(
    request: Request,
    bucket: object,
    key: str,
    headers_for: Callable[[object], dict[str, str]],
    today: bool,
    not_found_msg: str,
) -> Response:
    """Serve a single fixed key (metadata, manifest, or detached signature)."""
    if_none_match = request.headers.get("if-none-match")
    is_head = request.method == "HEAD"

    if is_head or if_none_match:
        head = await bucket.head(key)  # type: ignore[attr-defined]
        if head is None:
            return _not_found(not_found_msg)
        if if_none_match and etag_matches(if_none_match, _etag(head)):
            return _not_modified(_etag(head) or "", today)
        if is_head:
            return Response(status_code=200, headers=headers_for(head))

    obj = await bucket.get(key)  # type: ignore[attr-defined]
    if obj is None:
        return _not_found(not_found_msg)
    return Response(content=await _body_bytes(obj), headers=headers_for(obj))


async def _serve_json(
    request: Request, bucket: object, key: str, today: bool, not_found_msg: str
) -> Response:
    _check_format(request)
    return await _serve_object(
        request,
        bucket,
        key,
        lambda obj: json_headers(_etag(obj), _cache_control(obj), today),
        today,
        not_found_msg,
    )


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


async def _handle_telemetry(request: Request, kind: str) -> Response:
    env = request.scope["env"]
    origin = request.headers.get("origin", "")
    allowed = get_allowed_origins(getattr(env, "ALLOWED_ORIGINS", None))

    if request.method == "OPTIONS":
        if origin not in allowed:
            return Response(status_code=403)
        return Response(status_code=204, headers=telemetry_cors_headers(origin))

    if request.method != "POST":
        return Response(content=b"Method Not Allowed", status_code=405)

    if origin not in allowed:
        return Response(content=b"Forbidden", status_code=403)

    # Reject oversized requests early via Content-Length, then re-check the body
    # itself because the header is client-supplied and may be absent or a lie.
    try:
        declared = int(request.headers.get("content-length", "0"))
    except ValueError:
        declared = 0
    if declared > TELEMETRY_BODY_LIMIT:
        return Response(status_code=413, headers=telemetry_cors_headers(origin))

    raw = await request.body()
    try:
        body = raw.decode()
    except UnicodeDecodeError:
        return Response(status_code=400, headers=telemetry_cors_headers(origin))
    if len(body) > TELEMETRY_BODY_LIMIT:
        return Response(status_code=413, headers=telemetry_cors_headers(origin))

    try:
        data = json.loads(body)
    except ValueError:
        return Response(status_code=400, headers=telemetry_cors_headers(origin))
    if not isinstance(data, Mapping):
        return Response(status_code=400, headers=telemetry_cors_headers(origin))

    ua_class = classify_ua(request.headers.get("user-agent", ""))
    if kind == "vitals":
        env.WEB_VITALS.writeDataPoint(_to_js(vitals_data_point(data, ua_class)))
    else:
        env.WEB_ERRORS.writeDataPoint(_to_js(error_data_point(data, ua_class)))

    return Response(status_code=204, headers=telemetry_cors_headers(origin))


_ANY_METHOD = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@app.api_route("/api/vitals", methods=_ANY_METHOD, include_in_schema=False)
async def vitals(request: Request) -> Response:
    return await _handle_telemetry(request, "vitals")


@app.api_route("/api/err", methods=_ANY_METHOD, include_in_schema=False)
async def err(request: Request) -> Response:
    return await _handle_telemetry(request, "err")


# ---------------------------------------------------------------------------
# Health and archive
# ---------------------------------------------------------------------------


@app.get("/api/health", include_in_schema=False)
async def health(request: Request) -> Response:
    """Publish freshness, for an external uptime monitor.

    The failure mode with no signal at all is the cron simply not running:
    GitHub disables scheduled workflows after 60 days of repo inactivity, and a
    skipped or failed schedule produces no run, so the ntfy hooks in generate.yml
    never fire. Staleness measured at the serving end catches that, plus R2 write
    failures and publish bugs, in one probe.
    """
    import time

    env = request.scope["env"]
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store", **cors_headers()}
    try:
        today = await _get_today(env.BUCKET)
    except Exception as exc:
        # Never published, or R2 is down — either way this probe must report
        # unhealthy rather than the 404 the generic handler would produce.
        reason = "no artwork published" if isinstance(exc, NoLatestError) else "storage unavailable"
        return Response(
            content=_json_body({"status": "unhealthy", "error": reason}),
            status_code=503,
            headers=headers,
        )

    status, body = health_payload(today, time.time())
    return Response(content=_json_body(body), status_code=status, headers=headers)


@app.api_route("/api/archive", methods=["GET", "HEAD"], include_in_schema=False)
async def archive(request: Request) -> Response:
    """Which dates have published artwork.

    Without this the archive is undiscoverable: every other date endpoint
    requires the caller to already know the date, so a gallery or a "random past
    day" consumer has nothing to enumerate and has to guess.
    """
    env = request.scope["env"]
    try:
        limit, before = parse_archive_query(request.query_params)
    except ArchiveQueryError as exc:
        return _error_response(400, str(exc))

    # R2 lists keys in lexicographic order and the keys are metadata/YYYY/MM/DD,
    # so lexicographic order is already chronological order — no sorting needed.
    dates: list[str] = []
    cursor: str | None = None
    complete = False
    for _ in range(ARCHIVE_MAX_LIST_CALLS):
        options: dict[str, object] = {"prefix": "metadata/"}
        if cursor:
            options["cursor"] = cursor
        page = await env.BUCKET.list(**options)
        for obj in page["objects"]:
            date = date_from_metadata_key(obj.key)
            if date:
                dates.append(date)
        if not page["truncated"]:
            complete = True
            break
        cursor = page["cursor"]

    body = build_archive_page(dates, complete, limit, before)
    headers = {
        "Content-Type": "application/json",
        # The newest page gains an entry every morning, so this tracks /api/today
        # rather than the immutable per-date resources it indexes.
        "Cache-Control": TODAY_CACHE,
        **cors_headers(),
    }
    content = None if request.method == "HEAD" else _json_body(body)
    return Response(content=content, status_code=200, headers=headers)


# ---------------------------------------------------------------------------
# Today
# ---------------------------------------------------------------------------


@app.api_route("/api/today", methods=["GET", "HEAD"], include_in_schema=False)
async def today_image(request: Request) -> Response:
    # Checked before resolving today's date so a bad ?format= is a 400 even when
    # nothing has been published yet, matching the Worker's ordering.
    _check_format(request)
    bucket = request.scope["env"].BUCKET
    date = await _get_today(bucket)
    return await _serve_image(
        request, bucket, f"stylized/{date_path(date)}", True, "No image for today"
    )


@app.api_route("/api/today.json", methods=["GET", "HEAD"], include_in_schema=False)
async def today_metadata(request: Request) -> Response:
    # Checked before resolving today's date so a bad ?format= is a 400 even when
    # nothing has been published yet, matching the Worker's ordering.
    _check_format(request)
    bucket = request.scope["env"].BUCKET
    date = await _get_today(bucket)
    return await _serve_json(
        request, bucket, f"metadata/{date_path(date)}.json", True, "No metadata for today"
    )


@app.api_route("/api/today.manifest.json", methods=["GET", "HEAD"], include_in_schema=False)
async def today_manifest(request: Request) -> Response:
    # Checked before resolving today's date so a bad ?format= is a 400 even when
    # nothing has been published yet, matching the Worker's ordering.
    _check_format(request)
    bucket = request.scope["env"].BUCKET
    date = await _get_today(bucket)
    return await _serve_json(
        request, bucket, f"manifests/{date_path(date)}.json", True, "No manifest for today"
    )


# ---------------------------------------------------------------------------
# Per-date resources
# ---------------------------------------------------------------------------


def _valid_date(request: Request, date: str) -> bool:
    """Whether the path segment is a date, after the shared ?format= check.

    A non-date segment is not a 400 — it simply is not one of these routes, and
    falls through to the same catch-all 404 the Worker ends with.
    """
    _check_format(request)
    return bool(_DATE_RE.match(date))


@app.api_route("/api/{date}.manifest.json", methods=["GET", "HEAD"], include_in_schema=False)
async def date_manifest(request: Request, date: str) -> Response:
    if not _valid_date(request, date):
        return _not_found(NOT_FOUND_MESSAGE)
    bucket = request.scope["env"].BUCKET
    return await _serve_json(
        request, bucket, f"manifests/{date_path(date)}.json", False, f"No manifest for {date}"
    )


@app.api_route("/api/{date}.json.sig", methods=["GET", "HEAD"], include_in_schema=False)
async def date_signature(request: Request, date: str) -> Response:
    """The detached PGP signature over the metadata.

    The pipeline has always been able to upload this object, but nothing served
    it, so a published signature could never actually be fetched and checked —
    signing was unverifiable by construction.
    """
    if not _valid_date(request, date):
        return _not_found(NOT_FOUND_MESSAGE)
    bucket = request.scope["env"].BUCKET
    return await _serve_object(
        request,
        bucket,
        f"metadata/{date_path(date)}.json.sig",
        lambda obj: binary_headers(_etag(obj), "application/pgp-signature"),
        False,
        f"No signature for {date}",
    )


@app.api_route("/api/{date}.json", methods=["GET", "HEAD"], include_in_schema=False)
async def date_metadata(request: Request, date: str) -> Response:
    if not _valid_date(request, date):
        return _not_found(NOT_FOUND_MESSAGE)
    bucket = request.scope["env"].BUCKET
    return await _serve_json(
        request, bucket, f"metadata/{date_path(date)}.json", False, f"No metadata for {date}"
    )


@app.api_route("/api/{date}/original", methods=["GET", "HEAD"], include_in_schema=False)
async def date_original(request: Request, date: str) -> Response:
    if not _valid_date(request, date):
        return _not_found(NOT_FOUND_MESSAGE)
    bucket = request.scope["env"].BUCKET
    return await _serve_image(
        request, bucket, f"originals/{date_path(date)}", False, f"No original for {date}"
    )


@app.api_route("/api/{date}", methods=["GET", "HEAD"], include_in_schema=False)
async def date_image(request: Request, date: str) -> Response:
    if not _valid_date(request, date):
        return _not_found(NOT_FOUND_MESSAGE)
    bucket = request.scope["env"].BUCKET
    return await _serve_image(
        request, bucket, f"stylized/{date_path(date)}", False, f"No image for {date}"
    )


# ---------------------------------------------------------------------------
# Fallbacks
# ---------------------------------------------------------------------------


@app.api_route("/{full_path:path}", methods=["GET", "HEAD", "OPTIONS"], include_in_schema=False)
async def fallback(request: Request, full_path: str) -> Response:
    if request.method == "OPTIONS":
        return Response(status_code=204, headers=cors_headers())
    _check_format(request)
    return _not_found(NOT_FOUND_MESSAGE)
