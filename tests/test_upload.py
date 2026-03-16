"""Tests for upload.py — key generation, metadata enrichment, and S3 calls."""

import json
from datetime import date
from unittest.mock import MagicMock, patch

from upload import upload


class TestUpload:
    def _run_upload(self, today: date | None = None, variants: dict[str, bytes] | None = None):
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
        variant_calls = {c.kwargs["Key"]: c.kwargs["ContentType"] for c in calls if ".avif" in c.kwargs.get("Key", "") or ".webp" in c.kwargs.get("Key", "")}
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
