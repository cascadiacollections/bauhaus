"""Tests for upload.py — key generation, metadata enrichment, and S3 calls."""

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError
from helpers import make_r2_client

from upload import (
    IMMUTABLE_CACHE,
    LATEST_CACHE,
    AlreadyPublishedError,
    prepare_metadata_for_upload,
    serialize_metadata,
    upload,
    utc_today,
)


class TestUpload:
    def _run_upload(
        self,
        today: date | None = None,
        manifest: dict | None = None,
        variants: dict[str, bytes] | None = None,
        stripped_bytes: bytes | None = None,
    ):
        today = today or date(2025, 7, 14)
        mock_client = make_r2_client()

        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art", "artist": "Test Artist"},
                manifest=manifest,
                bucket="test-bucket",
                today=today,
                variants=variants,
                stripped_bytes=stripped_bytes,
            )
        return keys, mock_client

    def test_date_path_formatting(self):
        keys, _ = self._run_upload(date(2025, 7, 4))
        assert keys["original"] == "originals/2025/07/04.jpg"
        assert keys["stylized"] == "stylized/2025/07/04.jpg"
        assert keys["metadata"] == "metadata/2025/07/04.json"

    def test_key_structure(self):
        keys, _ = self._run_upload(date(2025, 12, 25))
        assert keys["original"] == "originals/2025/12/25.jpg"
        assert keys["stylized"] == "stylized/2025/12/25.jpg"
        assert keys["metadata"] == "metadata/2025/12/25.json"
        assert keys["latest"] == "latest.json"

    def test_put_object_call_count(self):
        _, mock_client = self._run_upload()
        assert mock_client.put_object.call_count == 4

    def test_metadata_includes_date_and_generated_at(self):
        _, mock_client = self._run_upload(date(2025, 7, 14))
        # Find the metadata put_object call (3rd call, 0-indexed=2)
        calls = mock_client.put_object.call_args_list
        metadata_call = calls[2]
        body = json.loads(metadata_call.kwargs["Body"])
        assert body["date"] == "2025-07-14"
        assert "generated_at" in body

    def test_latest_json_content(self):
        _, mock_client = self._run_upload(date(2025, 7, 14))
        calls = mock_client.put_object.call_args_list
        latest_call = calls[3]
        body = json.loads(latest_call.kwargs["Body"])
        assert body == {"date": "2025-07-14"}

    # --- Variant upload tests ---

    def test_variant_keys_uploaded(self):
        variants = {"avif": b"avif-data", "webp": b"webp-data"}
        keys, _ = self._run_upload(date(2025, 7, 14), variants=variants)
        assert keys["stylized_avif"] == "stylized/2025/07/14.avif"
        assert keys["stylized_webp"] == "stylized/2025/07/14.webp"

    def test_variant_put_object_count(self):
        variants = {"avif": b"avif-data", "webp": b"webp-data"}
        _, mock_client = self._run_upload(variants=variants)
        # 4 base calls + 2 variant calls = 6
        assert mock_client.put_object.call_count == 6

    def test_variant_content_types(self):
        variants = {"avif": b"avif-data", "webp": b"webp-data"}
        _, mock_client = self._run_upload(variants=variants)
        calls = mock_client.put_object.call_args_list
        # Variants are uploaded after stylized JPEG (index 1) and before metadata (index 4+)
        variant_calls = {
            c.kwargs["Key"]: c.kwargs["ContentType"]
            for c in calls
            if ".avif" in c.kwargs.get("Key", "") or ".webp" in c.kwargs.get("Key", "")
        }
        assert variant_calls["stylized/2025/07/14.avif"] == "image/avif"
        assert variant_calls["stylized/2025/07/14.webp"] == "image/webp"

    def test_no_variants_no_extra_calls(self):
        _, mock_client = self._run_upload(variants=None)
        assert mock_client.put_object.call_count == 4

    def test_empty_variants_no_extra_calls(self):
        _, mock_client = self._run_upload(variants={})
        assert mock_client.put_object.call_count == 4

    def test_single_variant_webp_only(self):
        variants = {"webp": b"webp-data"}
        keys, mock_client = self._run_upload(variants=variants)
        assert "stylized_webp" in keys
        assert "stylized_avif" not in keys
        assert mock_client.put_object.call_count == 5

    def test_stripped_variant_not_uploaded_when_none(self):
        keys, mock_client = self._run_upload()
        assert "stripped" not in keys
        assert mock_client.put_object.call_count == 4

    def test_stripped_variant_uploaded_when_provided(self):
        keys, mock_client = self._run_upload(
            date(2025, 7, 14), stripped_bytes=b"stripped-data",
        )
        assert "stripped" in keys
        assert keys["stripped"] == "stylized/2025/07/14.stripped.jpg"
        assert mock_client.put_object.call_count == 5

    def test_stripped_variant_key_formatting(self):
        keys, _ = self._run_upload(
            date(2025, 12, 25), stripped_bytes=b"stripped-data",
        )
        assert keys["stripped"] == "stylized/2025/12/25.stripped.jpg"


