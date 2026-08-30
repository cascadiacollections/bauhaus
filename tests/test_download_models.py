"""Tests for models/download_models.py checksum verification.

The weights are cached in CI under a key derived from this script, and
urlretrieve() reports success for a truncated body, so the checksum is the only
thing standing between a corrupt download and every later run replaying it out
of the Actions cache.
"""

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "models" / "download_models.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("download_models", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["download_models"] = module
    spec.loader.exec_module(module)
    return module


dm = _load_module()


# Each downloaded file and the source module that consumes it. The NIMA
# archive is converted before use, so nima.py names the .npz rather than the
# .h5 it was built from.
_CONSUMERS = {
    "vgg_normalised.pth": ("stylize.py", "vgg_normalised.pth"),
    "decoder.pth": ("stylize.py", "decoder.pth"),
    "mobilenet_weights.h5": ("nima.py", dm.NIMA_CONVERTED),
}


class TestPinnedChecksums:
    def test_every_file_has_a_sha256(self):
        assert set(dm._FILES) == set(_CONSUMERS)
        for name, (url, digest) in dm._FILES.items():
            assert url.startswith("https://"), f"{name} is not fetched over TLS"
            assert len(digest) == 64, f"{name} checksum is not a SHA-256 hex digest"
            int(digest, 16)  # raises unless hex

    def test_each_weight_file_is_loaded_by_its_module(self):
        """A weight file renamed in one place and not the other fails silently."""
        src = Path(__file__).resolve().parent.parent / "src"
        for name, (module, symbol) in _CONSUMERS.items():
            source = (src / module).read_text(encoding="utf-8")
            assert symbol in source, f"{name} is downloaded but {module} never loads it"


class TestSha256File:
    def test_matches_hashlib(self, tmp_path):
        target = tmp_path / "blob.bin"
        payload = b"bauhaus" * 5000
        target.write_bytes(payload)
        assert dm.sha256_file(target) == hashlib.sha256(payload).hexdigest()

    def test_reads_files_larger_than_one_chunk(self, tmp_path):
        target = tmp_path / "big.bin"
        payload = b"x" * (dm._CHUNK * 2 + 17)
        target.write_bytes(payload)
        assert dm.sha256_file(target) == hashlib.sha256(payload).hexdigest()


class TestDownloadModels:
    def _pin(self, monkeypatch, payload: bytes, name: str = "decoder.pth"):
        monkeypatch.setattr(
            dm, "_FILES",
            {name: (f"https://example.invalid/{name}", hashlib.sha256(payload).hexdigest())},
        )
        # The real conversion needs the NIMA .h5, which these fixtures do not
        # write; the conversion itself is covered by TestConvertNima.
        monkeypatch.setattr(dm, "convert_nima", lambda weights_dir: None)

    def test_valid_existing_file_is_not_redownloaded(self, tmp_path, monkeypatch):
        payload = b"good-weights"
        self._pin(monkeypatch, payload)
        (tmp_path / "decoder.pth").write_bytes(payload)

        called = []
        monkeypatch.setattr(dm, "urlretrieve", lambda *a, **k: called.append(a))
        dm.download_models(tmp_path)
        assert called == []

    def test_corrupt_existing_file_is_replaced(self, tmp_path, monkeypatch):
        """The cached-corruption case: a truncated file already on disk."""
        payload = b"good-weights"
        self._pin(monkeypatch, payload)
        dest = tmp_path / "decoder.pth"
        dest.write_bytes(b"truncated")

        def fake_retrieve(url, target):
            Path(target).write_bytes(payload)

        monkeypatch.setattr(dm, "urlretrieve", fake_retrieve)
        dm.download_models(tmp_path)
        assert dest.read_bytes() == payload

    def test_download_with_bad_checksum_raises_and_removes_the_file(self, tmp_path, monkeypatch):
        self._pin(monkeypatch, b"expected-content")

        def fake_retrieve(url, target):
            Path(target).write_bytes(b"something-else")

        monkeypatch.setattr(dm, "urlretrieve", fake_retrieve)
        with pytest.raises(RuntimeError, match="Checksum mismatch"):
            dm.download_models(tmp_path)
        assert not (tmp_path / "decoder.pth").exists()

    def test_missing_file_is_downloaded(self, tmp_path, monkeypatch):
        payload = b"fresh-weights"
        self._pin(monkeypatch, payload)

        def fake_retrieve(url, target):
            Path(target).write_bytes(payload)

        monkeypatch.setattr(dm, "urlretrieve", fake_retrieve)
        dm.download_models(tmp_path)
        assert (tmp_path / "decoder.pth").read_bytes() == payload

    def test_network_failure_cleans_up_the_partial_file(self, tmp_path, monkeypatch):
        self._pin(monkeypatch, b"never-arrives")

        def fake_retrieve(url, target):
            Path(target).write_bytes(b"half-a-fi")
            raise OSError("connection reset")

        monkeypatch.setattr(dm, "urlretrieve", fake_retrieve)
        with pytest.raises(RuntimeError, match="Failed to download"):
            dm.download_models(tmp_path)
        assert not (tmp_path / "decoder.pth").exists()


class TestConvertNima:
    """The .npz is a build product, so its freshness has to be self-checking."""

    def _archive(self, path: Path, source_sha: str) -> None:
        import numpy as np

        np.savez(path, **{dm._SOURCE_SHA_KEY: np.array(source_sha), "dense.bias": np.zeros(10)})

    def test_conversion_is_skipped_when_the_archive_is_current(self, tmp_path, monkeypatch):
        source = tmp_path / dm.NIMA_SOURCE
        source.write_bytes(b"keras-weights")
        self._archive(tmp_path / dm.NIMA_CONVERTED, dm.sha256_file(source))

        monkeypatch.setattr(dm, "_keras_to_torch", _unexpected_conversion)
        dm.convert_nima(tmp_path)

    def test_a_changed_source_triggers_reconversion(self, tmp_path, monkeypatch):
        """Re-pinning the upstream weights must not leave stale arrays behind."""
        import numpy as np

        source = tmp_path / dm.NIMA_SOURCE
        source.write_bytes(b"new-keras-weights")
        self._archive(tmp_path / dm.NIMA_CONVERTED, "0" * 64)

        monkeypatch.setattr(dm, "_keras_to_torch", lambda path: {"dense.bias": np.ones(10)})
        dm.convert_nima(tmp_path)

        with np.load(tmp_path / dm.NIMA_CONVERTED) as archive:
            assert archive["dense.bias"].tolist() == [1.0] * 10
            assert str(archive[dm._SOURCE_SHA_KEY]) == dm.sha256_file(source)

    def test_an_unreadable_archive_is_rebuilt(self, tmp_path, monkeypatch):
        import numpy as np

        source = tmp_path / dm.NIMA_SOURCE
        source.write_bytes(b"keras-weights")
        (tmp_path / dm.NIMA_CONVERTED).write_bytes(b"not an npz")

        monkeypatch.setattr(dm, "_keras_to_torch", lambda path: {"dense.bias": np.zeros(10)})
        dm.convert_nima(tmp_path)

        with np.load(tmp_path / dm.NIMA_CONVERTED) as archive:
            assert str(archive[dm._SOURCE_SHA_KEY]) == dm.sha256_file(source)


def _unexpected_conversion(path):
    raise AssertionError("conversion ran despite an up-to-date archive")
