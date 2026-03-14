#!/usr/bin/env python3
"""Download VGG-19 encoder and AdaIN decoder weights (cross-platform)."""

from pathlib import Path
from urllib.request import urlretrieve

BASE_URL = "https://github.com/naoto0804/pytorch-AdaIN/releases/download/v0.0.0"
WEIGHTS_DIR = Path(__file__).resolve().parent / "weights"

_FILES = ("vgg_normalised.pth", "decoder.pth")


def download_models(weights_dir: Path = WEIGHTS_DIR) -> None:
    """Download model files that are not already present."""
    weights_dir.mkdir(parents=True, exist_ok=True)
    for name in _FILES:
        dest = weights_dir / name
        if dest.exists():
            continue
        url = f"{BASE_URL}/{name}"
        print(f"Downloading {name}...")
        urlretrieve(url, dest)  # noqa: S310 — trusted fixed URL
    print(f"Models ready in {weights_dir}")


if __name__ == "__main__":
    download_models()