class TestUploadVariants:
    """Tests for variant and manifest upload."""

    def _run_upload(self, variants=None, manifest=None, today=None):
        today = today or date(2025, 7, 14)
        mock_client = make_r2_client()
        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art", "artist": "Test Artist"},
                bucket="test-bucket",
                today=today,
                variants=variants,
                manifest=manifest,
            )
        return keys, mock_client

    def test_upload_with_variants(self):
        variants = {"avif": b"avif-data", "webp": b"webp-data"}
        keys, mock_client = self._run_upload(variants=variants)
        # 4 base (original, stylized, metadata, latest) + 2 variants = 6
        assert mock_client.put_object.call_count == 6
        assert "stylized_avif" in keys
        assert "stylized_webp" in keys

    def test_variant_keys_use_date_path(self):
        variants = {"avif": b"avif-data"}
        keys, _ = self._run_upload(variants=variants, today=date(2025, 12, 25))
        assert keys["stylized_avif"] == "stylized/2025/12/25.avif"

    def test_variant_content_types(self):
        variants = {"avif": b"a", "webp": b"w", "progressive.jpg": b"p", "stripped.jpg": b"s"}
        _, mock_client = self._run_upload(variants=variants)
        calls = mock_client.put_object.call_args_list
        variant_calls = {
            c.kwargs["Key"]: c.kwargs["ContentType"]
            for c in calls
            if "stylized/" in c.kwargs.get("Key", "")
            and c.kwargs["Key"] != "stylized/2025/07/14.jpg"
        }
        assert variant_calls["stylized/2025/07/14.avif"] == "image/avif"
        assert variant_calls["stylized/2025/07/14.webp"] == "image/webp"
        assert variant_calls["stylized/2025/07/14.progressive.jpg"] == "image/jpeg"
        assert variant_calls["stylized/2025/07/14.stripped.jpg"] == "image/jpeg"

    def test_upload_with_manifest(self):
        manifest = {"date": "2025-07-14", "variants": []}
        keys, mock_client = self._run_upload(manifest=manifest)
        # 4 base + 1 manifest = 5
        assert mock_client.put_object.call_count == 5
        assert keys["manifest"] == "manifests/2025/07/14.json"

    def test_upload_without_variants_unchanged(self):
        """Without variants/manifest, upload behaviour matches the original."""
        keys, mock_client = self._run_upload()
        assert mock_client.put_object.call_count == 4
        assert "stylized_avif" not in keys
        assert "manifest" not in keys


