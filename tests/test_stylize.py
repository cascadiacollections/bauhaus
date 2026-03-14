"""Tests for stylize.py — alpha mask generation and per-region blending."""

import torch

from stylize import gradient_alpha_mask, luminance_alpha_mask


class TestGradientAlphaMask:
    def test_shape(self):
        mask = gradient_alpha_mask(64, 128)
        assert mask.shape == (1, 1, 64, 128)

    def test_top_row_equals_top_alpha(self):
        mask = gradient_alpha_mask(100, 50, top_alpha=0.9, bottom_alpha=0.5)
        assert torch.allclose(mask[0, 0, 0, :], torch.tensor(0.9))

    def test_bottom_row_equals_bottom_alpha(self):
        mask = gradient_alpha_mask(100, 50, top_alpha=0.9, bottom_alpha=0.5)
        assert torch.allclose(mask[0, 0, -1, :], torch.tensor(0.5))

    def test_monotonically_decreasing_when_top_gt_bottom(self):
        mask = gradient_alpha_mask(64, 32, top_alpha=1.0, bottom_alpha=0.0)
        col = mask[0, 0, :, 0]
        diffs = col[1:] - col[:-1]
        assert (diffs <= 0).all()

    def test_uniform_when_equal_alphas(self):
        mask = gradient_alpha_mask(10, 10, top_alpha=0.7, bottom_alpha=0.7)
        assert torch.allclose(mask, torch.tensor(0.7))

    def test_values_in_range(self):
        mask = gradient_alpha_mask(50, 50, top_alpha=0.9, bottom_alpha=0.3)
        assert mask.min() >= 0.3
        assert mask.max() <= 0.9

    def test_single_row(self):
        mask = gradient_alpha_mask(1, 10, top_alpha=0.8, bottom_alpha=0.2)
        # With a single row, linspace produces a single value equal to top_alpha
        assert mask.shape == (1, 1, 1, 10)
        assert torch.allclose(mask, torch.tensor(0.8))

    def test_uniform_across_width(self):
        mask = gradient_alpha_mask(8, 16, top_alpha=1.0, bottom_alpha=0.5)
        row = mask[0, 0, 3, :]
        assert torch.allclose(row, row[0].expand_as(row))


class TestLuminanceAlphaMask:
    def test_shape_matches_input(self):
        t = torch.rand(1, 3, 64, 128)
        mask = luminance_alpha_mask(t)
        assert mask.shape == (1, 1, 64, 128)

    def test_values_in_range(self):
        t = torch.rand(1, 3, 32, 32)
        mask = luminance_alpha_mask(t, bright_alpha=0.9, dark_alpha=0.5)
        assert mask.min() >= 0.5 - 1e-6
        assert mask.max() <= 0.9 + 1e-6

    def test_bright_region_gets_higher_alpha(self):
        # Top half bright, bottom half dark
        t = torch.zeros(1, 3, 20, 20)
        t[:, :, :10, :] = 1.0  # bright top
        mask = luminance_alpha_mask(t, bright_alpha=0.9, dark_alpha=0.1)
        top_mean = mask[0, 0, :10, :].mean().item()
        bottom_mean = mask[0, 0, 10:, :].mean().item()
        assert top_mean > bottom_mean

    def test_uniform_image_produces_midpoint(self):
        t = torch.full((1, 3, 10, 10), 0.5)
        mask = luminance_alpha_mask(t, bright_alpha=0.8, dark_alpha=0.2)
        # All pixels same luminance → range=0 → fallback to 0.5 normalized
        # Result: dark_alpha + (bright_alpha - dark_alpha) * 0.5 = 0.5
        assert torch.allclose(mask, torch.tensor(0.5), atol=1e-5)

    def test_feature_size_downsamples(self):
        t = torch.rand(1, 3, 128, 128)
        mask = luminance_alpha_mask(t, feature_size=(16, 16))
        assert mask.shape == (1, 1, 16, 16)

    def test_grayscale_consistency(self):
        # Pure white → bright_alpha, pure black → dark_alpha
        t = torch.zeros(1, 3, 2, 1)
        t[:, :, 0, :] = 1.0  # first pixel white
        # second pixel stays black
        mask = luminance_alpha_mask(t, bright_alpha=1.0, dark_alpha=0.0)
        assert torch.allclose(mask[0, 0, 0, 0], torch.tensor(1.0), atol=1e-5)
        assert torch.allclose(mask[0, 0, 1, 0], torch.tensor(0.0), atol=1e-5)

    def test_uniform_input_fallback(self):
        # Constant-color image: range = 0
        tensor = torch.full((1, 3, 4, 4), 0.5)
        mask = luminance_alpha_mask(tensor, bright_alpha=1.0, dark_alpha=0.0)
        # Falls back to luminance=0.5 everywhere
        expected = 0.0 + (1.0 - 0.0) * 0.5
        assert torch.allclose(mask, torch.full_like(mask, expected), atol=1e-5)
