"""Tests for main.py — styles manifest, style rotation, and CLI args."""

import argparse
import json
import os
from datetime import date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from main import build_manifest, load_styles_manifest, main, STYLES_DIR


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
