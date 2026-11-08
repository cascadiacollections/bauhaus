"""Publish artwork and metadata as GitHub Release assets.

Storage layout (the Worker in worker/ reads exactly this — change both together):

* One prerelease per published UTC date D, tagged ``art-D`` and named
  ``Bauhaus D``. Its assets are the files that used to live under date keys in
  R2, under flat names (``original.jpg``, ``stylized.jpg``, ``stylized.avif``,
  ``metadata.json``, ``manifest.json``, ...), plus ``objects.json``: a map from
  each old R2 key to its asset name, sha256 etag, size, content type, cache
  control and upload time. objects.json is uploaded last, so its presence marks
  the day's release as complete.
* One prerelease tagged ``art-index`` holding ``dates.json`` (every published
  date, newest first) and ``latest.json`` (the pointer R2 used to hold). Both
  are rewritten after the day release is complete, dates.json first.

GitHub has no "overwrite asset": replacing one means deleting it and uploading
again. Auth is GITHUB_TOKEN (or GH_TOKEN); the repository is BAUHAUS_REPO or,
in Actions, GITHUB_REPOSITORY.
"""

import hashlib
import json
import os
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any

import requests
from requests.utils import parse_header_links

DEFAULT_REPO = "cascadiacollections/bauhaus"
API_URL = "https://api.github.com"
INDEX_TAG = "art-index"
INDEX_NAME = "Bauhaus index"
OBJECTS_ASSET = "objects.json"
DATES_ASSET = "dates.json"
LATEST_ASSET = "latest.json"

# Content types for all supported image variant suffixes.
_VARIANT_CONTENT_TYPES: dict[str, str] = {
    "avif": "image/avif",
    "webp": "image/webp",
    "progressive.jpg": "image/jpeg",
    "stripped.jpg": "image/jpeg",
}

# Date-keyed objects are published once and never rewritten, so they are safe to
# cache indefinitely. This string must stay byte-identical to IMMUTABLE_CACHE in
# worker/src/index.ts. GitHub does not store a Cache-Control per asset, so the
# value travels in objects.json and the Worker serves it from there.
IMMUTABLE_CACHE = "public, max-age=31536000, s-maxage=31536000, immutable"

# latest.json is a pointer that changes daily — short cache, revalidate often.
LATEST_CACHE = "public, max-age=300"

_TIMEOUT = 60
_MAX_ATTEMPTS = 4
_MAX_BACKOFF = 30.0

# Indirection so tests can skip real sleeps between retries.
_sleep: Callable[[float], None] = time.sleep


class AlreadyPublishedError(RuntimeError):
    """Raised when a date already has a release and overwrite is off."""


class GitHubAPIError(RuntimeError):
    """A GitHub REST call failed with a status the caller did not expect."""

    def __init__(self, method: str, url: str, status: int, body: str):
        super().__init__(f"GitHub API {method} {url} failed: HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body


def utc_today() -> date:
    """Today's date in UTC.

    The publish date must never depend on the runner's local timezone. Hosted
    runners are UTC, but generate.yml can route to a self-hosted runner whose
    local clock is Pacific time: a PT-evening run (including 8 PM PT, the time
    the README advertises) would compute *yesterday's* date there, overwrite
    the previous day's release, and rewind latest.json.
    """
    return datetime.now(UTC).date()


def day_tag(today: date) -> str:
    return f"art-{today.isoformat()}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _is_retryable(resp: requests.Response) -> bool:
    """5xx, 429, and GitHub's secondary rate limit (a 403 that says so)."""
    if resp.status_code >= 500 or resp.status_code == 429:
        return True
    if resp.status_code == 403:
        if resp.headers.get("Retry-After") or resp.headers.get("x-ratelimit-remaining") == "0":
            return True
        return "rate limit" in resp.text.lower()
    return False


def _backoff(resp: requests.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), _MAX_BACKOFF)
            except ValueError:
                pass
    return min(2.0 ** attempt, _MAX_BACKOFF)


