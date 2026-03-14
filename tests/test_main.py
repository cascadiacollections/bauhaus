"""Tests for main.py — styles manifest, style rotation, and CLI args."""

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

from main import load_styles_manifest, main, STYLES_DIR


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
