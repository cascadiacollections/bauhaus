"""A fake R2 bucket and env, so the FastAPI app can be tested without workerd.

The app touches R2 through a small surface — ``get``, ``head``, ``list``, and on
the returned object ``httpEtag``, ``httpMetadata.cacheControl`` and
``blob()`` — so faking it is cheap and lets the routing, negotiation and
conditional-request behaviour be tested with an ordinary pytest run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class FakeHttpMetadata:
    cacheControl: str | None = None  # noqa: N815 — mirrors the R2 JS property name


class FakeBlob:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def bytes(self) -> bytes:
        return self._data

    async def text(self) -> str:
        return self._data.decode()


@dataclass
class FakeR2Object:
    key: str
    data: bytes
    httpEtag: str | None = None  # noqa: N815 — mirrors the R2 JS property name
    httpMetadata: FakeHttpMetadata | None = None  # noqa: N815

    async def blob(self) -> FakeBlob:
        return FakeBlob(self.data)


class FakeBucket:
    """In-memory stand-in for an R2 bucket binding."""

    def __init__(self, objects: dict[str, FakeR2Object] | None = None) -> None:
        self.objects = objects or {}
        #: Every key passed to get(), so tests can assert a 304 or HEAD never
        #: read an object body.
        self.get_calls: list[str] = []
        self.head_calls: list[str] = []
        #: Set to raise from get()/head(), to simulate an R2 outage.
        self.fail = False

    def put(
        self, key: str, data: bytes, etag: str | None = None, cache_control: str | None = None
    ) -> None:
        self.objects[key] = FakeR2Object(
            key=key,
            data=data,
            httpEtag=etag,
            httpMetadata=FakeHttpMetadata(cache_control) if cache_control else None,
        )

    async def get(self, key: str) -> FakeR2Object | None:
        self.get_calls.append(key)
        if self.fail:
            raise RuntimeError("R2 unavailable")
        return self.objects.get(key)

    async def head(self, key: str) -> FakeR2Object | None:
        self.head_calls.append(key)
        if self.fail:
            raise RuntimeError("R2 unavailable")
        return self.objects.get(key)

    async def list(self, **options: object) -> dict[str, object]:
        prefix = str(options.get("prefix", ""))
        cursor = options.get("cursor")
        keys = sorted(k for k in self.objects if k.startswith(prefix))
        start = keys.index(str(cursor)) if cursor in keys else 0
        page = keys[start : start + 1000]
        truncated = len(keys) > start + len(page)
        return {
            "objects": [self.objects[k] for k in page],
            "truncated": truncated,
            "cursor": keys[start + len(page)] if truncated else None,
        }


@dataclass
class FakeDataset:
    """Stand-in for an Analytics Engine binding."""

    points: list[object] = field(default_factory=list)

    def writeDataPoint(self, point: object) -> None:  # noqa: N802 — JS API name
        self.points.append(point)


@dataclass
class FakeEnv:
    BUCKET: FakeBucket
    WEB_VITALS: FakeDataset = field(default_factory=FakeDataset)
    WEB_ERRORS: FakeDataset = field(default_factory=FakeDataset)
    ALLOWED_ORIGINS: str = "https://kevintcoughlin.com,https://www.kevintcoughlin.com"


def bucket_with_day(date: str = "2026-09-04", *, latest: bool = True) -> FakeBucket:
    """A bucket holding one published day in every format the API serves."""
    path = date.replace("-", "/")
    bucket = FakeBucket()
    if latest:
        bucket.put("latest.json", json.dumps({"date": date}).encode())
    bucket.put(f"stylized/{path}.jpg", b"jpeg-bytes", etag='"jpg-etag"')
    bucket.put(f"stylized/{path}.webp", b"webp-bytes", etag='"webp-etag"')
    bucket.put(f"stylized/{path}.avif", b"avif-bytes", etag='"avif-etag"')
    bucket.put(f"stylized/{path}.progressive.jpg", b"prog-bytes", etag='"prog-etag"')
    bucket.put(f"stylized/{path}.stripped.jpg", b"strip-bytes", etag='"strip-etag"')
    bucket.put(f"originals/{path}.jpg", b"original-bytes", etag='"orig-etag"')
    bucket.put(
        f"metadata/{path}.json",
        json.dumps({"date": date}).encode(),
        etag='"meta-etag"',
        cache_control="public, max-age=99",
    )
    bucket.put(f"metadata/{path}.json.sig", b"-----BEGIN PGP SIGNATURE-----", etag='"sig-etag"')
    bucket.put(f"manifests/{path}.json", json.dumps({"sizes": []}).encode(), etag='"man-etag"')
    return bucket
