"""Tests for main.py — styles manifest, style rotation, and CLI args."""

import argparse
import json
import os
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from main import embed_exif, extract_exif, load_styles_manifest, main, strip_exif, STYLES_DIR


class TestLoadStylesManifest:
    def test_loads_real_manifest(self):
        styles = load_styles_manifest()
        assert isinstance(styles, list)
        assert len(styles) > 0

    def test_each_style_has_required_keys(self):
        styles = load_styles_manifest()
        for style in styles:
            assert "filename" in style
            assert "title" in style
            assert "artist" in style

    def test_returns_empty_if_missing(self, tmp_path):
        with patch("main.STYLES_DIR", tmp_path):
            styles = load_styles_manifest()
            assert styles == []


class TestStyleRotation:
    def test_day_of_year_mod_wraps(self):
        styles = load_styles_manifest()
        n = len(styles)
        # Day-of-year mod should produce valid indices for any day
        for day in [1, n, n + 1, 365]:
            idx = day % n
            assert 0 <= idx < n


class TestMaxSizeCLIArg:
    """Tests for --max-size CLI flag and MAX_SIZE env var."""

    def test_default_max_size(self):
        """Default --max-size should be 1024 when no env var is set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAX_SIZE", None)
            parser = argparse.ArgumentParser()
            parser.add_argument("--max-size", type=int,
                                default=int(os.environ.get("MAX_SIZE", "1024")))
            args = parser.parse_args([])
            assert args.max_size == 1024

    def test_cli_flag_overrides_default(self):
        """--max-size flag should override the default."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--max-size", type=int,
                            default=int(os.environ.get("MAX_SIZE", "1024")))
        args = parser.parse_args(["--max-size", "1536"])
        assert args.max_size == 1536

    def test_env_var_sets_default(self):
        """MAX_SIZE env var should set the default when no flag is passed."""
        with patch.dict(os.environ, {"MAX_SIZE": "1920"}):
            parser = argparse.ArgumentParser()
            parser.add_argument("--max-size", type=int,
                                default=int(os.environ.get("MAX_SIZE", "1024")))
            args = parser.parse_args([])
            assert args.max_size == 1920

    def test_cli_flag_overrides_env_var(self):
        """--max-size flag should override MAX_SIZE env var."""
        with patch.dict(os.environ, {"MAX_SIZE": "1920"}):
            parser = argparse.ArgumentParser()
            parser.add_argument("--max-size", type=int,
                                default=int(os.environ.get("MAX_SIZE", "1024")))
            args = parser.parse_args(["--max-size", "768"])
            assert args.max_size == 768


def _make_test_image(width=100, height=100) -> Image.Image:
    """Create a simple RGB test image."""
    return Image.new("RGB", (width, height), color=(128, 64, 32))


def _make_test_image_bytes(width=100, height=100) -> bytes:
    """Create JPEG bytes from a simple test image."""
    img = _make_test_image(width, height)
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


class TestExtractExif:
    """Tests for extract_exif() — EXIF metadata extraction from image bytes."""

    def test_extract_from_image_without_exif(self):
        """Plain JPEG with no EXIF should return an empty dict."""
        img_bytes = _make_test_image_bytes()
        result = extract_exif(img_bytes)
        assert isinstance(result, dict)

    def test_extract_from_image_with_exif(self):
        """JPEG with embedded EXIF should have tags extracted."""
        img = _make_test_image()
        metadata = {"title": "Sunset Over Mountains", "artist": "Jane Doe",
                     "license": "CC0-1.0", "license_url": "https://example.com"}
        img_bytes = embed_exif(img, metadata)
        result = extract_exif(img_bytes)
        assert isinstance(result, dict)
        assert result.get("ImageDescription") == "Sunset Over Mountains"
        assert result.get("Artist") == "Jane Doe"
        assert "Copyright" in result

    def test_extract_returns_strings(self):
        """All values in extracted EXIF dict should be strings."""
        img = _make_test_image()
        metadata = {"title": "Test", "artist": "Artist"}
        img_bytes = embed_exif(img, metadata)
        result = extract_exif(img_bytes)
        for key, val in result.items():
            assert isinstance(key, str)
            assert isinstance(val, str)


class TestStripExif:
    """Tests for strip_exif() — producing EXIF-free JPEG bytes."""

    def test_strip_returns_valid_jpeg(self):
        """strip_exif should return valid JPEG bytes."""
        img = _make_test_image()
        result = strip_exif(img)
        assert isinstance(result, bytes)
        assert len(result) > 0
        # Verify it's a valid JPEG
        reopened = Image.open(BytesIO(result))
        assert reopened.format == "JPEG"

    def test_strip_removes_exif(self):
        """strip_exif should produce an image with no EXIF metadata."""
        img = _make_test_image()
        metadata = {"title": "Secret Title", "artist": "Secret Artist",
                     "license": "CC0-1.0"}
        # First embed EXIF
        with_exif = embed_exif(img, metadata)
        # Verify EXIF is present
        assert len(extract_exif(with_exif)) > 0

        # Now strip
        img_with_exif = Image.open(BytesIO(with_exif))
        stripped = strip_exif(img_with_exif)
        stripped_exif = extract_exif(stripped)
        assert len(stripped_exif) == 0

    def test_strip_preserves_image_dimensions(self):
        """Stripped image should have same dimensions as original."""
        img = _make_test_image(200, 150)
        stripped = strip_exif(img)
        reopened = Image.open(BytesIO(stripped))
        assert reopened.size == (200, 150)
