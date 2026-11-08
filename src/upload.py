"""Publish artwork and metadata as GitHub Release assets.

Storage layout (the Worker in worker/ reads exactly this — change both together):

One prerelease per published UTC date D, tagged ``art-D`` and named
``Bauhaus D``. Its assets are the files that used to live under date keys in
R2, under flat names (``original.jpg``, ``stylized.jpg``, ``stylized.avif``,
``metadata.json``, ``manifest.json``, ...), plus:

* ``dates.json``: every published date including D, newest first — a
  snapshot carried forward from the previous published day's copy.
* ``objects.json``: a map from each old R2 key to its asset name, sha256 etag,
  size, content type, cache control and upload time. Uploaded last.

The repository has GitHub *immutable releases* on: once a release is
published, its assets can't change and its tag can never be reused, even after
the release is deleted. So each day is assembled as a **draft** (drafts are
mutable and own no tag yet), and publishing the draft is the final write. A
leftover draft from a crashed run is not "published" and is simply redone.

Auth is GITHUB_TOKEN (or GH_TOKEN); the repository is BAUHAUS_REPO or, in
Actions, GITHUB_REPOSITORY.
"""

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any

import requests
from requests.utils import parse_header_links

DEFAULT_REPO = "cascadiacollections/bauhaus"
API_URL = "https://api.github.com"
OBJECTS_ASSET = "objects.json"
DATES_ASSET = "dates.json"
DAY_TAG_RE = re.compile(r"^art-(\d{4}-\d{2}-\d{2})$")

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

    def _paginate(self, path: str) -> list[dict]:
        """Every item of a list endpoint, following Link: rel="next" pagination."""
        items: list[dict] = []
        url: str | None = path
        params: dict | None = {"per_page": 100}
        while url:
            resp = self.request("GET", url, params=params)
            items.extend(resp.json())
            url = None
            params = None  # the next link already carries the query string
            for link in parse_header_links(resp.headers.get("Link", "")):
                if link.get("rel") == "next":
                    url = link["url"]
        return items

    def get_release(self, tag: str) -> dict | None:
        """The *published* release for ``tag``, or None.

        GitHub's by-tag endpoint never returns drafts, which is exactly the
        "already published" question. Any status but 200/404 raises.
        """
        resp = self.request("GET", f"/repos/{self.repo}/releases/tags/{tag}", ok=(200, 404))
        return resp.json() if resp.status_code == 200 else None

    def list_releases(self) -> list[dict]:
        """Every release, drafts included (they are only visible here)."""
        return self._paginate(f"/repos/{self.repo}/releases")

    def create_draft(self, tag: str, name: str, body: str) -> dict:
        """Create a draft prerelease that will never become "latest".

        A draft creates no tag, so this cannot collide with anything; the
        collision, if any, surfaces when the draft is published.
        """
        return self.request(
            "POST", f"/repos/{self.repo}/releases", ok=(201,),
            json_body={
                "tag_name": tag,
                "target_commitish": self.default_branch(),
                "name": name,
                "body": body,
                "draft": True,
                "prerelease": True,
                "make_latest": "false",
            },
        ).json()

    def publish_release(self, release: dict) -> dict:
        """Turn a draft into a published (and, here, immutable) release."""
        return self.request(
            "PATCH", f"/repos/{self.repo}/releases/{release['id']}",
            json_body={"draft": False, "prerelease": True, "make_latest": "false"},
        ).json()

    def delete_release(self, release: dict) -> None:
        """Delete a release. Never its tag: drafts have none, and an immutable
        release's tag is burned for good anyway."""
        self.request("DELETE", f"/repos/{self.repo}/releases/{release['id']}", ok=(204, 404))

    # -- assets ------------------------------------------------------------

    def list_assets(self, release: dict) -> list[dict]:
        return self._paginate(f"/repos/{self.repo}/releases/{release['id']}/assets")

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


def _already_published(today: date, overwrite: bool = False) -> AlreadyPublishedError:
    if overwrite:
        return AlreadyPublishedError(
            f"{today.isoformat()} is already published (release {day_tag(today)}) and "
            "cannot be overwritten: this repository uses GitHub immutable releases, so a "
            "published release's assets can't be replaced, and if the release were "
            f"deleted the tag {day_tag(today)} could never be used again. --overwrite "
            "only applies to dates that are not yet published."
        )
    return AlreadyPublishedError(
        f"{today.isoformat()} is already published (found release {day_tag(today)}). "
        "Published releases are immutable and served with immutable caching. "
        "--overwrite cannot change that either."
    )


def _assert_unpublished(client: GitHubReleases, today: date, overwrite: bool = False) -> None:
    """Refuse to publish a date that already has a published release.

    Published releases are immutable on GitHub, and every date's files are also
    served with `immutable` and a one-year TTL — a promise that the bytes never
    change. Publishing is therefore write-once, and with immutable releases
    there is no way to republish at all: deleting the release would burn its
    tag name permanently. ``overwrite`` only changes the error message.
    """
    if client.get_release(day_tag(today)) is not None:
        raise _already_published(today, overwrite)


