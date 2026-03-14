"""Tests for quality.py — image quality scoring for source filtering."""

from PIL import Image, ImageDraw, ImageFilter

from quality import (
    MIN_ASPECT_RATIO,
    MIN_DIMENSION,
    MIN_SHARPNESS,
    check_aspect_ratio,
    check_resolution,
    score_image,
    sharpness_score,
)


# --- helpers ---

def _solid_image(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> Image.Image:
    return Image.new("RGB", size, color)


def _sharp_image(size: tuple[int, int] = (640, 480)) -> Image.Image:
    """Create an image with high-contrast edges (high sharpness)."""
    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    w, h = size
    for i in range(0, w, 10):
        draw.line([(i, 0), (i, h)], fill=(0, 0, 0), width=2)
    for j in range(0, h, 10):
        draw.line([(0, j), (w, j)], fill=(0, 0, 0), width=2)
    return img


def _blurry_image(size: tuple[int, int] = (640, 480)) -> Image.Image:
    """Create a very blurry (low sharpness) image."""
    img = _solid_image((128, 128, 128), size)
    for _ in range(10):
        img = img.filter(ImageFilter.GaussianBlur(radius=5))
    return img


# --- sharpness_score ---

class TestSharpnessScore:
    def test_returns_float(self):
        img = _solid_image((100, 100, 100), (64, 64))
        score = sharpness_score(img)
        assert isinstance(score, float)

    def test_solid_image_low_sharpness(self):
        img = _solid_image((100, 100, 100), (64, 64))
        score = sharpness_score(img)
        assert score < MIN_SHARPNESS

    def test_sharp_image_high_sharpness(self):
        img = _sharp_image()
        score = sharpness_score(img)
        assert score > MIN_SHARPNESS

    def test_blurry_image_low_sharpness(self):
        img = _blurry_image()
        score = sharpness_score(img)
        assert score < MIN_SHARPNESS

    def test_nonnegative(self):
        img = _solid_image((0, 0, 0), (32, 32))
        assert sharpness_score(img) >= 0.0


# --- check_resolution ---

class TestCheckResolution:
    def test_large_image_passes(self):
        img = _solid_image((0, 0, 0), (1024, 768))
        assert check_resolution(img) is True

    def test_exact_minimum_passes(self):
        img = _solid_image((0, 0, 0), (MIN_DIMENSION, MIN_DIMENSION))
        assert check_resolution(img) is True

    def test_too_small_fails(self):
        img = _solid_image((0, 0, 0), (256, 256))
        assert check_resolution(img) is False

    def test_one_dimension_too_small(self):
        img = _solid_image((0, 0, 0), (1024, 100))
        assert check_resolution(img) is False

    def test_custom_min_dim(self):
        img = _solid_image((0, 0, 0), (100, 100))
        assert check_resolution(img, min_dim=50) is True


# --- check_aspect_ratio ---

class TestCheckAspectRatio:
    def test_normal_landscape(self):
        img = _solid_image((0, 0, 0), (1920, 1080))
        assert check_aspect_ratio(img) is True

    def test_square(self):
        img = _solid_image((0, 0, 0), (500, 500))
        assert check_aspect_ratio(img) is True

    def test_extreme_panoramic_fails(self):
        img = _solid_image((0, 0, 0), (4000, 100))
        assert check_aspect_ratio(img) is False

    def test_extreme_portrait_fails(self):
        img = _solid_image((0, 0, 0), (100, 4000))
        assert check_aspect_ratio(img) is False

    def test_zero_height(self):
        # Edge case — 100×1 gives ratio 100 which exceeds MAX_ASPECT_RATIO
        img = _solid_image((0, 0, 0), (100, 1))
        assert check_aspect_ratio(img) is False


    def test_boundary_min(self):
        img = _solid_image((0, 0, 0), (50, 100))  # 0.5 ratio = MIN_ASPECT_RATIO
        assert check_aspect_ratio(img) is True


# --- score_image ---

class TestScoreImage:
    def test_returns_dict(self):
        img = _sharp_image((800, 600))
        result = score_image(img)
        assert isinstance(result, dict)

    def test_good_image_passes(self):
        img = _sharp_image((800, 600))
        result = score_image(img)
        assert result["pass"] is True
        assert result["resolution_ok"] is True
        assert result["aspect_ratio_ok"] is True
        assert result["sharpness_ok"] is True

    def test_small_image_fails(self):
        img = _sharp_image((200, 200))
        result = score_image(img)
        assert result["resolution_ok"] is False
        assert result["pass"] is False

    def test_includes_dimensions(self):
        img = _solid_image((0, 0, 0), (640, 480))
        result = score_image(img)
        assert result["width"] == 640
        assert result["height"] == 480

    def test_includes_sharpness(self):
        img = _solid_image((0, 0, 0), (640, 480))
        result = score_image(img)
        assert "sharpness" in result
        assert isinstance(result["sharpness"], float)
