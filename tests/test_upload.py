"""Tests for upload.py — GitHub Releases layout, ordering, and the write-once guard.

Every test drives the real GitHubReleases client against FakeGitHub (an
in-memory GitHub REST API), so URL building, pagination, retries and status
handling are exercised, not mocked away.
"""

import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from helpers import FakeGitHub, FakeResponse, make_github_client

import upload as upload_mod
from upload import (
    IMMUTABLE_CACHE,
    LATEST_CACHE,
    AlreadyPublishedError,
    GitHubAPIError,
    assert_unpublished,
    prepare_metadata_for_upload,
    serialize_metadata,
    upload,
    utc_today,
)

DAY = date(2026, 1, 2)
TAG = "art-2026-01-02"

_VARIANTS = {
    "avif": b"avif-data",
    "webp": b"webp-data",
    "progressive.jpg": b"progressive-data",
    "stripped.jpg": b"variant-stripped",
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(upload_mod, "_sleep", lambda _s: None)


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


def _patched(fake: FakeGitHub):
    client = make_github_client(fake)
    return patch("upload._get_client", side_effect=lambda repo=None: client)


def run_upload(fake: FakeGitHub, today: date = DAY, **kwargs) -> dict[str, str]:
    kwargs.setdefault("metadata", {"title": "Test Art", "artist": "Test Artist"})
    original = kwargs.pop("original_bytes", b"original-data")
    stylized = kwargs.pop("stylized_bytes", b"stylized-data")
    with _patched(fake):
        return upload(original_bytes=original, stylized_bytes=stylized, today=today, **kwargs)


def full_upload(fake: FakeGitHub, today: date = DAY, **kwargs) -> dict[str, str]:
    return run_upload(
        fake, today,
        variants=dict(_VARIANTS),
        stripped_bytes=b"explicit-stripped",
        manifest={"date": today.isoformat(), "variants": []},
        metadata_sig=b"-----BEGIN PGP SIGNATURE-----\nfake\n",
        **kwargs,
    )


def objects_json(fake: FakeGitHub, tag: str = TAG) -> dict:
    return json.loads(fake.files(tag)["objects.json"])


class TestAssetMapping:
    def test_every_key_maps_to_its_flat_asset_name(self, fake):
        full_upload(fake)
        assert {k: v["asset"] for k, v in objects_json(fake).items()} == {
            "originals/2026/01/02.jpg": "original.jpg",
            "stylized/2026/01/02.jpg": "stylized.jpg",
            "stylized/2026/01/02.avif": "stylized.avif",
            "stylized/2026/01/02.webp": "stylized.webp",
            "stylized/2026/01/02.progressive.jpg": "stylized.progressive.jpg",
            "stylized/2026/01/02.stripped.jpg": "stylized.stripped.jpg",
            "metadata/2026/01/02.json": "metadata.json",
            "metadata/2026/01/02.json.sig": "metadata.json.sig",
            "manifests/2026/01/02.json": "manifest.json",
        }

    def test_release_assets_are_exactly_the_mapped_files_plus_objects(self, fake):
        full_upload(fake)
        names = set(fake.files(TAG))
        assert names == {v["asset"] for v in objects_json(fake).values()} | {"objects.json"}

    def test_returned_keys_are_the_legacy_keys(self, fake):
        keys = full_upload(fake)
        assert keys["original"] == "originals/2026/01/02.jpg"
        assert keys["stylized"] == "stylized/2026/01/02.jpg"
        assert keys["stylized_avif"] == "stylized/2026/01/02.avif"
        assert keys["stylized_progressive_jpg"] == "stylized/2026/01/02.progressive.jpg"
        assert keys["metadata"] == "metadata/2026/01/02.json"
        assert keys["metadata_sig"] == "metadata/2026/01/02.json.sig"
        assert keys["manifest"] == "manifests/2026/01/02.json"
        assert keys["stripped"] == "stylized/2026/01/02.stripped.jpg"
        assert keys["latest"] == "latest.json"

    def test_date_path_formatting(self, fake):
        keys = run_upload(fake, date(2025, 12, 25))
        assert keys["original"] == "originals/2025/12/25.jpg"
        assert "metadata/2025/12/25.json" in objects_json(fake, "art-2025-12-25")

    def test_minimal_upload(self, fake):
        keys = run_upload(fake)
        assert set(fake.files(TAG)) == {"original.jpg", "stylized.jpg", "metadata.json", "objects.json"}
        assert "manifest" not in keys and "metadata_sig" not in keys and "stripped" not in keys

    def test_content_types(self, fake):
        full_upload(fake)
        assert fake.content_types(TAG) == {
            "original.jpg": "image/jpeg",
            "stylized.jpg": "image/jpeg",
            "stylized.avif": "image/avif",
            "stylized.webp": "image/webp",
            "stylized.progressive.jpg": "image/jpeg",
            "stylized.stripped.jpg": "image/jpeg",
            "metadata.json": "application/json",
            "metadata.json.sig": "application/pgp-signature",
            "manifest.json": "application/json",
            "objects.json": "application/json",
        }

    def test_release_shape(self, fake):
        run_upload(fake)
        release = fake.release_by_tag(TAG)
        assert release is not None
        assert release["name"] == "Bauhaus 2026-01-02"
        assert release["prerelease"] is True
        assert release["make_latest"] == "false"
        assert release["target_commitish"] == "main"
        assert release["body"] == "Test Art — Test Artist"


class TestBytes:
    def test_metadata_bytes_match_canonical_serialization(self, fake):
        prepared = prepare_metadata_for_upload(
            {"title": "Test Art", "artist": "Test Artist"},
            today=DAY, generated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        run_upload(fake, metadata=prepared, metadata_sig=b"fakesig")
        assert fake.files(TAG)["metadata.json"] == serialize_metadata(prepared)

    def test_metadata_includes_date_and_generated_at(self, fake):
        run_upload(fake)
        body = json.loads(fake.files(TAG)["metadata.json"])
        assert body["date"] == "2026-01-02"
        assert body["published_date"] == "2026-01-02"
        assert "generated_at" in body

    def test_signature_uploaded_verbatim(self, fake):
        sig = b"-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n"
        run_upload(fake, metadata_sig=sig)
        assert fake.files(TAG)["metadata.json.sig"] == sig

    def test_manifest_serialization(self, fake):
        manifest = {"date": "2026-01-02", "variants": [{"w": 1}]}
        run_upload(fake, manifest=manifest)
        assert fake.files(TAG)["manifest.json"] == json.dumps(manifest, indent=2).encode()

    def test_explicit_stripped_bytes_win_and_upload_once(self, fake):
        full_upload(fake)
        assert fake.files(TAG)["stylized.stripped.jpg"] == b"explicit-stripped"
        uploads = [name for op, _, name in fake.log if op == "upload"]
        assert len(uploads) == len(set(uploads))

    def test_variant_stripped_used_when_no_explicit_bytes(self, fake):
        run_upload(fake, variants=dict(_VARIANTS))
        assert fake.files(TAG)["stylized.stripped.jpg"] == b"variant-stripped"


class TestObjectsJson:
    def test_entries_describe_the_uploaded_bytes(self, fake):
        full_upload(fake)
        files = fake.files(TAG)
        types = fake.content_types(TAG)
        for key, entry in objects_json(fake).items():
            data = files[entry["asset"]]
            assert entry["etag"] == hashlib.sha256(data).hexdigest(), key
            assert re.fullmatch(r"[0-9a-f]{64}", entry["etag"])
            assert entry["size"] == len(data)
            assert entry["contentType"] == types[entry["asset"]]
            assert entry["cacheControl"] == IMMUTABLE_CACHE
            offset = datetime.fromisoformat(entry["uploaded"]).utcoffset()
            assert offset is not None and offset.total_seconds() == 0

    def test_objects_json_is_not_listed_in_itself(self, fake):
        full_upload(fake)
        assert all(e["asset"] != "objects.json" for e in objects_json(fake).values())

    def test_objects_json_uploaded_last_in_the_day_release(self, fake):
        full_upload(fake)
        day_uploads = [name for op, tag, name in fake.log if op == "upload" and tag == TAG]
        assert day_uploads[-1] == "objects.json"
        assert len(day_uploads) == len(fake.files(TAG))


class TestIndex:
    def test_creates_index_release_with_dates_and_latest(self, fake):
        run_upload(fake)
        index = fake.release_by_tag("art-index")
        assert index is not None
        assert index["name"] == "Bauhaus index"
        assert index["prerelease"] is True
        assert index["make_latest"] == "false"
        files = fake.files("art-index")
        assert json.loads(files["dates.json"]) == {"dates": ["2026-01-02"]}
        assert fake.content_types("art-index")["latest.json"] == "application/json"

    def test_latest_json_bytes_match_the_old_r2_pointer(self, fake):
        run_upload(fake)
        assert fake.files("art-index")["latest.json"] == json.dumps({"date": "2026-01-02"}).encode()
        assert fake.files("art-index")["latest.json"] == b'{"date": "2026-01-02"}'

    def test_dates_json_updated_sorted_descending_and_deduplicated(self, fake):
        index = fake.add_release("art-index", name="Bauhaus index")
        existing = {"dates": ["2026-01-01", "2025-12-31", "2026-01-03", "2026-01-01"]}
        fake.add_asset(index, "dates.json", json.dumps(existing).encode())
        fake.add_asset(index, "latest.json", b'{"date": "2026-01-03"}')
        run_upload(fake)
        files = fake.files("art-index")
        assert json.loads(files["dates.json"])["dates"] == [
            "2026-01-03", "2026-01-02", "2026-01-01", "2025-12-31",
        ]
        assert files["latest.json"] == b'{"date": "2026-01-02"}'

    def test_already_listed_date_is_not_duplicated(self, fake):
        index = fake.add_release("art-index")
        fake.add_asset(index, "dates.json", json.dumps({"dates": ["2026-01-02"]}).encode())
        run_upload(fake)
        assert json.loads(fake.files("art-index")["dates.json"]) == {"dates": ["2026-01-02"]}

    def test_index_assets_replaced_by_delete_then_upload(self, fake):
        index = fake.add_release("art-index")
        fake.add_asset(index, "dates.json", b'{"dates": ["2026-01-01"]}')
        fake.add_asset(index, "latest.json", b'{"date": "2026-01-01"}')
        run_upload(fake)
        index_ops = [(op, name) for op, tag, name in fake.log if tag == "art-index"]
        assert index_ops == [
            ("delete_asset", "dates.json"),
            ("upload", "dates.json"),
            ("delete_asset", "latest.json"),
            ("upload", "latest.json"),
        ]
        assert sorted(fake.files("art-index")) == ["dates.json", "latest.json"]

    def test_order_day_release_then_dates_then_latest(self, fake):
        full_upload(fake)
        sequence = list(fake.log)
        objects_at = sequence.index(("upload", TAG, "objects.json"))
        dates_at = sequence.index(("upload", "art-index", "dates.json"))
        latest_at = sequence.index(("upload", "art-index", "latest.json"))
        assert objects_at < dates_at < latest_at
        assert all(tag != "art-index" for _, tag, _ in sequence[:objects_at + 1])

    def test_index_assets_found_across_pages(self):
        """dates.json on a later page of the listing must still be read and replaced."""
        fake = FakeGitHub(page_size=2)
        index = fake.add_release("art-index")
        for i in range(3):
            fake.add_asset(index, f"junk-{i}.txt", b"x")
        fake.add_asset(index, "dates.json", b'{"dates": ["2025-06-01"]}')
        fake.add_asset(index, "latest.json", b'{"date": "2025-06-01"}')
        run_upload(fake)
        files = fake.files("art-index")
        assert json.loads(files["dates.json"])["dates"] == ["2026-01-02", "2025-06-01"]
        assert files["latest.json"] == b'{"date": "2026-01-02"}'
        assert len(fake.files("art-index")) == 5  # replaced, not duplicated
        list_calls = [u for m, u in fake.calls if m == "GET" and u.split("?")[0].endswith("/assets")]
        assert any("page=3" in u for u in list_calls)


class TestPagination:
    def test_list_assets_follows_next_links(self):
        fake = FakeGitHub(page_size=2)
        release = fake.add_release(TAG)
        for i in range(5):
            fake.add_asset(release, f"a{i}", b"x")
        client = make_github_client(fake)
        assert [a["name"] for a in client.list_assets(release)] == ["a0", "a1", "a2", "a3", "a4"]


class TestWriteOnceGuard:
    """upload() refuses to rewrite a date that has already been published.

    Dates are served with `immutable` and a one-year TTL. Rewriting them leaves
    caches holding bytes they will never revalidate.
    """

    def test_published_date_raises_and_writes_nothing(self, fake):
        fake.add_release(TAG)
        with pytest.raises(AlreadyPublishedError, match="2026-01-02"):
            run_upload(fake)
        assert fake.log == []

    def test_unpublished_date_uploads(self, fake):
        keys = run_upload(fake)
        assert keys["metadata"] == "metadata/2026/01/02.json"

    def test_another_days_release_is_no_obstacle(self, fake):
        fake.add_release("art-2026-01-01")
        run_upload(fake)
        assert fake.release_by_tag(TAG) is not None

    def test_overwrite_deletes_and_recreates_release_and_tag(self, fake):
        old = fake.add_release(TAG)
        fake.add_asset(old, "stylized.jpg", b"old-stylized")
        run_upload(fake, overwrite=True)
        assert fake.log[:3] == [
            ("delete_release", TAG, None),
            ("delete_tag", TAG, None),
            ("create_release", TAG, None),
        ]
        new = fake.release_by_tag(TAG)
        assert new is not None and new["id"] != old["id"]
        assert fake.files(TAG)["stylized.jpg"] == b"stylized-data"

    def test_overwrite_on_unpublished_date_just_publishes(self, fake):
        run_upload(fake, overwrite=True)
        assert ("delete_release", TAG, None) not in fake.log
        assert fake.release_by_tag(TAG) is not None

    def test_lost_creation_race_raises_already_published(self, fake):
        """A concurrent generator creates the release between check and create."""
        client = make_github_client(fake)
        real_get = client.get_release

        def get_then_race(tag):
            result = real_get(tag)
            if tag == TAG and result is None:
                fake.add_release(TAG)
            return result

        client.get_release = get_then_race
        with patch("upload._get_client", side_effect=lambda repo=None: client), \
             pytest.raises(AlreadyPublishedError):
            upload(b"o", b"s", {"title": "T"}, today=DAY)
        assert all(op != "upload" for op, _, _ in fake.log)

    def test_non_404_lookup_error_propagates(self, fake):
        """A 401 answers nothing; it must not read as "free to publish"."""
        fake.faults[("GET", "release_by_tag")] = [401]
        with pytest.raises(GitHubAPIError):
            run_upload(fake)
        assert fake.log == []

    def test_failed_asset_upload_rolls_back_the_release(self, fake):
        """A half-published day would otherwise be skipped by every retry run."""
        fake.faults[("POST", "upload")] = [400]
        with pytest.raises(GitHubAPIError):
            run_upload(fake)
        assert fake.release_by_tag(TAG) is None
        assert TAG not in fake.tags
        assert fake.release_by_tag("art-index") is None


class TestAssertUnpublishedPreflight:
    """The public wrapper main.py calls before spending 30s on an image."""

    def _preflight(self, fake):
        with _patched(fake):
            return assert_unpublished(today=DAY)

    def test_unpublished_date_passes(self, fake):
        assert self._preflight(fake) is None

    def test_published_date_raises(self, fake):
        fake.add_release(TAG)
        with pytest.raises(AlreadyPublishedError, match="2026-01-02"):
            self._preflight(fake)

    def test_checks_the_same_tag_the_upload_guard_checks(self, fake):
        self._preflight(fake)
        assert fake.calls == [("GET", f"https://api.github.com/repos/{fake.repo}/releases/tags/{TAG}")]

    def test_never_writes(self, fake):
        self._preflight(fake)
        assert fake.log == []

    def test_non_404_error_propagates(self, fake):
        fake.faults[("GET", "release_by_tag")] = [401]
        with pytest.raises(GitHubAPIError):
            self._preflight(fake)


class TestRetries:
    def test_transient_5xx_is_retried(self, fake):
        fake.faults[("POST", "upload")] = [502, 503]
        run_upload(fake)
        assert "original.jpg" in fake.files(TAG)

    def test_secondary_rate_limit_is_retried(self, fake):
        fake.faults[("GET", "release_by_tag")] = [FakeResponse(
            403, b'{"message": "You have exceeded a secondary rate limit."}', {"Retry-After": "2"},
        )]
        sleeps: list[float] = []
        with patch.object(upload_mod, "_sleep", sleeps.append):
            run_upload(fake)
        assert sleeps == [2.0]

    def test_plain_403_is_not_retried(self, fake):
        fake.faults[("GET", "release_by_tag")] = [403]
        with pytest.raises(GitHubAPIError):
            run_upload(fake)
        assert len(fake.calls) == 1

    def test_retries_are_bounded(self, fake):
        fake.faults[("GET", "release_by_tag")] = [500] * 10
        with pytest.raises(GitHubAPIError):
            run_upload(fake)
        assert len(fake.calls) == upload_mod._MAX_ATTEMPTS

    def test_upload_answered_already_exists_is_replaced(self, fake):
        """A retried POST whose first attempt landed gets 422; replace, don't fail."""
        client = make_github_client(fake)
        release = fake.add_release(TAG)
        fake.add_asset(release, "original.jpg", b"partial", "image/jpeg")
        client.upload_asset(release, "original.jpg", b"full", "image/jpeg")
        assert fake.files(TAG) == {"original.jpg": b"full"}


class TestClientConfig:
    def test_token_required(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        upload_mod._get_client.cache_clear()
        with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
            upload_mod._get_client()

    def test_repo_from_env_with_override(self, monkeypatch):
        monkeypatch.setenv("GH_TOKEN", "t")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("GITHUB_REPOSITORY", "someone/fork")
        monkeypatch.delenv("BAUHAUS_REPO", raising=False)
        upload_mod._get_client.cache_clear()
        assert upload_mod._get_client().repo == "someone/fork"
        monkeypatch.setenv("BAUHAUS_REPO", "other/repo")
        upload_mod._get_client.cache_clear()
        assert upload_mod._get_client().repo == "other/repo"
        assert upload_mod._get_client("explicit/repo").repo == "explicit/repo"
        upload_mod._get_client.cache_clear()


class TestCacheControlHeaders:
    def test_immutable_header_matches_the_worker_constant(self):
        """objects.json records this header for the Worker to serve.

        worker/src/index.ts declares the same string; if these diverge the
        served header depends on which source the Worker happens to read.
        """
        worker_src = (
            Path(__file__).resolve().parent.parent / "worker" / "src" / "index.ts"
        ).read_text(encoding="utf-8")
        match = re.search(r'const IMMUTABLE_CACHE = "([^"]+)"', worker_src)
        assert match, "IMMUTABLE_CACHE not found in worker/src/index.ts"
        assert match.group(1) == IMMUTABLE_CACHE

    def test_latest_pointer_is_short_lived(self):
        assert LATEST_CACHE == "public, max-age=300"


class TestUtcToday:
    """The publish date must not depend on the runner's timezone."""

    def test_uses_utc_not_local_time(self):
        """22:00 UTC is already the next day in UTC+... and still 'today' here."""
        with patch("upload.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 2, 22, 0, tzinfo=UTC)
            assert utc_today() == date(2026, 1, 2)
        mock_dt.now.assert_called_once_with(UTC)

    def test_pacific_evening_still_reports_the_utc_date(self):
        """8 PM PT on Jan 1 is 04:00 UTC on Jan 2 — the publish date is Jan 2.

        This is the self-hosted Mac Mini case: local date.today() there would
        say Jan 1 and overwrite the previous day's keys.
        """
        with patch("upload.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 1, 2, 4, 0, tzinfo=UTC)
            assert utc_today() == date(2026, 1, 2)

    def test_upload_defaults_to_utc_today(self, fake):
        with _patched(fake), patch("upload.utc_today", return_value=date(2026, 3, 4)):
            keys = upload(original_bytes=b"o", stylized_bytes=b"s", metadata={"title": "T"})
        assert keys["stylized"] == "stylized/2026/03/04.jpg"
        assert fake.release_by_tag("art-2026-03-04") is not None

    def test_prepare_metadata_defaults_to_utc_today(self):
        with patch("upload.utc_today", return_value=date(2026, 3, 4)):
            prepared = prepare_metadata_for_upload({"title": "T"})
        assert prepared["date"] == "2026-03-04"

    def test_prepare_metadata_sets_published_date(self):
        with patch("upload.utc_today", return_value=date(2026, 3, 4)):
            prepared = prepare_metadata_for_upload({"title": "T"})
        assert prepared["published_date"] == "2026-03-04"

    def test_published_date_is_independent_of_the_artwork_date(self):
        """A source's object date ("ca. 1750") stays in "date"; published_date is the day."""
        prepared = prepare_metadata_for_upload(
            {"title": "T", "date": "ca. 1750"}, today=date(2026, 10, 4)
        )
        assert prepared["date"] == "ca. 1750"
        assert prepared["published_date"] == "2026-10-04"

    def test_prepare_metadata_is_idempotent(self):
        """upload() re-prepares metadata that was already prepared (and signed)."""
        once = prepare_metadata_for_upload(
            {"title": "T", "date": "ca. 1750"},
            today=date(2026, 10, 4),
            generated_at=datetime(2026, 10, 4, tzinfo=UTC),
        )
        assert prepare_metadata_for_upload(once, today=date(2026, 10, 4)) == once
