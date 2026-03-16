"""Tests for variants.py — AVIF and WebP variant generation."""

import pytest
from PIL import Image

from variants import generate_variants


def _dummy_image(width: int = 64, height: int = 64) -> Image.Image:
    return Image.new("RGB", (width, height), color=(120, 80, 200))


class TestGenerateVariants:
    def test_returns_webp(self):
        variants = generate_variants(_dummy_image())
        assert "webp" in variants
        assert len(variants["webp"]) > 0

    def test_webp_is_valid_image(self):
        from io import BytesIO

        variants = generate_variants(_dummy_image())
        img = Image.open(BytesIO(variants["webp"]))
        assert img.format == "WEBP"

    def test_avif_when_available(self):
        variants = generate_variants(_dummy_image())
        if "avif" in variants:
            from io import BytesIO

            img = Image.open(BytesIO(variants["avif"]))
            assert img.format == "AVIF"

    def test_variant_sizes_smaller_than_jpeg(self):
        from io import BytesIO

        img = _dummy_image(256, 256)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=95)
        jpeg_size = len(buf.getvalue())

        variants = generate_variants(img)
        assert len(variants["webp"]) < jpeg_size

    def test_empty_dict_on_error(self, monkeypatch):
        """If both formats fail, returns whatever succeeded."""
        import variants as mod

        original_save = Image.Image.save

        def broken_save(self, fp, format=None, **kwargs):
            if format in ("WebP", "AVIF"):
                raise OSError(f"{format} not supported")
            return original_save(self, fp, format=format, **kwargs)

        monkeypatch.setattr(Image.Image, "save", broken_save)
        result = generate_variants(_dummy_image())
        assert "webp" not in result
        assert "avif" not in result