class TestUploadMetadataSig:
    """Tests for the metadata_sig parameter in upload()."""

    def _run_upload(self, metadata_sig=None, today=None):
        from datetime import date
        today = today or date(2025, 7, 14)
        mock_client = make_r2_client()
        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art"},
                bucket="test-bucket",
                today=today,
                metadata_sig=metadata_sig,
            )
        return keys, mock_client

    def test_no_sig_no_extra_call(self):
        """Without metadata_sig, call count stays at 4."""
        keys, mock_client = self._run_upload()
        assert mock_client.put_object.call_count == 4
        assert "metadata_sig" not in keys

    def test_sig_uploaded_when_provided(self):
        """Providing metadata_sig should add one extra put_object call."""
        sig_bytes = b"fakesig"
        keys, mock_client = self._run_upload(metadata_sig=sig_bytes)
        assert mock_client.put_object.call_count == 5
        assert keys["metadata_sig"] == "metadata/2025/07/14.json.sig"

    def test_sig_key_date_formatting(self):
        """Signature key should use the same date path as the metadata JSON."""
        from datetime import date
        sig_bytes = b"fakesig"
        keys, _ = self._run_upload(metadata_sig=sig_bytes, today=date(2025, 12, 25))
        assert keys["metadata_sig"] == "metadata/2025/12/25.json.sig"

    def test_sig_content_type(self):
        """Signature object should use application/pgp-signature content type."""
        sig_bytes = b"fakesig"
        _, mock_client = self._run_upload(metadata_sig=sig_bytes)
        calls = mock_client.put_object.call_args_list
        sig_call = next(
            c for c in calls if c.kwargs.get("Key", "").endswith(".json.sig")
        )
        assert sig_call.kwargs["ContentType"] == "application/pgp-signature"

    def test_sig_body_matches_input(self):
        """The bytes passed as metadata_sig should be uploaded verbatim."""
        sig_bytes = b"-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n"
        _, mock_client = self._run_upload(metadata_sig=sig_bytes)
        calls = mock_client.put_object.call_args_list
        sig_call = next(
            c for c in calls if c.kwargs.get("Key", "").endswith(".json.sig")
        )
        assert sig_call.kwargs["Body"] == sig_bytes

    def test_uploaded_metadata_bytes_match_canonical_serialization(self):
        """Uploaded metadata payload should exactly match canonical serializer output."""
        today = date(2025, 7, 14)
        metadata = {"title": "Test Art", "artist": "Test Artist"}
        prepared = prepare_metadata_for_upload(
            metadata,
            today=today,
            generated_at=datetime(2025, 7, 14, tzinfo=UTC),
        )
        expected_bytes = serialize_metadata(prepared)

        mock_client = make_r2_client()
        with patch("upload._get_client", return_value=mock_client):
            upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata=prepared,
                bucket="test-bucket",
                today=today,
                metadata_sig=b"fakesig",
            )

        calls = mock_client.put_object.call_args_list
        metadata_call = next(
            c for c in calls if c.kwargs.get("Key", "").endswith(".json")
            and c.kwargs.get("Key", "").startswith("metadata/")
        )
        assert metadata_call.kwargs["Body"] == expected_bytes


class TestStrippedVariantNotDuplicated:
    """generate_variants() always emits "stripped.jpg", and main.py builds
    stripped_bytes separately when --strip is on. Both default to on, so the
    default path used to PUT the same key twice."""

    def _upload(self, **kwargs):
        mock_client = make_r2_client()
        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                b"original", b"stylized", {"title": "t"},
                bucket="test", today=date(2026, 1, 2), **kwargs,
            )
        put_keys = [c.kwargs["Key"] for c in mock_client.put_object.call_args_list]
        bodies = {c.kwargs["Key"]: c.kwargs["Body"] for c in mock_client.put_object.call_args_list}
        return keys, put_keys, bodies

    _VARIANTS = {
        "avif": b"avif-data",
        "webp": b"webp-data",
        "progressive.jpg": b"progressive-data",
        "stripped.jpg": b"variant-stripped",
    }

    def test_no_key_written_twice(self):
        _, put_keys, _ = self._upload(
            variants=self._VARIANTS, stripped_bytes=b"explicit-stripped",
        )
        assert len(put_keys) == len(set(put_keys))

    def test_explicit_stripped_bytes_win(self):
        _, _, bodies = self._upload(
            variants=self._VARIANTS, stripped_bytes=b"explicit-stripped",
        )
        assert bodies["stylized/2026/01/02.stripped.jpg"] == b"explicit-stripped"

    def test_variant_stripped_used_when_no_explicit_bytes(self):
        _, put_keys, bodies = self._upload(variants=self._VARIANTS)
        assert "stylized/2026/01/02.stripped.jpg" in put_keys
        assert bodies["stylized/2026/01/02.stripped.jpg"] == b"variant-stripped"

    def test_other_variants_unaffected(self):
        _, put_keys, _ = self._upload(
            variants=self._VARIANTS, stripped_bytes=b"explicit-stripped",
        )
        for suffix in ("avif", "webp", "progressive.jpg"):
            assert f"stylized/2026/01/02.{suffix}" in put_keys


