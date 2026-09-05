"""End-to-end tests for the FastAPI app, driven through ASGI with a fake bucket."""

from __future__ import annotations

import json

import pytest
from conftest import make_client
from fakes import FakeBucket, FakeEnv, bucket_with_day
from starlette.exceptions import HTTPException as StarletteHTTPException

from bauhaus_api.logic import IMMUTABLE_CACHE, TODAY_CACHE

pytestmark = pytest.mark.anyio

DATE = "2026-09-04"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class TestToday:
    async def test_serves_jpeg_by_default(self, client):
        r = await client.get("/api/today")
        assert r.status_code == 200
        assert r.content == b"jpeg-bytes"
        assert r.headers["content-type"] == "image/jpeg"
        assert r.headers["cache-control"] == TODAY_CACHE
        assert r.headers["vary"] == "Accept"
        assert r.headers["x-variant"] == "baseline"
        assert r.headers["access-control-allow-origin"] == "*"

    async def test_negotiates_avif(self, client):
        r = await client.get("/api/today", headers={"Accept": "image/avif,image/webp"})
        assert r.content == b"avif-bytes"
        assert r.headers["content-type"] == "image/avif"

    async def test_explicit_format_beats_accept(self, client):
        r = await client.get("/api/today?format=webp", headers={"Accept": "image/avif"})
        assert r.content == b"webp-bytes"

    async def test_progressive_variant(self, client):
        r = await client.get("/api/today?progressive=true", headers={"Accept": "image/avif"})
        assert r.content == b"prog-bytes"
        assert r.headers["x-variant"] == "progressive"

    async def test_strip_variant(self, client):
        r = await client.get("/api/today?strip=true")
        assert r.content == b"strip-bytes"
        assert r.headers["x-variant"] == "stripped"

    async def test_falls_back_to_jpeg_when_variant_missing(self):
        bucket = bucket_with_day()
        del bucket.objects[f"stylized/{DATE.replace('-', '/')}.avif"]
        async with make_client(FakeEnv(BUCKET=bucket)) as client:
            r = await client.get("/api/today", headers={"Accept": "image/avif"})
        assert r.content == b"jpeg-bytes"
        assert r.headers["content-type"] == "image/jpeg"

    async def test_metadata_and_manifest(self, client):
        assert (await client.get("/api/today.json")).json() == {"date": DATE}
        assert (await client.get("/api/today.manifest.json")).json() == {"sizes": []}

    async def test_metadata_uses_today_cache_not_stored_value(self, client):
        # The stored object carries max-age=99, but /api/today.json resolves to a
        # new date every morning and must not be cached like an immutable one.
        r = await client.get("/api/today.json")
        assert r.headers["cache-control"] == TODAY_CACHE

    async def test_404_when_nothing_published(self, empty_env):
        async with make_client(empty_env) as client:
            r = await client.get("/api/today")
        assert r.status_code == 404
        assert r.json() == {"error": "No artwork published yet"}
        assert r.headers["access-control-allow-origin"] == "*"


class TestDatedResources:
    async def test_image(self, client):
        r = await client.get(f"/api/{DATE}")
        assert r.status_code == 200
        assert r.content == b"jpeg-bytes"
        # Dated resources are immutable; the object carries no stored value here.
        assert r.headers["cache-control"] == IMMUTABLE_CACHE

    async def test_original(self, client):
        r = await client.get(f"/api/{DATE}/original")
        assert r.content == b"original-bytes"

    async def test_metadata_prefers_stored_cache_control(self, client):
        r = await client.get(f"/api/{DATE}.json")
        assert r.json() == {"date": DATE}
        assert r.headers["cache-control"] == "public, max-age=99"

    async def test_manifest(self, client):
        assert (await client.get(f"/api/{DATE}.manifest.json")).status_code == 200

    async def test_signature_is_servable(self, client):
        r = await client.get(f"/api/{DATE}.json.sig")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pgp-signature"
        assert r.headers["cache-control"] == IMMUTABLE_CACHE

    async def test_missing_date_404s_with_specific_message(self, client):
        r = await client.get("/api/2020-01-01")
        assert r.status_code == 404
        assert r.json() == {"error": "No image for 2020-01-01"}

    @pytest.mark.parametrize("path", ["/api/2026-9-4", "/api/nonsense", "/api/2026-09-04x"])
    async def test_non_date_paths_fall_through_to_catch_all(self, client, path):
        r = await client.get(path)
        assert r.status_code == 404
        assert "Try /api/today" in r.json()["error"]


