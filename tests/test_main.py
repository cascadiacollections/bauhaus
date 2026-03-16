"""Tests for main.py — styles manifest, style rotation, CLI args, and metadata helpers."""

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from main import (
    build_license_details,
    build_variants,
    load_styles_manifest,
    main,
    STYLES_DIR,
)


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
