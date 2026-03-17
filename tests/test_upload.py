"""Tests for upload.py — key generation, metadata enrichment, and S3 calls."""

import json
from datetime import date
from unittest.mock import MagicMock, patch

from upload import upload


class TestUpload:
    def _run_upload(self, today: date | None = None, manifest: dict | None = None, stripped_bytes: bytes | None = None):
        today = today or date(2025, 7, 14)
        mock_client = MagicMock()

        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art", "artist": "Test Artist"},
                manifest=manifest,
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


class TestUploadProgressive:
    """Tests for progressive JPEG variant uploads."""

    def _run_upload_with_progressive(self, today: date | None = None):
        today = today or date(2025, 7, 14)
        mock_client = MagicMock()

        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art", "artist": "Test Artist"},
                bucket="test-bucket",
                today=today,
                original_progressive_bytes=b"original-progressive-data",
                stylized_progressive_bytes=b"stylized-progressive-data",
            )
        return keys, mock_client

    def test_progressive_keys_present(self):
        keys, _ = self._run_upload_with_progressive()
        assert "original_progressive" in keys
        assert "stylized_progressive" in keys

    def test_progressive_key_paths(self):
        keys, _ = self._run_upload_with_progressive(date(2025, 7, 14))
        assert keys["original_progressive"] == "originals/2025/07/14.progressive.jpg"
        assert keys["stylized_progressive"] == "stylized/2025/07/14.progressive.jpg"

    def test_progressive_put_object_call_count(self):
        """With progressive bytes, upload should make 6 put_object calls."""
        _, mock_client = self._run_upload_with_progressive()
        assert mock_client.put_object.call_count == 6

    def test_no_progressive_when_not_provided(self):
        """Without progressive bytes, upload should behave as before (4 calls)."""
        today = date(2025, 7, 14)
        mock_client = MagicMock()
        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art", "artist": "Test Artist"},
                bucket="test-bucket",
                today=today,
            )
        assert mock_client.put_object.call_count == 4
        assert "original_progressive" not in keys
        assert "stylized_progressive" not in keys


class TestUploadWithManifest:
    def _run_upload_with_manifest(self, today: date | None = None):
        today = today or date(2025, 7, 14)
        manifest = {
            "date": today.isoformat(),
            "variants": [
                {"width": 1920, "height": 1080, "format": "jpeg",
                 "url": f"/api/{today.isoformat()}", "size_bytes": 12345},
            ],
            "aspect_ratio": "16:9",
            "license": "CC0-1.0",
            "license_url": "",
            "source": "met",
            "source_url": "",
        }
        mock_client = MagicMock()

        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art", "artist": "Test Artist"},
                manifest=manifest,
                bucket="test-bucket",
                today=today,
            )
        return keys, mock_client

    def test_manifest_key_structure(self):
        keys, _ = self._run_upload_with_manifest(date(2025, 7, 14))
        assert keys["manifest"] == "manifests/2025/07/14.json"

    def test_manifest_put_object_call_count(self):
        _, mock_client = self._run_upload_with_manifest()
        # original + stylized + metadata + manifest + latest = 5
        assert mock_client.put_object.call_count == 5

    def test_manifest_content_uploaded(self):
        _, mock_client = self._run_upload_with_manifest(date(2025, 7, 14))
        calls = mock_client.put_object.call_args_list
        # Manifest is the 4th call (0-indexed=3), before latest
        manifest_call = calls[3]
        body = json.loads(manifest_call.kwargs["Body"])
        assert body["date"] == "2025-07-14"
        assert "variants" in body
        assert "aspect_ratio" in body
        assert manifest_call.kwargs["ContentType"] == "application/json"

    def test_no_manifest_key_when_none(self):
        keys, _ = TestUpload()._run_upload()
        assert "manifest" not in keys
