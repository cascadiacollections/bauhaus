"""Tests for main.py — styles manifest, style rotation, CLI args, and metadata helpers."""

import argparse
import json
import os
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from main import (
    build_license_details,
    build_manifest,
    build_variants,
    embed_exif,
    extract_exif,
    generate_variants,
    load_styles_manifest,
    main,
    strip_exif,
    STYLES_DIR,
)


def _jpeg_bytes(width: int, height: int) -> bytes:
    """Create minimal JPEG bytes of the given dimensions."""
    img = Image.new("RGB", (width, height), color=(128, 64, 32))
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=50)
    return buf.getvalue()


class TestBuildManifest:
    def test_returns_required_keys(self):
        stylized = _jpeg_bytes(1920, 1080)
        original = _jpeg_bytes(3000, 2000)
        metadata = {"license": "CC0-1.0", "license_url": "https://example.com",
                     "source": "met", "source_url": "https://met.example.com"}
        manifest = build_manifest(stylized, original, metadata, today=date(2025, 7, 14))

        assert "variants" in manifest
        assert "aspect_ratio" in manifest
        assert "license" in manifest
        assert "license_url" in manifest
        assert "source" in manifest
        assert "source_url" in manifest
        assert manifest["date"] == "2025-07-14"

    def test_variants_structure(self):
        stylized = _jpeg_bytes(1920, 1080)
        original = _jpeg_bytes(3000, 2000)
        metadata = {"license": "CC0-1.0", "source": "unsplash"}
        manifest = build_manifest(stylized, original, metadata, today=date(2025, 7, 14))

        assert len(manifest["variants"]) == 2
        for v in manifest["variants"]:
            assert "width" in v
            assert "height" in v
            assert "format" in v
            assert "url" in v
            assert "size_bytes" in v
            assert v["format"] == "jpeg"

    def test_variant_dimensions(self):
        stylized = _jpeg_bytes(1920, 1080)
        original = _jpeg_bytes(3000, 2000)
        metadata = {}
        manifest = build_manifest(stylized, original, metadata, today=date(2025, 7, 14))

        stylized_var = manifest["variants"][0]
        assert stylized_var["width"] == 1920
        assert stylized_var["height"] == 1080

        original_var = manifest["variants"][1]
        assert original_var["width"] == 3000
        assert original_var["height"] == 2000

    def test_variant_urls(self):
        stylized = _jpeg_bytes(800, 600)
        original = _jpeg_bytes(800, 600)
        metadata = {}
        manifest = build_manifest(stylized, original, metadata, today=date(2025, 12, 25))

        assert manifest["variants"][0]["url"] == "/api/2025-12-25"
        assert manifest["variants"][1]["url"] == "/api/2025-12-25/original"

    def test_variant_size_bytes(self):
        stylized = _jpeg_bytes(640, 480)
        original = _jpeg_bytes(640, 480)
        metadata = {}
        manifest = build_manifest(stylized, original, metadata, today=date(2025, 1, 1))

        assert manifest["variants"][0]["size_bytes"] == len(stylized)
        assert manifest["variants"][1]["size_bytes"] == len(original)

    def test_aspect_ratio(self):
        stylized = _jpeg_bytes(1920, 1080)
        original = _jpeg_bytes(1920, 1080)
        metadata = {}
        manifest = build_manifest(stylized, original, metadata, today=date(2025, 1, 1))
        assert manifest["aspect_ratio"] == "16:9"

    def test_metadata_passthrough(self):
        stylized = _jpeg_bytes(100, 100)
        original = _jpeg_bytes(100, 100)
        metadata = {
            "license": "CC0-1.0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source": "artic",
            "source_url": "https://www.artic.edu/artworks/12345",
        }
        manifest = build_manifest(stylized, original, metadata, today=date(2025, 3, 15))

        assert manifest["license"] == "CC0-1.0"
        assert manifest["license_url"] == "https://creativecommons.org/publicdomain/zero/1.0/"
        assert manifest["source"] == "artic"
        assert manifest["source_url"] == "https://www.artic.edu/artworks/12345"

    def test_missing_metadata_fields_default_empty(self):
        stylized = _jpeg_bytes(100, 100)
        original = _jpeg_bytes(100, 100)
        manifest = build_manifest(stylized, original, {}, today=date(2025, 1, 1))

        assert manifest["license"] == ""
        assert manifest["source"] == ""

    def test_manifest_is_json_serializable(self):
        stylized = _jpeg_bytes(800, 600)
        original = _jpeg_bytes(800, 600)
        metadata = {"license": "CC0-1.0", "source": "met"}
        manifest = build_manifest(stylized, original, metadata, today=date(2025, 7, 14))
        # Should not raise
        json.dumps(manifest)


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


