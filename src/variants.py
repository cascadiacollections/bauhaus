"""Generate AVIF, WebP, progressive JPEG, and stripped JPEG variants from a PIL Image."""

import sys
from io import BytesIO

from PIL import Image


def generate_variants(
    image: Image.Image,
    exif_bytes: bytes | None = None,
) -> dict[str, bytes]:
    """Encode *image* as AVIF, WebP, progressive JPEG, and stripped JPEG.

    Returns a dict mapping variant suffix (e.g. ``"avif"``, ``"webp"``,
    ``"progressive.jpg"``, ``"stripped.jpg"``) to the encoded bytes.
    AVIF is silently skipped when the codec is unavailable.

    Args:
        image: PIL Image to encode.
        exif_bytes: Raw EXIF bytes to embed in AVIF, WebP, and progressive
                    JPEG variants. Stripped JPEG is always written without EXIF.
    """
    variants: dict[str, bytes] = {}

    # AVIF
    try:
        buf = BytesIO()
        kw: dict = {"format": "AVIF", "quality": 80}
        if exif_bytes:
            kw["exif"] = exif_bytes
        image.save(buf, **kw)
        variants["avif"] = buf.getvalue()
    except Exception:
        print("  ⚠ AVIF codec unavailable, skipping AVIF variant", file=sys.stderr)

    # WebP — Pillow ships with built-in WebP support on all platforms.
    try:
        buf = BytesIO()
        kw = {"format": "WEBP", "quality": 85, "method": 6}
        if exif_bytes:
            kw["exif"] = exif_bytes
        image.save(buf, **kw)
        variants["webp"] = buf.getvalue()
    except Exception:
        print("  ⚠ WebP encoding not available, skipping", file=sys.stderr)

    # Progressive JPEG (with EXIF)
    buf = BytesIO()
    kw = {"format": "JPEG", "quality": 95, "progressive": True}
    if exif_bytes:
        kw["exif"] = exif_bytes
    image.save(buf, **kw)
    variants["progressive.jpg"] = buf.getvalue()

    # Stripped JPEG (no EXIF metadata)
    buf = BytesIO()
    image.save(buf, format="JPEG", quality=95)
    variants["stripped.jpg"] = buf.getvalue()

    return variants
