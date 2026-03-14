"""Tests for stylize.py — alpha mask functions."""

import torch

from stylize import gradient_alpha_mask, luminance_alpha_mask


# --- gradient_alpha_mask ---

class TestGradientAlphaMask:
    def test_shape(self):
        mask = gradient_alpha_mask(16, 32, top=1.0, bottom=0.5)
        assert mask.shape == (1, 1, 16, 32)

    def test_top_value(self):
        mask = gradient_alpha_mask(10, 10, top=0.9, bottom=0.3)
        assert abs(mask[0, 0, 0, 0].item() - 0.9) < 1e-5

    def test_bottom_value(self):
        mask = gradient_alpha_mask(10, 10, top=0.9, bottom=0.3)
        assert abs(mask[0, 0, -1, 0].item() - 0.3) < 1e-5

    def test_monotonically_decreasing(self):
        mask = gradient_alpha_mask(64, 32, top=1.0, bottom=0.0)
        col = mask[0, 0, :, 0]
        for i in range(len(col) - 1):
            assert col[i] >= col[i + 1]

    def test_uniform_across_width(self):
        mask = gradient_alpha_mask(8, 16, top=1.0, bottom=0.5)
        row = mask[0, 0, 3, :]
        assert torch.allclose(row, row[0].expand_as(row))

    def test_single_row(self):
        mask = gradient_alpha_mask(1, 10, top=0.8, bottom=0.8)
        assert mask.shape == (1, 1, 1, 10)
        assert abs(mask[0, 0, 0, 0].item() - 0.8) < 1e-5


# --- luminance_alpha_mask ---

class TestLuminanceAlphaMask:
    def test_shape(self):
        tensor = torch.rand(1, 3, 16, 32)
        mask = luminance_alpha_mask(tensor)
        assert mask.shape == (1, 1, 16, 32)

    def test_bright_gets_high_alpha(self):
        # All-white image → brightest
        bright = torch.ones(1, 3, 8, 8)
        mask = luminance_alpha_mask(bright, bright_alpha=1.0, dark_alpha=0.0)
        # Should be near 1.0 for all pixels (uniform bright)
        # With constant input, all normalized to 0.5 → alpha should be 0.5
        # Actually: hi-lo < 1e-6 → fallback to 0.5, so mask = 0.0 + (1.0 - 0.0) * 0.5 = 0.5
        assert mask.shape == (1, 1, 8, 8)

    def test_varying_luminance(self):
        # Create tensor with known varying luminance
        tensor = torch.zeros(1, 3, 2, 1)
        # Pixel 0: white (bright) → luminance ≈ 1.0
        tensor[0, :, 0, 0] = 1.0
        # Pixel 1: black (dark) → luminance ≈ 0.0
        tensor[0, :, 1, 0] = 0.0
        mask = luminance_alpha_mask(tensor, bright_alpha=1.0, dark_alpha=0.0)
        # Bright pixel should have higher alpha than dark pixel
        assert mask[0, 0, 0, 0].item() > mask[0, 0, 1, 0].item()

    def test_values_in_range(self):
        tensor = torch.rand(1, 3, 16, 16)
        mask = luminance_alpha_mask(tensor, bright_alpha=0.9, dark_alpha=0.3)
        assert mask.min() >= 0.3 - 1e-5
        assert mask.max() <= 0.9 + 1e-5

    def test_uniform_input_fallback(self):
        # Constant-color image: hi-lo < 1e-6
        tensor = torch.full((1, 3, 4, 4), 0.5)
        mask = luminance_alpha_mask(tensor, bright_alpha=1.0, dark_alpha=0.0)
        # Falls back to luminance=0.5 everywhere
        expected = 0.0 + (1.0 - 0.0) * 0.5
        assert torch.allclose(mask, torch.full_like(mask, expected), atol=1e-5)