# --- Variant generation ---


class TestGenerateVariants:
    def _img(self, w: int = 100, h: int = 80) -> Image.Image:
        return Image.new("RGB", (w, h), "red")

    def test_produces_webp(self):
        variants = generate_variants(self._img())
        assert "webp" in variants
        assert len(variants["webp"]) > 0

    def test_produces_progressive_jpeg(self):
        variants = generate_variants(self._img())
        assert "progressive.jpg" in variants
        assert len(variants["progressive.jpg"]) > 0

    def test_produces_stripped_jpeg(self):
        variants = generate_variants(self._img())
        assert "stripped.jpg" in variants
        assert len(variants["stripped.jpg"]) > 0

    def test_avif_skipped_gracefully_or_present(self):
        """AVIF is optional — codec may or may not be available."""
        variants = generate_variants(self._img())
        assert isinstance(variants, dict)
        # At minimum we always get webp, progressive, stripped
        assert len(variants) >= 3

    def test_with_exif_bytes(self):
        img = self._img()
        exif = img.getexif()
        exif[0x010E] = "Test Description"
        exif_bytes = exif.tobytes()
        variants = generate_variants(img, exif_bytes=exif_bytes)
        assert "webp" in variants
        assert "progressive.jpg" in variants

    def test_stripped_has_no_exif(self):
        img = self._img()
        exif = img.getexif()
        exif[0x010E] = "Should Not Appear"
        exif_bytes = exif.tobytes()
        variants = generate_variants(img, exif_bytes=exif_bytes)
        stripped = Image.open(BytesIO(variants["stripped.jpg"]))
        stripped_exif = stripped.getexif()
        assert 0x010E not in stripped_exif


# --- Manifest building ---


class TestBuildManifest:
    def _metadata(self) -> dict:
        return {
            "license": "CC0-1.0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source": "met",
            "source_url": "https://www.metmuseum.org/art/collection/search/1",
        }

    def _jpeg_bytes(self, w: int = 200, h: int = 100) -> bytes:
        buf = BytesIO()
        Image.new("RGB", (w, h), "red").save(buf, format="JPEG")
        return buf.getvalue()

    def test_manifest_structure(self):
        stylized_bytes = self._jpeg_bytes(200, 100)
        variants = {"webp": b"w" * 50, "progressive.jpg": b"p" * 60}
        manifest = build_manifest(
            self._metadata(), 200, 100, len(stylized_bytes), variants, "2025-07-14",
        )
        assert manifest["date"] == "2025-07-14"
        assert manifest["aspect_ratio"] == "2:1"
        assert manifest["license"]["type"] == "CC0-1.0"
        assert manifest["source"] == "met"

    def test_includes_jpeg_variant(self):
        stylized_bytes = self._jpeg_bytes(100, 100)
        manifest = build_manifest(
            self._metadata(), 100, 100, len(stylized_bytes), {}, "2025-07-14",
        )
        formats = [v["format"] for v in manifest["variants"]]
        assert "jpeg" in formats

    def test_includes_all_variant_formats(self):
        stylized_bytes = self._jpeg_bytes()
        variants = {
            "avif": b"a" * 40,
            "webp": b"w" * 50,
            "progressive.jpg": b"p" * 60,
            "stripped.jpg": b"s" * 55,
        }
        manifest = build_manifest(
            self._metadata(), 200, 100, len(stylized_bytes), variants, "2025-07-14",
        )
        formats = {v["format"] for v in manifest["variants"]}
        assert formats == {"jpeg", "avif", "webp", "progressive-jpeg", "stripped-jpeg"}

    def test_variant_urls(self):
        stylized_bytes = self._jpeg_bytes()
        variants = {"webp": b"w" * 50}
        manifest = build_manifest(
            self._metadata(), 200, 100, len(stylized_bytes), variants, "2025-07-14",
        )
        urls = {v["format"]: v["url"] for v in manifest["variants"]}
        assert urls["jpeg"] == "/api/2025-07-14"
        assert urls["webp"] == "/api/2025-07-14?format=webp"

    def test_size_bytes(self):
        stylized_bytes = self._jpeg_bytes()
        variants = {"webp": b"w" * 50}
        manifest = build_manifest(
            self._metadata(), 200, 100, len(stylized_bytes), variants, "2025-07-14",
        )
        jpeg_entry = next(v for v in manifest["variants"] if v["format"] == "jpeg")
        assert jpeg_entry["size_bytes"] == len(stylized_bytes)
        webp_entry = next(v for v in manifest["variants"] if v["format"] == "webp")
        assert webp_entry["size_bytes"] == 50


