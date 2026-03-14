"""Tests for postprocess.py — color harmonization, sharpening, and pipeline."""

from PIL import Image

from postprocess import (
    _build_histogram_lut,
    _cumulative_sum,
    color_harmonize,
    postprocess,
    sharpen,
    upscale,
)


# --- helpers ---

def _solid_image(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> Image.Image:
    """Create a solid-color RGB image."""
    return Image.new("RGB", size, color)


def _gradient_image(size: tuple[int, int] = (256, 64)) -> Image.Image:
    """Create an image with a horizontal red gradient for histogram testing."""
    img = Image.new("RGB", size)
    pixels = img.load()
    w, h = size
    for x in range(w):
        for y in range(h):
            pixels[x, y] = (x % 256, 128, 128)
    return img


# --- _cumulative_sum ---

class TestCumulativeSum:
    def test_simple(self):
        assert _cumulative_sum([1, 2, 3]) == [1, 3, 6]

    def test_empty(self):
        assert _cumulative_sum([]) == []

    def test_single(self):
        assert _cumulative_sum([5]) == [5]

    def test_zeros(self):
        assert _cumulative_sum([0, 0, 0]) == [0, 0, 0]


# --- _build_histogram_lut ---

class TestBuildHistogramLut:
    def test_identity_histograms(self):
        """Identical histograms should produce near-identity LUT."""
        hist = [0] * 256
        hist[100] = 100
        hist[200] = 100
        lut = _build_histogram_lut(hist, hist)
        assert lut[100] == 100
        assert lut[200] == 200

    def test_lut_length(self):
        hist = [1] * 256
        lut = _build_histogram_lut(hist, hist)
        assert len(lut) == 256

    def test_lut_values_in_range(self):
        src = [0] * 256
        ref = [0] * 256
        src[50] = 500
        ref[200] = 500
        lut = _build_histogram_lut(src, ref)
        assert all(0 <= v <= 255 for v in lut)


# --- color_harmonize ---

class TestColorHarmonize:
    def test_returns_image(self):
        stylized = _solid_image((200, 100, 50))
        content = _solid_image((100, 150, 200))
        result = color_harmonize(stylized, content)
        assert isinstance(result, Image.Image)

    def test_preserves_size(self):
        stylized = _solid_image((200, 100, 50), size=(100, 80))
        content = _solid_image((100, 150, 200), size=(200, 160))
        result = color_harmonize(stylized, content)
        assert result.size == (100, 80)

    def test_zero_strength_returns_original(self):
        stylized = _solid_image((200, 100, 50))
        content = _solid_image((100, 150, 200))
        result = color_harmonize(stylized, content, strength=0.0)
        assert list(result.getdata()) == list(stylized.getdata())

    def test_different_sizes(self):
        stylized = _solid_image((200, 100, 50), size=(64, 64))
        content = _solid_image((100, 150, 200), size=(128, 128))
        result = color_harmonize(stylized, content)
        assert result.size == (64, 64)

    def test_mode_is_rgb(self):
        stylized = _solid_image((200, 100, 50))
        content = _solid_image((100, 150, 200))
        result = color_harmonize(stylized, content)
        assert result.mode == "RGB"


# --- sharpen ---

class TestSharpen:
    def test_returns_image(self):
        img = _gradient_image()
        result = sharpen(img)
        assert isinstance(result, Image.Image)

    def test_preserves_size(self):
        img = _gradient_image((128, 96))
        result = sharpen(img)
        assert result.size == (128, 96)

    def test_preserves_mode(self):
        img = _gradient_image()
        result = sharpen(img)
        assert result.mode == img.mode

    def test_custom_parameters(self):
        img = _gradient_image()
        result = sharpen(img, radius=1.0, percent=100, threshold=1)
        assert isinstance(result, Image.Image)


# --- upscale ---

class TestUpscale:
    def test_doubles_size(self):
        img = _solid_image((100, 100, 100), size=(32, 32))
        result = upscale(img, scale=2)
        assert result.size == (64, 64)

    def test_triple_scale(self):
        img = _solid_image((100, 100, 100), size=(20, 30))
        result = upscale(img, scale=3)
        assert result.size == (60, 90)

    def test_returns_image(self):
        img = _solid_image((100, 100, 100), size=(16, 16))
        result = upscale(img, scale=2)
        assert isinstance(result, Image.Image)


# --- postprocess pipeline ---

class TestPostprocess:
    def test_all_disabled_returns_original(self):
        stylized = _gradient_image()
        content = _gradient_image()
        result = postprocess(
            stylized, content,
            harmonize=False, do_sharpen=False, do_upscale=False,
        )
        assert list(result.getdata()) == list(stylized.getdata())

    def test_only_harmonize(self):
        stylized = _solid_image((200, 100, 50))
        content = _solid_image((100, 150, 200))
        result = postprocess(
            stylized, content,
            harmonize=True, do_sharpen=False, do_upscale=False,
        )
        assert result.size == stylized.size

    def test_only_sharpen(self):
        stylized = _gradient_image()
        content = _gradient_image()
        result = postprocess(
            stylized, content,
            harmonize=False, do_sharpen=True, do_upscale=False,
        )
        assert result.size == stylized.size

    def test_only_upscale(self):
        stylized = _solid_image((128, 128, 128), size=(32, 32))
        content = _solid_image((128, 128, 128), size=(32, 32))
        result = postprocess(
            stylized, content,
            harmonize=False, do_sharpen=False, do_upscale=True, upscale_factor=2,
        )
        assert result.size == (64, 64)

    def test_all_enabled(self):
        stylized = _gradient_image((64, 48))
        content = _gradient_image((64, 48))
        result = postprocess(
            stylized, content,
            harmonize=True, do_sharpen=True, do_upscale=True, upscale_factor=2,
        )
        assert result.size == (128, 96)

    def test_returns_image(self):
        stylized = _gradient_image()
        content = _gradient_image()
        result = postprocess(stylized, content)
        assert isinstance(result, Image.Image)
