"""Tests for upload.py — key generation, metadata enrichment, and S3 calls."""

import json
from datetime import date
from unittest.mock import MagicMock, patch

from upload import upload


class TestUpload:
    def _run_upload(self, today: date | None = None, stripped_bytes: bytes | None = None):
        today = today or date(2025, 7, 14)
        mock_client = MagicMock()

        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art", "artist": "Test Artist"},
                bucket="test-bucket",
                today=today,
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
        mock_client = MagicMock()
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
        variant_calls = {c.kwargs["Key"]: c.kwargs["ContentType"] for c in calls
                         if "stylized/" in c.kwargs.get("Key", "") and c.kwargs["Key"] != "stylized/2025/07/14.jpg"}
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