class TestConditionalRequests:
    async def test_matching_etag_returns_304(self, client, env):
        r = await client.get(f"/api/{DATE}", headers={"If-None-Match": '"jpg-etag"'})
        assert r.status_code == 304
        assert r.content == b""
        assert r.headers["cache-control"] == IMMUTABLE_CACHE
        assert r.headers["vary"] == "Accept"
        # The whole point of the head()-first path: no body was ever read.
        assert env.BUCKET.get_calls == []

    async def test_weak_etag_matches(self, client):
        r = await client.get(f"/api/{DATE}", headers={"If-None-Match": 'W/"jpg-etag"'})
        assert r.status_code == 304

    async def test_star_matches(self, client):
        assert (
            await client.get(f"/api/{DATE}.json", headers={"If-None-Match": "*"})
        ).status_code == 304

    async def test_stale_etag_returns_body(self, client):
        r = await client.get(f"/api/{DATE}", headers={"If-None-Match": '"old"'})
        assert r.status_code == 200
        assert r.content == b"jpeg-bytes"

    async def test_today_304_uses_today_cache(self, client):
        r = await client.get("/api/today", headers={"If-None-Match": '"jpg-etag"'})
        assert r.status_code == 304
        assert r.headers["cache-control"] == TODAY_CACHE

    async def test_missing_object_still_404s(self, client):
        r = await client.get("/api/2020-01-01", headers={"If-None-Match": '"x"'})
        assert r.status_code == 404


class TestHeadRequests:
    async def test_head_matches_get_headers_without_body(self, client, env):
        head = await client.head(f"/api/{DATE}")
        get = await client.get(f"/api/{DATE}")
        assert head.status_code == 200
        assert head.content == b""
        for key in ("content-type", "cache-control", "etag", "vary", "x-variant"):
            assert head.headers[key] == get.headers[key]

    async def test_head_does_not_read_the_body(self, env):
        async with make_client(env) as client:
            await client.head(f"/api/{DATE}")
        assert env.BUCKET.get_calls == []
        assert env.BUCKET.head_calls  # it did resolve the key

    async def test_head_json(self, client):
        r = await client.head(f"/api/{DATE}.json")
        assert r.status_code == 200
        assert r.content == b""
        assert r.headers["etag"] == '"meta-etag"'

    async def test_head_archive(self, client):
        r = await client.head("/api/archive")
        assert r.status_code == 200
        assert r.content == b""


class TestArchive:
    async def test_lists_published_dates_newest_first(self):
        bucket = FakeBucket()
        for day in ("01", "02", "03"):
            bucket.put(f"metadata/2026/09/{day}.json", b"{}")
        async with make_client(FakeEnv(BUCKET=bucket)) as client:
            body = (await client.get("/api/archive")).json()
        assert body["dates"] == ["2026-09-03", "2026-09-02", "2026-09-01"]
        assert body["total"] == 3

    async def test_signatures_are_not_counted_as_days(self):
        bucket = FakeBucket()
        bucket.put("metadata/2026/09/01.json", b"{}")
        bucket.put("metadata/2026/09/01.json.sig", b"sig")
        async with make_client(FakeEnv(BUCKET=bucket)) as client:
            body = (await client.get("/api/archive")).json()
        assert body["dates"] == ["2026-09-01"]

    async def test_paging(self):
        bucket = FakeBucket()
        for day in ("01", "02"):
            bucket.put(f"metadata/2026/09/{day}.json", b"{}")
        async with make_client(FakeEnv(BUCKET=bucket)) as client:
            body = (await client.get("/api/archive?limit=1")).json()
        assert body["count"] == 1
        assert body["next"] == "/api/archive?limit=1&before=2026-09-02"

    async def test_cache_control_tracks_today(self, client):
        r = await client.get("/api/archive")
        assert r.headers["cache-control"] == TODAY_CACHE

    @pytest.mark.parametrize("query", ["limit=abc", "limit=0", "limit=1001", "before=2026-9-4"])
    async def test_rejects_bad_query(self, client, query):
        r = await client.get(f"/api/archive?{query}")
        assert r.status_code == 400
        assert "error" in r.json()
        assert r.headers["access-control-allow-origin"] == "*"


