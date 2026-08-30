"""Tests for quality.py — image quality scoring for source filtering."""

import sys
import types

from PIL import Image, ImageDraw, ImageFilter

from quality import (
    MIN_ASPECT_RATIO,
    MIN_DIMENSION,
    MIN_LANDSCAPE_ASPECT_RATIO,
    MIN_SHARPNESS,
    aesthetic_score,
    check_aspect_ratio,
    check_resolution,
    colorfulness_score,
    contrast_score,
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

    def test_extreme_ratio_rejected(self):
        # 100x1 gives ratio 100, far above MAX_ASPECT_RATIO. Named for what
        # it tests: PIL cannot construct a zero-height image, so the h == 0
        # guard in check_aspect_ratio is unreachable from here.
        img = _solid_image((0, 0, 0), (100, 1))
        assert check_aspect_ratio(img) is False


    def test_boundary_min(self):
        img = _solid_image((0, 0, 0), (50, 100))  # 0.5 ratio = MIN_ASPECT_RATIO
        assert check_aspect_ratio(img) is True


# --- score_image ---

class TestAestheticHeuristics:
    def test_aesthetic_score_returns_dict(self):
        img = _sharp_image((640, 480))
        result = aesthetic_score(img)
        assert isinstance(result, dict)
        assert "score" in result
        assert "method" in result

    def test_nima_disabled_leaves_only_heuristics(self):
        result = aesthetic_score(_sharp_image(), nima=False)

        assert result["method"] == "heuristic-v1"
        assert "nima_mean" not in result

    def test_nima_results_are_recorded_alongside_the_heuristics(self, monkeypatch):
        monkeypatch.setitem(
            sys.modules, "nima", types.SimpleNamespace(score=lambda img: {"mean": 5.5, "std": 1.25}),
        )
        result = aesthetic_score(_sharp_image())

        assert result["method"] == "heuristic-v1+nima-mobilenet-v1"
        assert result["nima_mean"] == 5.5
        assert result["nima_std"] == 1.25
        # The heuristic score keeps its own meaning rather than being replaced.
        assert result["score"] != 5.5

    def test_nima_failure_degrades_to_heuristics(self, monkeypatch, capsys):
        """A missing or broken model must never cost the day its publish."""
        def explode(_image):
            raise RuntimeError("weights are toast")

        monkeypatch.setitem(sys.modules, "nima", types.SimpleNamespace(score=explode))
        result = aesthetic_score(_sharp_image())

        assert result["method"] == "heuristic-v1"
        assert "nima_mean" not in result
        assert result["score"] > 0
        assert "weights are toast" in capsys.readouterr().err

    def test_colorfulness_and_contrast_are_finite(self):
        img = _sharp_image((640, 480))
        assert isinstance(colorfulness_score(img), float)
        assert isinstance(contrast_score(img), float)
        assert colorfulness_score(img) >= 0.0
        assert contrast_score(img) >= 0.0


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


class TestLandscapeOrientation:
    """score_image(min_aspect_ratio=...) — the orientation constraint the
    museum APIs don't provide, unlike Unsplash's orientation=landscape."""

    def test_portrait_passes_by_default(self):
        img = _sharp_image((600, 900))  # ratio 0.67, inside the default range
        assert score_image(img)["aspect_ratio_ok"] is True

    def test_portrait_rejected_when_landscape_required(self):
        img = _sharp_image((600, 900))
        result = score_image(img, min_aspect_ratio=MIN_LANDSCAPE_ASPECT_RATIO)
        assert result["aspect_ratio_ok"] is False
        assert result["pass"] is False

    def test_landscape_passes_when_landscape_required(self):
        img = _sharp_image((1200, 800))  # ratio 1.5
        result = score_image(img, min_aspect_ratio=MIN_LANDSCAPE_ASPECT_RATIO)
        assert result["aspect_ratio_ok"] is True
        assert result["pass"] is True

    def test_square_is_allowed(self):
        img = _sharp_image((800, 800))  # ratio exactly 1.0, the boundary
        result = score_image(img, min_aspect_ratio=MIN_LANDSCAPE_ASPECT_RATIO)
        assert result["aspect_ratio_ok"] is True

    def test_panoramic_still_capped(self):
        img = _sharp_image((3200, 800))  # ratio 4.0, above MAX_ASPECT_RATIO
        result = score_image(img, min_aspect_ratio=MIN_LANDSCAPE_ASPECT_RATIO)
        assert result["aspect_ratio_ok"] is False

    def test_landscape_floor_is_stricter_than_default(self):
        assert MIN_LANDSCAPE_ASPECT_RATIO > MIN_ASPECT_RATIO
