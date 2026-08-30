#!/usr/bin/env python3
# /// script
# requires-python = ">=3.13"
# dependencies = ["h5py>=3.16,<4", "numpy>=2.5,<3"]
# ///
"""Download and verify model weights (cross-platform).

Fetches the VGG-19 encoder and AdaIN decoder used for style transfer, plus the
NIMA aesthetic-scoring model, and converts the last of these from its upstream
Keras format into the NumPy archive ``src/nima.py`` loads.

Standalone — no *project* dependencies (h5py and numpy come from the inline
script metadata above, so `uv run --script` provides them):

    uv run --script models/download_models.py

Idempotent: files already on disk are checksummed and left alone when they
match, re-downloaded when they do not. Safe to run before every generation.
"""

import hashlib
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlretrieve

_ADAIN_BASE = "https://github.com/naoto0804/pytorch-AdaIN/releases/download/v0.0.0"
_NIMA_BASE = "https://github.com/titu1994/neural-image-assessment/releases/download/v0.3"
WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"

# Source URL and SHA-256 of each release asset. The AdaIN pins were recorded
# 2026-08-01, the NIMA pin 2026-08-30, each from the upstream release.
#
# These pins are load-bearing twice over. urlretrieve() reports success for a
# truncated body as long as the transfer ends cleanly, so without a checksum a
# partial download is indistinguishable from a good one — and both generate
# workflows cache models/weights, which would then serve that corrupt file to
# every later run until a human manually evicted the cache. Both release tags
# (v0.0.0, v0.3) are also mutable upstream, so pinning the bytes is the only
# thing that makes "the model we tested" and "the model we run" the same
# artifact.
_FILES: dict[str, tuple[str, str]] = {
    "vgg_normalised.pth": (
        f"{_ADAIN_BASE}/vgg_normalised.pth",
        "804ca2835ecf7539f0cd2a7ac3c18ce81e6f8468969ae7117ac0c148d286bb4a",
    ),
    "decoder.pth": (
        f"{_ADAIN_BASE}/decoder.pth",
        "379ca41d59f3a37eed3599bbbc2560c19da5c458870a5ffd3a9dd41aa88f9472",
    ),
    # NIMA MobileNet-v1 trained on AVA (MIT, 0.0804 EMD on the validation
    # split). Keras format — converted to nima_mobilenet.npz below.
    "mobilenet_weights.h5": (
        f"{_NIMA_BASE}/mobilenet_weights.h5",
        "feff35c54daed3fcd2534c310ebe82257bfdd488b8d227d25c736bce95aef9e2",
    ),
}

# Converted NIMA weights, in PyTorch layout under src/nima.py's parameter names.
NIMA_SOURCE = "mobilenet_weights.h5"
NIMA_CONVERTED = "nima_mobilenet.npz"

# Records which .h5 the .npz was built from, so a re-pinned or re-downloaded
# source triggers a reconversion instead of silently keeping stale arrays.
_SOURCE_SHA_KEY = "_source_sha256"

_CHUNK = 1024 * 1024


def sha256_file(path: Path) -> str:
    """Return the hex SHA-256 of a file, read in chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _keras_to_torch(h5_path: Path) -> dict:
    """Convert the Keras NIMA MobileNet-v1 weights to PyTorch layout.

    Returns a dict of NumPy arrays keyed by ``src/nima.py``'s parameter names.
    The mapping is name-for-name — the PyTorch modules are named after the Keras
    layers — so nothing depends on layer ordering. Only the memory layout
    changes: Keras stores convolution kernels as (kh, kw, in, out) and PyTorch
    as (out, in, kh, kw), depthwise kernels as (kh, kw, in, mult) against
    (in * mult, 1, kh, kw), and dense kernels transposed.
    """
    import h5py
    import numpy as np

    def _tensor(group: h5py.File, layer: str, weight: str) -> np.ndarray:
        return np.asarray(group[f"{layer}/{layer}/{weight}:0"], dtype=np.float32)

    def _batchnorm(group: h5py.File, layer: str) -> dict:
        # Keras keeps the variance; PyTorch's running_var is the same quantity.
        return {
            f"{layer}.weight": _tensor(group, layer, "gamma"),
            f"{layer}.bias": _tensor(group, layer, "beta"),
            f"{layer}.running_mean": _tensor(group, layer, "moving_mean"),
            f"{layer}.running_var": _tensor(group, layer, "moving_variance"),
            # Unused in eval mode, but load_state_dict(strict=True) expects it,
            # and strictness is what catches a botched conversion.
            f"{layer}.num_batches_tracked": np.array(0, dtype=np.int64),
        }

    state: dict = {}
    with h5py.File(h5_path, "r") as f:
        state["conv1.weight"] = _tensor(f, "conv1", "kernel").transpose(3, 2, 0, 1)
        state.update(_batchnorm(f, "conv1_bn"))

        for i in range(1, 14):
            dw = _tensor(f, f"conv_dw_{i}", "depthwise_kernel")
            state[f"conv_dw_{i}.weight"] = dw.transpose(2, 3, 0, 1)
            state.update(_batchnorm(f, f"conv_dw_{i}_bn"))
            state[f"conv_pw_{i}.weight"] = _tensor(f, f"conv_pw_{i}", "kernel").transpose(3, 2, 0, 1)
            state.update(_batchnorm(f, f"conv_pw_{i}_bn"))

        state["dense.weight"] = _tensor(f, "dense_1", "kernel").transpose(1, 0)
        state["dense.bias"] = _tensor(f, "dense_1", "bias")

    return {k: np.ascontiguousarray(v) for k, v in state.items()}


def convert_nima(weights_dir: Path = WEIGHTS_DIR) -> None:
    """Build nima_mobilenet.npz from the downloaded Keras weights, if needed.

    Skipped when an existing archive already records the current source file's
    checksum: the conversion is deterministic, so re-running it would produce
    identical bytes.
    """
    import numpy as np

    source = weights_dir / NIMA_SOURCE
    dest = weights_dir / NIMA_CONVERTED
    source_sha = sha256_file(source)

    if dest.exists():
        try:
            with np.load(dest) as archive:
                if str(archive[_SOURCE_SHA_KEY]) == source_sha:
                    return
        except (OSError, ValueError, KeyError):
            print(f"⚠ {NIMA_CONVERTED} is unreadable — reconverting", file=sys.stderr)

    print(f"Converting {NIMA_SOURCE} → {NIMA_CONVERTED}...")
    state = _keras_to_torch(source)
    state[_SOURCE_SHA_KEY] = np.array(source_sha)
    np.savez(dest, **state)


def download_models(weights_dir: Path = WEIGHTS_DIR) -> None:
    """Ensure every weight file is present and matches its recorded checksum."""
    weights_dir.mkdir(parents=True, exist_ok=True)
    for name, (url, expected) in _FILES.items():
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

    convert_nima(weights_dir)

    print(f"Models ready and verified in {weights_dir}")


if __name__ == "__main__":
    download_models()