class TestHealth:
    async def test_ok_when_fresh(self):
        from datetime import UTC, datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        async with make_client(FakeEnv(BUCKET=bucket_with_day(today))) as client:
            r = await client.get("/api/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.headers["cache-control"] == "no-store"

    async def test_stale_when_old(self):
        async with make_client(FakeEnv(BUCKET=bucket_with_day("2020-01-01"))) as client:
            r = await client.get("/api/health")
        assert r.status_code == 503
        assert r.json()["status"] == "stale"

    async def test_unhealthy_when_nothing_published(self, empty_env):
        async with make_client(empty_env) as client:
            r = await client.get("/api/health")
        assert r.status_code == 503
        assert r.json() == {"status": "unhealthy", "error": "no artwork published"}

    async def test_unhealthy_when_storage_fails(self, env):
        env.BUCKET.fail = True
        async with make_client(env) as client:
            r = await client.get("/api/health")
        assert r.status_code == 503
        assert r.json()["error"] == "storage unavailable"


class TestErrors:
    @pytest.mark.parametrize("path", ["/api/today", f"/api/{DATE}", "/api/nope"])
    async def test_invalid_format_is_400(self, client, path):
        r = await client.get(f"{path}?format=png")
        assert r.status_code == 400
        assert r.json()["error"] == "Unsupported format. Use avif, webp, jpeg, or auto"

    async def test_invalid_format_is_400_even_with_nothing_published(self, empty_env):
        async with make_client(empty_env) as client:
            r = await client.get("/api/today?format=png")
        assert r.status_code == 400

    async def test_method_not_allowed(self, client):
        r = await client.request("DELETE", "/api/today")
        assert r.status_code == 405
        assert r.json() == {"error": "Method not allowed"}
        assert r.headers["access-control-allow-origin"] == "*"

    async def test_framework_error_body_never_echoes_exception_detail(self, client):
        """A framework-raised HTTPException must not leak its detail to the client.

        Starlette builds exc.detail from arbitrary text, and an exception's own
        message can carry internal state such as a stack trace or an upstream
        path. The handler maps status codes to messages this module owns, so a
        detail set anywhere upstream can never reach a response body.
        """
        from bauhaus_api.app import _http_exception_handler

        leaked = 'Traceback (most recent call last): File "/secret/path.py"'
        response = await _http_exception_handler(
            None, StarletteHTTPException(status_code=404, detail=leaked)
        )

        assert leaked not in response.body.decode()
        assert json.loads(response.body) == {
            "error": "Not found. Try /api/today, /api/YYYY-MM-DD, or /api/archive"
        }

    async def test_options_preflight(self, client):
        r = await client.request("OPTIONS", "/api/today")
        assert r.status_code == 204
        assert r.headers["access-control-allow-methods"] == "GET, HEAD, OPTIONS"

    async def test_storage_failure_is_503_with_cors(self, env):
        env.BUCKET.fail = True
        async with make_client(env) as client:
            r = await client.get(f"/api/{DATE}")
        assert r.status_code == 503
        assert r.json() == {"error": "Upstream storage unavailable"}
        assert r.headers["access-control-allow-origin"] == "*"

    async def test_unknown_path_404(self, client):
        r = await client.get("/")
        assert r.status_code == 404
        assert "Try /api/today" in r.json()["error"]


class TestTelemetry:
    ORIGIN = "https://kevintcoughlin.com"

    async def test_vitals_writes_a_data_point(self, client, env):
        payload = {"name": "LCP", "rating": "good", "url": "https://x.com/p", "value": 1200}
        r = await client.post("/api/vitals", json=payload, headers={"Origin": self.ORIGIN})
        assert r.status_code == 204
        assert env.WEB_VITALS.points[0]["blobs"][0] == "LCP"
        assert r.headers["access-control-allow-origin"] == self.ORIGIN

    async def test_errors_write_a_data_point(self, client, env):
        r = await client.post(
            "/api/err", json={"message": "boom", "source": "https://x.com/a.js"},
            headers={"Origin": self.ORIGIN},
        )
        assert r.status_code == 204
        assert env.WEB_ERRORS.points[0]["blobs"][0] == "boom"

    async def test_disallowed_origin_is_403(self, client, env):
        r = await client.post("/api/vitals", json={}, headers={"Origin": "https://evil.com"})
        assert r.status_code == 403
        assert env.WEB_VITALS.points == []

    async def test_missing_origin_is_403(self, client):
        assert (await client.post("/api/vitals", json={})).status_code == 403

    async def test_preflight_from_allowed_origin(self, client):
        r = await client.request("OPTIONS", "/api/vitals", headers={"Origin": self.ORIGIN})
        assert r.status_code == 204
        assert r.headers["access-control-allow-headers"] == "content-type"

    async def test_preflight_from_other_origin_is_403(self, client):
        r = await client.request("OPTIONS", "/api/vitals", headers={"Origin": "https://evil.com"})
        assert r.status_code == 403

    async def test_get_is_405(self, client):
        assert (await client.get("/api/vitals")).status_code == 405

    async def test_oversized_body_is_413(self, client, env):
        big = json.dumps({"name": "x" * 5000})
        r = await client.post(
            "/api/vitals", content=big,
            headers={"Origin": self.ORIGIN, "Content-Type": "application/json"},
        )
        assert r.status_code == 413
        assert env.WEB_VITALS.points == []

    async def test_malformed_json_is_400(self, client):
        r = await client.post(
            "/api/vitals", content="{not json",
            headers={"Origin": self.ORIGIN, "Content-Type": "application/json"},
        )
        assert r.status_code == 400

    async def test_non_object_json_is_400(self, client):
        r = await client.post(
            "/api/vitals", content="[1,2,3]",
            headers={"Origin": self.ORIGIN, "Content-Type": "application/json"},
        )
        assert r.status_code == 400

    async def test_user_agent_is_classified(self, client, env):
        await client.post(
            "/api/vitals", json={"name": "CLS"},
            headers={"Origin": self.ORIGIN, "User-Agent": "iPhone"},
        )
        assert env.WEB_VITALS.points[0]["blobs"][-1] == "mobile"
