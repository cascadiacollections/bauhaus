"""Shared test helpers.

Importable because conftest.py puts this directory on sys.path.
"""

import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

API = "https://api.github.com"
UPLOADS = "https://uploads.github.com"
REPO = "cascadiacollections/bauhaus"


@dataclass
class FakeResponse:
    status_code: int
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        return json.loads(self.content)


def _json(status: int, body, headers: dict[str, str] | None = None) -> FakeResponse:
    return FakeResponse(status, json.dumps(body).encode(), headers or {})


class FakeGitHub:
    """An in-memory stand-in for the slice of the GitHub REST API upload.py uses.

    Models a repository with *immutable releases* enabled, as the real one is:

    * drafts own no tag, are invisible to GET /releases/tags/{tag}, and accept
      asset uploads and deletes;
    * a published release rejects asset uploads/deletes with 422;
    * a tag used by a published release can never be published again, even
      after that release is deleted ("burned").

    Plugged in as the requests session of a real GitHubReleases client, so the
    client's URL building, pagination, retries and status handling all run.
    ``log`` records every mutating call as (op, tag, asset name) tuples, in
    order. ``faults`` maps (method, kind) to statuses/responses to answer
    before behaving normally.
    """

    def __init__(self, repo: str = REPO, page_size: int = 100):
        self.repo = repo
        self.page_size = page_size
        self.releases: dict[int, dict] = {}
        self.assets: dict[int, dict] = {}  # id -> asset dict (+ "_release", "_data")
        self.tags: set[str] = set()
        self.burned: set[str] = set()
        self.log: list[tuple[str, str, str | None]] = []
        self.calls: list[tuple[str, str]] = []
        self.faults: dict[tuple[str, str], list[int | FakeResponse]] = {}
        self._next_id = 1

    # -- seeding -------------------------------------------------------------

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def add_release(self, tag: str, draft: bool = False, **extra) -> dict:
        rid = self._id()
        release = {
            "id": rid,
            "tag_name": tag,
            "name": extra.get("name", tag),
            "body": extra.get("body", ""),
            "draft": draft,
            "prerelease": extra.get("prerelease", True),
            "make_latest": extra.get("make_latest", "false"),
            "target_commitish": extra.get("target_commitish", "main"),
            "upload_url": f"{UPLOADS}/repos/{self.repo}/releases/{rid}/assets{{?name,label}}",
        }
        self.releases[rid] = release
        if not draft:
            self.tags.add(tag)
        return release

    def add_asset(self, release: dict, name: str, data: bytes, content_type: str = "application/json"):
        aid = self._id()
        self.assets[aid] = {
            "id": aid, "name": name, "size": len(data), "content_type": content_type,
            "_release": release["id"], "_data": data,
        }
        return self.assets[aid]

    def add_day(self, day: str, dates: list[str] | None, draft: bool = False) -> dict:
        """A day release, optionally carrying a dates.json snapshot."""
        release = self.add_release(f"art-{day}", draft=draft)
        if dates is not None:
            self.add_asset(release, "dates.json", json.dumps({"dates": dates}).encode())
        return release

    # -- inspection ----------------------------------------------------------

    def release_by_tag(self, tag: str, draft: bool | None = False) -> dict | None:
        return next((r for r in self.releases.values()
                     if r["tag_name"] == tag and (draft is None or r["draft"] == draft)), None)

    def files(self, tag: str) -> dict[str, bytes]:
        release = self.release_by_tag(tag)
        assert release is not None, f"no published release {tag}"
        return {a["name"]: a["_data"] for a in self.assets.values() if a["_release"] == release["id"]}

    def content_types(self, tag: str) -> dict[str, str]:
        release = self.release_by_tag(tag)
        assert release is not None
        return {a["name"]: a["content_type"] for a in self.assets.values()
                if a["_release"] == release["id"]}

    def _public(self, asset: dict) -> dict:
        return {k: v for k, v in asset.items() if not k.startswith("_")}

    # -- transport -----------------------------------------------------------

    def request(self, method, url, params=None, json=None, data=None, headers=None, timeout=None):
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        query.update({k: str(v) for k, v in (params or {}).items()})
        path = parts.path
        host = f"{parts.scheme}://{parts.netloc}"
        self.calls.append((method, url))
        kind = self._kind(method, host, path)
        pending = self.faults.get((method, kind))
        if pending:
            fault = pending.pop(0)
            if isinstance(fault, FakeResponse):
                return fault
            return FakeResponse(fault, b'{"message": "injected"}')
        return self._dispatch(method, host, path, query, json, data, headers or {})

    def _kind(self, method: str, host: str, path: str) -> str:
        if host == UPLOADS:
            return "upload"
        if path.endswith("/assets"):
            return "list_assets"
        if "/releases/assets/" in path:
            return "asset"
        if "/releases/tags/" in path:
            return "release_by_tag"
        if "/git/refs/tags/" in path:
            return "tag_ref"
        if path.endswith("/releases"):
            return "releases"
        if re.search(r"/releases/\d+$", path):
            return "release"
        return "repo"

    def _page(self, items: list, path: str, query: dict) -> FakeResponse:
        per_page = min(int(query.get("per_page", 30)), self.page_size)
        page = int(query.get("page", 1))
        chunk = items[(page - 1) * per_page: page * per_page]
        headers_out = {}
        if page * per_page < len(items):
            nxt = urlencode({"per_page": query.get("per_page", 30), "page": page + 1})
            headers_out["Link"] = f'<{API}{path}?{nxt}>; rel="next"'
        return _json(200, chunk, headers_out)

    def _dispatch(self, method, host, path, query, body, data, headers) -> FakeResponse:
        prefix = f"/repos/{self.repo}"
        assert path.startswith(prefix), path
        rest = path[len(prefix):]

        if host == UPLOADS:
            m = re.fullmatch(r"/releases/(\d+)/assets", rest)
            assert m and method == "POST"
            release = self.releases.get(int(m.group(1)))
            if release is None:
                return _json(404, {"message": "Not Found"})
            if not release["draft"]:
                return _json(422, {"message": "Cannot upload assets to an immutable release."})
            name = query["name"]
            if any(a["name"] == name and a["_release"] == release["id"] for a in self.assets.values()):
                return _json(422, {"errors": [{"code": "already_exists"}]})
            asset = self.add_asset(release, name, data, headers.get("Content-Type", ""))
            self.log.append(("upload", release["tag_name"], name))
            return _json(201, self._public(asset))

        if rest == "" and method == "GET":
            return _json(200, {"default_branch": "main"})

        if m := re.fullmatch(r"/releases/tags/(.+)", rest):
            release = self.release_by_tag(m.group(1), draft=False)
            return _json(200, release) if release else _json(404, {"message": "Not Found"})

        if rest == "/releases" and method == "GET":
            newest_first = sorted(self.releases.values(), key=lambda r: r["id"], reverse=True)
            return self._page(newest_first, path, query)

        if rest == "/releases" and method == "POST":
            if not body.get("draft"):
                raise AssertionError("upload.py must only ever create drafts")
            release = self.add_release(body["tag_name"], **body)
            self.log.append(("create_draft", body["tag_name"], None))
            return _json(201, release)

        if (m := re.fullmatch(r"/releases/(\d+)", rest)) and method == "PATCH":
            release = self.releases.get(int(m.group(1)))
            if release is None:
                return FakeResponse(404)
            if body.get("draft") is False and release["draft"]:
                tag = release["tag_name"]
                if tag in self.tags or tag in self.burned:
                    return _json(422, {"errors": [{"code": "already_exists", "field": "tag_name"}]})
                release.update(body)
                self.tags.add(tag)
                self.log.append(("publish", tag, None))
            return _json(200, release)

        if (m := re.fullmatch(r"/releases/(\d+)", rest)) and method == "DELETE":
            release = self.releases.pop(int(m.group(1)), None)
            if release is None:
                return FakeResponse(404)
            for aid in [a for a, v in self.assets.items() if v["_release"] == release["id"]]:
                del self.assets[aid]
            if not release["draft"]:
                self.burned.add(release["tag_name"])
            self.log.append(("delete_release", release["tag_name"], None))
            return FakeResponse(204)

        if (m := re.fullmatch(r"/git/refs/tags/(.+)", rest)) and method == "DELETE":
            raise AssertionError("upload.py must never delete tags")

        if (m := re.fullmatch(r"/releases/(\d+)/assets", rest)) and method == "GET":
            rid = int(m.group(1))
            items = [self._public(a) for a in self.assets.values() if a["_release"] == rid]
            return self._page(items, path, query)

        if m := re.fullmatch(r"/releases/assets/(\d+)", rest):
            asset = self.assets.get(int(m.group(1)))
            if asset is None:
                return FakeResponse(404)
            release = self.releases[asset["_release"]]
            if method == "GET":
                assert headers.get("Accept") == "application/octet-stream"
                return FakeResponse(200, asset["_data"])
            if method == "DELETE":
                if not release["draft"]:
                    return _json(422, {"message": "Cannot modify an immutable release."})
                del self.assets[asset["id"]]
                self.log.append(("delete_asset", release["tag_name"], asset["name"]))
                return FakeResponse(204)

        raise AssertionError(f"unhandled fake GitHub call: {method} {host}{path}")


def make_github_client(fake: FakeGitHub):
    """A real GitHubReleases client talking to ``fake``."""
    from upload import GitHubReleases

    return GitHubReleases(fake.repo, "test-token", session=fake)