class GitHubReleases:
    """Just enough of the GitHub Releases REST API to publish one day."""

    def __init__(self, repo: str, token: str, session: Any = None, api_url: str = API_URL):
        self.repo = repo
        self.api_url = api_url.rstrip("/")
        self.session = session if session is not None else requests.Session()
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "bauhaus-publisher",
        }
        self._default_branch: str | None = None

    # -- transport ---------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        ok: tuple[int, ...] = (200,),
        params: dict | None = None,
        json_body: dict | None = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> requests.Response:
        """One API call with bounded retries on transient failures.

        Returns the response when its status is in ``ok``; raises
        GitHubAPIError otherwise.
        """
        if not url.startswith("http"):
            url = f"{self.api_url}{url}"
        merged = {**self._headers, **(headers or {})}
        resp: requests.Response | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                resp = self.session.request(
                    method, url, params=params, json=json_body, data=data,
                    headers=merged, timeout=_TIMEOUT,
                )
            except (requests.ConnectionError, requests.Timeout):
                if attempt == _MAX_ATTEMPTS - 1:
                    raise
                _sleep(_backoff(None, attempt))
                continue
            if resp.status_code in ok:
                return resp
            if _is_retryable(resp) and attempt < _MAX_ATTEMPTS - 1:
                _sleep(_backoff(resp, attempt))
                continue
            break
        if resp is None:  # unreachable: the final attempt either returns or raises
            raise RuntimeError("no response")
        raise GitHubAPIError(method, url, resp.status_code, resp.text)

    # -- repository / releases --------------------------------------------

    def default_branch(self) -> str:
        if self._default_branch is None:
            self._default_branch = self.request("GET", f"/repos/{self.repo}").json()["default_branch"]
        return self._default_branch

    def get_release(self, tag: str) -> dict | None:
        """The release for ``tag``, or None. Any status but 200/404 raises."""
        resp = self.request("GET", f"/repos/{self.repo}/releases/tags/{tag}", ok=(200, 404))
        return resp.json() if resp.status_code == 200 else None

    def create_release(self, tag: str, name: str, body: str) -> dict | None:
        """Create a prerelease that never becomes "latest".

        Returns None when a release for ``tag`` already exists (GitHub answers
        422 already_exists) — creation is atomic, so this is what loses a race.
        """
        resp = self.request(
            "POST", f"/repos/{self.repo}/releases", ok=(201, 422),
            json_body={
                "tag_name": tag,
                "target_commitish": self.default_branch(),
                "name": name,
                "body": body,
                "draft": False,
                "prerelease": True,
                "make_latest": "false",
            },
        )
        if resp.status_code == 201:
            return resp.json()
        if "already_exists" in resp.text:
            return None
        raise GitHubAPIError("POST", f"/repos/{self.repo}/releases", resp.status_code, resp.text)

    def delete_release(self, release: dict) -> None:
        """Delete a release *and* its tag, so a recreate points at a fresh commit."""
        self.request("DELETE", f"/repos/{self.repo}/releases/{release['id']}", ok=(204, 404))
        tag = release.get("tag_name")
        if tag:
            self.request("DELETE", f"/repos/{self.repo}/git/refs/tags/{tag}", ok=(204, 404, 422))

    # -- assets ------------------------------------------------------------

    def list_assets(self, release: dict) -> list[dict]:
        """Every asset on a release, following Link: rel="next" pagination."""
        assets: list[dict] = []
        url: str | None = f"/repos/{self.repo}/releases/{release['id']}/assets"
        params: dict | None = {"per_page": 100}
        while url:
            resp = self.request("GET", url, params=params)
            assets.extend(resp.json())
            url = None
            params = None  # the next link already carries the query string
            for link in parse_header_links(resp.headers.get("Link", "")):
                if link.get("rel") == "next":
                    url = link["url"]
        return assets

    def find_asset(self, release: dict, name: str) -> dict | None:
        return next((a for a in self.list_assets(release) if a["name"] == name), None)

    def download_asset(self, asset: dict) -> bytes:
        # The API redirects to object storage; requests drops Authorization on
        # the cross-host hop.
        resp = self.request(
            "GET", f"/repos/{self.repo}/releases/assets/{asset['id']}",
            headers={"Accept": "application/octet-stream"},
        )
        return resp.content

    def delete_asset(self, asset: dict) -> None:
        self.request("DELETE", f"/repos/{self.repo}/releases/assets/{asset['id']}", ok=(204, 404))

    def upload_asset(self, release: dict, name: str, data: bytes, content_type: str) -> dict:
        upload_url = release["upload_url"].split("{", 1)[0]

        def post(ok: tuple[int, ...]) -> requests.Response:
            return self.request(
                "POST", upload_url, ok=ok, params={"name": name}, data=data,
                headers={"Content-Type": content_type},
            )

        resp = post((201, 422))
        if resp.status_code == 201:
            return resp.json()
        # 422 already_exists: a retried POST whose first attempt landed after
        # all (or left a broken "starter" asset). Replace it once.
        stale = self.find_asset(release, name)
        if stale is None:
            raise GitHubAPIError("POST", upload_url, resp.status_code, resp.text)
        self.delete_asset(stale)
        return post((201,)).json()

    def replace_asset(self, release: dict, name: str, data: bytes, content_type: str) -> dict:
        """Delete any asset called ``name`` and upload the new bytes in its place."""
        existing = self.find_asset(release, name)
        if existing is not None:
            self.delete_asset(existing)
        return self.upload_asset(release, name, data, content_type)


def _repo() -> str:
    return os.environ.get("BAUHAUS_REPO") or os.environ.get("GITHUB_REPOSITORY") or DEFAULT_REPO