class TestWriteOnceGuard:
    """upload() refuses to rewrite a date that has already been published.

    Date keys are served with `immutable` and a one-year TTL. Rewriting them
    leaves caches holding bytes they will never revalidate, and because uploads
    land key-by-key a client can end up with one artwork's image beside another
    artwork's metadata, permanently.
    """

    def _upload(self, client, **kwargs):
        with patch("upload._get_client", return_value=client):
            return upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art"},
                bucket="test-bucket",
                today=date(2026, 1, 2),
                **kwargs,
            )

    def test_unpublished_date_uploads(self):
        client = make_r2_client(published=False)
        keys = self._upload(client)
        assert keys["metadata"] == "metadata/2026/01/02.json"

    def test_published_date_raises(self):
        client = make_r2_client(published=True)
        with pytest.raises(AlreadyPublishedError, match="2026-01-02"):
            self._upload(client)

    def test_published_date_writes_nothing(self):
        """The guard must run before the first PUT, not partway through."""
        client = make_r2_client(published=True)
        with pytest.raises(AlreadyPublishedError):
            self._upload(client)
        assert client.put_object.call_count == 0

    def test_overwrite_allows_republish(self):
        client = make_r2_client(published=True)
        keys = self._upload(client, overwrite=True)
        assert keys["metadata"] == "metadata/2026/01/02.json"

    def test_overwrite_skips_the_existence_check(self):
        client = make_r2_client(published=True)
        self._upload(client, overwrite=True)
        assert client.head_object.call_count == 0

    def test_guard_checks_the_metadata_key_for_the_run_date(self):
        client = make_r2_client(published=False)
        self._upload(client)
        assert client.head_object.call_args.kwargs["Key"] == "metadata/2026/01/02.json"
        assert client.head_object.call_args.kwargs["Bucket"] == "test-bucket"

    def test_non_404_client_error_propagates(self):
        """A 403 means we could not determine the answer — do not publish over it."""
        client = make_r2_client(published=False)
        client.head_object.side_effect = ClientError(
            {"Error": {"Code": "403", "Message": "Forbidden"},
             "ResponseMetadata": {"HTTPStatusCode": 403}},
            "HeadObject",
        )
        with pytest.raises(ClientError):
            self._upload(client)
        assert client.put_object.call_count == 0


class TestCacheControlHeaders:
    def test_date_keys_are_immutable_and_shared_cacheable(self):
        client = make_r2_client()
        with patch("upload._get_client", return_value=client):
            upload(
                original_bytes=b"o", stylized_bytes=b"s",
                metadata={"title": "T"}, bucket="b", today=date(2026, 1, 2),
                variants={"webp": b"w"}, stripped_bytes=b"x",
                manifest={"date": "2026-01-02"}, metadata_sig=b"sig",
            )
        for call in client.put_object.call_args_list:
            if call.kwargs["Key"] == "latest.json":
                continue
            assert call.kwargs["CacheControl"] == IMMUTABLE_CACHE

    def test_immutable_header_matches_the_worker_constant(self):
        """The Worker prefers R2's stored header, so a drift here is what ships.

        worker/src/index.ts declares the same string; if these diverge the
        Worker's constant becomes unreachable for pipeline-written objects and
        the served header is silently whatever upload.py wrote.
        """
        worker_src = (
            Path(__file__).resolve().parent.parent / "worker" / "src" / "index.ts"
        ).read_text(encoding="utf-8")
        match = re.search(r'const IMMUTABLE_CACHE = "([^"]+)"', worker_src)
        assert match, "IMMUTABLE_CACHE not found in worker/src/index.ts"
        assert match.group(1) == IMMUTABLE_CACHE

    def test_latest_pointer_is_short_lived(self):
        client = make_r2_client()
        with patch("upload._get_client", return_value=client):
            upload(
                original_bytes=b"o", stylized_bytes=b"s",
                metadata={"title": "T"}, bucket="b", today=date(2026, 1, 2),
            )
        latest = [c for c in client.put_object.call_args_list
                  if c.kwargs["Key"] == "latest.json"][0]
        assert latest.kwargs["CacheControl"] == LATEST_CACHE


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

    def test_upload_defaults_to_utc_today(self):
        client = make_r2_client()
        with patch("upload._get_client", return_value=client), \
             patch("upload.utc_today", return_value=date(2026, 3, 4)):
            keys = upload(
                original_bytes=b"o", stylized_bytes=b"s",
                metadata={"title": "T"}, bucket="b",
            )
        assert keys["stylized"] == "stylized/2026/03/04.jpg"

    def test_prepare_metadata_defaults_to_utc_today(self):
        with patch("upload.utc_today", return_value=date(2026, 3, 4)):
            prepared = prepare_metadata_for_upload({"title": "T"})
        assert prepared["date"] == "2026-03-04"
