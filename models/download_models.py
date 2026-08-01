#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = []
# ///
"""Download and verify VGG-19 encoder and AdaIN decoder weights (cross-platform).

Standalone — no project dependencies:

    uv run --script models/download_models.py

Idempotent: files already on disk are checksummed and left alone when they
match, re-downloaded when they do not. Safe to run before every generation.
"""

import hashlib
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlretrieve

BASE_URL = "https://github.com/naoto0804/pytorch-AdaIN/releases/download/v0.0.0"
WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"

# SHA-256 of each release asset, recorded 2026-08-01 from the upstream release.
#
# These pins are load-bearing twice over. urlretrieve() reports success for a
# truncated body as long as the transfer ends cleanly, so without a checksum a
# partial download is indistinguishable from a good one — and both generate
# workflows cache models/weights, which would then serve that corrupt file to
# every later run until a human manually evicted the cache. The release tag
# (v0.0.0) is also mutable upstream, so pinning the bytes is the only thing
# that makes "the model we tested" and "the model we run" the same artifact.
_FILES: dict[str, str] = {
    "vgg_normalised.pth": "804ca2835ecf7539f0cd2a7ac3c18ce81e6f8468969ae7117ac0c148d286bb4a",
    "decoder.pth": "379ca41d59f3a37eed3599bbbc2560c19da5c458870a5ffd3a9dd41aa88f9472",
}

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def download_models(weights_dir: Path = WEIGHTS_DIR) -> None:
    """Ensure every weight file is present and matches its recorded checksum."""
    weights_dir.mkdir(parents=True, exist_ok=True)
    for name, expected in _FILES.items():
        dest = weights_dir / name

        if dest.exists():
            actual = sha256_file(dest)
            if actual == expected:
                continue
            print(
                f"⚠ {name} checksum mismatch (have {actual[:12]}…, "
                f"want {expected[:12]}…) — re-downloading",
                file=sys.stderr,
            )
            dest.unlink()

        url = f"{BASE_URL}/{name}"
        print(f"Downloading {name}...")
        try:
            urlretrieve(url, dest)  # noqa: S310 — trusted fixed URL, verified below
        except (URLError, OSError) as exc:
            dest.unlink(missing_ok=True)  # remove partial download
            raise RuntimeError(f"Failed to download {name}: {exc}") from exc

        actual = sha256_file(dest)
        if actual != expected:
            dest.unlink(missing_ok=True)
            raise RuntimeError(
                f"Checksum mismatch for {name}: expected {expected}, got {actual}. "
                "The upstream asset changed or the download was corrupted; "
                "the file has been removed."
            )

    print(f"Models ready and verified in {weights_dir}")


if __name__ == "__main__":
    download_models()