@lru_cache(maxsize=4)
def _get_client(repo: str | None = None) -> GitHubReleases:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("Publishing needs a GitHub token in GITHUB_TOKEN (or GH_TOKEN).")
    return GitHubReleases(repo or _repo(), token)


def prepare_metadata_for_upload(
    metadata: dict,
    today: date | None = None,
    generated_at: datetime | None = None,
) -> dict:
    """Return metadata augmented with stable upload-time fields."""
    today = today or utc_today()
    prepared = dict(metadata)
    prepared.setdefault("date", today.isoformat())
    # "date" is the artwork's own date when the source has one (a Met object date such
    # as "ca. 1750"), so clients can't use it to tell which day this was published.
    # published_date is always the UTC publish day, the same key the archive uses.
    prepared["published_date"] = today.isoformat()
    prepared.setdefault("generated_at", (generated_at or datetime.now(UTC)).isoformat())
    return prepared


def serialize_metadata(metadata: dict) -> bytes:
    """Serialize metadata with canonical ordering for signing + upload."""
    return json.dumps(metadata, indent=2, sort_keys=True).encode()


def _is_published(client: GitHubReleases, today: date) -> bool:
    """Whether the release for a date already exists."""
    return client.get_release(day_tag(today)) is not None


def _already_published(today: date) -> AlreadyPublishedError:
    return AlreadyPublishedError(
        f"{today.isoformat()} is already published (found release {day_tag(today)}). "
        "Published dates are served immutable, so rewriting them leaves stale and "
        "possibly mismatched copies in caches forever. Re-run with --overwrite if "
        "that is what you want."
    )


def _assert_unpublished(client: GitHubReleases, today: date) -> None:
    """Refuse to overwrite a date that has already been published.

    Every date-keyed file is served with `immutable` and a one-year TTL, which
    is a promise that the bytes behind that URL never change. Re-running a date
    breaks that promise in a way no cache ever recovers from: assets land
    one-by-one rather than atomically, so a client that fetched during the
    rewrite can hold the image from one artwork alongside the metadata of
    another — permanently, because `immutable` tells it never to revalidate.

    Publishing is therefore write-once by default. Pass overwrite=True
    (`--overwrite`, or the workflow_dispatch input) to republish a date
    deliberately, accepting that already-cached copies stay stale.
    """
    if _is_published(client, today):
        raise _already_published(today)


def assert_unpublished(today: date | None = None, repo: str | None = None) -> None:
    """Preflight the write-once check before any expensive work happens.

    upload() checks again, and its check is the authoritative one: creating the
    day's release is atomic, so a concurrent generator that slips in between
    the two loses there. This wrapper exists so that the *common* rejection
    lands early: without it a collision costs a full fetch → style transfer →
    variants pipeline, half a minute of work, before anything looks at GitHub.
    """
    _assert_unpublished(_get_client(repo), today or utc_today())


def _release_body(metadata: dict) -> str:
    title = metadata.get("title") or "Untitled"
    artist = metadata.get("artist")
    return f"{title} — {artist}" if artist else str(title)


def _plan_day_assets(
    date_path: str,
    original_bytes: bytes,
    stylized_bytes: bytes,
    metadata_bytes: bytes,
    manifest: dict | None,
    variants: dict[str, bytes] | None,
    stripped_bytes: bytes | None,
    metadata_sig: bytes | None,
) -> list[tuple[str, str, str, bytes, str]]:
    """(result key, old R2 key, asset name, bytes, content type), in upload order."""
    plan = [
        ("original", f"originals/{date_path}.jpg", "original.jpg", original_bytes, "image/jpeg"),
        ("stylized", f"stylized/{date_path}.jpg", "stylized.jpg", stylized_bytes, "image/jpeg"),
    ]

    # Image variants (AVIF, WebP, progressive, stripped).
    #
    # generate_variants() always emits a "stripped.jpg" entry, and main.py also
    # builds stripped_bytes independently when --strip is on (both default on).
    # An explicit stripped_bytes wins; the variant copy is skipped so the same
    # asset is never uploaded twice.
    if variants:
        skip = {"stripped.jpg"} if stripped_bytes is not None else set()
        for suffix, data in variants.items():
            if suffix in skip:
                continue
            plan.append((
                f"stylized_{suffix.replace('.', '_')}",
                f"stylized/{date_path}.{suffix}",
                f"stylized.{suffix}",
                data,
                _VARIANT_CONTENT_TYPES.get(suffix, f"image/{suffix.split('.')[-1]}"),
            ))

    plan.append(("metadata", f"metadata/{date_path}.json", "metadata.json", metadata_bytes,
                 "application/json"))
    if metadata_sig is not None:
        plan.append(("metadata_sig", f"metadata/{date_path}.json.sig", "metadata.json.sig",
                     metadata_sig, "application/pgp-signature"))
    if manifest is not None:
        plan.append(("manifest", f"manifests/{date_path}.json", "manifest.json",
                     json.dumps(manifest, indent=2).encode(), "application/json"))
    if stripped_bytes is not None:
        plan.append(("stripped", f"stylized/{date_path}.stripped.jpg", "stylized.stripped.jpg",
                     stripped_bytes, "image/jpeg"))
    return plan


