"""Tests for stylize.py — _load_image resolution handling."""

import torch
from PIL import Image

from stylize import _load_image


class TestLoadImage:
    def test_default_max_size_is_1024(self):
        """Default max_size should be 1024."""
        img = Image.new("RGB", (2000, 1500))
        tensor = _load_image(img)
        # Longest side should be scaled to 1024
        _, _, h, w = tensor.shape
        assert max(w, h) == 1024

    def test_custom_max_size(self):
        """Custom max_size should control output resolution."""
        img = Image.new("RGB", (2000, 1500))
        tensor = _load_image(img, max_size=512)
        _, _, h, w = tensor.shape
        assert max(w, h) == 512

    def test_no_upscale(self):
        """Images smaller than max_size should not be upscaled."""
        img = Image.new("RGB", (400, 300))
        tensor = _load_image(img, max_size=1024)
        _, _, h, w = tensor.shape
        assert w == 400
        assert h == 300

    def test_returns_4d_tensor(self):
        """Output should be a 4D tensor (batch, channels, H, W)."""
        img = Image.new("RGB", (100, 100))
        tensor = _load_image(img)
        assert tensor.dim() == 4
        assert tensor.shape[0] == 1
        assert tensor.shape[1] == 3

    def test_preserves_aspect_ratio(self):
        """Resizing should preserve aspect ratio."""
        img = Image.new("RGB", (2000, 1000))
        tensor = _load_image(img, max_size=1024)
        _, _, h, w = tensor.shape
        assert w == 1024
        assert h == 512
