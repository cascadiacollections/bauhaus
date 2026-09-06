"""Differential conformance check between the two Bauhaus API implementations.

The TypeScript worker in ``worker/`` and the FastAPI port in ``worker-py/`` are
each tested against their own expectations. Nothing compares them, so the only
evidence that the port is a drop-in replacement is that two suites, written from
the same reading of the same spec, both pass. This runs one request matrix
against both deployed Workers and diffs what actually comes back.

Both Workers read the same R2 bucket, so a matching response is a strong signal:
the ETag, the stored ``Cache-Control`` and the body all originate from the same
object, and any difference is the serving layer's doing.

Usage::

    uv run python tools/parity.py \\
        --reference https://bauhaus.cascadiacollections.workers.dev \\
        --candidate https://bauhaus-py.cascadiacollections.workers.dev

Exits non-zero when any case differs. ``--out`` writes the full report as JSON.

Deliberately excluded: requests that would write. ``POST /api/vitals`` and
``POST /api/err`` are exercised only on their rejection paths (bad or absent
Origin), which return before ``writeDataPoint`` and so leave no rows in
Analytics Engine.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import httpx

#: Headers compared between the two implementations.
#:
#: Everything else the edge adds (``date``, ``cf-ray``, ``server``,
#: ``content-encoding``, ``content-length``) is a property of the connection
#: rather than of the handler, and differs run to run on a single Worker.
#: ``content-length`` in particular tracks the transfer encoding, so the body
#: itself is compared instead.
COMPARED_HEADERS: tuple[str, ...] = (
    "content-type",
    "cache-control",
    "vary",
    "etag",
    "x-variant",
    "accept-ch",
    "access-control-allow-origin",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-max-age",
)

#: Content types whose bodies are shown verbatim in a diff. Anything else is
#: compared by digest, so an image mismatch reports a hash rather than a few
#: hundred KB of binary.
_TEXT_TYPES = ("application/json", "text/")

_WHITESPACE_RE = re.compile(r"\s+")

#: Origin used for the telemetry CORS cases. It must NOT be in the Worker's
#: ALLOWED_ORIGINS, so both implementations take the reject path.
_DISALLOWED_ORIGIN = "https://parity-check.invalid"


@dataclass(frozen=True)
class Case:
    """One request, sent identically to both implementations."""

    name: str
    path: str
    method: str = "GET"
    headers: Mapping[str, str] = field(default_factory=dict)
    #: Path to HEAD first, per host, whose ETag becomes this case's
    #: ``If-None-Match``. Resolved separately against each host so neither side
    #: is handed the other's validator.
    etag_from: str | None = None


@dataclass(frozen=True)
class Observed:
    """What one implementation answered."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.body).hexdigest()[:16]


def normalize_header(value: str) -> str:
    """Collapse insignificant whitespace so ``a, b`` and ``a,b`` compare equal."""
    return _WHITESPACE_RE.sub(" ", value.strip())


def normalize_etag(value: str) -> str:
    """Drop the ``W/`` prefix, leaving the opaque tag.

    The prefix is not the handler's. Neither implementation writes one: both
    return R2's ``httpEtag`` verbatim, from the same object, through the same
    code path for every content type. The edge adds it when it compresses a
    response, which is why it appears on HEAD of the JSON resources and on none
    of the image ones — a handler-level difference could not be selective by
    compressibility. It belongs with ``content-encoding`` and ``content-length``
    among the properties of the connection rather than of the handler.

    Only the prefix is dropped, so two implementations serving genuinely
    different validators still differ. That is also the comparison HTTP itself
    specifies for ``If-None-Match`` on GET and HEAD (RFC 9110 §8.8.3.2), so a
    client revalidating against either implementation gets the same 304.
    """
    tag = normalize_header(value)
    return tag[2:] if tag.startswith("W/") else tag


def normalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Lowercase the compared header names and normalize their values."""
    lowered = {k.lower(): v for k, v in headers.items()}
    return {
        name: normalize_etag(lowered[name]) if name == "etag" else normalize_header(lowered[name])
        for name in COMPARED_HEADERS
        if name in lowered
    }


def _is_text(headers: Mapping[str, str]) -> bool:
    content_type = normalize_headers(headers).get("content-type", "")
    return any(content_type.startswith(prefix) for prefix in _TEXT_TYPES)


def _body_repr(observed: Observed) -> str:
    if _is_text(observed.headers):
        text = observed.body.decode("utf-8", errors="replace")
        return text if len(text) <= 400 else text[:400] + "…"
    return f"<{len(observed.body)} bytes sha256:{observed.digest}>"


def compare(reference: Observed, candidate: Observed) -> list[str]:
    """Differences between two responses, as lines fit for a CI log.

    Empty means the two implementations are indistinguishable on this request.
    """
    diffs: list[str] = []

    if reference.status != candidate.status:
        diffs.append(f"status: reference {reference.status}, candidate {candidate.status}")

    ref_headers = normalize_headers(reference.headers)
    cand_headers = normalize_headers(candidate.headers)
    for name in COMPARED_HEADERS:
        ref_value = ref_headers.get(name)
        cand_value = cand_headers.get(name)
        if ref_value == cand_value:
            continue
        diffs.append(
            f"header {name}: reference {ref_value or '<absent>'!r}, candidate {cand_value or '<absent>'!r}"
        )

    if reference.body != candidate.body:
        diffs.append(f"body: reference {_body_repr(reference)}, candidate {_body_repr(candidate)}")

    return diffs


# ---------------------------------------------------------------------------
# The request matrix
# ---------------------------------------------------------------------------

_AVIF_ACCEPT = "image/avif,image/webp,image/*,*/*;q=0.8"
_WEBP_ACCEPT = "image/webp,image/*,*/*;q=0.8"


def build_cases(today: str, older: str, unpublished: str) -> list[Case]:
    """The full matrix.

    ``today`` is the date the reference implementation currently reports,
    ``older`` an earlier published date (or ``today`` again when the archive
    holds only one), and ``unpublished`` a well-formed date that was never
    published — the 404 path for a valid date.
    """
    cases: list[Case] = [
        # /api/today — negotiation, the query flags, and the conditional paths.
        Case("today/default", "/api/today"),
        Case("today/accept-avif", "/api/today", headers={"Accept": _AVIF_ACCEPT}),
        Case("today/accept-webp", "/api/today", headers={"Accept": _WEBP_ACCEPT}),
        Case("today/format-jpeg", "/api/today?format=jpeg"),
        Case("today/format-auto", "/api/today?format=auto", headers={"Accept": _AVIF_ACCEPT}),
        Case("today/format-invalid", "/api/today?format=png"),
        Case("today/progressive", "/api/today?progressive=true"),
        Case("today/strip", "/api/today?strip=true"),
        Case("today/head", "/api/today", method="HEAD"),
        Case("today/if-none-match", "/api/today", etag_from="/api/today"),
        Case(
            "today/if-none-match-stale",
            "/api/today",
            headers={"If-None-Match": '"parity-check-not-a-real-etag"'},
        ),
        # /api/today.json and the manifest.
        Case("today-json/default", "/api/today.json"),
        Case("today-json/head", "/api/today.json", method="HEAD"),
        Case("today-json/if-none-match", "/api/today.json", etag_from="/api/today.json"),
        Case("today-json/format-invalid", "/api/today.json?format=png"),
        Case("today-manifest/default", "/api/today.manifest.json"),
        Case("today-manifest/head", "/api/today.manifest.json", method="HEAD"),
        # Per-date resources. These are the immutable ones — a difference in
        # Cache-Control here is the difference between a client fetching an
        # archived image once and fetching it forever.
        Case("date/default", f"/api/{older}"),
        Case("date/accept-avif", f"/api/{older}", headers={"Accept": _AVIF_ACCEPT}),
        Case("date/accept-webp", f"/api/{older}", headers={"Accept": _WEBP_ACCEPT}),
        Case("date/progressive", f"/api/{older}?progressive=true"),
        Case("date/strip", f"/api/{older}?strip=true"),
        Case("date/head", f"/api/{older}", method="HEAD"),
        Case("date/if-none-match", f"/api/{older}", etag_from=f"/api/{older}"),
        Case("date/format-invalid", f"/api/{older}?format=jpg"),
        Case("date-original/default", f"/api/{older}/original"),
        Case("date-original/head", f"/api/{older}/original", method="HEAD"),
        Case("date-json/default", f"/api/{older}.json"),
        Case("date-json/head", f"/api/{older}.json", method="HEAD"),
        Case("date-json/if-none-match", f"/api/{older}.json", etag_from=f"/api/{older}.json"),
        # The detached signature. Whether it 200s or 404s depends on when the
        # date was published; either way both sides must agree.
        Case("date-sig/default", f"/api/{older}.json.sig"),
        Case("date-sig/head", f"/api/{older}.json.sig", method="HEAD"),
        Case("date-manifest/default", f"/api/{older}.manifest.json"),
        # A valid date that was never published — 404 rather than 503.
        Case("unpublished/image", f"/api/{unpublished}"),
        Case("unpublished/json", f"/api/{unpublished}.json"),
        Case("unpublished/manifest", f"/api/{unpublished}.manifest.json"),
        Case("unpublished/original", f"/api/{unpublished}/original"),
        # /api/archive — paging, and the query validation around it.
        Case("archive/default", "/api/archive"),
        Case("archive/head", "/api/archive", method="HEAD"),
        Case("archive/limit", "/api/archive?limit=3"),
        Case("archive/before", f"/api/archive?limit=3&before={today}"),
        Case("archive/limit-zero", "/api/archive?limit=0"),
        Case("archive/limit-non-numeric", "/api/archive?limit=abc"),
        Case("archive/limit-over-max", "/api/archive?limit=100000"),
        Case("archive/before-malformed", "/api/archive?before=not-a-date"),
        # /api/health. The body is derived from the published date and a day
        # count, so it is stable between two requests seconds apart.
        Case("health/default", "/api/health"),
        # Routing edges: a non-date segment is a 404, not a 422; OPTIONS is a
        # bare 204; anything but GET/HEAD is a 405.
        Case("fallback/unknown-path", "/api/not-a-route"),
        Case("fallback/root", "/"),
        Case("fallback/malformed-date", "/api/2026-13-45"),
        Case("fallback/partial-date", "/api/2026-09"),
        Case("fallback/date-trailing-slash", f"/api/{older}/"),
        Case("fallback/unknown-subpath", f"/api/{older}/thumbnail"),
        Case("options/today", "/api/today", method="OPTIONS"),
        Case("options/unknown-path", "/api/not-a-route", method="OPTIONS"),
        Case("method/post-today", "/api/today", method="POST"),
        Case("method/delete-date", f"/api/{older}", method="DELETE"),
        # Telemetry, rejection paths only — see the module docstring.
        Case(
            "vitals/options-disallowed-origin",
            "/api/vitals",
            method="OPTIONS",
            headers={"Origin": _DISALLOWED_ORIGIN},
        ),
        Case(
            "vitals/post-disallowed-origin",
            "/api/vitals",
            method="POST",
            headers={"Origin": _DISALLOWED_ORIGIN, "Content-Type": "application/json"},
        ),
        Case("vitals/post-no-origin", "/api/vitals", method="POST"),
        Case("vitals/get", "/api/vitals"),
        Case(
            "err/post-disallowed-origin",
            "/api/err",
            method="POST",
            headers={"Origin": _DISALLOWED_ORIGIN, "Content-Type": "application/json"},
        ),
        Case("err/get", "/api/err"),
    ]
    return cases


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def _observe(client: httpx.AsyncClient, base: str, case: Case) -> Observed:
    headers = dict(case.headers)

    if case.etag_from is not None:
        # Resolved per host: handing one implementation the other's ETag would
        # test agreement on a validator neither side generated.
        probe = await client.request("HEAD", base + case.etag_from, headers=dict(case.headers))
        etag = probe.headers.get("etag")
        if etag is None:
            raise RuntimeError(f"{base}{case.etag_from} returned no ETag to condition on")
        headers["If-None-Match"] = etag

    response = await client.request(case.method, base + case.path, headers=headers)
    return Observed(status=response.status_code, headers=dict(response.headers), body=response.content)


async def run_case(
    client: httpx.AsyncClient, reference: str, candidate: str, case: Case
) -> dict[str, object]:
    """Run one case against both hosts and return its report entry."""
    try:
        ref = await _observe(client, reference, case)
        cand = await _observe(client, candidate, case)
    except (httpx.HTTPError, RuntimeError) as exc:
        return {"case": case.name, "path": case.path, "method": case.method, "error": str(exc)}

    return {
        "case": case.name,
        "path": case.path,
        "method": case.method,
        "status": ref.status,
        "diffs": compare(ref, cand),
    }


def _is_today_case(case: Case) -> bool:
    """Whether a case resolves through ``latest.json``.

    A publish landing between the two requests changes the answer under the
    candidate but not the reference, which looks exactly like a real difference.
    Only these cases are affected, and only for the seconds around 04:00 UTC.
    """
    return case.path.startswith("/api/today")


async def run_all(
    reference: str,
    candidate: str,
    cases: Sequence[Case],
    timeout: float = 30.0,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, object]]:
    """Run the matrix against both hosts.

    ``transport`` exists so the tests can drive the whole loop — the ETag probe,
    the retry, the error path — against stub implementations without a network.
    """
    results: list[dict[str, object]] = []
    limits = httpx.Limits(max_connections=4)
    async with httpx.AsyncClient(
        timeout=timeout, limits=limits, follow_redirects=False, transport=transport
    ) as client:
        for case in cases:
            result = await run_case(client, reference, candidate, case)
            if (result.get("diffs") or result.get("error")) and _is_today_case(case):
                # Retried once before being reported, so a publish that landed
                # mid-run is not indistinguishable from a regression.
                result = await run_case(client, reference, candidate, case)
                result["retried"] = True
            results.append(result)
    return results


async def discover_dates(reference: str, timeout: float = 30.0) -> tuple[str, str]:
    """``(today, older)`` from the reference implementation's own archive index.

    Hardcoding dates would make the matrix rot: today moves, and an older date
    has to be one that was actually published.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.get(f"{reference}/api/archive?limit=2")
        response.raise_for_status()
        dates = response.json()["dates"]

    if not dates:
        raise RuntimeError(f"{reference}/api/archive lists no published dates")
    today = dates[0]
    # One-entry archives are real on a fresh bucket; fall back to the same date
    # rather than skipping every per-date case.
    older = dates[1] if len(dates) > 1 else dates[0]
    return today, older


