"""Tests for upload.py — key generation, metadata enrichment, and S3 calls."""

import json
from datetime import date
from unittest.mock import MagicMock, patch

from upload import upload


class TestUpload:
    def _run_upload(self, today: date | None = None):
        today = today or date(2025, 7, 14)
        mock_client = MagicMock()

        with patch("upload._get_client", return_value=mock_client):
            keys = upload(
                original_bytes=b"original-data",
                stylized_bytes=b"stylized-data",
                metadata={"title": "Test Art", "artist": "Test Artist"},
                bucket="test-bucket",
                today=today,
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
