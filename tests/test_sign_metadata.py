"""Tests for sign_metadata.py — GPG signing of metadata JSON."""

import json
import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from sign_metadata import sign_metadata


_SAMPLE_METADATA = {
    "title": "Sunset Over Mountains",
    "artist": "Jane Doe",
    "license": "CC0-1.0",
    "date": "2025-07-14",
}

_FAKE_SIG = b"-----BEGIN PGP SIGNATURE-----\nfakesig\n-----END PGP SIGNATURE-----\n"


class TestSignMetadataDict:
    """sign_metadata() called with a dict."""

    def test_returns_bytes_on_success(self, tmp_path):
        """Should return bytes when GPG exits 0."""
        def _fake_run(cmd, **kwargs):
            # Write a fake .sig file to the path GPG is told to use
            out_idx = cmd.index("--output") + 1
            sig_path = cmd[out_idx]
            import pathlib
            pathlib.Path(sig_path).write_bytes(_FAKE_SIG)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("sign_metadata.subprocess.run", side_effect=_fake_run):
            sig = sign_metadata(_SAMPLE_METADATA)

        assert sig == _FAKE_SIG

    def test_serialises_dict_with_sorted_keys(self):
        """Dicts should be serialised with sorted keys for reproducibility."""
        captured = {}

        def _fake_run(cmd, **kwargs):
            meta_path = cmd[-1]  # last arg is the metadata file
            import pathlib
            captured["json"] = pathlib.Path(meta_path).read_text(encoding="utf-8")
            # Write fake sig
            out_idx = cmd.index("--output") + 1
            pathlib.Path(cmd[out_idx]).write_bytes(_FAKE_SIG)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("sign_metadata.subprocess.run", side_effect=_fake_run):
            sign_metadata({"b": 2, "a": 1})

        parsed = json.loads(captured["json"])
        keys = list(parsed.keys())
        assert keys == sorted(keys)

    def test_includes_key_id_in_command(self):
        """When key_id is provided, --local-user should appear in the GPG command."""
        calls_made = []

        def _fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            out_idx = cmd.index("--output") + 1
            import pathlib
            pathlib.Path(cmd[out_idx]).write_bytes(_FAKE_SIG)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("sign_metadata.subprocess.run", side_effect=_fake_run):
            sign_metadata(_SAMPLE_METADATA, key_id="DEADBEEF")

        assert len(calls_made) == 1
        cmd = calls_made[0]
        assert "--local-user" in cmd
        assert "DEADBEEF" in cmd

    def test_includes_passphrase_in_command(self):
        """When passphrase is provided, --passphrase and --pinentry-mode loopback should be used."""
        calls_made = []

        def _fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            out_idx = cmd.index("--output") + 1
            import pathlib
            pathlib.Path(cmd[out_idx]).write_bytes(_FAKE_SIG)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("sign_metadata.subprocess.run", side_effect=_fake_run):
            sign_metadata(_SAMPLE_METADATA, passphrase="s3cr3t")

        cmd = calls_made[0]
        assert "--passphrase" in cmd
        assert "s3cr3t" in cmd
        assert "--pinentry-mode" in cmd
        assert "loopback" in cmd

    def test_returns_none_on_nonzero_exit(self):
        """Should return None and not raise when GPG exits non-zero."""
        def _fake_run(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 2
            result.stderr = b"error: no secret key"
            return result

        with patch("sign_metadata.subprocess.run", side_effect=_fake_run):
            sig = sign_metadata(_SAMPLE_METADATA)

        assert sig is None

    def test_returns_none_when_gpg_not_found(self):
        """Should return None and not raise when the gpg binary is missing."""
        with patch("sign_metadata.subprocess.run", side_effect=FileNotFoundError):
            sig = sign_metadata(_SAMPLE_METADATA)

        assert sig is None

    def test_returns_none_on_timeout(self):
        """Should return None and not raise on timeout."""
        with patch(
            "sign_metadata.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="gpg", timeout=30),
        ):
            sig = sign_metadata(_SAMPLE_METADATA)

        assert sig is None

    def test_returns_none_on_os_error(self):
        """Should return None and not raise on generic OS errors."""
        with patch("sign_metadata.subprocess.run", side_effect=OSError("permission denied")):
            sig = sign_metadata(_SAMPLE_METADATA)

        assert sig is None


class TestSignMetadataString:
    """sign_metadata() called with a pre-serialised JSON string."""

    def test_accepts_string_input(self):
        """Should accept a JSON string and pass it through without re-serialising."""
        captured = {}

        def _fake_run(cmd, **kwargs):
            meta_path = cmd[-1]
            import pathlib
            captured["json"] = pathlib.Path(meta_path).read_text(encoding="utf-8")
            out_idx = cmd.index("--output") + 1
            pathlib.Path(cmd[out_idx]).write_bytes(_FAKE_SIG)
            result = MagicMock()
            result.returncode = 0
            return result

        raw = '{"hello": "world"}'
        with patch("sign_metadata.subprocess.run", side_effect=_fake_run):
            sig = sign_metadata(raw)

        assert sig == _FAKE_SIG
        assert captured["json"] == raw

    def test_no_key_id_no_local_user_flag(self):
        """When key_id is None, --local-user should not appear in the command."""
        calls_made = []

        def _fake_run(cmd, **kwargs):
            calls_made.append(cmd)
            out_idx = cmd.index("--output") + 1
            import pathlib
            pathlib.Path(cmd[out_idx]).write_bytes(_FAKE_SIG)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch("sign_metadata.subprocess.run", side_effect=_fake_run):
            sign_metadata(_SAMPLE_METADATA, key_id=None)

        assert "--local-user" not in calls_made[0]