def _object_entry(asset_name: str, data: bytes, content_type: str, cache_control: str) -> dict:
    return {
        "asset": asset_name,
        "etag": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "contentType": content_type,
        "cacheControl": cache_control,
        "uploaded": _now_iso(),
    }


def _get_or_create_index(client: GitHubReleases) -> dict:
    release = client.get_release(INDEX_TAG)
    if release is not None:
        return release
    created = client.create_release(INDEX_TAG, INDEX_NAME, "Index of published Bauhaus dates.")
    if created is not None:
        return created
    # Lost a creation race: someone else just made it.
    release = client.get_release(INDEX_TAG)
    if release is None:
        raise RuntimeError(f"Could not create or find the {INDEX_TAG} release")
    return release


def _merge_dates(existing: bytes | None, today: date) -> bytes:
    dates: set[str] = set()
    if existing:
        dates.update(json.loads(existing).get("dates", []))
    dates.add(today.isoformat())
    return json.dumps({"dates": sorted(dates, reverse=True)}).encode()


def _update_index(client: GitHubReleases, today: date) -> None:
    """Add ``today`` to dates.json, then point latest.json at it — in that order."""
    index = _get_or_create_index(client)
    assets = client.list_assets(index)
    by_name = {a["name"]: a for a in assets}

    current = by_name.get(DATES_ASSET)
    dates_bytes = _merge_dates(client.download_asset(current) if current else None, today)
    if current is not None:
        client.delete_asset(current)
    client.upload_asset(index, DATES_ASSET, dates_bytes, "application/json")

    latest = by_name.get(LATEST_ASSET)
    if latest is not None:
        client.delete_asset(latest)
    client.upload_asset(
        index, LATEST_ASSET, json.dumps({"date": today.isoformat()}).encode(), "application/json",
    )


def upload(
    original_bytes: bytes,
    stylized_bytes: bytes,
    metadata: dict,
    manifest: dict | None = None,
    repo: str | None = None,
    today: date | None = None,
    variants: dict[str, bytes] | None = None,
    stripped_bytes: bytes | None = None,
    metadata_sig: bytes | None = None,
    overwrite: bool = False,
) -> dict[str, str]:
    """Publish one day as a GitHub release and update the index release.

    Returns a dict mapping each published file to its legacy R2-style key (the
    keys objects.json is indexed by), plus ``latest``.

    Raises AlreadyPublishedError when ``today`` already has a release and
    ``overwrite`` is False — see _assert_unpublished().
    """
    today = today or utc_today()
    date_path = today.strftime("%Y/%m/%d")
    tag = day_tag(today)

    client = _get_client(repo)

    existing = client.get_release(tag)
    if existing is not None:
        if not overwrite:
            raise _already_published(today)
        client.delete_release(existing)

    prepared_metadata = prepare_metadata_for_upload(metadata, today=today)
    plan = _plan_day_assets(
        date_path, original_bytes, stylized_bytes, serialize_metadata(prepared_metadata),
        manifest, variants, stripped_bytes, metadata_sig,
    )

    # Creation is atomic: if a concurrent generator created the release since
    # the check above, this is where we find out — before uploading anything.
    release = client.create_release(tag, f"Bauhaus {today.isoformat()}", _release_body(prepared_metadata))
    if release is None:
        raise _already_published(today)

    keys: dict[str, str] = {}
    objects: dict[str, dict] = {}
    try:
        for result_key, r2_key, asset_name, data, content_type in plan:
            client.upload_asset(release, asset_name, data, content_type)
            objects[r2_key] = _object_entry(asset_name, data, content_type, IMMUTABLE_CACHE)
            keys[result_key] = r2_key
        # Last: objects.json's presence is what marks the day as complete.
        client.upload_asset(
            release, OBJECTS_ASSET, json.dumps(objects, indent=2, sort_keys=True).encode(),
            "application/json",
        )
    except BaseException:
        # A half-uploaded release would otherwise read as "published" forever
        # and the retry run would skip it. Roll back so the date stays open.
        try:
            client.delete_release(release)
        except Exception as cleanup_exc:  # report the original failure, not this one
            print(f"⚠ Could not roll back partial release {tag}: {cleanup_exc}")
        raise

    _update_index(client, today)
    keys["latest"] = LATEST_ASSET
    return keys