def render(results: Sequence[Mapping[str, object]]) -> tuple[str, int]:
    """``(report, failures)`` — a plain-text summary and how many cases failed."""
    lines: list[str] = []
    failures = 0

    for result in results:
        name = result["case"]
        if result.get("error"):
            failures += 1
            lines.append(f"ERROR {name} ({result['method']} {result['path']}): {result['error']}")
            continue

        diffs = result.get("diffs") or []
        if diffs:
            failures += 1
            retried = " (retried)" if result.get("retried") else ""
            lines.append(f"DIFF  {name} ({result['method']} {result['path']}){retried}")
            lines.extend(f"        {line}" for line in diffs)
        else:
            lines.append(f"ok    {name} → {result['status']}")

    lines.append("")
    lines.append(f"{len(results) - failures}/{len(results)} cases identical")
    return "\n".join(lines), failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--reference",
        default="https://bauhaus.cascadiacollections.workers.dev",
        help="Implementation treated as correct (the TypeScript worker serving production)",
    )
    parser.add_argument(
        "--candidate",
        default="https://bauhaus-py.cascadiacollections.workers.dev",
        help="Implementation under test (the FastAPI port)",
    )
    parser.add_argument(
        "--unpublished",
        default="1970-01-01",
        help="A well-formed date that was never published, for the valid-date 404 path",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-request timeout in seconds")
    parser.add_argument("--out", help="Write the full report to this path as JSON")
    args = parser.parse_args(argv)

    reference = args.reference.rstrip("/")
    candidate = args.candidate.rstrip("/")

    try:
        today, older = asyncio.run(discover_dates(reference, args.timeout))
    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
        print(f"Could not discover published dates from {reference}: {exc}", file=sys.stderr)
        return 2

    cases = build_cases(today, older, args.unpublished)
    print(f"reference: {reference}")
    print(f"candidate: {candidate}")
    print(f"dates:     today={today} older={older} unpublished={args.unpublished}")
    print(f"cases:     {len(cases)}\n")

    results = asyncio.run(run_all(reference, candidate, cases, args.timeout))
    report, failures = render(results)
    print(report)

    if args.out:
        with open(args.out, "w") as handle:
            json.dump(
                {
                    "reference": reference,
                    "candidate": candidate,
                    "today": today,
                    "older": older,
                    "failures": failures,
                    "results": list(results),
                },
                handle,
                indent=2,
            )

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