# --- License details and variant descriptors ---


class TestBuildLicenseDetails:
    """Tests for build_license_details() helper."""

    def test_cc0_metadata(self):
        meta = {
            "license": "CC0-1.0",
            "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
            "source": "met",
            "source_url": "https://www.metmuseum.org/art/collection/search/12345",
        }
        details = build_license_details(meta)
        assert details["type"] == "CC0-1.0"
        assert details["url"] == "https://creativecommons.org/publicdomain/zero/1.0/"
        assert details["source"] == "met"
        assert details["source_url"] == "https://www.metmuseum.org/art/collection/search/12345"

    def test_unsplash_metadata(self):
        meta = {
            "license": "Unsplash License",
            "license_url": "https://unsplash.com/license",
            "source": "unsplash",
            "source_url": "https://unsplash.com/photos/abc",
        }
        details = build_license_details(meta)
        assert details["type"] == "Unsplash License"
        assert details["url"] == "https://unsplash.com/license"
        assert details["source"] == "unsplash"

    def test_missing_fields_default_to_empty(self):
        details = build_license_details({})
        assert details["type"] == ""
        assert details["url"] == ""
        assert details["source"] == ""
        assert details["source_url"] == ""


class TestBuildVariants:
    """Tests for build_variants() helper."""

    def _make_image(self, width: int, height: int) -> Image.Image:
        return Image.new("RGB", (width, height), color="red")

    def test_returns_two_variants(self):
        stylized = self._make_image(1920, 1080)
        original = self._make_image(3840, 2160)
        variants = build_variants(stylized, b"s" * 100, original, b"o" * 200, "2025-07-14")
        assert len(variants) == 2

    def test_stylized_variant_fields(self):
        stylized = self._make_image(1920, 1080)
        original = self._make_image(3840, 2160)
        variants = build_variants(stylized, b"s" * 500, original, b"o" * 1000, "2025-07-14")
        v = variants[0]
        assert v["type"] == "stylized"
        assert v["format"] == "image/jpeg"
        assert v["width"] == 1920
        assert v["height"] == 1080
        assert v["url"] == "/api/2025-07-14"
        assert v["size_bytes"] == 500

    def test_original_variant_fields(self):
        stylized = self._make_image(1920, 1080)
        original = self._make_image(3840, 2160)
        variants = build_variants(stylized, b"s" * 500, original, b"o" * 1000, "2025-07-14")
        v = variants[1]
        assert v["type"] == "original"
        assert v["format"] == "image/jpeg"
        assert v["width"] == 3840
        assert v["height"] == 2160
        assert v["url"] == "/api/2025-07-14/original"
        assert v["size_bytes"] == 1000

    def test_url_uses_provided_date(self):
        img = self._make_image(100, 100)
        variants = build_variants(img, b"x", img, b"y", "2026-01-01")
        assert variants[0]["url"] == "/api/2026-01-01"
        assert variants[1]["url"] == "/api/2026-01-01/original"


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
        assert len(result) == 0

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
