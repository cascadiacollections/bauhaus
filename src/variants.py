"""Generate AVIF and WebP variants from a PIL Image."""

import sys
from io import BytesIO

from PIL import Image


def generate_variants(image: Image.Image) -> dict[str, bytes]:
    """Encode *image* as WebP and AVIF. Returns ``{ext: raw_bytes}``."""
    variants: dict[str, bytes] = {}

    # WebP — Pillow ships with built-in WebP support on all platforms.
    try:
        buf = BytesIO()
        image.save(buf, format="WebP", quality=85)
        variants["webp"] = buf.getvalue()
    except Exception:
        print("  ⚠ WebP encoding not available, skipping", file=sys.stderr)

    # AVIF — requires Pillow compiled with libavif (standard on recent wheels).
    try:
        buf = BytesIO()
        image.save(buf, format="AVIF", quality=80)
        variants["avif"] = buf.getvalue()
    except Exception:
        print("  ⚠ AVIF encoding not available, skipping", file=sys.stderr)

    return variants
