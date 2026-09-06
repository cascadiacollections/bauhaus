"""Unit tests for the differential parity runner.

The runner's value depends on it reporting a difference when there is one and
staying quiet when there is not, so the comparison itself is tested here without
touching the network. What cannot be tested offline — that the two deployed
Workers agree — is what the tool exists to answer.
"""

from __future__ import annotations

import httpx
import pytest
from parity import (
    COMPARED_HEADERS,
    Case,
    Observed,
    _is_today_case,
    build_cases,
    compare,
    normalize_headers,
    render,
    run_all,
)

JSON_HEADERS = {
    "Content-Type": "application/json",
    "Cache-Control": "public, max-age=300",
    "ETag": '"abc123"',
    "Access-Control-Allow-Origin": "*",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def observed(status: int = 200, headers: dict[str, str] | None = None, body: bytes = b"{}") -> Observed:
    return Observed(status=status, headers=dict(JSON_HEADERS if headers is None else headers), body=body)


class TestNormalizeHeaders:
    def test_keeps_only_compared_headers(self):
        normalized = normalize_headers({"ETag": '"a"', "CF-Ray": "x", "Date": "now"})
        assert normalized == {"etag": '"a"'}

    def test_header_names_are_case_insensitive(self):
        assert normalize_headers({"CACHE-CONTROL": "no-store"}) == {"cache-control": "no-store"}

    def test_collapses_insignificant_whitespace(self):
        assert normalize_headers({"Vary": " Accept,  Origin "}) == {"vary": "Accept, Origin"}

    def test_weak_etag_prefix_is_dropped(self):
        # The edge weakens an ETag when it compresses the response, so the same
        # R2 validator arrives with and without W/ depending on the transfer.
        assert normalize_headers({"ETag": 'W/"abc"'}) == normalize_headers({"ETag": '"abc"'})

    def test_a_different_etag_still_differs(self):
        assert normalize_headers({"ETag": 'W/"abc"'}) != normalize_headers({"ETag": 'W/"def"'})

    def test_content_length_is_not_compared(self):
        # It tracks the transfer encoding rather than the handler; the body is
        # compared instead.
        assert "content-length" not in COMPARED_HEADERS


class TestCompare:
    def test_identical_responses_have_no_diffs(self):
        assert compare(observed(), observed()) == []

    def test_status_difference_is_reported(self):
        diffs = compare(observed(status=200), observed(status=404))
        assert len(diffs) == 1
        assert "status: reference 200, candidate 404" in diffs[0]

    def test_weak_and_strong_forms_of_one_etag_are_not_a_difference(self):
        weak = observed(headers={**JSON_HEADERS, "ETag": 'W/"abc123"'})
        assert compare(weak, observed()) == []

    def test_header_value_difference_is_reported(self):
        cand = observed(headers={**JSON_HEADERS, "Cache-Control": "no-store"})
        assert any("header cache-control" in d for d in compare(observed(), cand))

    def test_missing_header_is_reported(self):
        stripped = {k: v for k, v in JSON_HEADERS.items() if k != "ETag"}
        diffs = compare(observed(), observed(headers=stripped))
        assert any("header etag" in d and "<absent>" in d for d in diffs)

    def test_body_difference_is_reported(self):
        diffs = compare(observed(body=b'{"a":1}'), observed(body=b'{"a":2}'))
        assert any(d.startswith("body:") for d in diffs)

    def test_json_bodies_are_shown_verbatim(self):
        diffs = compare(observed(body=b'{"date":"2026-09-04"}'), observed(body=b"{}"))
        assert '{"date":"2026-09-04"}' in diffs[0]

    def test_binary_bodies_are_shown_as_digests(self):
        headers = {"Content-Type": "image/jpeg"}
        diffs = compare(
            observed(headers=headers, body=b"\xff\xd8jpeg-a"),
            observed(headers=headers, body=b"\xff\xd8jpeg-b"),
        )
        assert "sha256:" in diffs[0]
        assert "\xff" not in diffs[0]

    def test_reports_every_difference_at_once(self):
        cand = observed(status=503, headers={"Content-Type": "text/plain"}, body=b"nope")
        # One line for the status, one per differing header, one for the body:
        # a single run should surface the whole picture rather than the first
        # thing that went wrong.
        assert len(compare(observed(), cand)) > 3


class TestCaseMatrix:
    @pytest.fixture
    def cases(self) -> list[Case]:
        return build_cases("2026-09-06", "2026-09-04", "1970-01-01")

    def test_case_names_are_unique(self, cases):
        names = [case.name for case in cases]
        assert len(names) == len(set(names))

    def test_every_path_is_absolute(self, cases):
        assert all(case.path.startswith("/") for case in cases)

    def test_no_case_writes_telemetry(self, cases):
        # A POST that reaches writeDataPoint would put parity-check rows into
        # Analytics Engine. Every telemetry case must be one the Worker rejects
        # before it writes, which means an Origin it does not allow, or none.
        for case in cases:
            if case.method == "POST":
                origin = case.headers.get("Origin", "")
                assert origin == "" or origin.endswith(".invalid"), case.name

    def test_covers_every_route_family(self, cases):
        paths = " ".join(case.path for case in cases)
        for route in (
            "/api/today",
            "/api/today.json",
            "/api/today.manifest.json",
            "/api/2026-09-04",
            "/api/2026-09-04/original",
            "/api/2026-09-04.json",
            "/api/2026-09-04.json.sig",
            "/api/2026-09-04.manifest.json",
            "/api/archive",
            "/api/health",
            "/api/vitals",
            "/api/err",
        ):
            assert route in paths, route

    def test_conditional_cases_resolve_their_own_etag(self, cases):
        conditional = [case for case in cases if case.etag_from]
        assert conditional
        assert all(case.etag_from == case.path.split("?")[0] for case in conditional)

    def test_uses_the_discovered_dates(self, cases):
        # Nothing may be hardcoded to a date the bucket happens to hold today.
        assert not any("2026-09-05" in case.path for case in cases)


class TestTodayCaseDetection:
    @pytest.mark.parametrize("path", ["/api/today", "/api/today.json", "/api/today.manifest.json"])
    def test_today_routes_are_retried(self, path):
        assert _is_today_case(Case("c", path)) is True

    @pytest.mark.parametrize("path", ["/api/2026-09-04", "/api/archive", "/api/health"])
    def test_fixed_routes_are_not(self, path):
        assert _is_today_case(Case("c", path)) is False


@pytest.mark.anyio
class TestRunAll:
    """The loop itself, driven against stub implementations over MockTransport.

    Everything here is what a network run would exercise and the pure tests
    above cannot: that both hosts get the same request, that a conditional case
    resolves its ETag per host, and that a transport failure is reported rather
    than raised.
    """

    REFERENCE = "https://ref.test"
    CANDIDATE = "https://cand.test"

    def transport(self, handler) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    async def run(self, handler, cases) -> list[dict]:
        return await run_all(
            self.REFERENCE, self.CANDIDATE, cases, transport=self.transport(handler)
        )

    async def test_identical_implementations_report_no_diffs(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers=JSON_HEADERS, content=b'{"date":"2026-09-04"}')

        results = await self.run(handler, [Case("json", "/api/2026-09-04.json")])
        assert results[0]["diffs"] == []
        assert results[0]["status"] == 200

    async def test_candidate_difference_is_caught(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "cand.test":
                # The regression this whole tool exists to catch: an immutable
                # resource served with today's short cache lifetime.
                headers = {**JSON_HEADERS, "Cache-Control": "public, max-age=300"}
                return httpx.Response(200, headers=headers, content=b"{}")
            headers = {**JSON_HEADERS, "Cache-Control": "public, max-age=31536000, immutable"}
            return httpx.Response(200, headers=headers, content=b"{}")

        results = await self.run(handler, [Case("json", "/api/2026-09-04.json")])
        assert any("cache-control" in diff for diff in results[0]["diffs"])

    async def test_both_hosts_receive_the_same_request(self):
        seen: list[tuple[str, str, str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append((request.url.host, request.method, str(request.url)))
            return httpx.Response(200, headers=JSON_HEADERS, content=b"{}")

        await self.run(handler, [Case("head", "/api/today.json?format=jpeg", method="HEAD")])
        hosts = {host for host, _, _ in seen}
        assert hosts == {"ref.test", "cand.test"}
        assert {method for _, method, _ in seen} == {"HEAD"}
        assert all(url.endswith("/api/today.json?format=jpeg") for _, _, url in seen)

    async def test_conditional_case_conditions_on_each_hosts_own_etag(self):
        sent: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            etag = f'"{request.url.host}-etag"'
            if request.method == "HEAD":
                return httpx.Response(200, headers={**JSON_HEADERS, "ETag": etag})
            sent[request.url.host] = request.headers["if-none-match"]
            return httpx.Response(304, headers={"ETag": etag})

        results = await self.run(
            handler, [Case("cond", "/api/today.json", etag_from="/api/today.json")]
        )
        assert sent == {"ref.test": '"ref.test-etag"', "cand.test": '"cand.test-etag"'}
        # Each side echoed its own validator, so the ETag values differing is
        # not itself the difference being reported — the 304 matched on both.
        assert results[0]["status"] == 304

    async def test_missing_etag_is_an_error_not_a_crash(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Type": "application/json"})

        results = await self.run(
            handler, [Case("cond", "/api/today.json", etag_from="/api/today.json")]
        )
        assert "no ETag" in results[0]["error"]

    async def test_transport_failure_is_reported(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectTimeout("timed out")

        results = await self.run(handler, [Case("json", "/api/today.json")])
        assert results[0]["error"] == "timed out"

    async def test_today_case_is_retried_once(self):
        attempts: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(request.url.host)
            # Differs on the first pass only, as a publish landing mid-run
            # would: the retry sees both hosts agree.
            body = b'{"date":"old"}' if len(attempts) <= 2 and request.url.host == "cand.test" else b"{}"
            return httpx.Response(200, headers=JSON_HEADERS, content=body)

        results = await self.run(handler, [Case("today", "/api/today.json")])
        assert results[0]["retried"] is True
        assert results[0]["diffs"] == []

    async def test_fixed_date_case_is_not_retried(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = b"a" if request.url.host == "cand.test" else b"b"
            return httpx.Response(200, headers=JSON_HEADERS, content=body)

        results = await self.run(handler, [Case("date", "/api/2026-09-04.json")])
        assert "retried" not in results[0]
        assert results[0]["diffs"]


class TestRender:
    def test_all_identical_reports_no_failures(self):
        results = [{"case": "a", "method": "GET", "path": "/api/today", "status": 200, "diffs": []}]
        report, failures = render(results)
        assert failures == 0
        assert "1/1 cases identical" in report

    def test_a_diff_is_a_failure(self):
        results = [
            {"case": "a", "method": "GET", "path": "/api/today", "status": 200, "diffs": ["status: …"]}
        ]
        report, failures = render(results)
        assert failures == 1
        assert "DIFF  a" in report

    def test_a_transport_error_is_a_failure(self):
        results = [{"case": "a", "method": "GET", "path": "/api/today", "error": "timed out"}]
        report, failures = render(results)
        assert failures == 1
        assert "ERROR a" in report