def assert_unpublished(
    today: date | None = None, repo: str | None = None, overwrite: bool = False,
) -> None:
    """Preflight the write-once check before any expensive work happens.

    upload() checks again (and once more right before publishing). This
    wrapper exists so that the *common* rejection lands early: without it a
    collision costs a full fetch → style transfer → variants pipeline, half a
    minute of work, before anything looks at GitHub.
    """
    _assert_unpublished(_get_client(repo), today or utc_today(), overwrite)


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


def _published_day_releases(releases: list[dict]) -> dict[str, dict]:
    """Published (non-draft) art-YYYY-MM-DD releases, keyed by their date."""
    days: dict[str, dict] = {}
    for release in releases:
        match = DAY_TAG_RE.match(release.get("tag_name") or "")
        if match and not release.get("draft"):
            days[match.group(1)] = release
    return days


def _dates_snapshot(client: GitHubReleases, releases: list[dict], today: date) -> bytes:
    """dates.json for ``today``: the previous published day's list, plus today.

    The previous day is the newest published art-* release dated before
    ``today``. Dates of published releases seen in the listing are folded in
    too, which changes nothing normally and repairs the chain if a previous
    release ever lacks dates.json.
    """
    published = _published_day_releases(releases)
    earlier = sorted(d for d in published if d < today.isoformat())
    dates: set[str] = set(earlier)
    if earlier:
        previous = published[earlier[-1]]
        asset = client.find_asset(previous, DATES_ASSET)
        if asset is not None:
            dates.update(json.loads(client.download_asset(asset)).get("dates", []))
    dates.add(today.isoformat())
    return json.dumps({"dates": sorted(dates, reverse=True)}).encode()


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
    """Publish one day as an immutable GitHub release, assembled as a draft.

    Order: delete leftover drafts for the date → create a draft → upload every
    asset (dates.json, then objects.json last) → publish the draft. Publishing
    is the only write that makes anything visible, and it is the last one.

    Returns a dict mapping each published file to its legacy R2-style key (the
    keys objects.json is indexed by), plus ``dates``.

    Raises AlreadyPublishedError when ``today`` already has a published release.
    ``overwrite`` cannot get around that (see _assert_unpublished); it only
    changes the message.
    """
    today = today or utc_today()
    date_path = today.strftime("%Y/%m/%d")
    tag = day_tag(today)

    client = _get_client(repo)

    releases = client.list_releases()
    mine = [r for r in releases if r.get("tag_name") == tag]
    if any(not r.get("draft") for r in mine):
        raise _already_published(today, overwrite)
    for leftover in mine:
        # A draft is what a crashed run leaves behind. It was never visible,
        # so it is not "published" — throw it away and start over.
        print(f"Deleting leftover draft release {tag} (id {leftover['id']})")
        client.delete_release(leftover)

    prepared_metadata = prepare_metadata_for_upload(metadata, today=today)
    plan = _plan_day_assets(
        date_path, original_bytes, stylized_bytes, serialize_metadata(prepared_metadata),
        manifest, variants, stripped_bytes, metadata_sig,
    )
    dates_bytes = _dates_snapshot(client, releases, today)

    draft = client.create_draft(tag, f"Bauhaus {today.isoformat()}", _release_body(prepared_metadata))

    keys: dict[str, str] = {}
    objects: dict[str, dict] = {}
    try:
        for result_key, r2_key, asset_name, data, content_type in plan:
            client.upload_asset(draft, asset_name, data, content_type)
            objects[r2_key] = _object_entry(asset_name, data, content_type, IMMUTABLE_CACHE)
            keys[result_key] = r2_key
        # Not an R2 key, so not in objects.json.
        client.upload_asset(draft, DATES_ASSET, dates_bytes, "application/json")
        keys["dates"] = DATES_ASSET
        client.upload_asset(
            draft, OBJECTS_ASSET, json.dumps(objects, indent=2, sort_keys=True).encode(),
            "application/json",
        )
        # A concurrent generator may have published the date meanwhile.
        if client.get_release(tag) is not None:
            raise _already_published(today, overwrite)
    except BaseException:
        # Still a draft: invisible and mutable, so deleting it is free.
        try:
            client.delete_release(draft)
        except Exception as cleanup_exc:  # report the original failure, not this one
            print(f"⚠ Could not delete draft release {tag}: {cleanup_exc}")
        raise

    try:
        client.publish_release(draft)
    except GitHubAPIError:
        # The publish may have landed despite the error, or someone else's
        # release may own the tag now. Only ever delete our draft if it is
        # still a draft — deleting a published immutable release burns the tag.
        current = client.get_release(tag)
        if current is not None and current.get("id") == draft["id"]:
            return keys
        if current is not None:
            client.delete_release(draft)
            raise _already_published(today, overwrite) from None
        raise  # leave the draft; the next run deletes and redoes it

    return keys
