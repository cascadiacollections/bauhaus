"""Tests for quality.py — image quality scoring and gating."""

from io import BytesIO

from PIL import Image, ImageFilter

from quality import (
    MIN_DIMENSION,
    MAX_ASPECT_RATIO,
    MIN_SHARPNESS,
    check_aspect_ratio,
    check_resolution,
    compute_sharpness,
    passes_quality_gate,
)


def _make_image(width: int, height: int, color: tuple = (128, 128, 128)) -> Image.Image:
    """Create a solid-color test image."""
    return Image.new("RGB", (width, height), color)


def _make_noisy_image(width: int, height: int, seed: int = 42) -> Image.Image:
    """Create a noisy test image with high sharpness."""
    import random as rng
    rng.seed(seed)
    img = Image.new("RGB", (width, height))
    pixels = [(rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
               for _ in range(width * height)]
    img.putdata(pixels)
    return img


def _to_bytes(img: Image.Image, fmt: str = "JPEG") -> bytes:
    """Encode a PIL Image to bytes."""
    buf = BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


# --- check_resolution ---

class TestCheckResolution:
    def test_meets_minimum(self):
        assert check_resolution(1024, 768) is True

    def test_exact_minimum(self):
        assert check_resolution(MIN_DIMENSION, MIN_DIMENSION) is True

    def test_below_minimum_width(self):
        assert check_resolution(256, 1024) is False

    def test_below_minimum_height(self):
        assert check_resolution(1024, 256) is False

    def test_both_below(self):
        assert check_resolution(100, 100) is False

    def test_custom_min_dim(self):
        assert check_resolution(200, 200, min_dim=200) is True
        assert check_resolution(199, 200, min_dim=200) is False


# --- check_aspect_ratio ---

class TestCheckAspectRatio:
    def test_square(self):
        assert check_aspect_ratio(1000, 1000) is True

    def test_standard_landscape(self):
        # 16:9 ≈ 1.78:1
        assert check_aspect_ratio(1920, 1080) is True

    def test_standard_portrait(self):
        assert check_aspect_ratio(1080, 1920) is True

    def test_extreme_panorama(self):
        # 4:1 exceeds default 3:1
        assert check_aspect_ratio(4000, 1000) is False

    def test_extreme_tall(self):
        assert check_aspect_ratio(500, 2000) is False

    def test_exact_limit(self):
        assert check_aspect_ratio(3000, 1000, max_ratio=3.0) is True

    def test_zero_dimension(self):
        assert check_aspect_ratio(0, 1000) is False
        assert check_aspect_ratio(1000, 0) is False

    def test_negative_dimension(self):
        assert check_aspect_ratio(-1, 1000) is False


# --- compute_sharpness ---

class TestComputeSharpness:
    def test_solid_color_is_low(self):
        # A solid-color image has zero edges → very low sharpness
        img = _make_image(800, 600)
        sharpness = compute_sharpness(img)
        assert sharpness < 10.0

    def test_noisy_image_is_high(self):
        # High-frequency noise → high Laplacian variance
        img = _make_noisy_image(800, 600)
        sharpness = compute_sharpness(img)
        assert sharpness > 100.0

    def test_blurred_is_lower_than_sharp(self):
        img = _make_noisy_image(800, 600)

        sharp = compute_sharpness(img)
        blurred_img = img.filter(ImageFilter.GaussianBlur(radius=10))
        blurry = compute_sharpness(blurred_img)
        assert blurry < sharp

    def test_returns_float(self):
        img = _make_image(200, 200)
        assert isinstance(compute_sharpness(img), float)


# --- passes_quality_gate ---

class TestPassesQualityGate:
    def test_good_image_passes(self):
        # Create a sharp, large image with some texture
        img = _make_noisy_image(1024, 768)
        passed, reason = passes_quality_gate(_to_bytes(img))
        assert passed is True
        assert reason == ""

    def test_too_small_fails(self):
        img = _make_image(200, 200)
        passed, reason = passes_quality_gate(_to_bytes(img))
        assert passed is False
        assert "resolution too low" in reason

    def test_extreme_aspect_ratio_fails(self):
        img = _make_image(2000, 600)
        passed, reason = passes_quality_gate(_to_bytes(img), min_sharpness=0.0)
        assert passed is False
        assert "aspect ratio too extreme" in reason

    def test_blurry_image_fails(self):
        # Solid color → zero sharpness → rejected
        img = _make_image(1024, 768)
        passed, reason = passes_quality_gate(_to_bytes(img))
        assert passed is False
        assert "too blurry" in reason

    def test_invalid_bytes_fails(self):
        passed, reason = passes_quality_gate(b"not-an-image")
        assert passed is False
        assert "could not decode" in reason

    def test_custom_thresholds(self):
        # Solid-color image passes if we set sharpness threshold to 0
        img = _make_image(1024, 768)
        passed, reason = passes_quality_gate(
            _to_bytes(img), min_sharpness=0.0,
        )
        assert passed is True

    def test_custom_min_dimension(self):
        img = _make_image(300, 300)
        # Fails with default min_dimension (512)
        passed, _ = passes_quality_gate(_to_bytes(img))
        assert passed is False
        # Passes with lower threshold
        passed, _ = passes_quality_gate(
            _to_bytes(img), min_dimension=200, min_sharpness=0.0,
        )
        assert passed is True

    def test_returns_tuple(self):
        img = _make_image(1024, 768)
        result = passes_quality_gate(_to_bytes(img))
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_png_format(self):
        img = _make_noisy_image(1024, 768)
        passed, reason = passes_quality_gate(_to_bytes(img, fmt="PNG"))
        assert passed is True
